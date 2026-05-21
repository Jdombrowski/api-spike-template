"""Configuration for the API spike investigation."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Paths ──────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "data" / "db.sqlite"
SAMPLES_DIR = PROJECT_ROOT / "data" / "samples"
SAMPLES_DIR.mkdir(parents=True, exist_ok=True)

# ── EDGAR API ──────────────────────────────────────────────────────────────
EDGAR_BASE = "https://data.sec.gov"
EDGAR_USER_AGENT = os.getenv(
    "EDGAR_USER_AGENT",
    "Mozilla/5.0 (API Spike Investigation; +https://github.com/yourusername/repo)"
)

# ── Polygon.io API ─────────────────────────────────────────────────────────
POLYGON_BASE = "https://api.polygon.io"
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")

# ── HTTP Client (circuit breaker, backoff) ─────────────────────────────────
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))

# Circuit breaker parameters
CIRCUIT_FAIL_MAX = int(os.getenv("CIRCUIT_FAIL_MAX", "5"))
CIRCUIT_RESET_S = int(os.getenv("CIRCUIT_RESET_S", "60"))

# Exponential backoff: wait = random(0, min(CAP, BASE * 2^attempt))
BACKOFF_BASE = float(os.getenv("BACKOFF_BASE", "1.0"))
BACKOFF_CAP = float(os.getenv("BACKOFF_CAP", "30.0"))

# Proactive rate limiting (set to 0 to disable; Polygon free tier = 5 req/min)
POLYGON_RATE_LIMIT_RPM = int(os.getenv("POLYGON_RATE_LIMIT_RPM", "5"))


def validate():
    """Validate required configuration at startup."""
    if not POLYGON_API_KEY:
        raise RuntimeError(
            "POLYGON_API_KEY not set. Add to .env or set environment variable."
        )
