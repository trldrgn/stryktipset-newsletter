"""
Fetches the current Stryktipset coupon and most recent results
from the undocumented but stable Svenska Spel JSON API.

Endpoints used:
  GET /draws           → list of open draws (find current draw number)
  GET /draws/{number}  → full coupon for that draw
  GET /draws/result    → most recently finalized draw (for evaluation)

No authentication required.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

import requests

from config import SVENSKA_SPEL_API_BASE
from models.match import (
    Match,
    MarketSignals,
    Outcome,
)
from utils.logger import get_logger

logger = get_logger(__name__)

_SESSION = requests.Session()
_SESSION.headers.update({
    "Accept": "application/json",
    "User-Agent": "stryktipset-newsletter/1.0",
})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sv_float(value: str | None) -> Optional[float]:
    """Convert Swedish decimal string '2,55' to Python float 2.55."""
    if not value:
        return None
    try:
        return float(str(value).replace(",", "."))
    except (ValueError, TypeError):
        return None


def _parse_kickoff(raw: str | None) -> Optional[datetime]:
    """Parse ISO-8601 datetime string from the API into UTC-aware datetime."""
    if not raw:
        return None
    try:
        # API returns e.g. "2026-03-21T21:00:00+01:00"
        return datetime.fromisoformat(raw).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _parse_market(event: dict) -> MarketSignals:
    """Extract odds + public distribution + newspaper tips from one event dict."""
    odds = event.get("startOdds", {})
    svfolk = event.get("svenskaFolket", {})
    tips = event.get("tioTidningarsTips", {})

    return MarketSignals(
        odds_home=_sv_float(odds.get("one")),
        odds_draw=_sv_float(odds.get("x")),
        odds_away=_sv_float(odds.get("two")),
        public_pct_home=int(svfolk["one"]) if svfolk.get("one") else None,
        public_pct_draw=int(svfolk["x"]) if svfolk.get("x") else None,
        public_pct_away=int(svfolk["two"]) if svfolk.get("two") else None,
        newspaper_tips_home=tips.get("one", 0),
        newspaper_tips_draw=tips.get("x", 0),
        newspaper_tips_away=tips.get("two", 0),
    )


def _parse_event(event: dict, draw_number: int) -> Match:
    """Parse one drawEvent into a Match dataclass."""
    match_data = event.get("match", {})
    participants = match_data.get("participants", [])

    home = next((p for p in participants if p.get("type") == "home"), {})
    away = next((p for p in participants if p.get("type") == "away"), {})

    league = match_data.get("league", {})

    return Match(
        game_number=event["eventNumber"],
        draw_number=draw_number,
        home_team=home.get("name", event.get("eventDescription", "").split(" - ")[0]),
        away_team=away.get("name", event.get("eventDescription", "").split(" - ")[-1]),
        league=league.get("name", ""),
        country=league.get("country", {}).get("name", ""),
        kickoff=_parse_kickoff(match_data.get("matchStart")),
        market=_parse_market(event),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_current_draw_number() -> int:
    """
    Find the draw number for the upcoming/open Stryktipset coupon.

    Strategy:
      1. Try /draws — returns list of open draws
      2. If empty (draw not yet open), fall back to /draws/result draw number + 1
    """
    url = f"{SVENSKA_SPEL_API_BASE}/draws"
    logger.info("Fetching open draws from %s", url)
    resp = _SESSION.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    draws = data.get("draws", [])
    if draws:
        draw_number = draws[0]["draw"]["drawNumber"]
        logger.info("Found open draw: %d", draw_number)
        return draw_number

    # No open draw yet — use result draw number + 1 as next week's number
    logger.warning("No open draws found, inferring from last result")
    result = get_latest_result_raw()
    next_draw = result["drawNumber"] + 1
    logger.info("Inferred upcoming draw number: %d", next_draw)
    return next_draw


def fetch_coupon(draw_number: int) -> list[Match]:
    """
    Fetch the 13 matches for a given draw number.
    Returns a list of Match objects with market signals populated.
    """
    url = f"{SVENSKA_SPEL_API_BASE}/draws/{draw_number}"
    logger.info("Fetching coupon for draw %d from %s", draw_number, url)
    resp = _SESSION.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if data.get("error"):
        raise ValueError(f"Svenska Spel API error: {data['error']}")

    draw = data["draw"]
    events = draw.get("drawEvents", [])
    if not events:
        raise ValueError(f"No events found in draw {draw_number}")

    matches = [_parse_event(ev, draw_number) for ev in events]
    logger.info("Fetched %d matches for draw %d", len(matches), draw_number)

    for m in matches:
        logger.debug(
            "  [%d] %s vs %s (%s) — odds 1:%s X:%s 2:%s",
            m.game_number, m.home_team, m.away_team, m.league,
            m.market.odds_home, m.market.odds_draw, m.market.odds_away,
        )

    return matches


def fetch_current_coupon() -> tuple[int, list[Match]]:
    """
    Convenience: find current draw number and return (draw_number, matches).
    """
    draw_number = get_current_draw_number()
    matches = fetch_coupon(draw_number)
    return draw_number, matches


def get_latest_result_raw() -> dict:
    """
    Fetch the most recently finalized draw result.
    Returns the raw 'draw' dict (with 'drawEvents' containing 'outcome' fields).
    """
    url = f"{SVENSKA_SPEL_API_BASE}/draws/result"
    logger.info("Fetching latest result from %s", url)
    resp = _SESSION.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if data.get("error"):
        raise ValueError(f"Svenska Spel result API error: {data['error']}")

    return data.get("result", data.get("draw", data))


def fetch_result(draw_number: int) -> dict[int, Outcome]:
    """
    Fetch the outcomes for a specific finalized draw.
    Returns a dict mapping game_number → Outcome (1/X/2).

    Note: the API only serves the LATEST result via /draws/result.
    Older results must be fetched from /draws/{number} (which includes
    outcome data once the draw is closed).
    """
    url = f"{SVENSKA_SPEL_API_BASE}/draws/{draw_number}"
    logger.info("Fetching result for draw %d", draw_number)
    resp = _SESSION.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    draw = data.get("draw", {})
    events = draw.get("drawEvents", [])

    results: dict[int, Outcome] = {}
    for ev in events:
        game_num = ev["eventNumber"]
        # Outcome is present once the draw is settled
        outcome_raw = ev.get("outcome") or ev.get("outcomeDescription", "")
        if outcome_raw in ("1", "X", "2"):
            results[game_num] = Outcome(outcome_raw)
        else:
            logger.warning("No outcome yet for game %d in draw %d", game_num, draw_number)

    logger.info(
        "Fetched %d/%d results for draw %d",
        len(results), len(events), draw_number,
    )
    return results
