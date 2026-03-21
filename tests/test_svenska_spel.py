"""Tests for fetchers/svenska_spel.py — coupon fetch and result parsing."""

from unittest.mock import MagicMock, patch

import pytest

from fetchers.svenska_spel import (
    _sv_float,
    _parse_kickoff,
    fetch_coupon,
    fetch_result,
    get_current_draw_number,
)
from models.match import Outcome


class TestSvFloat:
    """Swedish decimal string conversion."""

    @pytest.mark.parametrize("raw,expected", [
        ("2,55", 2.55),
        ("1,00", 1.00),
        ("10,50", 10.50),
        ("3,0", 3.0),
    ])
    def test_valid_conversions(self, raw, expected):
        assert _sv_float(raw) == pytest.approx(expected)

    def test_returns_none_for_empty_string(self):
        assert _sv_float("") is None

    def test_returns_none_for_none(self):
        assert _sv_float(None) is None

    def test_returns_none_for_invalid_string(self):
        assert _sv_float("N/A") is None


class TestParseKickoff:
    def test_parses_swedish_timezone(self):
        dt = _parse_kickoff("2026-03-22T15:00:00+01:00")
        assert dt is not None
        # Should be converted to UTC: 15:00 CET = 14:00 UTC
        assert dt.hour == 14
        assert dt.minute == 0

    def test_returns_none_for_none(self):
        assert _parse_kickoff(None) is None

    def test_returns_none_for_invalid(self):
        assert _parse_kickoff("not a date") is None


class TestFetchCoupon:
    def test_returns_13_matches(self, svenska_spel_draw_response):
        mock_resp = MagicMock()
        mock_resp.json.return_value = svenska_spel_draw_response
        mock_resp.raise_for_status.return_value = None

        with patch("fetchers.svenska_spel._SESSION") as mock_session:
            mock_session.get.return_value = mock_resp
            matches = fetch_coupon(4945)

        assert len(matches) == 13

    def test_match_fields_populated(self, svenska_spel_draw_response):
        mock_resp = MagicMock()
        mock_resp.json.return_value = svenska_spel_draw_response
        mock_resp.raise_for_status.return_value = None

        with patch("fetchers.svenska_spel._SESSION") as mock_session:
            mock_session.get.return_value = mock_resp
            matches = fetch_coupon(4945)

        first = matches[0]
        assert first.game_number == 1
        assert first.draw_number == 4945
        assert first.home_team == "HomeTeam1"
        assert first.away_team == "AwayTeam1"
        assert first.league == "Premier League"

    def test_market_signals_populated(self, svenska_spel_draw_response):
        mock_resp = MagicMock()
        mock_resp.json.return_value = svenska_spel_draw_response
        mock_resp.raise_for_status.return_value = None

        with patch("fetchers.svenska_spel._SESSION") as mock_session:
            mock_session.get.return_value = mock_resp
            matches = fetch_coupon(4945)

        m = matches[0].market
        assert m is not None
        assert m.odds_home == pytest.approx(2.10)
        assert m.odds_draw == pytest.approx(3.40)
        assert m.odds_away == pytest.approx(3.80)
        assert m.public_pct_home == 55
        assert m.newspaper_tips_home == 7

    def test_raises_on_api_error(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"draw": {}, "error": "DrawNotFound", "drawEvents": []}
        mock_resp.raise_for_status.return_value = None

        with patch("fetchers.svenska_spel._SESSION") as mock_session:
            mock_session.get.return_value = mock_resp
            with pytest.raises(ValueError, match="error"):
                fetch_coupon(9999)

    def test_raises_when_no_events(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "draw": {"drawNumber": 4945, "drawEvents": []},
            "error": None,
        }
        mock_resp.raise_for_status.return_value = None

        with patch("fetchers.svenska_spel._SESSION") as mock_session:
            mock_session.get.return_value = mock_resp
            with pytest.raises(ValueError, match="No events"):
                fetch_coupon(4945)

    def test_games_sorted_by_event_number(self, svenska_spel_draw_response):
        mock_resp = MagicMock()
        mock_resp.json.return_value = svenska_spel_draw_response
        mock_resp.raise_for_status.return_value = None

        with patch("fetchers.svenska_spel._SESSION") as mock_session:
            mock_session.get.return_value = mock_resp
            matches = fetch_coupon(4945)

        game_numbers = [m.game_number for m in matches]
        assert game_numbers == sorted(game_numbers)


class TestFetchResult:
    def test_returns_outcome_dict(self, svenska_spel_result_response):
        mock_resp = MagicMock()
        mock_resp.json.return_value = svenska_spel_result_response
        mock_resp.raise_for_status.return_value = None

        with patch("fetchers.svenska_spel._SESSION") as mock_session:
            mock_session.get.return_value = mock_resp
            results = fetch_result(4944)

        assert len(results) == 13
        assert all(isinstance(v, Outcome) for v in results.values())

    def test_outcome_values_are_valid(self, svenska_spel_result_response):
        mock_resp = MagicMock()
        mock_resp.json.return_value = svenska_spel_result_response
        mock_resp.raise_for_status.return_value = None

        with patch("fetchers.svenska_spel._SESSION") as mock_session:
            mock_session.get.return_value = mock_resp
            results = fetch_result(4944)

        for game_num, outcome in results.items():
            assert outcome in (Outcome.HOME, Outcome.DRAW, Outcome.AWAY)

    def test_handles_missing_outcome_gracefully(self):
        """If a game has no outcome yet (in-progress), it is skipped, not crashed."""
        response = {
            "draw": {
                "drawNumber": 4944,
                "drawEvents": [
                    {"eventNumber": 1, "outcome": "1"},
                    {"eventNumber": 2, "outcome": ""},   # no result yet
                    {"eventNumber": 3, "outcome": "X"},
                ],
            },
            "error": None,
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = response
        mock_resp.raise_for_status.return_value = None

        with patch("fetchers.svenska_spel._SESSION") as mock_session:
            mock_session.get.return_value = mock_resp
            results = fetch_result(4944)

        assert 1 in results
        assert 2 not in results   # missing outcome is skipped
        assert 3 in results
        assert results[3] == Outcome.DRAW


class TestGetCurrentDrawNumber:
    def test_returns_draw_number_from_open_draws(self):
        open_draws_resp = MagicMock()
        open_draws_resp.json.return_value = {
            "draws": [{"draw": {"drawNumber": 4946}}]
        }
        open_draws_resp.raise_for_status.return_value = None

        with patch("fetchers.svenska_spel._SESSION") as mock_session:
            mock_session.get.return_value = open_draws_resp
            draw_number = get_current_draw_number()

        assert draw_number == 4946

    def test_falls_back_to_result_plus_one_when_no_open_draws(self):
        draws_resp = MagicMock()
        draws_resp.json.return_value = {"draws": []}  # no open draws
        draws_resp.raise_for_status.return_value = None

        result_resp = MagicMock()
        result_resp.json.return_value = {
            "draw": {"drawNumber": 4944, "drawEvents": []}
        }
        result_resp.raise_for_status.return_value = None

        with patch("fetchers.svenska_spel._SESSION") as mock_session:
            mock_session.get.side_effect = [draws_resp, result_resp]
            draw_number = get_current_draw_number()

        assert draw_number == 4945  # 4944 + 1
