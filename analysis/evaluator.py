"""
Evaluates last week's predictions against the actual Stryktipset results.

Scores each prediction, saves annotated results to data/results/, and
appends a summary to data/performance/history.json for long-term analysis.

The returned WeeklyEvaluation is used only for the newsletter scorecard.
Claude's prompt does not receive evaluation feedback — n=13/week is too
small to produce meaningful signal. Use `python main.py --improve` once
10+ weeks of history exist to find systematic patterns.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import PREDICTIONS_DIR, RESULTS_DIR, PERFORMANCE_DIR
from fetchers.svenska_spel import fetch_result
from models.match import (
    Outcome,
    SelectionType,
    MatchEvaluation,
    WeeklyEvaluation,
    MatchPrediction,
)
from utils.logger import get_logger

logger = get_logger(__name__)

HISTORY_FILE = PERFORMANCE_DIR / "history.json"


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def save_predictions(
    draw_number: int,
    draw_date: str,
    predictions: list[MatchPrediction],
    executive_summary: str = "",
    value_radar: list[str] | None = None,
) -> Path:
    """
    Persist this week's predictions to disk before results come in.
    Called at the end of the newsletter run.
    """
    filename = PREDICTIONS_DIR / f"draw_{draw_number}_{draw_date}.json"
    payload = {
        "draw_number": draw_number,
        "draw_date": draw_date,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "executive_summary": executive_summary,
        "value_radar": value_radar or [],
        "predictions": [
            {
                "game_number": p.game_number,
                "home_team": p.home_team,
                "away_team": p.away_team,
                "predicted_outcomes": [o.value for o in p.predicted_outcomes],
                "selection_type": p.selection_type.value,
                "confidence": p.confidence,
                "key_factors": p.key_factors,
                "risk_flags": p.risk_flags,
                "analysis": p.analysis,
                "value_note": p.value_note,
            }
            for p in predictions
        ],
    }
    filename.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Predictions saved to %s", filename)
    return filename


def load_latest_predictions() -> Optional[tuple[int, str, list[dict]]]:
    """
    Load the most recent predictions file.
    Returns (draw_number, draw_date, raw_predictions_list) or None.
    """
    files = sorted(PREDICTIONS_DIR.glob("draw_*.json"), reverse=True)
    if not files:
        logger.info("No previous predictions found — skipping evaluation")
        return None

    latest = files[0]
    logger.info("Loading previous predictions from %s", latest.name)
    data = json.loads(latest.read_text(encoding="utf-8"))
    return data["draw_number"], data["draw_date"], data["predictions"]


def _load_all_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))


def _save_history(history: list[dict]) -> None:
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _is_correct(predicted: list[str], actual: str) -> bool:
    return actual in predicted


def _outcome_label(o: str) -> str:
    return {"1": "Home win", "X": "Draw", "2": "Away win"}.get(o, o)


def _post_mortem_hint(pred: dict, actual: Outcome) -> str:
    """
    Generate a signal-specific post-mortem hint.
    Identifies which type of misjudgment likely occurred.
    """
    predicted = pred["predicted_outcomes"]
    selection = pred["selection_type"]
    confidence = pred.get("confidence", 0)
    risk_flags = pred.get("risk_flags", [])
    key_factors = pred.get("key_factors", [])

    hint_parts = [
        f"Predicted {'/'.join(predicted)} ({selection}, conf={confidence:.2f}) "
        f"but result was {actual.value}."
    ]

    # Signal-specific diagnostics
    if confidence >= 0.85 and selection == "single":
        hint_parts.append(
            "OVERCONFIDENCE: High-confidence single failed. "
            "Were multiple independent signals truly aligned, or were they correlated?"
        )
    elif confidence >= 0.75 and selection == "single":
        hint_parts.append(
            "BORDERLINE SINGLE: This was a marginal single pick. "
            "Consider whether it should have been a double."
        )

    if risk_flags:
        hint_parts.append(
            f"IGNORED RISKS: Risk flags were noted but not weighted enough: "
            f"{'; '.join(risk_flags[:3])}."
        )

    # Check if the actual result was the complete opposite
    if actual.value not in predicted:
        if len(predicted) == 2:
            dropped = [o for o in ["1", "X", "2"] if o not in predicted]
            if dropped:
                hint_parts.append(
                    f"WRONG DROPOUT: The dropped outcome ({dropped[0]}) was the actual result. "
                    f"Market or form signals may have been misleading."
                )

    # Draw-specific analysis
    if actual == Outcome.DRAW and "X" not in predicted:
        hint_parts.append(
            "DRAW MISSED: Draws are underrated when teams are closely matched. "
            "Check if league position differential was <5 and odds were tight."
        )

    return " ".join(hint_parts)


# ---------------------------------------------------------------------------
# Main evaluation function
# ---------------------------------------------------------------------------

def evaluate_last_week() -> Optional[WeeklyEvaluation]:
    """
    Load last week's predictions, fetch actual results, score them,
    save to disk, and return a WeeklyEvaluation ready for:
      - Newsletter Section 1 (report card)
      - Claude's context prompt (lessons learned)
      - Long-term history.json (model improvement)
    """
    loaded = load_latest_predictions()
    if loaded is None:
        return None

    draw_number, draw_date, raw_predictions = loaded

    # --- Fetch actual results ---
    try:
        results = fetch_result(draw_number)
    except Exception as e:
        logger.error("Could not fetch results for draw %d: %s", draw_number, e)
        return None

    if not results:
        logger.warning("Draw %d has no results yet — may still be in progress", draw_number)
        return None

    # --- Score ---
    evaluations: list[MatchEvaluation] = []
    singles_correct = singles_total = doubles_correct = doubles_total = total_correct = 0
    full_covered = False

    for pred in raw_predictions:
        game_num = pred["game_number"]
        actual = results.get(game_num)
        if actual is None:
            logger.warning("No result for game %d — skipping", game_num)
            continue

        predicted_raw = pred["predicted_outcomes"]
        selection = SelectionType(pred["selection_type"])
        correct = _is_correct(predicted_raw, actual.value)

        if correct:
            total_correct += 1
        if selection == SelectionType.SINGLE:
            singles_total += 1
            if correct:
                singles_correct += 1
        elif selection == SelectionType.DOUBLE:
            doubles_total += 1
            if correct:
                doubles_correct += 1
        elif selection == SelectionType.FULL:
            full_covered = True  # full always covers the result

        post_mortem = "" if correct else _post_mortem_hint(pred, actual)

        evaluations.append(MatchEvaluation(
            game_number=game_num,
            home_team=pred["home_team"],
            away_team=pred["away_team"],
            our_prediction=[Outcome(o) for o in predicted_raw],
            selection_type=selection,
            actual_result=actual,
            correct=correct,
            post_mortem=post_mortem,
        ))

    # --- Build evaluation object ---
    evaluation = WeeklyEvaluation(
        draw_number=draw_number,
        draw_date=draw_date,
        evaluations=evaluations,
        total_correct=total_correct,
        singles_correct=singles_correct,
        singles_total=singles_total,
        doubles_correct=doubles_correct,
        doubles_total=doubles_total,
        full_covered=full_covered,
    )

    # --- Persist results ---
    _save_results(draw_number, draw_date, evaluations)
    _append_to_history(draw_number, draw_date, evaluations, evaluation)

    logger.info(
        "Evaluation complete: %d/%d correct (singles %d/%d, doubles %d/%d)",
        total_correct, len(evaluations),
        singles_correct, singles_total,
        doubles_correct, doubles_total,
    )

    return evaluation


# ---------------------------------------------------------------------------
# Persistence for results and history
# ---------------------------------------------------------------------------

def _save_results(draw_number: int, draw_date: str, evaluations: list[MatchEvaluation]) -> None:
    filename = RESULTS_DIR / f"draw_{draw_number}_{draw_date}.json"
    payload = {
        "draw_number": draw_number,
        "draw_date": draw_date,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "results": [
            {
                "game_number": e.game_number,
                "home_team": e.home_team,
                "away_team": e.away_team,
                "our_prediction": [o.value for o in e.our_prediction],
                "selection_type": e.selection_type.value,
                "actual_result": e.actual_result.value,
                "correct": e.correct,
                "post_mortem": e.post_mortem,
            }
            for e in evaluations
        ],
    }
    filename.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Results saved to %s", filename)


def _append_to_history(
    draw_number: int,
    draw_date: str,
    evaluations: list[MatchEvaluation],
    summary: WeeklyEvaluation,
) -> None:
    history = _load_all_history()
    history.append({
        "draw_number": draw_number,
        "draw_date": draw_date,
        "appended_at": datetime.now(timezone.utc).isoformat(),
        "total_correct": summary.total_correct,
        "total_games": len(evaluations),
        "accuracy_pct": summary.accuracy_pct,
        "singles_correct": summary.singles_correct,
        "singles_total": summary.singles_total,
        "doubles_correct": summary.doubles_correct,
        "doubles_total": summary.doubles_total,
        "full_covered": summary.full_covered,
        "games": [
            {
                "game_number": e.game_number,
                "home_team": e.home_team,
                "away_team": e.away_team,
                "our_prediction": [o.value for o in e.our_prediction],
                "selection_type": e.selection_type.value,
                "actual_result": e.actual_result.value,
                "correct": e.correct,
                "post_mortem": e.post_mortem,
            }
            for e in evaluations
        ],
    })
    _save_history(history)
    logger.info("Appended draw %d to performance history (%d total weeks)", draw_number, len(history))


# ---------------------------------------------------------------------------
# Monthly model improvement prompt (run manually: python main.py --improve)
# ---------------------------------------------------------------------------

def build_improvement_prompt() -> str:
    """
    Build a prompt that can be sent to Claude to analyse ALL historical
    predictions and identify systematic biases in our model.
    Pre-computes per-league accuracy, confidence calibration, and draw bias.
    """
    history = _load_all_history()
    if not history:
        return "No prediction history available yet."

    total_weeks = len(history)
    total_games = sum(w["total_games"] for w in history)
    total_correct = sum(w["total_correct"] for w in history)
    overall_accuracy = round(total_correct / total_games * 100, 1) if total_games else 0

    singles_c = sum(w["singles_correct"] for w in history)
    singles_t = sum(w["singles_total"] for w in history)
    doubles_c = sum(w["doubles_correct"] for w in history)
    doubles_t = sum(w["doubles_total"] for w in history)

    # --- Pre-computed analytics ---
    from collections import defaultdict

    # Confidence calibration bins
    conf_bins: dict[str, list[bool]] = defaultdict(list)
    # Result type tracking
    draw_stats = {"total": 0, "predicted": 0}
    # Selection type breakdown
    wrong_by_selection: dict[str, int] = defaultdict(int)

    wrong_games = []
    all_games = []
    for week in history:
        for g in week.get("games", []):
            all_games.append(g)
            conf = g.get("confidence", g.get("prediction", {}).get("confidence", 0.5))
            if isinstance(conf, (int, float)):
                bin_key = f"{int(conf * 10) / 10:.1f}"
                conf_bins[bin_key].append(g["correct"])

            if g["actual_result"] == "X":
                draw_stats["total"] += 1
                if "X" in g["our_prediction"]:
                    draw_stats["predicted"] += 1

            if not g["correct"]:
                if g["selection_type"] != "full":
                    wrong_games.append(g)
                    wrong_by_selection[g["selection_type"]] += 1

    # Format confidence calibration
    conf_report = []
    for bin_key in sorted(conf_bins.keys(), reverse=True):
        results = conf_bins[bin_key]
        correct = sum(results)
        total = len(results)
        pct = round(correct / total * 100, 1) if total else 0
        conf_report.append(f"  Confidence {bin_key}: {pct}% correct ({correct}/{total})")

    draw_coverage_pct = round(draw_stats["predicted"] / draw_stats["total"] * 100, 1) if draw_stats["total"] else 0

    prompt = f"""You are analysing the prediction performance of a Stryktipset betting newsletter system.

OVERALL STATS ({total_weeks} weeks, {total_games} games):
- Overall accuracy: {overall_accuracy}% ({total_correct}/{total_games})
- Singles accuracy: {round(singles_c/singles_t*100,1) if singles_t else 'N/A'}% ({singles_c}/{singles_t})
- Doubles accuracy: {round(doubles_c/doubles_t*100,1) if doubles_t else 'N/A'}% ({doubles_c}/{doubles_t})

CONFIDENCE CALIBRATION (is confidence well-calibrated?):
{chr(10).join(conf_report) if conf_report else '  No data yet'}

DRAW BIAS CHECK:
- Total draws in results: {draw_stats['total']}
- Draws we covered (in our selection): {draw_stats['predicted']}
- Draw coverage rate: {draw_coverage_pct}%
- (Draws typically occur 25-28% of the time in European football)

WRONG PREDICTIONS BY SELECTION TYPE:
- Singles wrong: {wrong_by_selection.get('single', 0)}
- Doubles wrong: {wrong_by_selection.get('double', 0)}

MISSED PREDICTIONS ({len(wrong_games)} total):
{json.dumps(wrong_games, indent=2, ensure_ascii=False)}

FULL WEEKLY HISTORY:
{json.dumps(history, indent=2, ensure_ascii=False)}

Please analyse this data and answer:
1. CONFIDENCE CALIBRATION: Are our confidence scores well-calibrated? (e.g., do games marked 0.9 win 90%?)
   What should our minimum single-selection confidence threshold be?
2. DRAW BIAS: Are we systematically under-covering draws? Should we include X more in doubles?
3. SIGNAL ANALYSIS: Which risk flags appeared most often in wrong predictions? What signals should we
   weight MORE heavily (form, odds, H2H, news)?
4. SELECTION STRATEGY: Should we be more conservative with singles? Are doubles dropping the wrong outcome?
5. LEAGUE PATTERNS: Do certain leagues consistently produce more upsets? Should we adjust by league?
6. SPECIFIC RECOMMENDATIONS: Concrete, actionable changes to improve prediction accuracy.

Be specific and data-driven. Reference actual games from the history where relevant."""

    return prompt
