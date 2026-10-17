"""Tests for football-data.co.uk team resolution and CSV-based H2H."""

from datetime import datetime, timezone
from unittest.mock import patch

import fetchers.football_data_csv as fdc
from models.match import H2HResult, Match, TeamStats

_ENGLISH_TEAMS = {
    "Man City", "Man United", "Bristol City", "Hull", "QPR", "Birmingham",
    "West Brom", "Sheffield United", "Sheffield Weds", "Wolves", "Plymouth",
    "Huddersfield", "Leicester",
}


def _make_match(home: str, away: str, h2h: list | None = None) -> Match:
    m = Match(
        game_number=1, draw_number=4975, home_team=home, away_team=away,
        league="Championship", country="England",
        kickoff=datetime(2026, 10, 17, 14, 0, tzinfo=timezone.utc),
    )
    m.home_stats = TeamStats(team_name=home)
    m.away_stats = TeamStats(team_name=away)
    m.h2h = h2h or []
    return m


def _row(date: str, home: str, away: str, hg: str, ag: str) -> dict:
    return {"Date": date, "HomeTeam": home, "AwayTeam": away, "FTHG": hg, "FTAG": ag}


# ---------------------------------------------------------------------------
# Team resolution
# ---------------------------------------------------------------------------

def test_manchester_city_never_matches_bristol_city():
    assert fdc._resolve_team("Manchester City", _ENGLISH_TEAMS) == "Man City"
    assert fdc._resolve_team("Manchester United", _ENGLISH_TEAMS) == "Man United"


def test_aliases_resolve():
    assert fdc._resolve_team("Queens Park Rangers", _ENGLISH_TEAMS) == "QPR"
    assert fdc._resolve_team("West Bromwich", _ENGLISH_TEAMS) == "West Brom"
    assert fdc._resolve_team("Sheffield U", _ENGLISH_TEAMS) == "Sheffield United"
    assert fdc._resolve_team("Sheffield W", _ENGLISH_TEAMS) == "Sheffield Weds"
    assert fdc._resolve_team("Wolverhampton", _ENGLISH_TEAMS) == "Wolves"


def test_generic_word_alone_does_not_match():
    # "Leicester City" must match Leicester, and a lone "City" never decides
    assert fdc._resolve_team("Leicester City", _ENGLISH_TEAMS) == "Leicester"
    assert fdc._resolve_team("Norwich City", {"Bristol City", "Man City"}) is None


# ---------------------------------------------------------------------------
# H2H
# ---------------------------------------------------------------------------

def test_season_codes():
    with patch.object(fdc, "_current_season_code", return_value="2627"):
        assert fdc._recent_season_codes(3) == ["2627", "2526", "2425", "2324"]
    with patch.object(fdc, "_current_season_code", return_value="0001"):
        assert fdc._recent_season_codes(1) == ["0001", "9900"]


def test_build_h2h_both_venues_uses_svenska_names():
    rows = [
        ("E1", _row("11/03/2026", "Birmingham", "QPR", "1", "0")),
        ("E1", _row("09/12/2025", "QPR", "Birmingham", "2", "1")),
        ("E0", _row("01/01/2025", "Birmingham", "Hull", "3", "3")),   # other fixture
        ("E1", _row("01/02/2027", "Birmingham", "QPR", "", "")),      # unplayed
    ]
    h2h = fdc._build_csv_h2h("Birmingham", "Queens Park Rangers", "Birmingham", "QPR", rows)
    assert [(h.date, h.home_team, h.away_team, h.home_goals, h.away_goals) for h in h2h] == [
        ("2026-03-11", "Birmingham", "Queens Park Rangers", 1, 0),
        ("2025-12-09", "Queens Park Rangers", "Birmingham", 2, 1),
    ]
    assert h2h[0].competition == "Championship"


def test_enrich_across_divisions_and_tolerates_fetch_failure():
    csvs = {
        ("E0", "2526"): [_row("20/09/2025", "Hull", "Plymouth", "0", "2")],
        ("E2", "2425"): [_row("13/01/2025", "Plymouth", "Hull", "1", "1")],
    }

    def fake_fetch(code, season=None):
        if code == "E3":
            raise ConnectionError("404")
        return csvs.get((code, season), [])

    m = _make_match("Plymouth", "Hull")
    with patch.object(fdc, "_fetch_csv", side_effect=fake_fetch), \
         patch.object(fdc, "_current_season_code", return_value="2627"):
        fdc.enrich_with_csv_h2h([m])

    assert [(h.date, h.competition) for h in m.h2h] == [
        ("2025-09-20", "Premier League"),
        ("2025-01-13", "League One"),
    ]
    assert m.h2h[0].home_team == "Hull" and m.h2h[0].away_team == "Plymouth"


def test_enrich_keeps_existing_h2h():
    existing = [H2HResult(date="2025-01-01", home_team="A", away_team="B", home_goals=1, away_goals=0)]
    m = _make_match("Plymouth", "Hull", h2h=existing)
    with patch.object(fdc, "_fetch_csv") as fetch:
        fdc.enrich_with_csv_h2h([m])
    fetch.assert_not_called()
    assert m.h2h == existing
