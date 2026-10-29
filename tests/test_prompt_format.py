"""Tests for the compact match-block formatting Claude sees."""

from datetime import date, timedelta

from analysis.claude_analyst import _format_absences, _format_form, _format_h2h
from models.match import FormResult, H2HResult, InjuryStatus, Outcome, PlayerAbsence


def test_form_shows_result_letter_opponent_venue_and_competition():
    form = [
        FormResult(opponent="Fulham", home_or_away="H", goals_for=2, goals_against=1,
                   result=Outcome.HOME, competition="Premier League"),
        FormResult(opponent="PSG", home_or_away="A", goals_for=0, goals_against=0,
                   result=Outcome.DRAW, competition="Champions League"),
        FormResult(opponent="Leeds", home_or_away="A", goals_for=0, goals_against=1,
                   result=Outcome.AWAY),   # CSV backup has no competition
    ]
    assert _format_form(form) == "W 2-1 Fulham(H,PL), D 0-0 PSG(A,UCL), L 0-1 Leeds(A)"
    assert _format_form([]) == "N/A"


def test_h2h_keeps_last_five_meetings_up_to_ten_years():
    today = date.today()
    meetings = [
        H2HResult(date=(today - timedelta(days=200 * i + 30)).isoformat(),
                  home_team="Bolton", away_team="Bristol City", home_goals=1, away_goals=0)
        for i in range(8)
    ]
    old = H2HResult(date=(today - timedelta(days=11 * 365)).isoformat(),
                    home_team="Bolton", away_team="Bristol City", home_goals=0, away_goals=0)
    text = _format_h2h([old] + meetings, "Bolton", "Bristol City")
    assert text.count("Bolton 1-0 Bristol City") == 5
    assert "0-0" not in text
    assert _format_h2h([old], "Bolton", "Bristol City") == "No H2H data within last 10 years"


def test_absence_shows_contribution_and_expected_return():
    a = PlayerAbsence(player_name="Semenyo", position="RW", status=InjuryStatus.OUT,
                      season_goals=2, season_assists=3, is_top_assister=True,
                      source="fotmob", expected_return="Mid December 2026")
    doubt = PlayerAbsence(player_name="Erhahon", position="CDM", status=InjuryStatus.DOUBT,
                          source="fotmob", expected_return="Doubtful")
    assert _format_absences([a, doubt]) == (
        "Semenyo (RW, out) [fotmob] 2G/3A back: Mid December 2026 ⚡TOP ASSISTER; "
        "Erhahon (CDM, doubt) [fotmob]"
    )


def test_suspension_only_team_is_not_tagged_fully_fit():
    from analysis.claude_analyst import _injury_source_tag
    from models.match import TeamStats
    ts = TeamStats(team_name="Fulham", absences_checked_source="fpl",
                   suspensions=[PlayerAbsence(player_name="Sessegnon", position="DEF", source="fpl")])
    assert _injury_source_tag(ts) == ""
    assert _injury_source_tag(TeamStats(team_name="X", absences_checked_source="fpl")) == "[checked: fpl — none missing]"


def test_suspensions_are_labelled_for_claude():
    injured = PlayerAbsence(player_name="Semenyo", position="RW", source="fotmob", season_goals=2)
    banned = PlayerAbsence(player_name="Sessegnon", position="DEF", source="fpl",
                           expected_return="Suspended until 7 Nov")
    text = _format_absences([injured], [banned])
    assert "Semenyo (RW, out) [fotmob] 2G/0A" in text
    assert "Sessegnon (DEF, SUSPENDED) [fpl] (Suspended until 7 Nov)" in text
