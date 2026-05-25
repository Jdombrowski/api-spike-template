"""
Unit tests for the resilient HTTP client.

All network calls are mocked — tests verify retry logic, circuit breaker
state transitions, status-code handling, and rate-limit throttling without
touching real infrastructure.
"""

import time
from unittest.mock import MagicMock, patch

import pytest
import requests as req_lib

import src.ingest.http_client as http_mod
from src.ingest.http_client import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    _breaker,
    _jitter,
    get,
    get_bytes,
    get_text,
)


@pytest.fixture(autouse=True)
def reset_http_state():
    """Isolate module-level breaker and rate-limit state between tests."""
    http_mod._breakers.clear()
    http_mod._last_call.clear()
    yield
    http_mod._breakers.clear()
    http_mod._last_call.clear()


def _resp(status=200, json_data=None, text="ok", content=b"bytes", headers=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data if json_data is not None else {}
    r.text = text
    r.content = content
    r.headers = headers or {}
    r.raise_for_status = MagicMock()
    return r


# ── CircuitBreaker ─────────────────────────────────────────────────────────


class TestCircuitBreaker:
    def test_initial_state_is_closed(self):
        cb = CircuitBreaker("test")
        assert cb.state == CircuitState.CLOSED

    def test_single_failure_does_not_open(self):
        cb = CircuitBreaker("test", fail_max=3)
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_record_failure_opens_at_fail_max(self):
        cb = CircuitBreaker("test", fail_max=2)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_record_success_resets_to_closed(self):
        cb = CircuitBreaker("test", fail_max=1)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_allow_request_raises_when_open(self):
        cb = CircuitBreaker("test", fail_max=1, reset_s=9999)
        cb.record_failure()
        with pytest.raises(CircuitOpenError, match="OPEN"):
            cb.allow_request()

    def test_allow_request_returns_true_when_closed(self):
        cb = CircuitBreaker("test")
        assert cb.allow_request() is True

    def test_transitions_to_half_open_after_reset(self):
        cb = CircuitBreaker("test", fail_max=1, reset_s=0)
        cb.record_failure()
        assert cb._state == CircuitState.OPEN
        time.sleep(0.01)
        assert cb.state == CircuitState.HALF_OPEN

    def test_allow_request_returns_true_when_half_open(self):
        cb = CircuitBreaker("test", fail_max=1, reset_s=0)
        cb.record_failure()
        time.sleep(0.01)
        assert cb.allow_request() is True


# ── Jitter ─────────────────────────────────────────────────────────────────


class TestJitter:
    def test_returns_non_negative_float(self):
        for attempt in range(6):
            assert _jitter(attempt) >= 0.0

    def test_bounded_by_cap(self):
        for attempt in range(6):
            assert _jitter(attempt) <= 30.0


# ── Breaker factory ────────────────────────────────────────────────────────


class TestBreakerFactory:
    def test_creates_circuit_breaker(self):
        cb = _breaker("myhost")
        assert isinstance(cb, CircuitBreaker)
        assert cb.name == "myhost"

    def test_returns_same_instance_on_repeat_calls(self):
        assert _breaker("samehost") is _breaker("samehost")


# ── Throttle ───────────────────────────────────────────────────────────────


class TestThrottle:
    def test_no_sleep_for_unknown_source(self):
        start = time.monotonic()
        http_mod._throttle("unknown_source_xyz")
        assert time.monotonic() - start < 0.05

    def test_records_last_call_time(self):
        http_mod._throttle("polygon")
        assert "polygon" in http_mod._last_call

    def test_sleeps_when_called_too_quickly(self):
        http_mod._last_call["polygon"] = time.monotonic()
        with patch("time.sleep") as mock_sleep:
            http_mod._throttle("polygon")
        mock_sleep.assert_called_once()
        wait = mock_sleep.call_args[0][0]
        assert wait > 0


# ── _fetch status-code handling ────────────────────────────────────────────


class TestFetch:
    def test_200_returns_response(self):
        with patch("requests.Session") as cls:
            cls.return_value.get.return_value = _resp(200)
            r = http_mod._fetch("http://x.com", source="ok_src")
        assert r.status_code == 200

    def test_400_raises_value_error_immediately(self):
        with patch("requests.Session") as cls:
            cls.return_value.get.return_value = _resp(400, text='{"err":"bad"}')
            with pytest.raises(ValueError, match="400"):
                http_mod._fetch("http://x.com", source="src400")
        assert cls.return_value.get.call_count == 1  # no retry

    def test_401_raises_permission_error(self):
        with patch("requests.Session") as cls:
            cls.return_value.get.return_value = _resp(401)
            with pytest.raises(PermissionError):
                http_mod._fetch("http://x.com", source="src401")

    def test_403_raises_permission_error(self):
        with patch("requests.Session") as cls:
            cls.return_value.get.return_value = _resp(403)
            with pytest.raises(PermissionError):
                http_mod._fetch("http://x.com", source="src403")

    def test_404_raises_lookup_error(self):
        with patch("requests.Session") as cls:
            cls.return_value.get.return_value = _resp(404)
            with pytest.raises(LookupError):
                http_mod._fetch("http://x.com", source="src404")

    def test_429_retries_and_eventually_succeeds(self):
        with patch("requests.Session") as cls, patch("time.sleep"):
            cls.return_value.get.side_effect = [
                _resp(429, headers={"Retry-After": "0"}),
                _resp(200),
            ]
            r = http_mod._fetch("http://x.com", source="src429")
        assert r.status_code == 200

    def test_500_retries_max_times_then_raises(self):
        r500 = _resp(500)
        r500.raise_for_status.side_effect = Exception("server error")
        with patch("requests.Session") as cls, patch("time.sleep"):
            cls.return_value.get.return_value = r500
            with pytest.raises(Exception):
                http_mod._fetch("http://x.com", source="src500")
        assert cls.return_value.get.call_count == 3  # MAX_RETRIES default

    def test_500_succeeds_on_retry(self):
        with patch("requests.Session") as cls, patch("time.sleep"):
            cls.return_value.get.side_effect = [_resp(500), _resp(200)]
            r = http_mod._fetch("http://x.com", source="src500ok")
        assert r.status_code == 200

    def test_timeout_retries_and_succeeds(self):
        with patch("requests.Session") as cls, patch("time.sleep"):
            cls.return_value.get.side_effect = [req_lib.Timeout(), _resp(200)]
            r = http_mod._fetch("http://x.com", source="srctmo")
        assert r.status_code == 200

    def test_timeout_exhaustion_raises(self):
        with patch("requests.Session") as cls, patch("time.sleep"):
            cls.return_value.get.side_effect = req_lib.Timeout()
            with pytest.raises(req_lib.Timeout):
                http_mod._fetch("http://x.com", source="srctmo2")

    def test_circuit_open_raises_immediately(self):
        cb = _breaker("open_src")
        cb._failures = cb.fail_max  # trip the breaker
        cb._state = CircuitState.OPEN
        cb._opened_at = time.monotonic()
        with pytest.raises(CircuitOpenError):
            http_mod._fetch("http://x.com", source="open_src")


# ── Public helpers ─────────────────────────────────────────────────────────


class TestGetHelpers:
    def test_get_returns_parsed_json(self):
        with patch("requests.Session") as cls:
            cls.return_value.get.return_value = _resp(200, json_data={"a": 1})
            result = get("http://x.com", source="gtest")
        assert result == {"a": 1}

    def test_get_saves_sample_when_requested(self, tmp_path, monkeypatch):
        import src.config as cfg

        monkeypatch.setattr(cfg, "SAMPLES_DIR", tmp_path)
        with patch("requests.Session") as cls:
            cls.return_value.get.return_value = _resp(200, json_data={})
            get("http://x.com", source="stest", save_sample=True, sample_name="snap")
        assert any(tmp_path.iterdir())

    def test_get_text_returns_string(self):
        with patch("requests.Session") as cls:
            cls.return_value.get.return_value = _resp(200, text="<xml/>")
            result = get_text("http://x.com", source="ttest")
        assert result == "<xml/>"

    def test_get_bytes_returns_bytes(self):
        with patch("requests.Session") as cls:
            cls.return_value.get.return_value = _resp(200, content=b"\x00\x01")
            result = get_bytes("http://x.com", source="btest")
        assert result == b"\x00\x01"
