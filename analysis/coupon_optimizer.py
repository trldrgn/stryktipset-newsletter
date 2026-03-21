"""
Allocates the 13 match predictions into the 768 SEK coupon system:

  4 singles  (1 outcome)  — highest confidence games
  8 doubles  (2 outcomes) — mid-confidence games (drop least likely outcome)
  1 full     (3 outcomes) — lowest confidence game (hedge all outcomes)

Total rows: 1^4 × 2^8 × 3^1 = 768 SEK

Strategy:
  - Sort all 13 predictions by Claude's confidence score
  - Top 4 → singles (keep only their top predicted outcome)
  - Bottom 1 → full (all 3 outcomes)
  - Remaining 8 → doubles (keep top 2 predicted outcomes, drop the 3rd)

For doubles where Claude already gave 2 outcomes: use as-is.
For doubles where Claude gave 1 or 3 outcomes: adjust to 2 using
the market implied probabilities as a tiebreaker.
"""

from __future__ import annotations

from models.match import (
    Match,
    MatchPrediction,
    WeeklyReport,
    SelectionType,
    Outcome,
)
from utils.logger import get_logger

logger = get_logger(__name__)

_ALL_OUTCOMES = [Outcome.HOME, Outcome.DRAW, Outcome.AWAY]


def _rank_outcomes_by_market(match: Match) -> list[Outcome]:
    """
    Return outcomes ranked by implied probability (highest first).
    Falls back to [1, X, 2] if no market data.
    """
    if not match or not match.market:
        return list(_ALL_OUTCOMES)

    m = match.market
    probs = {
        Outcome.HOME: m.implied_prob_home or 0.0,
        Outcome.DRAW: m.implied_prob_draw or 0.0,
        Outcome.AWAY: m.implied_prob_away or 0.0,
    }
    return sorted(probs.keys(), key=lambda o: probs[o], reverse=True)


def _best_single_outcome(pred: MatchPrediction, match: Match | None) -> Outcome:
    """
    For a single selection, pick the one outcome we're most confident about.
    Priority: first predicted_outcome from Claude (already ranked by confidence).
    """
    if pred.predicted_outcomes:
        return pred.predicted_outcomes[0]
    # Fallback: market favourite
    if match:
        ranked = _rank_outcomes_by_market(match)
        return ranked[0]
    return Outcome.HOME


def _best_double_outcomes(pred: MatchPrediction, match: Match | None) -> list[Outcome]:
    """
    For a double selection, pick 2 of the 3 outcomes.
    - If Claude predicted exactly 2: use them
    - If Claude predicted 1: add the market's 2nd most likely
    - If Claude predicted 3: drop the market's least likely
    """
    if len(pred.predicted_outcomes) == 2:
        return list(pred.predicted_outcomes)

    market_ranked = _rank_outcomes_by_market(match)

    if len(pred.predicted_outcomes) == 1:
        # Keep Claude's pick + add market's 2nd most likely (that isn't already picked)
        kept = pred.predicted_outcomes[0]
        second = next((o for o in market_ranked if o != kept), Outcome.DRAW)
        return [kept, second]

    # Claude predicted 3 — drop the market's least likely
    drop = market_ranked[-1]
    return [o for o in pred.predicted_outcomes if o != drop][:2]


# ---------------------------------------------------------------------------
# Main optimizer
# ---------------------------------------------------------------------------

def optimise_coupon(report: WeeklyReport, matches: list[Match]) -> WeeklyReport:
    """
    Takes Claude's raw predictions (with confidence scores) and allocates
    each of the 13 games into single / double / full.

    Modifies report in place and returns it.
    """
    if len(report.predictions) != 13:
        logger.warning(
            "Expected 13 predictions, got %d — coupon may be incomplete",
            len(report.predictions),
        )

    match_by_game: dict[int, Match] = {m.game_number: m for m in matches}

    # Sort by confidence: highest first
    sorted_preds = sorted(report.predictions, key=lambda p: p.confidence, reverse=True)

    singles_games = [p.game_number for p in sorted_preds[:4]]
    full_game = sorted_preds[-1].game_number          # least confident
    doubles_games = [
        p.game_number for p in sorted_preds[4:-1]    # the 8 in between
    ]

    logger.info("Coupon allocation:")
    logger.info("  Singles (top 4 confidence): games %s", singles_games)
    logger.info("  Doubles (mid 8): games %s", doubles_games)
    logger.info("  Full (least confident): game %d", full_game)

    report.singles = sorted(singles_games)
    report.doubles = sorted(doubles_games)
    report.full_game = full_game

    # --- Adjust each prediction's selection_type and predicted_outcomes ---
    for pred in report.predictions:
        match = match_by_game.get(pred.game_number)

        if pred.game_number in singles_games:
            pred.selection_type = SelectionType.SINGLE
            pred.predicted_outcomes = [_best_single_outcome(pred, match)]

        elif pred.game_number == full_game:
            pred.selection_type = SelectionType.FULL
            pred.predicted_outcomes = list(_ALL_OUTCOMES)

        else:  # double
            pred.selection_type = SelectionType.DOUBLE
            pred.predicted_outcomes = _best_double_outcomes(pred, match)

    # --- Verify and log coupon cost ---
    rows = 1
    for pred in report.predictions:
        rows *= len(pred.predicted_outcomes)

    report.total_rows = rows
    report.total_cost_sek = rows  # 1 SEK per row

    logger.info(
        "Final coupon: %d rows = %d SEK (expected ~768)",
        rows, rows,
    )

    # Log full coupon table
    for pred in report.predictions:
        outcomes_str = " / ".join(o.value for o in pred.predicted_outcomes)
        logger.info(
            "  Game %2d %-25s vs %-25s → %s [%s, conf=%.2f]",
            pred.game_number,
            pred.home_team[:25],
            pred.away_team[:25],
            outcomes_str,
            pred.selection_type.value,
            pred.confidence,
        )

    return report
