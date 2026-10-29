"""
Premier League structured absences from the official Fantasy Premier League API.

Replaces API-Football injuries for PL matches. One public endpoint, no key:
  https://fantasy.premierleague.com/api/bootstrap-static/
returns every PL player with availability status, news text, chance of
playing, season goals/assists and set-piece order. Updated by FPL daily.

Status codes: a=available, d=doubtful, i=injured, s=suspended,
u=unavailable (left club / long-term loan), n=not eligible (loaned to parent).

NOT cached — availability must be fresh each run (like Perplexity).
"""

from __future__ import annotations

from typing import Optional

import requests

from models.match import InjuryStatus, Match, PlayerAbsence
from utils.logger import get_logger

logger = get_logger(__name__)

_BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
_TIMEOUT = 20
_MAX_PER_TEAM = 6           # keep Claude's prompt compact

_ABSENT_STATUSES = {"i", "s", "d"}
_POSITIONS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

# Svenska Spel name (lower-case) -> FPL team "name"
_TEAM_ALIASES: dict[str, str] = {
    "manchester city": "Man City",
    "manchester united": "Man Utd",
    "tottenham": "Spurs",
    "nottingham forest": "Nott'm Forest",
    "nottingham": "Nott'm Forest",
    "wolverhampton": "Wolves",
    "newcastle united": "Newcastle",
    "leeds united": "Leeds",
}


def _fetch_bootstrap() -> Optional[dict]:
    try:
        resp = requests.get(
            _BOOTSTRAP_URL, timeout=_TIMEOUT,
            headers={"User-Agent": "stryktipset-newsletter/1.0"},
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("FPL bootstrap fetch failed: %s", e)
        return None


def _resolve_team(team_name: str, fpl_teams: list[dict]) -> Optional[int]:
    """Map a Svenska Spel team name to an FPL team id."""
    name_lower = team_name.lower().strip()
    alias = _TEAM_ALIASES.get(name_lower)
    for t in fpl_teams:
        if alias and t["name"] == alias:
            return t["id"]
    for t in fpl_teams:
        if t["name"].lower() == name_lower:
            return t["id"]
    # Substring either way ("Hull" -> "Hull City", "Ipswich" -> "Ipswich Town")
    for t in fpl_teams:
        fpl_lower = t["name"].lower()
        if name_lower in fpl_lower or fpl_lower in name_lower:
            return t["id"]
    return None


def _fpl_status(player: dict) -> Optional[InjuryStatus]:
    """
    FPL chance_of_playing is 0/25/50/75/100. None = not worth listing
    (doubtful but ≥75% likely to play).
    """
    if player.get("status") in ("i", "s"):
        return InjuryStatus.OUT
    chance = player.get("chance_of_playing_next_round")
    if chance is None or chance <= 25:
        return InjuryStatus.DOUBT
    if chance <= 50:
        return InjuryStatus.FIFTY_FIFTY
    return None


def _is_set_piece_taker(player: dict) -> bool:
    return any(
        player.get(k) == 1
        for k in ("penalties_order", "corners_and_indirect_freekicks_order", "direct_freekicks_order")
    )


def _build_team_absences(team_id: int, players: list[dict]) -> list[PlayerAbsence]:
    """Absent/doubtful players for one team, most important (by FPL price) first."""
    squad = [p for p in players if p["team"] == team_id]
    top_goals = max((p.get("goals_scored", 0) for p in squad), default=0)
    top_assists = max((p.get("assists", 0) for p in squad), default=0)

    absent = [
        (p, status) for p in squad
        if p.get("status") in _ABSENT_STATUSES and (status := _fpl_status(p)) is not None
    ]
    absent.sort(key=lambda ps: ps[0].get("now_cost", 0), reverse=True)

    absences: list[PlayerAbsence] = []
    for p, status in absent[:_MAX_PER_TEAM]:
        goals = p.get("goals_scored", 0)
        assists = p.get("assists", 0)
        absences.append(PlayerAbsence(
            player_name=p.get("web_name", "Unknown"),
            position=_POSITIONS.get(p.get("element_type"), ""),
            status=status,
            season_goals=goals,
            season_assists=assists,
            is_top_scorer=top_goals > 0 and goals == top_goals,
            is_top_assister=top_assists > 0 and assists == top_assists,
            is_set_piece_taker=_is_set_piece_taker(p),
            source="fpl",
        ))
    return absences


def enrich_with_fpl_absences(matches: list[Match]) -> list[Match]:
    """
    Fill TeamStats.injuries from FPL for Premier League teams that no structured
    source (FotMob) has checked yet. One HTTP call per run; any failure returns
    the matches unchanged.
    """
    pl_matches = [m for m in matches if m.league == "Premier League" and m.country == "England"]
    if not pl_matches:
        return matches

    data = _fetch_bootstrap()
    if not data:
        return matches

    try:
        teams = data.get("teams", [])
        players = data.get("elements", [])
        filled = 0
        for match in pl_matches:
            for team_name, stats in ((match.home_team, match.home_stats), (match.away_team, match.away_stats)):
                # Another structured source (FotMob) already answered — even "none missing"
                if stats is None or stats.injuries or stats.absences_checked_source:
                    continue
                team_id = _resolve_team(team_name, teams)
                if team_id is None:
                    logger.warning("FPL: could not resolve team '%s'", team_name)
                    continue
                stats.injuries = _build_team_absences(team_id, players)
                stats.absences_checked_source = "fpl"
                filled += 1
        logger.info("FPL absences complete: %d PL teams filled", filled)
    except Exception as e:
        logger.warning("FPL absences enrichment failed: %s", e)

    return matches
