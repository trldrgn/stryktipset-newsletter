"""A forced re-run of the current draw must evaluate LAST week once — never itself, never twice."""

import json

import pytest

import analysis.evaluator as ev
from models.match import WeeklyEvaluation


@pytest.fixture
def pred_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "PREDICTIONS_DIR", tmp_path)
    for draw, date in ((4973, "2026-10-03"), (4974, "2026-10-10"), (4975, "2026-10-17")):
        (tmp_path / f"draw_{draw}_{date}.json").write_text(
            json.dumps({"draw_number": draw, "draw_date": date, "predictions": []}), encoding="utf-8",
        )
    return tmp_path


def test_latest_without_limit_is_newest(pred_dir):
    assert ev.load_latest_predictions()[0] == 4975


def test_before_draw_skips_current_draw(pred_dir):
    assert ev.load_latest_predictions(before_draw=4975)[0] == 4974


def test_before_draw_with_no_older_files(pred_dir):
    assert ev.load_latest_predictions(before_draw=4973) is None


def test_append_to_history_replaces_same_draw(tmp_path, monkeypatch):
    history_file = tmp_path / "history.json"
    history_file.write_text(json.dumps([
        {"draw_number": 4973, "total_correct": 11},
        {"draw_number": 4974, "total_correct": 9},
    ]), encoding="utf-8")
    monkeypatch.setattr(ev, "HISTORY_FILE", history_file)

    summary = WeeklyEvaluation(draw_number=4974, draw_date="2026-10-10", total_correct=10)
    ev._append_to_history(4974, "2026-10-10", [], summary)

    history = json.loads(history_file.read_text(encoding="utf-8"))
    assert [w["draw_number"] for w in history] == [4973, 4974]
    assert history[1]["total_correct"] == 10
