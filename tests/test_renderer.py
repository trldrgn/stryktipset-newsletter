"""Tests for the newsletter renderer's view model and template."""

from datetime import datetime, timezone

from email_sender import renderer
from models.match import (
    FormResult,
    H2HResult,
    MarketSignals,
    Match,
    MatchPrediction,
    Outcome,
    PlayerAbsence,
    ScheduleContext,
    SelectionType,
    TeamStats,
    VenueRecord,
    WeeklyReport,
)

_KICKOFF = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc)


def _match() -> Match:
    m = Match(game_number=3, draw_number=4977, home_team="Manchester City", away_team="Brighton",
              league="Premier League", country="England", kickoff=_KICKOFF,
              referee="Craig Pawson", referee_yellows_avg=3.52, referee_reds_avg=0.1)
    m.market = MarketSignals(odds_home=1.70, odds_draw=4.35, odds_away=4.60,
                             public_pct_home=60, public_pct_draw=22, public_pct_away=18)
    win = FormResult(opponent="Ipswich", home_or_away="H", goals_for=3, goals_against=2, result=Outcome.HOME)
    m.home_stats = TeamStats(
        team_name="Manchester City", form_last5=[win] * 4, league_points=21, season_xpoints=15.2,
        home_record=VenueRecord(played=4, wins=4, draws=0, losses=0, goals_for=11, goals_against=6, points=12, rank=2),
        schedule=ScheduleContext(days_since_last_match=2, matches_last_14_days=3),
        injuries=[PlayerAbsence(player_name="Semenyo", position="RW", is_top_assister=True, source="fpl")],
        absences_checked_source="fpl",
    )
    m.away_stats = TeamStats(team_name="Brighton", absences_checked_source="fpl")
    m.h2h = [
        H2HResult(date="2026-10-28", home_team="Manchester City", away_team="Brighton", home_goals=2, away_goals=2),
        H2HResult(date="2025-08-31", home_team="Brighton", away_team="Manchester City", home_goals=2, away_goals=1),
    ]
    return m


def test_odds_flags_value_where_public_underbets():
    odds = renderer._odds(_match())
    assert [c["implied"] for c in odds["cells"]] == [57, 22, 21]
    assert [c["value"] for c in odds["cells"]] == [None, None, 3]    # 21% by odds vs 18% public


def test_h2h_is_from_home_team_perspective():
    h2h = renderer._h2h(_match())
    assert [x["letter"] for x in h2h] == ["D", "L"]                 # 1-2 away loss reads as L for City
    assert h2h[0]["year"] == "2026"


def test_short_names_and_missing_text():
    assert renderer._short("Manchester City") == "Man City"
    assert renderer._short("Queens Park Rangers") == "QPR"
    assert renderer._short("Brighton") == "Brighton"
    m = _match()
    assert renderer._missing(m.home_stats) == "Semenyo"
    assert renderer._missing(m.away_stats) == "Nobody missing"        # checked by FPL, none out
    assert renderer._missing(TeamStats(team_name="X")) == ""          # no source: show nothing


def test_chips_cover_form_fatigue_and_key_absence():
    chips = renderer._chips(_match().home_stats, absent_count=1)
    assert [(c["kind"], c["text"]) for c in chips] == [
        ("positive", "Up-form"), ("caution", "2 days rest"), ("danger", "Semenyo out"),
    ]


def test_render_shows_picks_referee_and_cards():
    m = _match()
    pred = MatchPrediction(game_number=3, home_team=m.home_team, away_team=m.away_team,
                           predicted_outcomes=[Outcome.HOME, Outcome.DRAW], selection_type=SelectionType.DOUBLE,
                           confidence=0.62, analysis="First paragraph.\n\nSecond paragraph.",
                           key_factors=["Unbeaten at home"], risk_flags=["Two days' rest"])
    report = WeeklyReport(draw_number=4977, generated_at=_KICKOFF, predictions=[pred],
                          doubles=[3], total_rows=2, total_cost_sek=2)
    subject, html = renderer.render_newsletter(report, [m])

    assert "Stryktipset" in subject
    assert html.count('class="bx b28 bo"') == 2                       # 1 and X filled on the card
    assert "static/icons/whistle.png" in html and "Craig Pawson" in html
    assert "3.5" in html and "0.10" in html                           # yellows and reds per game
    assert "First paragraph." in html and "Second paragraph." not in html
    assert "Man City" in html
    assert "\n  " not in html.split("</style>")[1]                    # indentation compacted away
