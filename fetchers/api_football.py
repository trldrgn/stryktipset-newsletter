"""
Fetches structured football statistics from API-Football (free tier: 100 req/day).

CALL BUDGET per weekly run (13 matches):
  - Fixture search (find IDs for our 13 games):  ~5 calls  (batch by date)
  - H2H (per match):                             13 calls
  - Team fixtures/form (per team, cached):       ~13 calls (cache same team across games)
  - Standings (per league, cached):              ~5 calls  (multiple matches share leagues)
  - Injuries (per fixture):                      13 calls
  - Player stats for absent players:             ~10 calls (only for flagged absences)
  ─────────────────────────────────────────────────────────
  Total (worst case):                            ~59 calls  ← well within 100/day

All responses cached to disk for 7 days — re-runs won't double-spend.

Matchup-aware injury analysis:
  For each absent player we fetch their season stats, then check whether
  the opponent has a direct threat that exploits the positional gap.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import requests

from config import API_FOOTBALL_BASE_URL, API_FOOTBALL_KEY, API_FOOTBALL_DAILY_LIMIT
from models.match import (
    Match,
    TeamStats,
    FormResult,
    H2HResult,
    ScheduleContext,
    PlayerAbsence,
    MatchupRisk,
    InjuryStatus,
    Outcome,
    RiskLevel,
)
from utils.cache import cached, get_cache
from utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Request counter — enforces the 100/day free-tier cap
# ---------------------------------------------------------------------------

_request_count = 0
_last_call_at: float = 0.0
_MIN_CALL_INTERVAL = 6.5   # seconds — keeps us under the 10 req/min free-tier limit


def get_request_count() -> int:
    return _request_count


def _api_get(endpoint: str, params: dict) -> dict:
    """
    Single gateway for all API-Football calls.
    Enforces daily limit and per-minute rate limit, adds auth header, returns parsed JSON.
    """
    global _request_count, _last_call_at
    if _request_count >= API_FOOTBALL_DAILY_LIMIT:
        raise RuntimeError(
            f"API-Football daily limit of {API_FOOTBALL_DAILY_LIMIT} requests reached. "
            "Remaining data will be omitted."
        )

    # Rate limiting: pause so we never exceed 10 req/min
    elapsed = time.time() - _last_call_at
    if elapsed < _MIN_CALL_INTERVAL:
        time.sleep(_MIN_CALL_INTERVAL - elapsed)

    url = f"{API_FOOTBALL_BASE_URL}/{endpoint}"
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    logger.debug("API-Football GET /%s %s [call #%d]", endpoint, params, _request_count + 1)

    resp = requests.get(url, headers=headers, params=params, timeout=20)
    resp.raise_for_status()
    _last_call_at = time.time()
    _request_count += 1

    data = resp.json()
    errors = data.get("errors", {})
    if errors:
        raise ValueError(f"API-Football error on /{endpoint}: {errors}")

    return data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OUTCOME_MAP = {"W": Outcome.HOME, "D": Outcome.DRAW, "L": Outcome.AWAY}


def _result_to_outcome(result_str: str, is_home: bool) -> Outcome:
    """Convert API result string 'W'/'D'/'L' (from the team's perspective) to Outcome."""
    r = result_str.upper()
    if r == "D":
        return Outcome.DRAW
    if (r == "W" and is_home) or (r == "L" and not is_home):
        return Outcome.HOME
    return Outcome.AWAY


def _fixture_to_form(fixture: dict, team_id: int) -> FormResult:
    teams = fixture["teams"]
    goals = fixture["goals"]
    score = fixture.get("score", {})

    is_home = teams["home"]["id"] == team_id
    opponent_name = teams["away"]["name"] if is_home else teams["home"]["name"]

    gf = goals["home"] if is_home else goals["away"]
    ga = goals["away"] if is_home else goals["home"]
    result_str = teams["home"].get("winner") if is_home else teams["away"].get("winner")

    # winner field: True=win, False=loss, None=draw
    if result_str is True:
        outcome = Outcome.HOME if is_home else Outcome.AWAY
    elif result_str is False:
        outcome = Outcome.AWAY if is_home else Outcome.HOME
    else:
        outcome = Outcome.DRAW

    return FormResult(
        opponent=opponent_name,
        home_or_away="H" if is_home else "A",
        goals_for=gf or 0,
        goals_against=ga or 0,
        result=outcome,
    )


def _parse_injury_status(reason: str) -> InjuryStatus:
    r = reason.lower()
    if "doubt" in r or "questionable" in r:
        return InjuryStatus.DOUBT
    if "50" in r:
        return InjuryStatus.FIFTY_FIFTY
    return InjuryStatus.OUT


# ---------------------------------------------------------------------------
# Cached API call wrappers
# ---------------------------------------------------------------------------

@cached(lambda date_str: f"fixtures_by_date_{date_str}")
def _fetch_fixtures_by_date(date_str: str) -> dict:
    """Fetch all fixtures on a given date (YYYY-MM-DD). One call covers many matches."""
    return _api_get("fixtures", {"date": date_str})


@cached(lambda league_id, season: f"league_fixtures_{league_id}_{season}_daterange")
def _fetch_league_fixtures(league_id: int, season: int) -> dict:
    """
    All finished fixtures in a league for the current season using date range.
    Avoids the free-tier season=2025 block. One call covers all teams in the league.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    season_start = f"{season}-07-01"
    return _api_get("fixtures", {
        "league": league_id,
        "from": season_start,
        "to": today,
        "status": "FT",
    })


@cached(lambda h_id, a_id: f"h2h_{min(h_id, a_id)}_{max(h_id, a_id)}_daterange")
def _fetch_h2h(home_team_id: int, away_team_id: int) -> dict:
    """Use date range instead of 'last' to avoid free-tier parameter restriction."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _api_get("fixtures/headtohead", {
        "h2h": f"{home_team_id}-{away_team_id}",
        "from": "2020-01-01",
        "to": today,
        "status": "FT",
    })


@cached(lambda league_id, season: f"standings_{league_id}_{season}")
def _fetch_standings(league_id: int, season: int) -> dict:
    # The free tier blocks season=2025. Try without explicit season first
    # (API defaults to current season), then fall back to explicit if needed.
    try:
        return _api_get("standings", {"league": league_id})
    except Exception:
        return _api_get("standings", {"league": league_id, "season": season})


@cached(lambda fixture_id: f"injuries_{fixture_id}")
def _fetch_injuries(fixture_id: int) -> dict:
    return _api_get("injuries", {"fixture": fixture_id})


@cached(lambda fixture_id: f"fixture_stats_{fixture_id}")
def _fetch_fixture_statistics(fixture_id: int) -> dict:
    """Per-fixture in-game stats: shots, possession, corners, passes. Free tier OK."""
    return _api_get("fixtures/statistics", {"fixture": fixture_id})


@cached(lambda fixture_id: f"fixture_lineups_{fixture_id}")
def _fetch_fixture_lineups(fixture_id: int) -> dict:
    """Starting XI, formation, substitutes. Usually published 1h before kickoff."""
    return _api_get("fixtures/lineups", {"fixture": fixture_id})


@cached(lambda player_id, season: f"player_stats_{player_id}_{season}")
def _fetch_player_stats(player_id: int, season: int) -> dict:
    return _api_get("players", {"id": player_id, "season": season})


@cached(lambda league_id, season: f"topscorers_{league_id}_{season}")
def _fetch_topscorers(league_id: int, season: int) -> dict:
    return _api_get("players/topscorers", {"league": league_id, "season": season})


@cached(lambda league_id, season: f"topassists_{league_id}_{season}")
def _fetch_topassists(league_id: int, season: int) -> dict:
    return _api_get("players/topassists", {"league": league_id, "season": season})


# ---------------------------------------------------------------------------
# Fixture ID resolution
# ---------------------------------------------------------------------------

def find_fixture_id(match: Match, season: int) -> Optional[int]:
    """
    Find the API-Football fixture ID for a Match.
    Searches by date and team name (fuzzy fallback).
    """
    if match.kickoff is None:
        logger.warning("No kickoff time for %s vs %s — can't resolve fixture ID", match.home_team, match.away_team)
        return None

    date_str = match.kickoff.strftime("%Y-%m-%d")
    data = _fetch_fixtures_by_date(date_str)
    fixtures = data.get("response", [])

    home_lower = match.home_team.lower()
    away_lower = match.away_team.lower()

    # Exact name match first
    for f in fixtures:
        teams = f.get("teams", {})
        h = teams.get("home", {}).get("name", "").lower()
        a = teams.get("away", {}).get("name", "").lower()
        if home_lower in h or h in home_lower:
            if away_lower in a or a in away_lower:
                fixture_id = f["fixture"]["id"]
                logger.info("Resolved fixture ID %d for %s vs %s", fixture_id, match.home_team, match.away_team)
                return fixture_id

    logger.warning("Could not resolve fixture ID for %s vs %s on %s", match.home_team, match.away_team, date_str)
    return None


# ---------------------------------------------------------------------------
# Top scorer / assister sets (cached per league)
# ---------------------------------------------------------------------------

_topscorer_ids: dict[int, set[int]] = {}   # league_id → set of top 10 player IDs
_topassist_ids: dict[int, set[int]] = {}


def _load_league_leaders(league_id: int, season: int) -> None:
    if league_id in _topscorer_ids:
        return
    try:
        sc = _fetch_topscorers(league_id, season)
        _topscorer_ids[league_id] = {
            p["player"]["id"] for p in sc.get("response", [])[:10]
        }
        ast = _fetch_topassists(league_id, season)
        _topassist_ids[league_id] = {
            p["player"]["id"] for p in ast.get("response", [])[:10]
        }
    except Exception as e:
        logger.warning("Could not load league leaders for league %d: %s", league_id, e)
        _topscorer_ids.setdefault(league_id, set())
        _topassist_ids.setdefault(league_id, set())


# ---------------------------------------------------------------------------
# Injury analysis — the smart part
# ---------------------------------------------------------------------------

def _assess_matchup_risk(
    absent_position: str,
    opponent_stats: list[dict],   # raw API player objects for opponent's squad
) -> Optional[MatchupRisk]:
    """
    Check if the opponent has a dangerous player that directly exploits
    the positional gap created by the absent player.

    Position mapping (simplified):
      LB/LWB absent → check opponent RW/RM
      RB/RWB absent → check opponent LW/LM
      CB absent     → check opponent ST/CF
      DM/CDM absent → check opponent CAM/AM
      LW/LM absent  → no direct opponent exploit (more offensive gap)
    """
    pos = absent_position.upper()

    # Which opponent positions exploit this gap?
    exploit_map = {
        "LB": ["RW", "RM", "RWB"],
        "LWB": ["RW", "RM"],
        "RB": ["LW", "LM", "LWB"],
        "RWB": ["LW", "LM"],
        "CB": ["ST", "CF", "SS"],
        "CDM": ["CAM", "AM", "SS"],
        "DM": ["CAM", "AM"],
    }

    exploit_positions = exploit_map.get(pos, [])
    if not exploit_positions:
        return None

    best_threat: Optional[dict] = None
    best_score = 0

    for p in opponent_stats:
        player = p.get("player", {})
        stats_list = p.get("statistics", [{}])
        stats = stats_list[0] if stats_list else {}

        p_pos = stats.get("games", {}).get("position", "").upper()
        if p_pos not in exploit_positions:
            continue

        goals = stats.get("goals", {}).get("total") or 0
        assists = stats.get("goals", {}).get("assists") or 0
        dribbles = stats.get("dribbles", {}).get("success") or 0
        score = goals * 3 + assists * 2 + dribbles

        if score > best_score:
            best_score = score
            best_threat = {"player": player, "stats": stats, "goals": goals, "assists": assists}

    if best_threat and best_score >= 6:
        goals = best_threat["goals"]
        assists = best_threat["assists"]
        risk = RiskLevel.CRITICAL if best_score >= 20 else (RiskLevel.HIGH if best_score >= 12 else RiskLevel.MEDIUM)

        return MatchupRisk(
            opponent_player=best_threat["player"].get("name", "Unknown"),
            opponent_position=best_threat["stats"].get("games", {}).get("position", "?"),
            opponent_goals=goals,
            opponent_assists=assists,
            risk_level=risk,
            note=(
                f"Opponent's {best_threat['stats'].get('games', {}).get('position', '?')} "
                f"{best_threat['player'].get('name', 'Unknown')} ({goals}G {assists}A) "
                f"attacks the exposed {absent_position} channel."
            ),
        )

    return None


def _build_absences(
    injury_data: dict,
    league_id: int,
    season: int,
    opponent_squad: Optional[list[dict]] = None,
) -> list[PlayerAbsence]:
    """
    Build PlayerAbsence objects from raw injury API response,
    with matchup risk analysis where possible.
    """
    _load_league_leaders(league_id, season)
    absences: list[PlayerAbsence] = []

    for entry in injury_data.get("response", []):
        player_info = entry.get("player", {})
        player_id = player_info.get("id")
        player_name = player_info.get("name", "Unknown")
        reason = entry.get("type", "")
        position = player_info.get("type", "")  # API uses "type" for position in injuries

        status = _parse_injury_status(reason)

        # Fetch player stats to evaluate their value
        goals, assists = 0, 0
        is_top_scorer = player_id in _topscorer_ids.get(league_id, set())
        is_top_assister = player_id in _topassist_ids.get(league_id, set())

        if player_id:
            try:
                ps = _fetch_player_stats(player_id, season)
                player_stats_list = ps.get("response", [{}])
                if player_stats_list:
                    s = player_stats_list[0].get("statistics", [{}])
                    if s:
                        goals = s[0].get("goals", {}).get("total") or 0
                        assists = s[0].get("goals", {}).get("assists") or 0
            except Exception as e:
                logger.debug("Could not fetch player stats for %s: %s", player_name, e)

        matchup_risk = None
        if opponent_squad:
            matchup_risk = _assess_matchup_risk(position, opponent_squad)

        absence = PlayerAbsence(
            player_name=player_name,
            position=position,
            status=status,
            season_goals=goals,
            season_assists=assists,
            is_top_scorer=is_top_scorer,
            is_top_assister=is_top_assister,
            matchup_risk=matchup_risk,
        )
        absences.append(absence)

    return absences


# ---------------------------------------------------------------------------
# Team form + stats
# ---------------------------------------------------------------------------

def _build_team_stats(
    team_id: int,
    team_name: str,
    season: int,
    league_id: int,
    standings_data: dict,
    league_fixtures: list | None = None,
) -> TeamStats:
    stats = TeamStats(team_name=team_name, team_id=team_id)

    # --- Standing (from pre-fetched standings) ---
    for league_entry in standings_data.get("response", []):
        for group in league_entry.get("league", {}).get("standings", []):
            for row in group:
                if row.get("team", {}).get("id") == team_id:
                    stats.league_position = row.get("rank")
                    stats.league_points = row.get("points")
                    break

    # --- Form from league fixtures (shared across all teams, no season param needed) ---
    # Filter league-wide fixtures to only this team's games
    all_fixtures = league_fixtures or []
    team_fixtures = [
        f for f in all_fixtures
        if f.get("teams", {}).get("home", {}).get("id") == team_id
        or f.get("teams", {}).get("away", {}).get("id") == team_id
    ]
    team_fixtures.sort(key=lambda f: f.get("fixture", {}).get("date", ""), reverse=True)

    for f in team_fixtures[:5]:
        stats.form_last5.append(_fixture_to_form(f, team_id))

    # Home-only and away-only form
    home_fixtures = [f for f in team_fixtures if f["teams"]["home"]["id"] == team_id]
    away_fixtures = [f for f in team_fixtures if f["teams"]["away"]["id"] == team_id]

    for f in home_fixtures[:5]:
        stats.form_last5_home_only.append(_fixture_to_form(f, team_id))
    for f in away_fixtures[:5]:
        stats.form_last5_away_only.append(_fixture_to_form(f, team_id))

    # --- Schedule / fatigue ---
    if fixtures:
        last = fixtures[0]
        last_date_str = last.get("fixture", {}).get("date", "")
        try:
            last_dt = datetime.fromisoformat(last_date_str.replace("Z", "+00:00"))
            days_since = (datetime.now(timezone.utc) - last_dt).days
        except (ValueError, TypeError):
            days_since = None

        last_comp = last.get("league", {}).get("name", "")
        matches_14d = sum(
            1 for f in fixtures
            if _days_ago(f.get("fixture", {}).get("date", "")) <= 14
        )

        stats.schedule = ScheduleContext(
            days_since_last_match=days_since,
            last_match_competition=last_comp,
            matches_last_14_days=matches_14d,
        )

    return stats


def _days_ago(date_str: str) -> int:
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).days
    except (ValueError, TypeError):
        return 999


# ---------------------------------------------------------------------------
# H2H
# ---------------------------------------------------------------------------

def _build_h2h(home_team_id: int, away_team_id: int, home_name: str, away_name: str) -> list[H2HResult]:
    data = _fetch_h2h(home_team_id, away_team_id)
    results = []
    for f in data.get("response", []):
        teams = f.get("teams", {})
        goals = f.get("goals", {})
        results.append(H2HResult(
            date=f.get("fixture", {}).get("date", "")[:10],
            home_team=teams.get("home", {}).get("name", ""),
            away_team=teams.get("away", {}).get("name", ""),
            home_goals=goals.get("home") or 0,
            away_goals=goals.get("away") or 0,
            venue=f.get("fixture", {}).get("venue", {}).get("name", ""),
        ))
    return results


# ---------------------------------------------------------------------------
# Fixture statistics helpers (shots, possession, corners)
# ---------------------------------------------------------------------------

def _parse_stat_value(stats_list: list, stat_name: str) -> Optional[float]:
    """Extract a named stat value from the API statistics array."""
    for s in stats_list:
        if s.get("type", "").lower() == stat_name.lower():
            val = s.get("value")
            if val is None or val == "":
                return None
            try:
                return float(str(val).replace("%", ""))
            except (ValueError, TypeError):
                return None
    return None


def _enrich_team_stats_with_fixtures(stats: TeamStats, team_id: int, season: int) -> None:
    """
    After building form from fixtures, also compute shot/possession/corner averages
    from the stats of those same last 5 fixtures.
    """
    form_data = _fetch_team_fixtures(team_id, season)
    fixtures = sorted(
        form_data.get("response", []),
        key=lambda f: f.get("fixture", {}).get("date", ""),
        reverse=True,
    )[:5]

    shots_on, shots_total, possession, corners = [], [], [], []
    for f in fixtures:
        fid = f.get("fixture", {}).get("id")
        if not fid:
            continue
        try:
            stat_data = _fetch_fixture_statistics(fid)
            for team_stat in stat_data.get("response", []):
                if team_stat.get("team", {}).get("id") != team_id:
                    continue
                s = team_stat.get("statistics", [])
                if (v := _parse_stat_value(s, "Shots on Goal")) is not None:
                    shots_on.append(v)
                if (v := _parse_stat_value(s, "Total Shots")) is not None:
                    shots_total.append(v)
                if (v := _parse_stat_value(s, "Ball Possession")) is not None:
                    possession.append(v)
                if (v := _parse_stat_value(s, "Corner Kicks")) is not None:
                    corners.append(v)
        except Exception as e:
            logger.debug("Could not fetch fixture stats for fixture %d: %s", fid, e)

    if shots_on:
        stats.shots_on_target_avg = round(sum(shots_on) / len(shots_on), 1)
    if shots_total:
        stats.shots_total_avg = round(sum(shots_total) / len(shots_total), 1)
    if possession:
        stats.possession_avg = round(sum(possession) / len(possession), 1)
    if corners:
        stats.corners_avg = round(sum(corners) / len(corners), 1)


def _enrich_with_lineups(match: Match, fixture_id: int) -> None:
    """
    Fetch confirmed lineups for a fixture and populate formation + starting_xi.
    Usually available from ~1h before kickoff — will be empty before that.
    """
    try:
        data = _fetch_fixture_lineups(fixture_id)
        for team_entry in data.get("response", []):
            tid = team_entry.get("team", {}).get("id")
            formation = team_entry.get("formation", "")
            starters = [
                p.get("player", {}).get("name", "")
                for p in team_entry.get("startXI", [])
                if p.get("player", {}).get("name")
            ]
            if match.home_stats and match.home_stats.team_id == tid:
                match.home_stats.formation = formation
                match.home_stats.starting_xi = starters
            elif match.away_stats and match.away_stats.team_id == tid:
                match.away_stats.formation = formation
                match.away_stats.starting_xi = starters
    except Exception as e:
        logger.debug("Could not fetch lineups for fixture %d: %s", fixture_id, e)


# ---------------------------------------------------------------------------
# League ID resolution
# ---------------------------------------------------------------------------

@cached(lambda league_name, country: f"league_search_{league_name.lower().replace(' ', '_')}_{country.lower()}")
def _fetch_league_search(league_name: str, country: str) -> dict:
    return _api_get("leagues", {"name": league_name, "country": country, "current": "true"})


def _resolve_league_id(league_name: str, country: str) -> Optional[int]:
    try:
        data = _fetch_league_search(league_name, country)
        results = data.get("response", [])
        if results:
            return results[0]["league"]["id"]
    except Exception as e:
        logger.warning("Could not resolve league '%s' (%s): %s", league_name, country, e)
    return None


# ---------------------------------------------------------------------------
# Main enrichment function
# ---------------------------------------------------------------------------

def enrich_match(match: Match, season: int) -> Match:
    """
    Populate match.home_stats, match.away_stats, and match.h2h
    with data from API-Football.

    Call this for each of the 13 matches after fetching the coupon.
    """
    logger.info(
        "Enriching [%d] %s vs %s (%s)",
        match.game_number, match.home_team, match.away_team, match.league,
    )

    # --- Resolve IDs ---
    fixture_id = find_fixture_id(match, season)
    if fixture_id:
        match.api_football_fixture_id = fixture_id

    league_id = _resolve_league_id(match.league, match.country)

    # --- Standings (shared across matches in same league — cache saves calls) ---
    standings_data: dict = {"response": []}
    if league_id:
        try:
            standings_data = _fetch_standings(league_id, season)
        except Exception as e:
            logger.warning("Could not fetch standings for %s: %s", match.league, e)

    # --- League fixtures (one call per league covers all team form — no season param) ---
    league_fixtures: list = []
    if league_id:
        try:
            lf_data = _fetch_league_fixtures(league_id, season)
            league_fixtures = lf_data.get("response", [])
            logger.info(
                "Loaded %d league fixtures for %s (form source)",
                len(league_fixtures), match.league,
            )
        except Exception as e:
            logger.warning("Could not fetch league fixtures for %s: %s", match.league, e)

    # --- Team stats ---
    home_team_id: Optional[int] = None
    away_team_id: Optional[int] = None

    # Try to resolve team IDs from fixture
    if fixture_id:
        cache = get_cache()
        date_key = f"fixtures_by_date_{match.kickoff.strftime('%Y-%m-%d')}" if match.kickoff else None
        if date_key:
            cached_fixtures = cache.get(date_key)
            if cached_fixtures:
                for f in cached_fixtures.get("response", []):
                    if f["fixture"]["id"] == fixture_id:
                        home_team_id = f["teams"]["home"]["id"]
                        away_team_id = f["teams"]["away"]["id"]
                        break

    if home_team_id and away_team_id:
        try:
            match.home_stats = _build_team_stats(
                home_team_id, match.home_team, season, league_id or 0, standings_data, league_fixtures
            )
        except Exception as e:
            logger.error("Failed to build home stats for %s: %s", match.home_team, e)
            match.home_stats = TeamStats(team_name=match.home_team)

        try:
            match.away_stats = _build_team_stats(
                away_team_id, match.away_team, season, league_id or 0, standings_data, league_fixtures
            )
        except Exception as e:
            logger.error("Failed to build away stats for %s: %s", match.away_team, e)
            match.away_stats = TeamStats(team_name=match.away_team)

        # --- H2H ---
        try:
            match.h2h = _build_h2h(home_team_id, away_team_id, match.home_team, match.away_team)
        except Exception as e:
            logger.warning("H2H fetch failed for %s vs %s: %s", match.home_team, match.away_team, e)

        # --- Injuries (with matchup-aware analysis) ---
        if fixture_id:
            try:
                injury_data = _fetch_injuries(fixture_id)
                all_absences = _build_absences(injury_data, league_id or 0, season)

                home_absences = [
                    a for a in all_absences
                    if any(
                        entry.get("team", {}).get("id") == home_team_id
                        for entry in injury_data.get("response", [])
                        if entry.get("player", {}).get("name") == a.player_name
                    )
                ]
                away_absences = [
                    a for a in all_absences
                    if a not in home_absences
                ]

                if match.home_stats:
                    match.home_stats.injuries = home_absences
                if match.away_stats:
                    match.away_stats.injuries = away_absences
            except Exception as e:
                logger.warning("Injury fetch failed for fixture %d: %s", fixture_id, e)

            # --- Lineups (available ~1h before kickoff) ---
            _enrich_with_lineups(match, fixture_id)

        # NOTE: Shot/possession/corner averages (_enrich_team_stats_with_fixtures) would add
        # 5 calls per team (130 total) — exceeds the free-tier 100/day limit.
        # Enable once on a paid plan. The fields (shots_on_target_avg etc.) are defined on
        # TeamStats and ready to use; just call _enrich_team_stats_with_fixtures here.
    else:
        logger.warning(
            "Could not resolve team IDs for %s vs %s — limited stats available",
            match.home_team, match.away_team,
        )
        match.home_stats = TeamStats(team_name=match.home_team)
        match.away_stats = TeamStats(team_name=match.away_team)

    return match


def enrich_all_matches(matches: list[Match], season: int) -> list[Match]:
    """
    Enrich all 13 matches. Standings are cached per league so multiple
    matches in the same league only cost 1 extra API call.
    """
    logger.info("Enriching %d matches (API-Football budget: %d/day)", len(matches), API_FOOTBALL_DAILY_LIMIT)
    for match in matches:
        try:
            enrich_match(match, season)
        except RuntimeError as e:
            # Daily limit reached — log and continue with partial data
            logger.error("API limit hit during enrichment: %s", e)
            break
        except Exception as e:
            logger.error("Unexpected error enriching match %d: %s", match.game_number, e)

    logger.info(
        "Enrichment complete. API-Football calls used: %d/%d",
        get_request_count(), API_FOOTBALL_DAILY_LIMIT,
    )
    return matches
