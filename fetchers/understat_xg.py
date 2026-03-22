"""
Fetches per-match xG data from Understat (free, no API key needed).

Covers Big 5 leagues: EPL, La Liga, Bundesliga, Serie A, Ligue 1.
Does NOT cover: Championship, League One, Eredivisie, Primeira Liga.

For each team, fetches last 5 completed matches and computes:
  - xG for average (expected goals scored)
  - xG against average (expected goals conceded)
  - xG overperformance (actual goals - xG total → luck indicator)

Data freshness: updated within hours of match completion.
Rate: ~1 HTTP request per team (web scraping, not official API).
"""

from __future__ import annotations

import time
from typing import Optional

from understatapi import UnderstatClient

from models.match import Match, TeamStats
from utils.cache import cached
from utils.logger import get_logger

logger = get_logger(__name__)

_client = UnderstatClient()

# Stryktipset league name → Understat league code
_LEAGUE_MAP: dict[tuple[str, str], str] = {
    ("Premier League", "England"): "EPL",
    ("Bundesliga", "Germany"): "Bundesliga",
    ("La Liga", "Spain"): "La_Liga",
    ("Primera Division", "Spain"): "La_Liga",
    ("Serie A", "Italy"): "Serie_A",
    ("Ligue 1", "France"): "Ligue_1",
}

# Polite rate limiting — 1.5s between requests (web scraping, not an API)
_last_call_at: float = 0.0
_MIN_CALL_INTERVAL = 1.5


def _rate_limit() -> None:
    global _last_call_at
    elapsed = time.time() - _last_call_at
    if elapsed < _MIN_CALL_INTERVAL:
        time.sleep(_MIN_CALL_INTERVAL - elapsed)
    _last_call_at = time.time()


# ---------------------------------------------------------------------------
# Team resolution
# ---------------------------------------------------------------------------

@cached(lambda league_code, season: f"understat_teams_{league_code}_{season}")
def _fetch_league_teams(league_code: str, season: str) -> dict:
    """Fetch all teams for a league/season. Returns {team_id: {title, ...}}."""
    _rate_limit()
    return _client.league(league=league_code).get_team_data(season=season)


def _resolve_team(team_name: str, league_teams: dict) -> Optional[str]:
    """
    Find a team in Understat's data by fuzzy name match.
    Returns the Understat team title (used for match data lookup) or None.
    """
    name_lower = team_name.lower().strip()
    best_title = None
    best_score = 0

    for _, team_info in league_teams.items():
        title = team_info.get("title", "")
        t_lower = title.lower()

        score = 0
        if name_lower == t_lower:
            score = 100
        elif name_lower in t_lower or t_lower in name_lower:
            score = 70
        else:
            # Word overlap
            name_words = set(w for w in name_lower.split() if len(w) > 3)
            title_words = set(w for w in t_lower.split() if len(w) > 3)
            if name_words and name_words & title_words:
                score = 50

        if score > best_score:
            best_score = score
            best_title = title

    if best_title and best_score >= 50:
        logger.debug("Matched '%s' → '%s' (score %d)", team_name, best_title, best_score)
        return best_title

    logger.debug("Could not match '%s' in Understat", team_name)
    return None


# ---------------------------------------------------------------------------
# xG computation
# ---------------------------------------------------------------------------

@cached(lambda team_title, season: f"understat_matches_{team_title.lower().replace(' ', '_')}_{season}")
def _fetch_team_matches(team_title: str, season: str) -> list[dict]:
    """Fetch all matches for a team in a season."""
    _rate_limit()
    return _client.team(team=team_title).get_match_data(season=season)


def _compute_xg(team_title: str, season: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Compute average xG for/against from last 5 completed matches.
    Returns (xg_for_avg, xg_against_avg, xg_overperformance) or (None, None, None).

    xg_overperformance = (actual goals - xG) averaged over last 5.
    Positive = scoring more than expected (lucky or clinical).
    Negative = scoring less than expected (unlucky or wasteful).
    """
    try:
        all_matches = _fetch_team_matches(team_title, season)
    except Exception as e:
        logger.warning("Understat match fetch failed for '%s': %s", team_title, e)
        return None, None, None

    # Filter completed matches, sort by date descending
    completed = [
        m for m in all_matches
        if m.get("isResult")
    ]
    completed.sort(key=lambda m: m.get("datetime", ""), reverse=True)

    if not completed:
        return None, None, None

    last_5 = completed[:5]

    xg_for_vals = []
    xg_against_vals = []
    overperf_vals = []

    for m in last_5:
        side = m.get("side", "")  # "h" or "a"
        xg = m.get("xG", {})
        goals = m.get("goals", {})

        if side == "h":
            xg_f = xg.get("h")
            xg_a = xg.get("a")
            g_f = goals.get("h")
        elif side == "a":
            xg_f = xg.get("a")
            xg_a = xg.get("h")
            g_f = goals.get("a")
        else:
            continue

        if xg_f is not None and xg_a is not None:
            try:
                xg_for_vals.append(float(xg_f))
                xg_against_vals.append(float(xg_a))
                if g_f is not None:
                    overperf_vals.append(float(g_f) - float(xg_f))
            except (ValueError, TypeError):
                continue

    if not xg_for_vals:
        return None, None, None

    xg_for_avg = round(sum(xg_for_vals) / len(xg_for_vals), 2)
    xg_against_avg = round(sum(xg_against_vals) / len(xg_against_vals), 2)
    xg_overperf = round(sum(overperf_vals) / len(overperf_vals), 2) if overperf_vals else None

    return xg_for_avg, xg_against_avg, xg_overperf


# ---------------------------------------------------------------------------
# Main enrichment entry point
# ---------------------------------------------------------------------------

def enrich_with_understat_xg(matches: list[Match]) -> list[Match]:
    """
    Fill xG averages for teams in Big 5 leagues using Understat.
    Only fills fields that are currently empty (xg_for_avg, xg_against_avg).
    """
    from datetime import datetime

    # Determine season (Understat uses the start year: 2025 for 2025/26)
    current_year = datetime.now().year
    # If we're in Jan-Jul, season started last year; Aug-Dec, season started this year
    current_month = datetime.now().month
    season = str(current_year - 1 if current_month < 8 else current_year)

    # Cache league team lists
    league_teams_cache: dict[str, dict] = {}
    enriched_count = 0

    for match in matches:
        league_code = _LEAGUE_MAP.get((match.league, match.country))
        if not league_code:
            continue

        if league_code not in league_teams_cache:
            try:
                league_teams_cache[league_code] = _fetch_league_teams(league_code, season)
                logger.info("Understat teams fetched for %s (%d teams)",
                            league_code, len(league_teams_cache[league_code]))
            except Exception as e:
                logger.warning("Understat team fetch failed for %s: %s", league_code, e)
                league_teams_cache[league_code] = {}

        league_teams = league_teams_cache[league_code]
        if not league_teams:
            continue

        for team_name, stats_attr in [(match.home_team, "home_stats"), (match.away_team, "away_stats")]:
            stats: Optional[TeamStats] = getattr(match, stats_attr)
            if stats is None:
                stats = TeamStats(team_name=team_name)
                setattr(match, stats_attr, stats)

            # Skip if we already have xG data
            if stats.xg_for_avg is not None:
                continue

            team_title = _resolve_team(team_name, league_teams)
            if not team_title:
                continue

            xg_for, xg_against, xg_overperf = _compute_xg(team_title, season)

            if xg_for is not None:
                stats.xg_for_avg = xg_for
                stats.xg_against_avg = xg_against
                enriched_count += 1
                logger.info(
                    "Understat xG for %s: xGF=%.2f, xGA=%.2f, overperf=%s",
                    team_name, xg_for, xg_against,
                    f"{xg_overperf:+.2f}" if xg_overperf is not None else "N/A",
                )

    logger.info("Understat xG enrichment complete: %d teams enriched", enriched_count)

    # --- Fill remaining gaps from our collected xG history (Championship, League One, etc.) ---
    _fill_from_xg_history(matches)

    return matches


def _fill_from_xg_history(matches: list[Match]) -> None:
    """
    For teams that Understat doesn't cover (Championship, League One, etc.),
    fill xG from our locally collected API-Football xG history.
    Also attaches the full xG profile for richer prompt formatting.
    """
    try:
        from fetchers.xg_collector import get_team_xg_profile
    except ImportError:
        return

    filled = 0
    for match in matches:
        for team_name, stats_attr in [(match.home_team, "home_stats"), (match.away_team, "away_stats")]:
            stats: Optional[TeamStats] = getattr(match, stats_attr)
            if stats is None:
                continue

            # Skip if already have xG from Understat
            if stats.xg_for_avg is not None:
                continue

            profile = get_team_xg_profile(team_name)
            if profile and profile.xg_for_5g is not None:
                stats.xg_for_avg = profile.xg_for_5g
                stats.xg_against_avg = profile.xg_against_5g
                # Store the full profile for prompt formatting
                stats._xg_profile = profile
                filled += 1
                logger.info(
                    "xG from history for %s: %s",
                    team_name, profile.format_for_prompt(),
                )

    if filled:
        logger.info("Filled %d teams from xG history (Championship/League One/etc.)", filled)
