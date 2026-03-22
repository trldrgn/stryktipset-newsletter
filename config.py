"""
Central configuration. All env vars loaded here — nothing else imports dotenv.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Project root
ROOT_DIR = Path(__file__).parent
DATA_DIR = ROOT_DIR / "data"
PREDICTIONS_DIR = DATA_DIR / "predictions"
RESULTS_DIR = DATA_DIR / "results"
NEWSLETTERS_DIR = DATA_DIR / "newsletters"
PERFORMANCE_DIR = DATA_DIR / "performance"
CACHE_DIR = DATA_DIR / "cache"
LOGS_DIR = ROOT_DIR / "logs"

# Create dirs on import so nothing else has to
for _d in (PREDICTIONS_DIR, RESULTS_DIR, NEWSLETTERS_DIR, PERFORMANCE_DIR, CACHE_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

load_dotenv(ROOT_DIR / ".env")


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]
PERPLEXITY_API_KEY: str = os.environ["PERPLEXITY_API_KEY"]
API_FOOTBALL_KEY: str = os.environ["API_FOOTBALL_KEY"]
FOOTBALL_DATA_API_KEY: str = os.getenv("FOOTBALL_DATA_API_KEY", "")

# Gmail SMTP — App Password auth (no Google Cloud Console needed)
GMAIL_SENDER: str = os.environ["GMAIL_SENDER"]           # e.g. stryktipset.tips@gmail.com
GMAIL_APP_PASSWORD: str = os.environ["GMAIL_APP_PASSWORD"]  # 16-char App Password from Google Account


# ---------------------------------------------------------------------------
# Recipients
# ---------------------------------------------------------------------------

def _parse_recipients() -> list[str]:
    raw = os.getenv("NEWSLETTER_RECIPIENTS", "")
    return [r.strip() for r in raw.split(",") if r.strip()]

NEWSLETTER_RECIPIENTS: list[str] = _parse_recipients()


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

SCHEDULE_DAY: str = os.getenv("SCHEDULE_DAY", "sat")           # sat = Saturday
SCHEDULE_HOUR: int = int(os.getenv("SCHEDULE_HOUR", "7"))
SCHEDULE_MINUTE: int = int(os.getenv("SCHEDULE_MINUTE", "30"))
SCHEDULE_TIMEZONE: str = os.getenv("SCHEDULE_TIMEZONE", "Europe/Stockholm")


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_MAX_TOKENS: int = int(os.getenv("CLAUDE_MAX_TOKENS", "32000"))
CLAUDE_THINKING_BUDGET: int = int(os.getenv("CLAUDE_THINKING_BUDGET", "10000"))


# ---------------------------------------------------------------------------
# Perplexity
# ---------------------------------------------------------------------------

PERPLEXITY_MODEL: str = os.getenv("PERPLEXITY_MODEL", "sonar")   # cheapest tier
PERPLEXITY_BASE_URL: str = "https://api.perplexity.ai"


# ---------------------------------------------------------------------------
# API-Football
# ---------------------------------------------------------------------------

API_FOOTBALL_BASE_URL: str = "https://v3.football.api-sports.io"
API_FOOTBALL_DAILY_LIMIT: int = 100   # free tier hard cap — tracked per run


# ---------------------------------------------------------------------------
# Svenska Spel
# ---------------------------------------------------------------------------

SVENSKA_SPEL_API_BASE: str = "https://api.spela.svenskaspel.se/draw/1/stryktipset"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

# API-Football responses cached for this many seconds (1 week — data won't change)
CACHE_TTL_SECONDS: int = int(os.getenv("CACHE_TTL_SECONDS", str(60 * 60 * 24 * 7)))


# ---------------------------------------------------------------------------
# Coupon system
# ---------------------------------------------------------------------------

COUPON_SINGLES: int = 4     # most confident games → 1 outcome each
COUPON_FULL: int = 1        # least confident game → all 3 outcomes
COUPON_DOUBLES: int = 8     # remaining → 2 outcomes each
# Total cost: 1^4 × 2^8 × 3^1 = 768 SEK
