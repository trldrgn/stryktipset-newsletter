"""Tests for models/match.py — data model properties and computed fields."""

import pytest
from models.match import (
    FormResult,
    InjuryStatus,
    MarketSignals,
    MatchupRisk,
    Outcome,
    PlayerAbsence,
    RiskLevel,
    ScheduleContext,
    SelectionType,
    TeamStats,
    WeeklyEvaluation,
    MatchEvaluation,
)


class TestMarketSignals:
    def test_implied_prob_home(self):
        m = MarketSignals(odds_home=2.0)
        assert m.implied_prob_home == 0.5

    def test_implied_prob_rounds_to_4dp(self):
        m = MarketSignals(odds_home=3.0)
        assert m.implied_prob_home == round(1 / 3, 4)

    def test_implied_prob_none_when_no_odds(self):
        m = MarketSignals()
        assert m.implied_prob_home is None
        assert m.implied_prob_draw is None
        assert m.implied_prob_away is None

    def test_market_favourite_home(self):
        m = MarketSignals(odds_home=1.80, odds_draw=3.50, odds_away=4.50)
        assert m.market_favourite == Outcome.HOME

    def test_market_favourite_draw(self):
        m = MarketSignals(odds_home=3.00, odds_draw=2.90, odds_away=2.80)
        assert m.market_favourite == Outcome.AWAY

    def test_market_favourite_none_with_no_odds(self):
        m = MarketSignals()
        assert m.market_favourite is None


class TestTeamStats:
    def test_fatigue_flag_true_when_played_3_days_ago(self):
        stats = TeamStats(
            team_name="Arsenal",
            schedule=ScheduleContext(days_since_last_match=3, matches_last_14_days=2),
        )
        assert stats.fatigue_flag is True

    def test_fatigue_flag_true_when_played_2_days_ago(self):
        stats = TeamStats(
            team_name="Arsenal",
            schedule=ScheduleContext(days_since_last_match=2, matches_last_14_days=2),
        )
        assert stats.fatigue_flag is True

    def test_fatigue_flag_false_when_played_5_days_ago(self):
        stats = TeamStats(
            team_name="Arsenal",
            schedule=ScheduleContext(days_since_last_match=5, matches_last_14_days=2),
        )
        assert stats.fatigue_flag is False

    def test_fatigue_flag_true_when_congested_schedule(self):
        stats = TeamStats(
            team_name="Arsenal",
            schedule=ScheduleContext(days_since_last_match=5, matches_last_14_days=4),
        )
        assert stats.fatigue_flag is True

    def test_fatigue_flag_false_when_no_schedule(self):
        stats = TeamStats(team_name="Arsenal")
        assert stats.fatigue_flag is False

    def test_new_manager_bounce_true_within_8_weeks(self):
        stats = TeamStats(team_name="Southampton", manager_weeks_in_post=3)
        assert stats.new_manager_bounce is True

    def test_new_manager_bounce_true_at_8_weeks(self):
        stats = TeamStats(team_name="Southampton", manager_weeks_in_post=8)
        assert stats.new_manager_bounce is True

    def test_new_manager_bounce_false_after_8_weeks(self):
        stats = TeamStats(team_name="Arsenal", manager_weeks_in_post=9)
        assert stats.new_manager_bounce is False

    def test_new_manager_bounce_false_when_none(self):
        stats = TeamStats(team_name="Arsenal")
        assert stats.new_manager_bounce is False

    def test_form_points_last5(self):
        form = [
            FormResult("A", "H", 2, 0, Outcome.HOME),    # 3 pts
            FormResult("B", "A", 1, 1, Outcome.DRAW),    # 1 pt
            FormResult("C", "H", 0, 1, Outcome.AWAY),    # 0 pts
            FormResult("D", "A", 2, 0, Outcome.HOME),    # 3 pts
            FormResult("E", "H", 1, 0, Outcome.HOME),    # 3 pts
        ]
        stats = TeamStats(team_name="Arsenal", form_last5=form)
        assert stats.form_points_last5 == 10

    def test_critical_absences_includes_top_scorer(self):
        absence = PlayerAbsence(
            player_name="Haaland",
            position="ST",
            is_top_scorer=True,
        )
        stats = TeamStats(team_name="Man City", injuries=[absence])
        assert absence in stats.critical_absences

    def test_critical_absences_includes_high_matchup_risk(self):
        absence = PlayerAbsence(
            player_name="Zinchenko",
            position="LB",
            matchup_risk=MatchupRisk(
                opponent_player="Neto",
                opponent_position="RW",
                risk_level=RiskLevel.HIGH,
            ),
        )
        stats = TeamStats(team_name="Arsenal", injuries=[absence])
        assert absence in stats.critical_absences

    def test_critical_absences_excludes_low_risk_regular_player(self):
        absence = PlayerAbsence(
            player_name="Squad Player",
            position="RB",
            is_top_scorer=False,
            is_top_assister=False,
            matchup_risk=MatchupRisk(
                opponent_player="Somebody",
                opponent_position="LW",
                risk_level=RiskLevel.LOW,
            ),
        )
        stats = TeamStats(team_name="Arsenal", injuries=[absence])
        assert absence not in stats.critical_absences


class TestPlayerAbsence:
    def test_impact_summary_top_scorer(self):
        p = PlayerAbsence(player_name="Haaland", position="ST", is_top_scorer=True, season_goals=22)
        assert "top scorer" in p.impact_summary

    def test_impact_summary_top_assister(self):
        p = PlayerAbsence(player_name="De Bruyne", position="CAM", is_top_assister=True)
        assert "top assister" in p.impact_summary

    def test_impact_summary_set_piece_taker(self):
        p = PlayerAbsence(player_name="Trent", position="RB", is_set_piece_taker=True)
        assert "set piece" in p.impact_summary

    def test_impact_summary_goals_and_assists(self):
        p = PlayerAbsence(player_name="Player", position="LW", season_goals=8, season_assists=6)
        assert "8G" in p.impact_summary
        assert "6A" in p.impact_summary

    def test_impact_summary_squad_player(self):
        p = PlayerAbsence(player_name="Backup", position="CB", season_goals=0)
        assert p.impact_summary == "squad player"


class TestWeeklyEvaluation:
    def test_accuracy_pct(self, sample_evaluation):
        assert sample_evaluation.accuracy_pct == pytest.approx(66.7, rel=0.01)

    def test_accuracy_pct_zero_when_no_evaluations(self):
        ev = WeeklyEvaluation(draw_number=1, draw_date="2026-01-01")
        assert ev.accuracy_pct == 0.0

    def test_singles_accuracy_pct(self, sample_evaluation):
        # 1 correct out of 2 singles
        assert sample_evaluation.singles_accuracy_pct == 50.0

    def test_singles_accuracy_pct_zero_when_no_singles(self):
        ev = WeeklyEvaluation(draw_number=1, draw_date="2026-01-01", singles_total=0)
        assert ev.singles_accuracy_pct == 0.0


class TestFormResult:
    @pytest.mark.parametrize("outcome,expected_points", [
        (Outcome.HOME, 3),
        (Outcome.DRAW, 1),
        (Outcome.AWAY, 0),
    ])
    def test_points(self, outcome, expected_points):
        r = FormResult("Opponent", "H", 1, 0, outcome)
        assert r.points == expected_points
