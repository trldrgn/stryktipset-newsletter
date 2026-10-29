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
