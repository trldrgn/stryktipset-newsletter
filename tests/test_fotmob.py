"""Tests for the FotMob primary source — parsers run against saved (trimmed) responses."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import fetchers.fotmob as fotmob
import fetchers.fpl as fpl
from models.match import H2HResult, InjuryStatus, Match, Outcome, TeamStats

_FIXTURES = Path(__file__).parent / "fixtures"
_KICKOFF = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc)
_BOLTON, _BRISTOL = 8559, 8427


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def details() -> dict:
    return _load("fotmob_match_details.json")


@pytest.fixture(scope="module")
def team() -> dict:
    return _load("fotmob_team.json")


@pytest.fixture(scope="module")
def league() -> dict:
    return _load("fotmob_league.json")


@pytest.fixture(scope="module")
def player_stats() -> dict:
    return _load("fotmob_player_stats.json")


def _match(home: str = "Bolton", away: str = "Bristol City", league: str = "Championship") -> Match:
    return Match(game_number=8, draw_number=4977, home_team=home, away_team=away,
                 league=league, country="England", kickoff=_KICKOFF)


# ---------------------------------------------------------------------------
# Team-name resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ours, theirs", [
    ("Nottingham", "Nottingham Forest"),
    ("West Bromwich", "West Bromwich Albion"),
    ("Bournemouth", "AFC Bournemouth"),
    ("Hull", "Hull City"),
    ("Brighton", "Brighton & Hove Albion"),
    ("Sheffield W", "Sheffield Wednesday"),
    ("Queens Park Rangers", "Queens Park Rangers"),
    ("Athletic Bilbao", "Athletic Club"),
    ("Celta de Vigo", "Celta Vigo"),
    ("Bodö/Glimt", "Bodø/Glimt"),
    ("Spanien", "Spain"),
    ("Tjeckien", "Czechia"),
    ("Nordmakedonien", "North Macedonia"),
])
def test_name_score_accepts_svenska_spel_short_names(ours, theirs):
    assert fotmob._name_score(ours, theirs) >= 0.9


@pytest.mark.parametrize("ours, theirs", [
    ("Manchester City", "Manchester United"),
    ("Bristol City", "Bristol Rovers"),
    ("West Ham", "West Bromwich Albion"),
    ("Sheffield W", "Sheffield United"),
])
def test_name_score_rejects_different_clubs(ours, theirs):
    assert fotmob._name_score(ours, theirs) < fotmob._MIN_NAME_SCORE


def test_resolve_fixture_ids_matches_coupon_names():
    day = _load("fotmob_matches.json")
    matches = [
        _match("Bolton", "Bristol City"),
        _match("Bournemouth", "Leeds", league="Premier League"),
        _match("West Ham", "West Bromwich"),
    ]
    for i, m in enumerate(matches, start=1):
        m.game_number = i
    with patch.object(fotmob, "_fixtures_on", return_value=day):
        ids = fotmob._resolve_fixture_ids(matches)
    assert ids == {1: 5836898, 2: 5795495, 3: 5836908}


# ---------------------------------------------------------------------------
# Absences
# ---------------------------------------------------------------------------

def test_player_index_takes_goals_from_league_files(team, player_stats):
    # Squad goal counts are all 0 for EFL teams — the league files must win
    index = fotmob.player_index(team, _BOLTON, player_stats)
    dalby = next(p for p in index.values() if p["goals"] == 4)
    assert dalby["position"] == "ST"
    assert index[914169]["goals"] == 1          # Erhahon, listed in goals.json
    assert index[1127293]["goals"] == 0         # Forino, not listed → 0


def test_parse_absences_bolton(details, team, player_stats):
    players = fotmob.player_index(team, _BOLTON, player_stats)
    injuries, suspensions = fotmob.parse_absences(
        details["content"]["lineup"]["homeTeam"], players, _KICKOFF,
    )
    by_name = {a.player_name: a for a in injuries}
    assert set(by_name) == {"Ethan Erhahon", "Chris Forino-Joseph"}
    assert by_name["Ethan Erhahon"].status == InjuryStatus.DOUBT
    assert by_name["Ethan Erhahon"].season_goals == 1
    assert by_name["Chris Forino-Joseph"].status == InjuryStatus.OUT
    assert by_name["Chris Forino-Joseph"].position == "CB"
    assert by_name["Chris Forino-Joseph"].expected_return == "Early November 2026"
    assert all(a.source == "fotmob" for a in injuries)
    assert injuries[0].player_name == "Ethan Erhahon"   # goal contributors first
    assert suspensions == []


def test_parse_absences_missing_block_is_no_data_not_fully_fit():
    assert fotmob.parse_absences({"name": "X"}, {}, _KICKOFF) is None
    assert fotmob.parse_absences(None, {}, _KICKOFF) is None
    assert fotmob.parse_absences({"unavailable": []}, {}, _KICKOFF) == ([], [])


def test_parse_absences_suspension_and_status_mapping():
    lineup_team = {"unavailable": [
        {"id": 1, "name": "Banned", "unavailability": {
            "type": "suspension", "expectedReturn": "Late October 2026",
            "expectedReturnDate": "2026-10-31T17:00:00Z"}},
        {"id": 2, "name": "Nearly Fit", "unavailability": {
            "type": "injury", "expectedReturn": "Late October 2026",
            "expectedReturnDate": "2026-10-30T00:00:00Z"}},
        {"id": 3, "name": "Training", "unavailability": {
            "type": "injury", "expectedReturn": "Back in training"}},
        {"id": 4, "name": "Top Scorer", "unavailability": {
            "type": "injury", "expectedReturn": "Out for season"}},
    ]}
    players = {4: {"position": "ST", "goals": 9, "assists": 2}, 5: {"position": "CM", "goals": 3, "assists": 5}}
    injuries, suspensions = fotmob.parse_absences(lineup_team, players, _KICKOFF)

    assert [s.player_name for s in suspensions] == ["Banned"]
    assert suspensions[0].status == InjuryStatus.OUT     # returns after this match
    by_name = {a.player_name: a for a in injuries}
    assert by_name["Nearly Fit"].status == InjuryStatus.DOUBT      # due back before kickoff
    assert by_name["Training"].status == InjuryStatus.FIFTY_FIFTY
    assert by_name["Top Scorer"].is_top_scorer and not by_name["Top Scorer"].is_top_assister
    assert injuries[0].player_name == "Top Scorer"


def test_one_goal_tie_is_not_a_top_scorer():
    lineup_team = {"unavailable": [
        {"id": 1, "name": "One Of Seven", "unavailability": {"type": "injury"}},
    ]}
    players = {i: {"position": "CM", "goals": 1, "assists": 1} for i in range(1, 8)}
    injuries, _ = fotmob.parse_absences(lineup_team, players, _KICKOFF)
    assert not injuries[0].is_top_scorer and not injuries[0].is_top_assister


# ---------------------------------------------------------------------------
# H2H, referee, tables
# ---------------------------------------------------------------------------

def test_parse_h2h_skips_unplayed_and_keeps_last_five(details):
    h2h = fotmob.parse_h2h(details["content"]["h2h"], "Bolton", "Bristol City", _BOLTON)
    assert len(h2h) == 5
    assert h2h[0] == H2HResult(date="2019-01-25", home_team="Bristol City", away_team="Bolton",
                               home_goals=2, away_goals=1, competition="FA Cup")
    assert h2h[2].home_team == "Bolton" and (h2h[2].home_goals, h2h[2].away_goals) == (2, 2)
    assert all(r.date < "2026-10-31" for r in h2h)       # the 2027 fixture is dropped


def test_parse_referee(details):
    name, avg = fotmob.parse_referee(details)
    assert name == "Robert Madley"
    assert avg == pytest.approx(4.39)
    assert fotmob.parse_referee({}) == ("", None)


def test_venue_records_and_season_xg(league):
    stats = TeamStats(team_name="Bolton")
    assert fotmob._fill_table(stats, league, _BOLTON)
    assert (stats.league_position, stats.league_points) == (18, 14)
    assert stats.home_record.summary == "W2 D0 L4, 7-12, 6pts (#20)"
    assert stats.away_record.goals_for == 9 and stats.away_record.goals_against == 11

    assert fotmob._fill_season_xg(stats, league, _BOLTON)
    assert stats.season_played == 12
    assert stats.season_xg_for == pytest.approx(16.62)
    assert stats.season_xpoints == pytest.approx(12.5)


def test_table_rows_handles_grouped_leagues():
    league = {"table": [{"data": {"tables": [
        {"table": {"all": [{"id": 1, "idx": 1, "pts": 9}]}},
        {"table": {"all": [{"id": 2, "idx": 1, "pts": 7}]}},
    ]}}]}
    assert [r["id"] for r in fotmob._table_rows(league, "all")] == [1, 2]


# ---------------------------------------------------------------------------
# Form, schedule, xG
# ---------------------------------------------------------------------------

def test_form_excludes_friendlies_and_keeps_venue(team):
    results = fotmob.competitive_results(team, _BOLTON, _KICKOFF)
    assert all("Friendl" not in f["tournament"]["name"] for f in results)

    form = fotmob.parse_form(results, _BOLTON)
    assert [(r.opponent, r.home_or_away, r.goals_for, r.goals_against) for r in form[:2]] == [
        ("Middlesbrough", "H", 1, 5), ("Southampton", "A", 0, 0),
    ]
    assert form[0].result == Outcome.AWAY      # loss from Bolton's perspective
    assert form[0].competition == "Championship"

    home_form = fotmob.parse_form(results, _BOLTON, venue="H")
    assert len(home_form) == 5 and all(r.home_or_away == "H" for r in home_form)


def test_schedule_counts_all_competitions_and_skips_own_league_midweek(team):
    sched = fotmob.parse_schedule(team, _BOLTON, _KICKOFF, league_id=48)
    assert sched.days_since_last_match == 7               # Fri 23 Oct 19:00 → Sat 31 Oct
    assert sched.last_match_competition == "Championship"
    assert sched.matches_last_14_days == 1                # 17 Oct 14:00 is just outside 14 days
    assert sched.upcoming_big_match == ""                 # Tue 3 Nov is a league game

    sched_any = fotmob.parse_schedule(team, _BOLTON, _KICKOFF, league_id=None)
    assert sched_any.upcoming_big_match == "Championship in 3d"


def test_parse_match_xg_both_perspectives():
    finished = _load("fotmob_finished_match.json")
    assert fotmob.parse_match_xg(finished, _BOLTON) == (0.97, 4.38)
    assert fotmob.parse_match_xg(finished, 8549) == (4.38, 0.97)   # Middlesbrough
    assert fotmob.parse_match_xg(finished, 1) is None
    assert fotmob.parse_match_xg(None, _BOLTON) is None


def test_rolling_xg_needs_three_games(team):
    payload = _load("fotmob_finished_match.json")   # Bolton (home) 0.97 – 4.38 xG
    results = fotmob.competitive_results(team, _BOLTON, _KICKOFF)
    stats = TeamStats(team_name="Bolton")

    only_one_game = lambda mid, finished=False: payload if mid == 5836887 else None
    with patch.object(fotmob, "_match_details", side_effect=only_one_game):
        assert not fotmob._fill_rolling_xg(stats, results, _BOLTON)
    assert stats.xg_for_avg is None

    # Same xG payload for every game — enough to exercise the averaging
    with patch.object(fotmob, "_match_details", side_effect=lambda mid, finished=False: payload):
        assert fotmob._fill_rolling_xg(stats, results, _BOLTON)
    assert (stats.xg_for_avg, stats.xg_against_avg) == (0.97, 4.38)
    assert stats._xg_profile.matches_available == 5
    assert stats._xg_profile.goals_for_5g == pytest.approx((1 + 0 + 3 + 1 + 4) / 5)


# ---------------------------------------------------------------------------
# Per-field fallback + failure handling
# ---------------------------------------------------------------------------

def _routes(details, team, league, player_stats, fail: set[str] = frozenset()):
    """Fake _cached_get: route by cache key prefix; keys in `fail` return None."""
    def fake(key, ttl, path, params):
        for prefix, value in (
            ("fotmob_md_final", None), ("fotmob_md", details), ("fotmob_league", league),
            ("fotmob_team", team), ("fotmob_matches", _load("fotmob_matches.json")),
        ):
            if key.startswith(prefix):
                return None if prefix in fail else value
        if key.startswith("fotmob_stat_"):
            return None if "fotmob_stat" in fail else player_stats.get(
                "goals" if path.endswith("/goals.json") else "assists")
        return None
    return fake


def test_enrich_fills_every_field(details, team, league, player_stats):
    m = _match()
    league_with_stats = {**league, "stats": {"players": [
        {"fetchAllUrl": "https://data.fotmob.com/stats/48/season/1/goals.json"},
        {"fetchAllUrl": "https://data.fotmob.com/stats/48/season/1/goal_assist.json"},
    ]}}
    with patch.object(fotmob, "_cached_get", side_effect=_routes(details, team, league_with_stats, player_stats)):
        fotmob.enrich_with_fotmob([m])

    h = m.home_stats
    assert h.absences_checked_source == "fotmob"
    assert {a.player_name for a in h.injuries} == {"Ethan Erhahon", "Chris Forino-Joseph"}
    assert h.home_record is not None and h.season_xg_for is not None
    assert h.form_last5 and h.schedule and h.manager_name == "Steven Schumacher"
    assert len(m.h2h) == 5 and m.referee == "Robert Madley"
    assert m.away_stats.manager_name == "Michael Skubala"


def test_failed_field_stays_empty_for_backup(details, team, league, player_stats):
    """League call fails → only table/xG-table fields stay empty; absences, H2H etc. still fill."""
    m = _match()
    with patch.object(fotmob, "_cached_get",
                      side_effect=_routes(details, team, league, player_stats, fail={"fotmob_league"})):
        fotmob.enrich_with_fotmob([m])

    h = m.home_stats
    assert h.league_position is None and h.home_record is None and h.season_xg_for is None
    assert h.injuries and h.form_last5 and m.h2h and m.referee


def test_does_not_overwrite_existing_fields(details, team, league, player_stats):
    m = _match()
    existing_h2h = [H2HResult(date="2025-01-01", home_team="Bolton", away_team="Bristol City",
                              home_goals=1, away_goals=0)]
    m.h2h = list(existing_h2h)
    m.referee = "Already Set"
    m.home_stats = TeamStats(team_name="Bolton", league_position=1)
    with patch.object(fotmob, "_cached_get", side_effect=_routes(details, team, league, player_stats)):
        fotmob.enrich_with_fotmob([m])
    assert m.h2h == existing_h2h
    assert m.referee == "Already Set"
    assert m.home_stats.league_position == 1


def test_fpl_backup_respects_fotmob_fully_fit():
    m = _match("Manchester City", "Hull", league="Premier League")
    m.home_stats = TeamStats(team_name="Manchester City", absences_checked_source="fotmob")
    m.away_stats = TeamStats(team_name="Hull")
    bootstrap = {
        "teams": [{"id": 1, "name": "Man City"}, {"id": 2, "name": "Hull City"}],
        "elements": [
            {"web_name": "Rodri", "team": 1, "status": "i", "now_cost": 60, "element_type": 3},
            {"web_name": "Greaves", "team": 2, "status": "i", "now_cost": 45, "element_type": 2},
        ],
    }
    with patch.object(fpl, "_fetch_bootstrap", return_value=bootstrap):
        fpl.enrich_with_fpl_absences([m])
    assert m.home_stats.injuries == []                      # FotMob said fully fit — kept
    assert [a.player_name for a in m.away_stats.injuries] == ["Greaves"]   # gap → FPL fills


def test_html_challenge_trips_circuit_breaker_and_pipeline_continues():
    challenge = MagicMock(status_code=403, headers={"content-type": "text/html"})
    m1 = _match()
    m2 = _match("Burnley", "Portsmouth")
    m2.game_number, m2.kickoff = 9, datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)
    with patch.object(fotmob, "get_cache") as cache, \
         patch.object(fotmob._SESSION, "get", return_value=challenge) as get, \
         patch.object(fotmob, "_MIN_INTERVAL_S", 0):
        cache.return_value.get.return_value = None
        result = fotmob.enrich_with_fotmob([m1, m2])
    assert result == [m1, m2]
    # 3 date lookups for game 8, 1 for game 9 — then the breaker stops all calls
    assert get.call_count == fotmob._MAX_CONSECUTIVE_FAILURES
    assert m1.home_stats is None and m1.h2h == []
