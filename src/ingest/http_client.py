"""
Resilient HTTP client with:
  - Exponential backoff + full jitter
  - Per-host circuit breaker
  - Structured error logging
  - Response snapshot saving (for offline investigation)

This is the layer you'd swap out for httpx or aiohttp in production.
Architecture is intentionally identical to what you'd write for a custodian API.
"""
import json
import random
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Any, Optional

import requests

from src import config

log = logging.getLogger(__name__)


# ── Circuit Breaker ────────────────────────────────────────────────────────

class CircuitState(Enum):
    CLOSED    = auto()   # normal — requests flow through
    OPEN      = auto()   # tripped — fast-fail all requests
    HALF_OPEN = auto()   # probing — one test request allowed


@dataclass
class CircuitBreaker:
    name: str
    fail_max: int   = config.CIRCUIT_FAIL_MAX
    reset_s: float  = config.CIRCUIT_RESET_S

    _state: CircuitState       = field(default=CircuitState.CLOSED, init=False, repr=False)
    _failures: int             = field(default=0,    init=False, repr=False)
    _opened_at: Optional[float]   = field(default=None, init=False, repr=False)

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if self._opened_at is not None and time.monotonic() - self._opened_at >= self.reset_s:
                log.info("[circuit:%s] half-open — sending probe", self.name)
                self._state = CircuitState.HALF_OPEN
        return self._state

    def record_success(self):
        self._state    = CircuitState.CLOSED

    def record_failure(self):
        self._failures += 1
        if self._failures >= self.fail_max:
            log.warning(
                "[circuit:%s] OPEN after %d failures", self.name, self._failures
            )
            self._state    = CircuitState.OPEN
            self._opened_at = time.monotonic()

    def allow_request(self) -> bool:
        s = self.state
        if s == CircuitState.CLOSED:
            return True
        if s == CircuitState.HALF_OPEN:
            return True   # one probe allowed
        # OPEN
        # _opened_at should be set when transitioning to OPEN, but guard for mypy
        if self._opened_at is None:
            remaining = self.reset_s
        else:
            remaining = max(0.0, self.reset_s - (time.monotonic() - self._opened_at))
        raise CircuitOpenError(
            f"Circuit '{self.name}' is OPEN — downstream may be degraded. "
            f"Resets in {remaining:.0f}s"
        )


class CircuitOpenError(Exception):
    pass


# ── Jitter ─────────────────────────────────────────────────────────────────

def _jitter(attempt: int) -> float:
    """Full jitter: random in [0, min(cap, base * 2^attempt)]."""
    ceiling = min(config.BACKOFF_CAP, config.BACKOFF_BASE * (2 ** attempt))
    return random.uniform(0, ceiling)


# ── HTTP Client ────────────────────────────────────────────────────────────

# One circuit breaker per named downstream
_breakers: dict[str, CircuitBreaker] = {}

def _breaker(name: str) -> CircuitBreaker:
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(name=name)
    return _breakers[name]


# ── Rate limiting ──────────────────────────────────────────────────────────

# Minimum seconds between requests per source (derived from RPM config)
_min_interval: dict[str, float] = {}
if config.POLYGON_RATE_LIMIT_RPM > 0:
    _min_interval["polygon"] = 60.0 / config.POLYGON_RATE_LIMIT_RPM

_last_call: dict[str, float] = {}


def _throttle(source: str) -> None:
    """Sleep as needed to honour the per-source minimum request interval."""
    interval = _min_interval.get(source, 0.0)
    if not interval:
        return
    elapsed = time.monotonic() - _last_call.get(source, 0.0)
    if elapsed < interval:
        wait = interval - elapsed
        log.info("[%s] rate-limiting — sleeping %.1fs", source, wait)
        time.sleep(wait)
    _last_call[source] = time.monotonic()


def _fetch(
    url: str,
    *,
    source: str,
    params: dict | None  = None,
    headers: dict | None = None,
) -> requests.Response:
    """
    Core retry + circuit-breaker loop. Returns the raw Response object.
    Called by get() and get_text() — don't call directly.
    """
    breaker = _breaker(source)
    session = requests.Session()

    for attempt in range(config.MAX_RETRIES):
        breaker.allow_request()   # raises CircuitOpenError if open
        _throttle(source)

        try:
            resp = session.get(url, params=params, headers=headers, timeout=config.REQUEST_TIMEOUT)

            # ── Don't retry client errors ──────────────────────────────
            if resp.status_code == 400:
                raise ValueError(f"[{source}] 400 Bad Request: {resp.text[:200]}")
            if resp.status_code in (401, 403):
                raise PermissionError(f"[{source}] Auth error {resp.status_code}")
            if resp.status_code == 404:
                raise LookupError(f"[{source}] 404 Not Found: {url}")

            # ── Rate limited — respect Retry-After ────────────────────
            if resp.status_code == 429:
                wait = max(int(resp.headers.get("Retry-After", 0)), _jitter(attempt))
                log.warning("[%s] 429 rate limited — waiting %.1fs", source, wait)
                time.sleep(wait)
                continue

            # ── Server errors — worth retrying ────────────────────────
            if resp.status_code >= 500:
                breaker.record_failure()
                if attempt == config.MAX_RETRIES - 1:
                    resp.raise_for_status()
                wait = _jitter(attempt)
                log.warning(
                    "[%s] %d server error (attempt %d/%d) — retrying in %.1fs",
                    source, resp.status_code, attempt + 1, config.MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue

            resp.raise_for_status()
            breaker.record_success()
            return resp

        except requests.Timeout:
            breaker.record_failure()
            if attempt == config.MAX_RETRIES - 1:
                raise
            wait = _jitter(attempt)
            log.warning("[%s] timeout (attempt %d) — retrying in %.1fs", source, attempt + 1, wait)
            time.sleep(wait)

        except (ValueError, PermissionError, LookupError, CircuitOpenError):
            raise   # non-retryable — propagate immediately

    raise RuntimeError(f"[{source}] exhausted {config.MAX_RETRIES} retries on {url}")


def get(
    url: str,
    *,
    source: str,
    params: dict | None  = None,
    headers: dict | None = None,
    save_sample: bool    = False,
    sample_name: str     = "",
) -> Any:
    """GET → parsed JSON. Retry + backoff + circuit breaker via _fetch()."""
    data = _fetch(url, source=source, params=params, headers=headers).json()
    if save_sample:
        _save_sample(data, source, sample_name or url.split("/")[-1])
    return data


def get_text(
    url: str,
    *,
    source: str,
    params: dict | None  = None,
    headers: dict | None = None,
) -> str:
    """GET → raw text (XML, CSV, or any non-JSON response). Same retry logic as get()."""
    return _fetch(url, source=source, params=params, headers=headers).text


def _save_sample(data: Any, source: str, name: str):
    """Persist raw API response for offline investigation and regression testing."""
    ts   = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = config.SAMPLES_DIR / f"{source}__{name}__{ts}.json"
    path.write_text(json.dumps(data, indent=2))
    log.info("[%s] sample saved → %s", source, path.name)
