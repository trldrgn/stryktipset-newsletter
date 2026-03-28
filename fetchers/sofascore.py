"""
Fetches xG data from Sofascore's internal API for leagues NOT covered by Understat.

Covers: Championship, League One, League Two, Allsvenskan, Eredivisie, Primeira Liga.
Understat already covers: PL, La Liga, Bundesliga, Serie A, Ligue 1.

Uses curl_cffi for TLS fingerprint impersonation (bypasses Cloudflare).
No API key or authentication required.

Rate limited: 2.5s between calls. All responses cached (7-day TTL).

If curl_cffi is not installed, this module degrades gracefully — it logs
a warning and returns matches unmodified.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from models.match import Match, TeamStats
from utils.cache import cached
from utils.logger import get_logger

logger = get_logger(__name__)

# Check for curl_cffi availability
try:
    from curl_cffi import requests as cffi_requests
    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False
    logger.warning("curl_cffi not installed — Sofascore xG enrichment disabled")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://api.sofascore.com/api/v1"
_MIN_CALL_INTERVAL = 2.5  # seconds between API calls
_last_call_at: float = 0.0

# Map: (Svenska Spel league, country) -> Sofascore tournament ID
_TOURNAMENT_MAP: dict[tuple[str, str], int] = {
    ("Championship", "England"): 18,
    ("League One", "England"): 24,
    ("League Two", "England"): 25,
    ("Allsvenskan", "Sweden"): 40,
    ("Eredivisie", "Netherlands"): 37,
    ("Primeira Liga", "Portugal"): 238,
}

# Hardcoded season IDs (updated each season; fallback fetches dynamically)
_SEASON_MAP: dict[int, int] = {
    18: 77347,   # Championship 2025-26
    24: 77352,   # League One 2025-26
    25: 77351,   # League Two 2025-26
    40: 87925,   # Allsvenskan 2026
    37: 77012,   # Eredivisie 2025-26
    238: 77806,  # Primeira Liga 2025-26
}


# ---------------------------------------------------------------------------
# HTTP layer with rate limiting
# ---------------------------------------------------------------------------

def _sofa_get(path: str) -> Optional[dict]:
    """GET from Sofascore API with rate limiting and error handling."""
    global _last_call_at

    if not _HAS_CURL_CFFI:
        return None

    elapsed = time.time() - _last_call_at
    if elapsed < _MIN_CALL_INTERVAL:
        time.sleep(_MIN_CALL_INTERVAL - elapsed)

    url = f"{_BASE_URL}/{path}"
    try:
        resp = cffi_requests.get(url, impersonate="chrome", timeout=15)
        _last_call_at = time.time()

        if resp.status_code == 403:
            logger.warning("Sofascore 403 (Cloudflare block) on %s — stopping", path)
            return None
        if resp.status_code == 429:
            logger.warning("Sofascore 429 (rate limited) on %s — stopping", path)
            return None
        if resp.status_code != 200:
            logger.debug("Sofascore %d on %s", resp.status_code, path)
            return None

        return resp.json()
    except Exception as e:
        logger.warning("Sofascore request failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Season ID resolution
# ---------------------------------------------------------------------------

_blocked = False  # set to True on 403/429 to stop all further calls


@cached(lambda tid: f"sofa_season_{tid}")
def _fetch_season_id(tid: int) -> Optional[int]:
    """Fetch the current season ID for a tournament."""
    data = _sofa_get(f"unique-tournament/{tid}/seasons")
    if data is None:
        return None
    seasons = data.get("seasons", [])
    return seasons[0]["id"] if seasons else None


def _get_season_id(tid: int) -> Optional[int]:
    """Get season ID — try hardcoded first, fallback to API."""
    if tid in _SEASON_MAP:
        return _SEASON_MAP[tid]
    return _fetch_season_id(tid)


# ---------------------------------------------------------------------------
# Standings and team resolution
# ---------------------------------------------------------------------------

@cached(lambda tid, sid: f"sofa_standings_{tid}_{sid}")
def _fetch_standings(tid: int, sid: int) -> Optional[list[dict]]:
    """Fetch league standings. Returns list of row dicts."""
    data = _sofa_get(f"unique-tournament/{tid}/season/{sid}/standings/total")
    if data is None:
        return None
    standings = data.get("standings", [])
    if not standings:
        return None
    return standings[0].get("rows", [])


def _resolve_team(team_name: str, standings: list[dict]) -> Optional[dict]:
    """Fuzzy match a team name against Sofascore standings."""
    name_lower = team_name.lower().strip()

    for row in standings:
        sofa_name = row.get("team", {}).get("name", "")
        sofa_lower = sofa_name.lower()

        # Exact match
        if sofa_lower == name_lower:
            return row

        # Substring match
        if name_lower in sofa_lower or sofa_lower in name_lower:
            return row

    # Word overlap
    name_words = {w for w in name_lower.split() if len(w) > 3}
    if name_words:
        best_row = None
        best_overlap = 0
        for row in standings:
            sofa_name = row.get("team", {}).get("name", "")
            sofa_words = {w for w in sofa_name.lower().split() if len(w) > 3}
            overlap = len(name_words & sofa_words)
            if overlap > best_overlap:
                best_overlap = overlap
                best_row = row
        if best_row and best_overlap > 0:
            return best_row

    return None


# ---------------------------------------------------------------------------
# Match-level xG extraction
# ---------------------------------------------------------------------------

@cached(lambda eid: f"sofa_xg_{eid}")
def _fetch_match_xg(eid: int) -> Optional[tuple[float, float]]:
    """Fetch xG for a single match. Returns (home_xg, away_xg) or None."""
    data = _sofa_get(f"event/{eid}/statistics")
    if data is None:
        return None

    for period in data.get("statistics", []):
        if period.get("period") != "ALL":
            continue
        for group in period.get("groups", []):
            for item in group.get("statisticsItems", []):
                if "expected" in item.get("name", "").lower():
                    try:
                        home_xg = float(item["home"])
                        away_xg = float(item["away"])
                        return (home_xg, away_xg)
                    except (KeyError, ValueError, TypeError):
                        pass
    return None


@cached(lambda tid, sid, rnd: f"sofa_round_{tid}_{sid}_{rnd}")
def _fetch_round_events(tid: int, sid: int, rnd: int) -> Optional[list[dict]]:
    """Fetch all events (matches) for a given round."""
    data = _sofa_get(f"unique-tournament/{tid}/season/{sid}/events/round/{rnd}")
    if data is None:
        return None
    return data.get("events", [])


# ---------------------------------------------------------------------------
# Team xG profile computation
# ---------------------------------------------------------------------------

@dataclass
class _TeamXGResult:
    """Internal result from Sofascore xG computation."""
    xg_for_avg: float
    xg_against_avg: float
    goals_for_avg: Optional[float] = None
    goals_against_avg: Optional[float] = None


def _compute_team_xg(
    team_name: str,
    team_sofa_id: int,
    tid: int,
    sid: int,
    standings: list[dict],
) -> Optional[_TeamXGResult]:
    """
    Find a team's last 5 finished matches and compute average xG and goals for/against.
    Returns _TeamXGResult with xG averages and actual goal averages, or None.
    """
    global _blocked
    if _blocked:
        return None

    # Determine current round from standings (matches played by this team)
    current_round = None
    for row in standings:
        if row.get("team", {}).get("id") == team_sofa_id:
            current_round = row.get("matches", 0)
            break

    if not current_round:
        return None

    # Walk backwards through rounds to find this team's matches
    # Each entry: (xg_for, xg_against, goals_for, goals_against)
    match_data: list[tuple[float, float, int, int]] = []

    for rnd in range(current_round, max(current_round - 12, 0), -1):
        if len(match_data) >= 5:
            break
        if _blocked:
            break

        events = _fetch_round_events(tid, sid, rnd)
        if events is None:
            _blocked = True
            break

        for ev in events:
            if ev.get("status", {}).get("type") != "finished":
                continue

            home_id = ev.get("homeTeam", {}).get("id")
            away_id = ev.get("awayTeam", {}).get("id")

            if team_sofa_id not in (home_id, away_id):
                continue

            eid = ev.get("id")
            if not eid:
                continue

            xg = _fetch_match_xg(eid)
            if _blocked or xg is None:
                if _sofa_get is None:
                    _blocked = True
                continue

            home_xg, away_xg = xg

            # Extract actual goals from the event scores
            home_goals = ev.get("homeScore", {}).get("current", 0)
            away_goals = ev.get("awayScore", {}).get("current", 0)
            try:
                home_goals = int(home_goals)
                away_goals = int(away_goals)
            except (ValueError, TypeError):
                home_goals, away_goals = 0, 0

            if team_sofa_id == home_id:
                match_data.append((home_xg, away_xg, home_goals, away_goals))
            else:
                match_data.append((away_xg, home_xg, away_goals, home_goals))

            if len(match_data) >= 5:
                break

    if len(match_data) < 2:
        return None

    n = len(match_data)
    return _TeamXGResult(
        xg_for_avg=round(sum(d[0] for d in match_data) / n, 2),
        xg_against_avg=round(sum(d[1] for d in match_data) / n, 2),
        goals_for_avg=round(sum(d[2] for d in match_data) / n, 2),
        goals_against_avg=round(sum(d[3] for d in match_data) / n, 2),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def enrich_with_sofascore_xg(matches: list[Match]) -> list[Match]:
    """
    Enrich matches with xG data from Sofascore for gap leagues.

    Only sets xg_for_avg / xg_against_avg if they are currently None
    (i.e. Understat didn't already fill them).
    """
    global _blocked
    _blocked = False

    if not _HAS_CURL_CFFI:
        logger.warning("Sofascore xG enrichment skipped (curl_cffi not installed)")
        return matches

    enriched_count = 0

    # Group by league to share standings lookups
    league_cache: dict[int, tuple[int, list[dict]]] = {}  # tid -> (sid, standings)

    for match in matches:
        if _blocked:
            break

        key = (match.league or "", match.country or "")
        tid = _TOURNAMENT_MAP.get(key)
        if tid is None:
            continue

        # Get or fetch standings for this league
        if tid not in league_cache:
            sid = _get_season_id(tid)
            if sid is None:
                continue
            standings = _fetch_standings(tid, sid)
            if standings is None:
                if _blocked:
                    break
                continue
            league_cache[tid] = (sid, standings)

        sid, standings = league_cache[tid]

        for is_home in (True, False):
            if _blocked:
                break

            team_name = match.home_team if is_home else match.away_team
            stats = match.home_stats if is_home else match.away_stats

            if stats is None:
                stats = TeamStats(team_name=team_name)
                if is_home:
                    match.home_stats = stats
                else:
                    match.away_stats = stats

            # Deduplication: skip if Understat already set xG
            if stats.xg_for_avg is not None:
                continue

            # Resolve team in Sofascore standings
            row = _resolve_team(team_name, standings)
            if row is None:
                logger.debug("Sofascore: could not resolve '%s'", team_name)
                continue

            team_sofa_id = row["team"]["id"]

            result = _compute_team_xg(team_name, team_sofa_id, tid, sid, standings)
            if result is not None:
                stats.xg_for_avg = result.xg_for_avg
                stats.xg_against_avg = result.xg_against_avg
                enriched_count += 1

                # Build a TeamXGProfile so overperformance is available in prompts
                try:
                    from fetchers.xg_collector import TeamXGProfile
                    profile = TeamXGProfile(
                        team_name=team_name,
                        matches_available=5,
                        xg_for_5g=result.xg_for_avg,
                        xg_against_5g=result.xg_against_avg,
                        goals_for_5g=result.goals_for_avg,
                        goals_against_5g=result.goals_against_avg,
                    )
                    stats._xg_profile = profile
                    logger.info(
                        "  %s: %s (Sofascore)",
                        team_name, profile.format_for_prompt(),
                    )
                except ImportError:
                    logger.info(
                        "  %s: xG %.2f for / %.2f against (Sofascore)",
                        team_name, result.xg_for_avg, result.xg_against_avg,
                    )

    logger.info("Sofascore xG enrichment complete: %d teams enriched", enriched_count)
    return matches
