"""
Fetches latest team news, xG context, and injury updates via Perplexity Sonar API.

Cost efficiency:
  - 1 query per match (combining both teams + all topics)
  - Uses the cheapest 'sonar' model
  - Structured prompt ensures compact, useful responses
  - Results are NOT cached (news must be fresh each run)
  - Rate limited: 2s between calls to avoid throttling
  - Parallelized: 4 concurrent workers for speed

What we ask per match (in priority order):
  1. INJURIES — confirmed absences for both teams (critical)
  2. xG stats from recent games (important)
  3. Rotation/fatigue risk (important)
  4. Manager press conference quotes (nice-to-have)
  5. Tactical/morale context (nice-to-have)
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

from config import PERPLEXITY_API_KEY, PERPLEXITY_BASE_URL, PERPLEXITY_MODEL
from models.match import Match, NewsContext
from utils.logger import get_logger

logger = get_logger(__name__)

# Rate limiter — 2s between calls (conservative safety margin)
_last_call_at: float = 0.0
_MIN_CALL_INTERVAL = 2.0

# Parallelization — 4 workers keeps us well within any rate limit
_MAX_WORKERS = 4


def _build_query(match: Match) -> str:
    """
    Build a single focused query for one match covering both teams.
    Structured with PRIORITY levels so truncation loses low-value data first.
    """
    kickoff_str = ""
    if match.kickoff:
        kickoff_str = match.kickoff.strftime("%d %B %Y")

    today = datetime.now(timezone.utc).strftime("%d %B %Y")

    return (
        f"Football match preview: {match.home_team} vs {match.away_team} "
        f"({match.league}{', ' + kickoff_str if kickoff_str else ''}). "
        f"Today is {today}. The match is {'on ' + kickoff_str if kickoff_str else 'this week'}. "
        f"CRITICAL: Only use information published within the LAST 7 DAYS. "
        f"Ignore any injury reports older than 7 days — players may have recovered. "
        f"\n\nPRIORITY 1 (MUST HAVE): "
        f"INJURIES, SUSPENSIONS & UNAVAILABLE PLAYERS for this specific upcoming match. "
        f"Only include players confirmed OUT or DOUBTFUL for THIS fixture by the manager or club this week. "
        f"For each player: name, position, status (out/doubt), and reason (injury type/suspension/illness/international duty). "
        f"End with a count: 'Total unavailable: {match.home_team} X players, {match.away_team} Y players'. "
        f"If no recent team news found this week, state 'no confirmed absence news found for [team] this week'. "
        f"Do NOT include players from old injury reports who may have returned. "
        f"\n\nPRIORITY 2 (IMPORTANT): "
        f"Rotation/fatigue: did either team play midweek? How many days rest? "
        f"Any upcoming big fixture within 7 days that could cause rotation? "
        f"\n\nPRIORITY 3 (NICE TO HAVE): "
        f"Manager press conference (this week only): quotes on team selection or absences. "
        f"\n\nPRIORITY 4 (NICE TO HAVE): "
        f"Any unusual context: new manager, morale issues, relegation/promotion pressure. "
        f"Only include if explicitly from this week's news. "
        f"\n\nBe factual and brief. Explicitly state when data is unavailable rather than speculating."
    )


def _rate_limited_call(query: str) -> str:
    """
    Single Perplexity Sonar API call with rate limiting.
    Returns the response text.
    """
    global _last_call_at

    # Enforce minimum interval between calls
    import threading
    _lock = getattr(_rate_limited_call, '_lock', threading.Lock())
    _rate_limited_call._lock = _lock

    with _lock:
        elapsed = time.time() - _last_call_at
        if elapsed < _MIN_CALL_INTERVAL:
            time.sleep(_MIN_CALL_INTERVAL - elapsed)
        _last_call_at = time.time()

    headers = {
        "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "model": PERPLEXITY_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a football analyst assistant. Provide concise, factual, "
                    "data-driven answers. Always cite sources briefly (e.g. 'FBref', "
                    "'BBC Sport', 'club official'). If you cannot find recent data, "
                    "say so clearly rather than speculating."
                ),
            },
            {"role": "user", "content": query},
        ],
        "max_tokens": 800,
        "temperature": 0.1,   # low temperature for factual retrieval
    }

    resp = requests.post(
        f"{PERPLEXITY_BASE_URL}/chat/completions",
        headers=headers,
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _parse_response(raw: str, team_name: str) -> NewsContext:
    """
    Wrap the raw Perplexity response in a NewsContext.
    The response already covers both teams — we keep the full text in summary
    and try to extract team-specific sections.
    """
    return NewsContext(
        team_name=team_name,
        summary=raw,
        retrieved_at=datetime.now(timezone.utc),
    )


def fetch_match_news(match: Match) -> Match:
    """
    Fetch news context for one match. Populates match.home_news and match.away_news.
    Both are set from the same API response (one call per match).
    """
    logger.info(
        "Fetching Perplexity news for [%d] %s vs %s",
        match.game_number, match.home_team, match.away_team,
    )

    query = _build_query(match)

    try:
        raw_response = _rate_limited_call(query)
        logger.debug("Perplexity response for %s vs %s: %s...", match.home_team, match.away_team, raw_response[:120])

        # Both home_news and away_news hold the combined response —
        # Claude will read it and extract what's relevant for each team
        match.home_news = _parse_response(raw_response, match.home_team)
        match.away_news = _parse_response(raw_response, match.away_team)

    except requests.HTTPError as e:
        logger.error("Perplexity HTTP error for %s vs %s: %s", match.home_team, match.away_team, e)
        match.home_news = NewsContext(team_name=match.home_team, summary="News unavailable (API error).")
        match.away_news = NewsContext(team_name=match.away_team, summary="News unavailable (API error).")
    except Exception as e:
        logger.error("Unexpected Perplexity error for %s vs %s: %s", match.home_team, match.away_team, e)
        match.home_news = NewsContext(team_name=match.home_team, summary="News unavailable.")
        match.away_news = NewsContext(team_name=match.away_team, summary="News unavailable.")

    return match


def fetch_all_match_news(matches: list[Match]) -> list[Match]:
    """
    Fetch news for all 13 matches using parallel workers.
    Rate limiting is enforced per-call via a thread lock.
    """
    logger.info("Fetching Perplexity news for %d matches (%d workers)", len(matches), _MAX_WORKERS)

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_match_news, match): match
            for match in matches
        }
        for i, future in enumerate(as_completed(futures), 1):
            match = futures[future]
            try:
                future.result()
                logger.info("Perplexity %d/%d complete: %s vs %s",
                            i, len(matches), match.home_team, match.away_team)
            except Exception as e:
                logger.error("Perplexity worker failed for %s vs %s: %s",
                             match.home_team, match.away_team, e)

    logger.info("Perplexity news fetch complete")
    return matches
