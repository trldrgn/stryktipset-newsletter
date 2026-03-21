"""Tests for analysis/coupon_optimizer.py — the 768 SEK coupon allocation logic."""

import pytest
from analysis.coupon_optimizer import optimise_coupon
from models.match import (
    MarketSignals,
    Match,
    MatchPrediction,
    Outcome,
    SelectionType,
    WeeklyReport,
)
from datetime import datetime, timezone


class TestCouponAllocation:
    def test_correct_split_4_singles_8_doubles_1_full(self, sample_weekly_report, sample_matches):
        report = optimise_coupon(sample_weekly_report, sample_matches)
        assert len(report.singles) == 4
        assert len(report.doubles) == 8
        assert report.full_game is not None

    def test_highest_confidence_become_singles(self, sample_weekly_report, sample_matches):
        report = optimise_coupon(sample_weekly_report, sample_matches)
        # Game 1 has confidence 0.92, game 2 has 0.88, etc. — top 4 by confidence
        single_confidences = [
            p.confidence for p in report.predictions if p.selection_type == SelectionType.SINGLE
        ]
        double_confidences = [
            p.confidence for p in report.predictions if p.selection_type == SelectionType.DOUBLE
        ]
        assert min(single_confidences) > max(double_confidences)

    def test_lowest_confidence_becomes_full(self, sample_weekly_report, sample_matches):
        report = optimise_coupon(sample_weekly_report, sample_matches)
        full_pred = next(p for p in report.predictions if p.selection_type == SelectionType.FULL)
        # Game 13 has confidence 0.40 — lowest
        assert full_pred.confidence == pytest.approx(0.40, rel=0.01)

    def test_full_game_has_all_3_outcomes(self, sample_weekly_report, sample_matches):
        report = optimise_coupon(sample_weekly_report, sample_matches)
        full_pred = next(p for p in report.predictions if p.selection_type == SelectionType.FULL)
        assert set(full_pred.predicted_outcomes) == {Outcome.HOME, Outcome.DRAW, Outcome.AWAY}

    def test_singles_have_exactly_1_outcome(self, sample_weekly_report, sample_matches):
        report = optimise_coupon(sample_weekly_report, sample_matches)
        for pred in report.predictions:
            if pred.selection_type == SelectionType.SINGLE:
                assert len(pred.predicted_outcomes) == 1

    def test_doubles_have_exactly_2_outcomes(self, sample_weekly_report, sample_matches):
        report = optimise_coupon(sample_weekly_report, sample_matches)
        for pred in report.predictions:
            if pred.selection_type == SelectionType.DOUBLE:
                assert len(pred.predicted_outcomes) == 2

    def test_total_rows_is_768(self, sample_weekly_report, sample_matches):
        report = optimise_coupon(sample_weekly_report, sample_matches)
        assert report.total_rows == 768
        assert report.total_cost_sek == 768

    def test_all_13_games_allocated(self, sample_weekly_report, sample_matches):
        report = optimise_coupon(sample_weekly_report, sample_matches)
        all_games = {p.game_number for p in report.predictions}
        assert len(all_games) == 13
        assert all_games == set(range(1, 14))

    def test_report_singles_list_sorted(self, sample_weekly_report, sample_matches):
        report = optimise_coupon(sample_weekly_report, sample_matches)
        assert report.singles == sorted(report.singles)
        assert report.doubles == sorted(report.doubles)


class TestDoubleOutcomeSelection:
    """When Claude gives 1 or 3 outcomes for a double game, the optimizer should fix it."""

    def _make_report_with_one_prediction_override(self, game_number: int, outcomes: list[Outcome]) -> WeeklyReport:
        """Create a 13-prediction report where game_number has a specific outcome count."""
        confidences = [0.92, 0.88, 0.85, 0.82, 0.75, 0.72, 0.68, 0.65, 0.60, 0.55, 0.52, 0.50, 0.40]
        predictions = []
        for i, conf in enumerate(confidences, 1):
            if i == game_number:
                preds = outcomes
            else:
                preds = [Outcome.HOME, Outcome.DRAW]
            predictions.append(MatchPrediction(
                game_number=i,
                home_team=f"Home{i}",
                away_team=f"Away{i}",
                predicted_outcomes=preds,
                selection_type=SelectionType.DOUBLE,
                confidence=conf,
            ))
        return WeeklyReport(
            draw_number=4945,
            generated_at=datetime(2026, 3, 21, tzinfo=timezone.utc),
            predictions=predictions,
        )

    def test_double_with_1_outcome_gets_second_from_market(self, sample_matches):
        """Claude predicted only 1 outcome for a double — optimizer adds a 2nd using market odds."""
        # Game 5 (confidence 0.75) will be a double
        report = self._make_report_with_one_prediction_override(5, [Outcome.HOME])
        result = optimise_coupon(report, sample_matches)
        game5 = next(p for p in result.predictions if p.game_number == 5)
        assert game5.selection_type == SelectionType.DOUBLE
        assert len(game5.predicted_outcomes) == 2

    def test_double_with_3_outcomes_drops_least_likely(self, sample_matches):
        """Claude predicted all 3 outcomes for a double — optimizer drops the least likely."""
        report = self._make_report_with_one_prediction_override(
            5, [Outcome.HOME, Outcome.DRAW, Outcome.AWAY]
        )
        result = optimise_coupon(report, sample_matches)
        game5 = next(p for p in result.predictions if p.game_number == 5)
        assert game5.selection_type == SelectionType.DOUBLE
        assert len(game5.predicted_outcomes) == 2
        # Total rows must still be 768
        assert result.total_rows == 768

    def test_warns_on_fewer_than_13_predictions(self, sample_matches, caplog):
        """Should log a warning but not crash when fewer than 13 predictions are given."""
        report = WeeklyReport(
            draw_number=4945,
            generated_at=datetime(2026, 3, 21, tzinfo=timezone.utc),
            predictions=[
                MatchPrediction(
                    game_number=i,
                    home_team=f"H{i}",
                    away_team=f"A{i}",
                    predicted_outcomes=[Outcome.HOME],
                    selection_type=SelectionType.SINGLE,
                    confidence=0.8,
                )
                for i in range(1, 11)  # only 10 predictions
            ],
        )
        import logging
        with caplog.at_level(logging.WARNING):
            optimise_coupon(report, sample_matches)
        assert any("Expected 13" in r.message for r in caplog.records)
