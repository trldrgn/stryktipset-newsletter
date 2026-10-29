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
