"""
Fetches current-season standings and form from Football-Data.org (free tier).

Covers what API-Football's free tier blocks:
  - League table (position, points)
  - Last 5 results + home/away splits
  - Days-since-last-match / fatigue context

Free plan:
  - Rate limit: 10 req/min  →  6.5s enforced between calls
  - Competitions: PL, Championship, League One, + major European leagues
  - No season-year restriction — always returns current season
  - Authentication: X-Auth-Token header
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import requests

from config import FOOTBALL_DATA_API_KEY
from models.match import Match, TeamStats, FormResult, ScheduleContext, Outcome
from utils.cache import cached
from utils.logger import get_logger

logger = get_logger(__name__)

_BASE_URL = "https://api.football-data.org/v4"
_last_call_at: float = 0.0
_MIN_CALL_INTERVAL = 6.5  # seconds — 10 req/min limit

_SESSION = requests.Session()
_SESSION.headers.update({
    "X-Auth-Token": FOOTBALL_DATA_API_KEY,
    "Accept": "application/json",
})

# Svenska Spel league name → Football-Data.org competition code
_COMPETITION_MAP: dict[tuple[str, str], str] = {
    ("Premier League", "England"): "PL",
    ("Championship", "England"): "ELC",
    ("League One", "England"): "EL1",
    ("League Two", "England"): "EL2",
    ("Bundesliga", "Germany"): "BL1",
    ("2. Bundesliga", "Germany"): "BL2",
    ("La Liga", "Spain"): "PD",
    ("Primera Division", "Spain"): "PD",
    ("Serie A", "Italy"): "SA",
    ("Ligue 1", "France"): "FL1",
    ("Eredivisie", "Netherlands"): "DED",
    ("Primeira Liga", "Portugal"): "PPL",
}

# Cup teams play in a league — map cup competitions to the league(s) to look up
# standings/form for. We try each league in order until the team is found.
_CUP_FALLBACK_LEAGUES: dict[tuple[str, str], list[str]] = {
    ("FA Cup", "England"): ["PL", "ELC", "EL1", "EL2"],
}


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def _api_get(path: str) -> dict:
    global _last_call_at
    elapsed = time.time() - _last_call_at
    if elapsed < _MIN_CALL_INTERVAL:
        time.sleep(_MIN_CALL_INTERVAL - elapsed)

    url = f"{_BASE_URL}/{path}"
    logger.debug("Football-Data GET %s", path)
    resp = _SESSION.get(url, timeout=20)
    _last_call_at = time.time()

    if resp.status_code == 429:
        logger.warning("Football-Data.org rate limit hit — sleeping 60s")
        time.sleep(60)
        resp = _SESSION.get(url, timeout=20)
        _last_call_at = time.time()

    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Cached API calls
# ---------------------------------------------------------------------------

@cached(lambda code: f"fd_standings_{code}")
def _fetch_standings(competition_code: str) -> dict:
    return _api_get(f"competitions/{competition_code}/standings")


@cached(lambda team_id: f"fd_team_matches_{team_id}")
def _fetch_team_matches(team_id: int) -> dict:
    """Last 15 finished matches for a team across all competitions."""
    return _api_get(f"teams/{team_id}/matches?status=FINISHED&limit=15")


# ---------------------------------------------------------------------------
# Team resolution
# ---------------------------------------------------------------------------

def _resolve_team(team_name: str, standings: dict) -> Optional[dict]:
    """
    Find a team in standings data by fuzzy name match.
    Returns the full standings row (position, points, team.id, ...) or None.
    """
    name_lower = team_name.lower().strip()

    best_row = None
    best_score = 0

    # standings["standings"] is a list of {type: "TOTAL"|"HOME"|"AWAY", table: [...]}
    total_table = next(
        (s["table"] for s in standings.get("standings", []) if s.get("type") == "TOTAL"),
        [],
    )

    for row in total_table:
        team = row.get("team", {})
        candidates = [
            team.get("name", ""),
            team.get("shortName", ""),
            team.get("tla", ""),
        ]
        score = 0
        for c in candidates:
            c_lower = c.lower()
            if not c_lower:
                continue
            if name_lower == c_lower:
                score = max(score, 100)
            elif name_lower in c_lower or c_lower in name_lower:
                # Substring match is only strong enough when the shorter side is
                # not a trivial fragment (e.g. "Port" matching "Portsmouth").
                shorter = min(len(name_lower), len(c_lower))
                if shorter >= 6:
                    score = max(score, 85)
                else:
                    score = max(score, 60)

        if score > best_score:
            best_score = score
            best_row = row

    # Fail closed: require a strong match. Previously threshold was 50, which
    # let Port Vale (League One) fuzzy-match to a Championship team and inherit
    # that team's fixtures + a bogus fatigue flag.
    if best_row and best_score >= 80:
        logger.debug(
            "Matched '%s' → '%s' (score %d)",
            team_name, best_row["team"].get("name"), best_score,
        )
        return best_row

    logger.warning(
        "Could not match '%s' in Football-Data.org standings (best score %d)",
        team_name, best_score,
    )
    return None


# ---------------------------------------------------------------------------
# Form parsing
# ---------------------------------------------------------------------------

def _form_result(fixture: dict, team_id: int) -> Optional[FormResult]:
    """Parse a Football-Data.org match into a FormResult."""
    if fixture.get("status") != "FINISHED":
        return None

    home_team = fixture.get("homeTeam", {})
    away_team = fixture.get("awayTeam", {})
    ft = fixture.get("score", {}).get("fullTime", {})

    is_home = home_team.get("id") == team_id
    opponent = away_team.get("name", "") if is_home else home_team.get("name", "")

    gf = ft.get("home") if is_home else ft.get("away")
    ga = ft.get("away") if is_home else ft.get("home")

    if gf is None or ga is None:
        return None

    # Team-perspective Outcome: 1 = tracked team won, X = draw, 2 = tracked team lost.
    winner = fixture.get("score", {}).get("winner")
    if winner == "DRAW":
        result = Outcome.DRAW
    elif (winner == "HOME_TEAM" and is_home) or (winner == "AWAY_TEAM" and not is_home):
        result = Outcome.HOME
    else:
        result = Outcome.AWAY

    return FormResult(
        opponent=opponent,
        home_or_away="H" if is_home else "A",
        goals_for=gf,
        goals_against=ga,
        result=result,
        competition=fixture.get("competition", {}).get("name", ""),
    )


def _build_form(team_id: int, team_name: str) -> tuple[list[FormResult], list[FormResult], list[FormResult], Optional[ScheduleContext]]:
    """
    Fetch and parse form for a team.
    Returns (form_last5, form_home_only, form_away_only, schedule).
    """
    try:
        data = _fetch_team_matches(team_id)
    except Exception as e:
        logger.warning("Football-Data form fetch failed for %s: %s", team_name, e)
        return [], [], [], None

    matches = sorted(
        data.get("matches", []),
        key=lambda m: m.get("utcDate", ""),
        reverse=True,
    )

    form: list[FormResult] = []
    home_form: list[FormResult] = []
    away_form: list[FormResult] = []

    for m in matches:
        fr = _form_result(m, team_id)
        if fr is None:
            continue
        if len(form) < 5:
            form.append(fr)
        if fr.home_or_away == "H" and len(home_form) < 5:
            home_form.append(fr)
        if fr.home_or_away == "A" and len(away_form) < 5:
            away_form.append(fr)
        if len(form) >= 5 and len(home_form) >= 5 and len(away_form) >= 5:
            break

    # Build schedule context from most recent match
    schedule: Optional[ScheduleContext] = None
    if matches:
        try:
            last_dt = datetime.fromisoformat(matches[0]["utcDate"].replace("Z", "+00:00"))
            days_since = (datetime.now(timezone.utc) - last_dt).days
            matches_14d = sum(
                1 for m in matches
                if (datetime.now(timezone.utc) - datetime.fromisoformat(
                    m["utcDate"].replace("Z", "+00:00")
                )).days <= 14
            )
            schedule = ScheduleContext(
                days_since_last_match=days_since,
                last_match_competition=matches[0].get("competition", {}).get("name", ""),
                matches_last_14_days=matches_14d,
            )
        except Exception:
            pass

    return form, home_form, away_form, schedule


# ---------------------------------------------------------------------------
# Main enrichment entry point
# ---------------------------------------------------------------------------

def enrich_with_football_data(matches: list[Match]) -> list[Match]:
    """
    Fill league position, form, and fatigue context using Football-Data.org.
    Only fills fields that API-Football left empty.
    Called after enrich_all_matches().
    """
    if not FOOTBALL_DATA_API_KEY:
        logger.warning("FOOTBALL_DATA_API_KEY not set — skipping Football-Data.org enrichment")
        return matches

    # Cache standings per competition (one call per league, shared across all teams)
    standings_cache: dict[str, dict] = {}

    for match in matches:
        comp_code = _COMPETITION_MAP.get((match.league, match.country))
        # For cup competitions, try fallback league lookups per team
        cup_fallbacks = _CUP_FALLBACK_LEAGUES.get((match.league, match.country))
        if not comp_code and not cup_fallbacks:
            logger.debug("No Football-Data.org mapping for %s (%s)", match.league, match.country)
            continue

        # For league matches, fetch standings once
        if comp_code and comp_code not in standings_cache:
            try:
                standings_cache[comp_code] = _fetch_standings(comp_code)
                logger.info("Football-Data standings fetched for %s", comp_code)
            except Exception as e:
                logger.warning("Football-Data standings failed for %s: %s", comp_code, e)
                standings_cache[comp_code] = {}

        for team_name, stats_attr in [(match.home_team, "home_stats"), (match.away_team, "away_stats")]:
            stats: Optional[TeamStats] = getattr(match, stats_attr)
            if stats is None:
                stats = TeamStats(team_name=team_name)
                setattr(match, stats_attr, stats)

            # Skip if we already have good data
            if stats.league_position is not None and stats.form_last5:
                continue

            # For cup matches, search through fallback leagues until team is found
            if cup_fallbacks:
                row = None
                for fallback_code in cup_fallbacks:
                    if fallback_code not in standings_cache:
                        try:
                            standings_cache[fallback_code] = _fetch_standings(fallback_code)
                            logger.info("Football-Data standings fetched for %s", fallback_code)
                        except Exception as e:
                            logger.warning("Football-Data standings failed for %s: %s", fallback_code, e)
                            standings_cache[fallback_code] = {}
                    standings = standings_cache[fallback_code]
                    if standings:
                        row = _resolve_team(team_name, standings)
                        if row:
                            break
            else:
                standings = standings_cache.get(comp_code, {})
                row = _resolve_team(team_name, standings) if standings else None

            if row is None:
                continue

            team_id = row["team"]["id"]

            # Fill position + points
            if stats.league_position is None:
                stats.league_position = row.get("position")
            if stats.league_points is None:
                stats.league_points = row.get("points")

            # Fill form
            if not stats.form_last5:
                form, home_form, away_form, schedule = _build_form(team_id, team_name)
                stats.form_last5 = form
                stats.form_last5_home_only = home_form
                stats.form_last5_away_only = away_form
                if stats.schedule is None and schedule is not None:
                    stats.schedule = schedule
                logger.info(
                    "Football-Data form for %s: %s (pos %s, %s pts)",
                    team_name,
                    "".join(r.result.value for r in form) or "empty",
                    stats.league_position,
                    stats.league_points,
                )

    return matches
