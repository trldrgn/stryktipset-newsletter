"""
Evaluates last week's predictions against the actual Stryktipset results.

Two purposes:
  1. SHORT-TERM: Build a feedback_summary fed into Claude's prompt this week
     so the model adjusts based on recent mistakes.

  2. LONG-TERM: Append to data/performance/history.json — a growing dataset
     of every prediction, all signals used, and the outcome. This can be fed
     to a monthly "model improvement" prompt to identify systematic biases.

Run flow:
  - Load last week's saved prediction file (data/predictions/*.json)
  - Fetch actual results from Svenska Spel API
  - Score: correct / wrong per game
  - Annotate each wrong prediction with what signal may have caused the error
  - Save annotated results to data/results/
  - Append summary to data/performance/history.json
  - Return WeeklyEvaluation for the newsletter and Claude context
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

def save_predictions(draw_number: int, draw_date: str, predictions: list[MatchPrediction]) -> Path:
    """
    Persist this week's predictions to disk before results come in.
    Called at the end of the newsletter run.
    """
    filename = PREDICTIONS_DIR / f"draw_{draw_number}_{draw_date}.json"
    payload = {
        "draw_number": draw_number,
        "draw_date": draw_date,
        "saved_at": datetime.now(timezone.utc).isoformat(),
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
    Generate a brief automatic post-mortem hint based on what we know
    about the prediction. Claude will later enrich this with real reasoning.
    """
    predicted = pred["predicted_outcomes"]
    selection = pred["selection_type"]
    confidence = pred.get("confidence", 0)
    risk_flags = pred.get("risk_flags", [])

    hint_parts = [
        f"Predicted {'/'.join(predicted)} ({selection}) but result was {actual.value}."
    ]

    if confidence >= 0.8 and selection == "single":
        hint_parts.append("High-confidence single — overconfidence warning.")
    if risk_flags:
        hint_parts.append(f"Risk flags were noted: {'; '.join(risk_flags[:2])}.")
        hint_parts.append("Review whether these flags should have downgraded this to a double.")

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

    # --- Build lessons for Claude's next prompt ---
    wrong = [e for e in evaluations if not e.correct and e.selection_type != SelectionType.FULL]
    lessons = []

    if wrong:
        for e in wrong:
            lessons.append(
                f"Game {e.game_number} ({e.home_team} vs {e.away_team}): "
                f"predicted {'/'.join(o.value for o in e.our_prediction)} "
                f"({e.selection_type.value}), actual was {e.actual_result.value}. "
                f"{e.post_mortem}"
            )

    if evaluation.singles_accuracy_pct < 50 and singles_total > 0:
        lessons.append(
            f"Singles accuracy was only {evaluation.singles_accuracy_pct}% — "
            "be more conservative with single selections this week."
        )

    evaluation.lessons = lessons
    evaluation.feedback_summary = _build_feedback_summary(evaluation)

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


def _build_feedback_summary(ev: WeeklyEvaluation) -> str:
    """
    Compact summary injected into Claude's system prompt this week.
    """
    lines = [
        f"LAST WEEK (Draw {ev.draw_number}): {ev.total_correct}/{len(ev.evaluations)} correct "
        f"({ev.accuracy_pct}%). "
        f"Singles: {ev.singles_correct}/{ev.singles_total}. "
        f"Doubles: {ev.doubles_correct}/{ev.doubles_total}. "
        f"Full: covered ({'yes' if ev.full_covered else 'no'}).",
    ]

    wrong = [e for e in ev.evaluations if not e.correct and e.selection_type != SelectionType.FULL]
    if wrong:
        lines.append("Missed predictions:")
        for e in wrong:
            lines.append(
                f"  - Game {e.game_number} {e.home_team} vs {e.away_team}: "
                f"tipped {'/'.join(o.value for o in e.our_prediction)}, "
                f"result was {e.actual_result.value}. {e.post_mortem}"
            )

    return "\n".join(lines)


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

    wrong_games = []
    for week in history:
        for g in week.get("games", []):
            if not g["correct"] and g["selection_type"] != "full":
                wrong_games.append(g)

    prompt = f"""You are analysing the prediction performance of a Stryktipset betting newsletter system.

OVERALL STATS ({total_weeks} weeks, {total_games} games):
- Overall accuracy: {overall_accuracy}% ({total_correct}/{total_games})
- Singles accuracy: {round(singles_c/singles_t*100,1) if singles_t else 'N/A'}% ({singles_c}/{singles_t})
- Doubles accuracy: {round(doubles_c/doubles_t*100,1) if doubles_t else 'N/A'}% ({doubles_c}/{doubles_t})

MISSED PREDICTIONS ({len(wrong_games)} total):
{json.dumps(wrong_games, indent=2, ensure_ascii=False)}

FULL WEEKLY HISTORY:
{json.dumps(history, indent=2, ensure_ascii=False)}

Please analyse this data and answer:
1. What patterns exist in our wrong predictions? (leagues, home/away bias, confidence levels)
2. Are we systematically over-confident on singles? What should our single-selection threshold be?
3. Which types of games (by league, motivation, odds range) do we consistently get wrong?
4. What signals should we weight MORE heavily in our analysis?
5. What signals are we likely ignoring or under-weighting?
6. Specific recommendations to improve our prediction accuracy.

Be specific and data-driven. Reference actual games from the history where relevant."""

    return prompt
