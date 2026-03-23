"""
Standalone xG data collector that runs separately from the newsletter pipeline.

Fetches fixture statistics (including xG) from API-Football for leagues that
Understat doesn't cover: Championship, League One, 2. Bundesliga, Eredivisie, etc.

Run schedule (GitHub Actions, 4x/week — avoids Saturday):
  - Sunday  23:30 UTC → covers Saturday + Sunday
  - Tuesday 23:30 UTC → covers Monday + Tuesday
  - Thursday 23:30 UTC → covers Wednesday + Thursday
  - Friday  23:30 UTC → covers Thursday + Friday (overlap is fine)

Free tier constraint: API-Football only allows today ± 1 day (3-day window).
Default --days 3 matches this. Backfill only works on paid tier.

Budget per run:
  - ~3 date queries + ~12 fixture stat calls ≈ 15 calls — well within 100/day limit

Data is stored in data/xg/xg_history.json and grows over time.
The Saturday pipeline reads from this file to populate xG fields.

Usage:
  python -m fetchers.xg_collector              # Collect last 7 days
  python -m fetchers.xg_collector --backfill   # Collect last 5 weeks
  python -m fetchers.xg_collector --days 14    # Collect last N days
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from config import API_FOOTBALL_BASE_URL, API_FOOTBALL_KEY, DATA_DIR
from utils.logger import get_logger

logger = get_logger(__name__)

XG_DIR = DATA_DIR / "xg"
XG_DIR.mkdir(parents=True, exist_ok=True)
XG_HISTORY_FILE = XG_DIR / "xg_history.json"

# Rate limiting — same as main API-Football module
_request_count = 0
_last_call_at: float = 0.0
_MIN_CALL_INTERVAL = 6.5  # seconds — 10 req/min limit
_DAILY_LIMIT = 100


# ---------------------------------------------------------------------------
# Leagues to collect xG for (ones NOT covered by Understat)
# ---------------------------------------------------------------------------

# API-Football league IDs for leagues we want xG data for.
# Understat covers Big 5: EPL (39), La Liga (140), Bundesliga (78), Serie A (135), Ligue 1 (61)
# We collect for the English lower leagues + Allsvenskan that appear on Stryktipset coupons:
_TARGET_LEAGUES: dict[int, str] = {
    40: "Championship",
    41: "League One",
    113: "Allsvenskan",
}

# Big 5 as fallback (in case Understat is down) — only used with --include-big5 flag
_FALLBACK_LEAGUES: dict[int, str] = {
    39: "Premier League",
    78: "Bundesliga",
    140: "La Liga",
    135: "Serie A",
    61: "Ligue 1",
}


def _api_get(endpoint: str, params: dict) -> dict:
    """API-Football GET with rate limiting and daily cap."""
    global _request_count, _last_call_at

    if _request_count >= _DAILY_LIMIT:
        raise RuntimeError(f"Daily limit of {_DAILY_LIMIT} requests reached.")

    elapsed = time.time() - _last_call_at
    if elapsed < _MIN_CALL_INTERVAL:
        time.sleep(_MIN_CALL_INTERVAL - elapsed)

    url = f"{API_FOOTBALL_BASE_URL}/{endpoint}"
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    logger.debug("xG Collector GET /%s %s [call #%d]", endpoint, params, _request_count + 1)

    resp = requests.get(url, headers=headers, params=params, timeout=20)
    resp.raise_for_status()
    _last_call_at = time.time()
    _request_count += 1

    data = resp.json()
    errors = data.get("errors", {})
    if errors:
        # Free tier date restriction — raise so caller can stop early
        err_str = str(errors)
        if "Free plans" in err_str or "do not have access" in err_str:
            raise ValueError(f"Free tier access denied: {err_str}")
        logger.warning("API-Football error on /%s: %s", endpoint, errors)
        return {"response": []}

    return data


# ---------------------------------------------------------------------------
# Data loading / saving
# ---------------------------------------------------------------------------

def _load_history() -> dict:
    """Load existing xG history or return empty structure."""
    if XG_HISTORY_FILE.exists():
        return json.loads(XG_HISTORY_FILE.read_text(encoding="utf-8"))
    return {"fixtures": {}, "last_updated": None}


def _save_history(history: dict) -> None:
    history["last_updated"] = datetime.now(timezone.utc).isoformat()
    XG_HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("xG history saved to %s (%d fixtures)", XG_HISTORY_FILE, len(history["fixtures"]))


# ---------------------------------------------------------------------------
# Fixture discovery and xG extraction
# ---------------------------------------------------------------------------

def _fetch_fixtures_by_date(date_str: str, league_ids: list[int]) -> list[dict]:
    """
    Fetch all finished fixtures for a given date, then filter to target leagues.
    Uses a single date-only API call (free tier doesn't support league+date combo).
    """
    try:
        data = _api_get("fixtures", {"date": date_str})
    except (RuntimeError, ValueError):
        raise
    except Exception as e:
        logger.warning("Fixture search failed for %s: %s", date_str, e)
        return []

    all_results = data.get("response", [])
    league_id_set = set(league_ids)

    # Filter to our target leagues + finished status
    filtered = [
        f for f in all_results
        if f.get("league", {}).get("id") in league_id_set
        and f.get("fixture", {}).get("status", {}).get("short") == "FT"
    ]

    if filtered:
        # Log per-league counts
        by_league: dict[str, int] = {}
        for f in filtered:
            name = f["league"]["name"]
            by_league[name] = by_league.get(name, 0) + 1
        for name, count in sorted(by_league.items()):
            logger.info("  %s: %d fixtures on %s", name, count, date_str)

    return filtered


def _fetch_fixture_xg(fixture_id: int) -> Optional[dict]:
    """
    Fetch fixture statistics and extract xG for both teams.
    Returns {home_team, away_team, home_xg, away_xg, ...} or None.
    """
    try:
        data = _api_get("fixtures/statistics", {"fixture": fixture_id})
    except RuntimeError:
        raise  # daily limit
    except Exception as e:
        logger.warning("Stats fetch failed for fixture %d: %s", fixture_id, e)
        return None

    result = {}
    for team_entry in data.get("response", []):
        team_name = team_entry.get("team", {}).get("name", "")
        team_id = team_entry.get("team", {}).get("id")
        stats = team_entry.get("statistics", [])

        xg = None
        for s in stats:
            if s.get("type", "").lower() == "expected_goals":
                try:
                    xg = float(s["value"])
                except (ValueError, TypeError):
                    pass

        if "home" not in result:
            result["home"] = {"name": team_name, "id": team_id, "xg": xg}
        else:
            result["away"] = {"name": team_name, "id": team_id, "xg": xg}

    return result if "home" in result and "away" in result else None


# ---------------------------------------------------------------------------
# Main collection logic
# ---------------------------------------------------------------------------

def collect_xg(days: int = 3, include_big5: bool = False) -> int:
    """
    Collect xG data for the last N days.
    Returns the number of new fixtures collected.
    """
    history = _load_history()
    new_count = 0

    league_ids = list(_TARGET_LEAGUES.keys())
    if include_big5:
        league_ids.extend(_FALLBACK_LEAGUES.keys())

    today = datetime.now(timezone.utc).date()

    # Phase 1: Discover fixtures by date
    # Free tier allows today ± 1 day only. We run 4x/week (Sun/Tue/Thu/Fri) to cover all days.
    all_fixtures = []
    dates_searched = 0
    for day_offset in range(days):
        date = today - timedelta(days=day_offset)  # today through N days ago
        date_str = date.isoformat()

        try:
            fixtures = _fetch_fixtures_by_date(date_str, league_ids)
            all_fixtures.extend(fixtures)
            dates_searched += 1
        except RuntimeError as e:
            logger.warning("Stopping fixture discovery: %s", e)
            break
        except ValueError as e:
            # API returns plan access error for older dates — expected on free tier
            if "Free plans" in str(e) or "access" in str(e).lower():
                logger.info("Free tier date limit reached at %s — stopping date search", date_str)
                break
            raise

    logger.info("Found %d total fixtures across %d days searched", len(all_fixtures), dates_searched)

    # Phase 2: Fetch xG stats for each fixture (skip already-collected ones)
    for fixture in all_fixtures:
        fix_id = str(fixture["fixture"]["id"])
        if fix_id in history["fixtures"]:
            continue  # already have this fixture

        fix_info = fixture["fixture"]
        teams = fixture["teams"]
        goals = fixture["goals"]
        league = fixture["league"]

        try:
            xg_data = _fetch_fixture_xg(int(fix_id))
        except RuntimeError as e:
            logger.warning("Stopping xG collection: %s", e)
            break

        if xg_data is None:
            continue

        home_xg = xg_data["home"]["xg"]
        away_xg = xg_data["away"]["xg"]

        if home_xg is None and away_xg is None:
            logger.debug("No xG data for fixture %s — skipping", fix_id)
            continue

        history["fixtures"][fix_id] = {
            "date": fix_info.get("date", "")[:10],
            "league": league.get("name", ""),
            "league_id": league.get("id"),
            "country": league.get("country", ""),
            "home_team": teams.get("home", {}).get("name", ""),
            "away_team": teams.get("away", {}).get("name", ""),
            "home_team_id": teams.get("home", {}).get("id"),
            "away_team_id": teams.get("away", {}).get("id"),
            "home_goals": goals.get("home"),
            "away_goals": goals.get("away"),
            "home_xg": home_xg,
            "away_xg": away_xg,
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }
        new_count += 1
        logger.info(
            "  Collected: %s %s-%s %s | xG: %.2f-%.2f (%s)",
            teams["home"]["name"],
            goals.get("home", "?"),
            goals.get("away", "?"),
            teams["away"]["name"],
            home_xg or 0,
            away_xg or 0,
            league.get("name", "?"),
        )

    _save_history(history)
    logger.info(
        "xG collection complete: %d new fixtures (API calls used: %d/%d)",
        new_count, _request_count, _DAILY_LIMIT,
    )
    return new_count


# ---------------------------------------------------------------------------
# Read interface for the Saturday pipeline
# ---------------------------------------------------------------------------

@dataclass
class TeamXGProfile:
    """Pre-computed xG profile for one team, ready to inject into Claude's prompt."""
    team_name: str
    matches_available: int  # how many matches we have data for

    # 5-game averages
    xg_for_5g: Optional[float] = None
    xg_against_5g: Optional[float] = None
    goals_for_5g: Optional[float] = None
    goals_against_5g: Optional[float] = None

    # 10-game averages (longer-term trend)
    xg_for_10g: Optional[float] = None
    xg_against_10g: Optional[float] = None
    goals_for_10g: Optional[float] = None
    goals_against_10g: Optional[float] = None

    @property
    def overperf_5g(self) -> Optional[float]:
        """Goals scored minus xG (5g). Positive = clinical, negative = wasteful."""
        if self.goals_for_5g is not None and self.xg_for_5g is not None:
            return round(self.goals_for_5g - self.xg_for_5g, 2)
        return None

    @property
    def overperf_10g(self) -> Optional[float]:
        """Goals scored minus xG (10g). Positive = clinical, negative = wasteful."""
        if self.goals_for_10g is not None and self.xg_for_10g is not None:
            return round(self.goals_for_10g - self.xg_for_10g, 2)
        return None

    @property
    def defensive_overperf_5g(self) -> Optional[float]:
        """Goals conceded minus xGA (5g). Negative = defence overperforming."""
        if self.goals_against_5g is not None and self.xg_against_5g is not None:
            return round(self.goals_against_5g - self.xg_against_5g, 2)
        return None

    def format_for_prompt(self) -> str:
        """Format as a compact line for Claude's match block."""
        parts = []
        if self.xg_for_5g is not None:
            op = self.overperf_5g
            op_str = f"{op:+.2f}" if op is not None else "?"
            parts.append(
                f"5g: xGF:{self.xg_for_5g:.2f} xGA:{self.xg_against_5g:.2f} "
                f"| GF:{self.goals_for_5g:.1f} GA:{self.goals_against_5g:.1f} "
                f"| overperf:{op_str}"
            )
        if self.xg_for_10g is not None:
            op = self.overperf_10g
            op_str = f"{op:+.2f}" if op is not None else "?"
            parts.append(
                f"10g: xGF:{self.xg_for_10g:.2f} xGA:{self.xg_against_10g:.2f} "
                f"| GF:{self.goals_for_10g:.1f} GA:{self.goals_against_10g:.1f} "
                f"| overperf:{op_str}"
            )
        return " || ".join(parts) if parts else ""


def _find_team_matches(team_name: str, history: dict) -> list[dict]:
    """Find all fixtures for a team in the history, sorted by date descending."""
    team_lower = team_name.lower().strip()
    matches = []

    sorted_fixtures = sorted(
        history["fixtures"].values(),
        key=lambda f: f.get("date", ""),
        reverse=True,
    )

    for f in sorted_fixtures:
        home = f.get("home_team", "").lower()
        away = f.get("away_team", "").lower()

        if f.get("home_xg") is None or f.get("away_xg") is None:
            continue

        # Fuzzy match
        is_home = (team_lower == home or team_lower in home or home in team_lower)
        is_away = (team_lower == away or team_lower in away or away in team_lower)

        if is_home:
            matches.append({
                "xg_for": f["home_xg"], "xg_against": f["away_xg"],
                "goals_for": f.get("home_goals", 0), "goals_against": f.get("away_goals", 0),
                "date": f["date"], "home_away": "H",
            })
        elif is_away:
            matches.append({
                "xg_for": f["away_xg"], "xg_against": f["home_xg"],
                "goals_for": f.get("away_goals", 0), "goals_against": f.get("home_goals", 0),
                "date": f["date"], "home_away": "A",
            })

        if len(matches) >= 10:  # cap at 10
            break

    return matches


def get_team_xg_profile(team_name: str) -> Optional[TeamXGProfile]:
    """
    Compute a full xG profile for a team from stored history.
    Returns 5-game and 10-game averages for xG and actual goals.
    Returns None if fewer than 2 matches available.
    """
    history = _load_history()
    if not history["fixtures"]:
        return None

    matches = _find_team_matches(team_name, history)
    if len(matches) < 2:
        return None

    def _avg(data: list[dict], key: str, n: int) -> Optional[float]:
        subset = data[:n]
        if not subset:
            return None
        vals = [m[key] for m in subset if m.get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    profile = TeamXGProfile(
        team_name=team_name,
        matches_available=len(matches),
        # 5-game averages
        xg_for_5g=_avg(matches, "xg_for", 5),
        xg_against_5g=_avg(matches, "xg_against", 5),
        goals_for_5g=_avg(matches, "goals_for", 5),
        goals_against_5g=_avg(matches, "goals_against", 5),
    )

    # 10-game averages (only if we have enough data)
    if len(matches) >= 5:
        profile.xg_for_10g = _avg(matches, "xg_for", 10)
        profile.xg_against_10g = _avg(matches, "xg_against", 10)
        profile.goals_for_10g = _avg(matches, "goals_for", 10)
        profile.goals_against_10g = _avg(matches, "goals_against", 10)

    return profile


def get_team_xg(team_name: str, n_matches: int = 5) -> Optional[tuple[float, float]]:
    """
    Simple interface for backward compatibility.
    Returns (xg_for_avg, xg_against_avg) or None.
    """
    profile = get_team_xg_profile(team_name)
    if profile is None or profile.xg_for_5g is None:
        return None
    return profile.xg_for_5g, profile.xg_against_5g


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Collect xG data from API-Football")
    parser.add_argument("--days", type=int, default=3, help="Number of days to look back (default: 3, free tier max)")
    parser.add_argument("--backfill", action="store_true", help="Backfill last 5 weeks (35 days)")
    parser.add_argument("--include-big5", action="store_true", help="Also collect Big 5 leagues (normally covered by Understat)")

    args = parser.parse_args()
    days = 35 if args.backfill else args.days

    logger.info("=" * 50)
    logger.info("xG COLLECTOR — fetching last %d days", days)
    logger.info("=" * 50)

    new = collect_xg(days=days, include_big5=args.include_big5)
    print(f"\nCollected {new} new fixtures. API calls: {_request_count}/{_DAILY_LIMIT}")

    # Show summary
    history = _load_history()
    if history["fixtures"]:
        leagues = {}
        for f in history["fixtures"].values():
            league = f.get("league", "Unknown")
            leagues[league] = leagues.get(league, 0) + 1
        print("\nxG database summary:")
        for league, count in sorted(leagues.items(), key=lambda x: -x[1]):
            print(f"  {league}: {count} fixtures")


if __name__ == "__main__":
    main()
