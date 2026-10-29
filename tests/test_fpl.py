"""Tests for the FPL (Fantasy Premier League) structured-absence source."""

from datetime import datetime, timezone
from unittest.mock import patch

import fetchers.fpl as fpl
from models.match import InjuryStatus, Match, PlayerAbsence, TeamStats


def _make_match(home: str = "Manchester City", away: str = "Hull",
                league: str = "Premier League", home_inj: list | None = None) -> Match:
    m = Match(
        game_number=1, draw_number=4975, home_team=home, away_team=away,
        league=league, country="England",
        kickoff=datetime(2026, 10, 17, 14, 0, tzinfo=timezone.utc),
    )
    m.home_stats = TeamStats(team_name=home, injuries=home_inj or [])
    m.away_stats = TeamStats(team_name=away)
    return m


def _player(name: str, team: int, status: str = "a", chance: int | None = None,
            cost: int = 50, goals: int = 0, assists: int = 0, etype: int = 3, **extra) -> dict:
    return {
        "web_name": name, "team": team, "status": status,
        "chance_of_playing_next_round": chance, "now_cost": cost,
        "goals_scored": goals, "assists": assists, "element_type": etype, **extra,
    }


_TEAMS = [
    {"id": 1, "name": "Man City"},
    {"id": 2, "name": "Hull City"},
    {"id": 3, "name": "Spurs"},
]


def _bootstrap(players: list[dict]) -> dict:
    return {"teams": _TEAMS, "elements": players}


# ---------------------------------------------------------------------------
# Team resolution
# ---------------------------------------------------------------------------

def test_resolve_aliases_and_substring():
    assert fpl._resolve_team("Manchester City", _TEAMS) == 1
    assert fpl._resolve_team("Hull", _TEAMS) == 2
    assert fpl._resolve_team("Tottenham", _TEAMS) == 3
    assert fpl._resolve_team("Arsenal", _TEAMS) is None


# ---------------------------------------------------------------------------
# Status mapping and player selection
# ---------------------------------------------------------------------------

def test_status_mapping():
    assert fpl._fpl_status(_player("A", 1, "i", 0)) == InjuryStatus.OUT
    assert fpl._fpl_status(_player("A", 1, "s", 0)) == InjuryStatus.OUT
    assert fpl._fpl_status(_player("A", 1, "d", 25)) == InjuryStatus.DOUBT
    assert fpl._fpl_status(_player("A", 1, "d", 50)) == InjuryStatus.FIFTY_FIFTY
    assert fpl._fpl_status(_player("A", 1, "d", 75)) is None


def test_build_absences_filters_and_flags():
    players = [
        _player("Striker", 1, "i", 0, cost=100, goals=6, etype=4, penalties_order=1),
        _player("Fit", 1, "a", None, goals=3),
        _player("Sold", 1, "u", 0, cost=90),            # transferred — excluded
        _player("Minor", 1, "d", 75, cost=80),           # likely plays — excluded
        _player("Doubt", 1, "d", 50, cost=40, assists=4),
        _player("Other", 2, "i", 0),                     # other team
    ]
    absences = fpl._build_team_absences(1, players)
    names = [a.player_name for a in absences]
    assert names == ["Striker", "Doubt"]                 # sorted by now_cost
    striker = absences[0]
    assert striker.status == InjuryStatus.OUT
    assert striker.position == "FWD"
    assert striker.is_top_scorer and striker.is_set_piece_taker
    assert striker.season_goals == 6
    assert striker.source == "fpl"
    assert absences[1].is_top_assister
    assert absences[1].status == InjuryStatus.FIFTY_FIFTY


def test_build_absences_keeps_every_player_up_to_safety_ceiling():
    players = [_player(f"P{i}", 1, "i", 0, cost=i) for i in range(10)]
    absences = fpl._build_team_absences(1, players)
    assert len(absences) == 10                       # nobody hidden
    assert absences[0].player_name == "P9"           # most important first
    many = [_player(f"P{i}", 1, "i", 0, cost=i) for i in range(30)]
    assert len(fpl._build_team_absences(1, many)) == fpl._MAX_PER_TEAM


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

def test_enrich_fills_pl_teams():
    data = _bootstrap([_player("Rodri", 1, "i", 0), _player("Hullman", 2, "s", 0)])
    m = _make_match()
    with patch.object(fpl, "_fetch_bootstrap", return_value=data):
        fpl.enrich_with_fpl_absences([m])
    assert [a.player_name for a in m.home_stats.injuries] == ["Rodri"]
    assert [a.player_name for a in m.away_stats.injuries] == ["Hullman"]


def test_enrich_keeps_existing_injuries():
    existing = [PlayerAbsence(player_name="Haaland", position="ST", source="api-football")]
    m = _make_match(home_inj=existing)
    data = _bootstrap([_player("Rodri", 1, "i", 0)])
    with patch.object(fpl, "_fetch_bootstrap", return_value=data):
        fpl.enrich_with_fpl_absences([m])
    assert m.home_stats.injuries == existing


def test_enrich_skips_non_pl_without_fetching():
    m = _make_match(home="Birmingham", away="Queens Park Rangers", league="Championship")
    with patch.object(fpl, "_fetch_bootstrap") as fetch:
        fpl.enrich_with_fpl_absences([m])
    fetch.assert_not_called()
    assert m.home_stats.injuries == []


def test_enrich_survives_http_failure():
    m = _make_match()
    with patch.object(fpl.requests, "get", side_effect=ConnectionError("boom")):
        result = fpl.enrich_with_fpl_absences([m])
    assert result == [m]
    assert m.home_stats.injuries == []


# ---------------------------------------------------------------------------
# Return info and suspensions (FPL is the Premier League's primary source)
# ---------------------------------------------------------------------------

def test_expected_return_parsing():
    assert fpl._expected_return("Hamstring injury - Expected back 8 Nov") == "Expected back 8 Nov"
    assert fpl._expected_return("Hip injury - 75% chance of playing") == "75% chance of playing"
    assert fpl._expected_return("Suspended until 29 Nov") == "Suspended until 29 Nov"
    assert fpl._expected_return("") == ""


def test_suspensions_split_from_injuries():
    m = _make_match()
    players = [
        _player("Rodri", 1, "i", 0, cost=60, news="Knee injury - Expected back 8 Nov"),
        _player("Banned", 1, "s", 0, cost=55, news="Suspended until 2 Nov"),
    ]
    with patch.object(fpl, "_fetch_bootstrap", return_value=_bootstrap(players)):
        fpl.enrich_with_fpl_absences([m])
    assert [a.player_name for a in m.home_stats.injuries] == ["Rodri"]
    assert m.home_stats.injuries[0].expected_return == "Expected back 8 Nov"
    assert [a.player_name for a in m.home_stats.suspensions] == ["Banned"]
    assert m.home_stats.absences_checked_source == "fpl"


def test_one_goal_tie_is_not_top_scorer():
    players = [_player(f"P{i}", 1, "i" if i == 0 else "a", 0, goals=1) for i in range(5)]
    absences = fpl._build_team_absences(1, players)
    assert not absences[0].is_top_scorer
