"""Tests for the review-then-send flow: --send-archived sends the reviewed HTML once."""

from unittest.mock import patch

import main


def _archive(tmp_path, draw: int = 4977) -> None:
    (tmp_path / f"draw_{draw}_2026-10-29.html").write_text("<p>reviewed</p>", encoding="utf-8")
    (tmp_path / f"draw_{draw}_2026-10-29.subject.txt").write_text("Stryktipset v.44 #4977", encoding="utf-8")


def test_sends_reviewed_html_with_saved_subject_once(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "NEWSLETTERS_DIR", tmp_path)
    _archive(tmp_path)
    with patch.object(main, "send_newsletter", return_value=3) as send:
        assert main.send_archived(4977) == 3
        send.assert_called_once_with("Stryktipset v.44 #4977", "<p>reviewed</p>")
        assert (tmp_path / "draw_4977_2026-10-29.sent").exists()

        assert main.send_archived(4977) == 0          # second send refused
        assert send.call_count == 1
        assert main.send_archived(4977, force=True) == 3
        assert send.call_count == 2


def test_nothing_archived_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "NEWSLETTERS_DIR", tmp_path)
    with patch.object(main, "send_newsletter") as send:
        assert main.send_archived(4977) == 0
        send.assert_not_called()


def test_failed_send_leaves_no_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "NEWSLETTERS_DIR", tmp_path)
    _archive(tmp_path)
    with patch.object(main, "send_newsletter", return_value=0):
        assert main.send_archived(4977) == 0
    assert not (tmp_path / "draw_4977_2026-10-29.sent").exists()


# ---------------------------------------------------------------------------
# Re-render from saved predictions (no Claude call)
# ---------------------------------------------------------------------------

import json

import analysis.evaluator as ev
from models.match import Outcome


def _save_prediction_file(path, draw=4977):
    preds = [{"game_number": n, "home_team": f"H{n}", "away_team": f"A{n}",
              "predicted_outcomes": o, "selection_type": t, "confidence": 0.7,
              "key_factors": ["k"], "risk_flags": ["r"], "analysis": "text", "value_note": ""}
             for n, o, t in [(1, ["1"], "single"), (2, ["1", "X"], "double"), (3, ["1", "X", "2"], "full")]]
    (path / f"draw_{draw}_2026-10-29.json").write_text(json.dumps({
        "draw_number": draw, "draw_date": "2026-10-29", "saved_at": "2026-10-29T17:31:14+00:00",
        "executive_summary": "summary", "value_radar": ["v"], "predictions": preds}), encoding="utf-8")


def test_load_saved_report_round_trips_picks(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "PREDICTIONS_DIR", tmp_path)
    _save_prediction_file(tmp_path)
    report = ev.load_saved_report(4977)
    assert [p.predicted_outcomes for p in report.predictions] == [
        [Outcome.HOME], [Outcome.HOME, Outcome.DRAW], [Outcome.HOME, Outcome.DRAW, Outcome.AWAY]]
    assert (report.singles, report.doubles, report.full_game) == ([1], [2], 3)
    assert report.total_cost_sek == 2 * 3                    # 2^1 doubles x 3^1 full
    assert report.executive_summary == "summary" and ev.load_saved_report(1) is None


def test_load_saved_evaluation_uses_history_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "RESULTS_DIR", tmp_path)
    (tmp_path / "draw_4976_2026-10-24.json").write_text(json.dumps({"draw_number": 4976, "draw_date": "2026-10-24",
        "results": [{"game_number": 1, "home_team": "H", "away_team": "A", "our_prediction": ["1"],
                     "selection_type": "single", "actual_result": "1", "correct": True}]}), encoding="utf-8")
    monkeypatch.setattr(ev, "_load_all_history", lambda: [{"draw_number": 4976, "market_correct": 8}])
    e = ev.load_saved_evaluation(4976)
    assert (e.total_correct, e.singles_correct, e.market_correct) == (1, 1, 8)
    assert ev.latest_evaluated_draw_before(4977) == 4976 and ev.latest_evaluated_draw_before(4976) is None


def test_rerender_overwrites_archive_and_emails_only_reviewer(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "NEWSLETTERS_DIR", tmp_path)
    _archive(tmp_path)
    with patch.object(main, "load_saved_report", return_value="report"), \
         patch.object(main, "fetch_coupon", return_value=[]), \
         patch.object(main, "enrich_matches", return_value=[]), \
         patch.object(main, "latest_evaluated_draw_before", return_value=None), \
         patch.object(main, "render_newsletter", return_value=("Subject v2", "<p>v2</p>")), \
         patch.object(main, "send_newsletter") as send:
        path = main.rerender_newsletter(4977, review_to="me@example.com")
    assert path.read_text(encoding="utf-8") == "<p>v2</p>"
    assert path.with_suffix(".subject.txt").read_text(encoding="utf-8") == "Subject v2"
    send.assert_called_once_with("[REVIEW] Subject v2", "<p>v2</p>", recipients=["me@example.com"])
