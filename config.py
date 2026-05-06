"""
Configuration for the Amsterdam Event Aggregator.
"""
import os
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"

DB_PATH = DATA_DIR / "events.db"

# ── Scraping ───────────────────────────────────────────────────────
REQUEST_TIMEOUT = 15  # seconds
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── Sources ────────────────────────────────────────────────────────
MELKWEG_AGENDA_URL = "https://www.melkweg.nl/en/agenda/"
AMSTERDAM_ALT_AGENDA_URL = "https://www.amsterdamalternative.nl/agenda"

# ── Output ─────────────────────────────────────────────────────────
DIGEST_LANGUAGE = "en"
CITY = "Amsterdam"

# ── Discovery Engine ───────────────────────────────────────────────
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "5a6bd5ed29c22d43df996f6331ecfd43")

# Tidal CSV: check env var first, then local user path, then repo-bundled fallback
_TIDAL_USER_PATH = "/Users/francescozaccaria/Downloads/My TIDAL Library.csv"
_TIDAL_REPO_PATH = str(DATA_DIR / "tidal_library.csv")
TIDAL_CSV_PATH = os.getenv(
    "TIDAL_CSV_PATH",
    _TIDAL_USER_PATH if os.path.exists(_TIDAL_USER_PATH) else _TIDAL_REPO_PATH,
)

# ── Email / SMTP (reuses oura_digest GitHub secrets) ─────────────
EMAIL_USER = os.getenv("EMAIL_USER", "")
EMAIL_PASS = os.getenv("EMAIL_PASS", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")
