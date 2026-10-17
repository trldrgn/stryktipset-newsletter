"""Stale xG history must not be presented as current form."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fetchers.xg_collector as xgc


def _fixture(days_ago: int, home: str = "Derby", away: str = "Stoke") -> dict:
    d = (datetime.now(timezone.utc) - timedelta(days=days_ago)).date().isoformat()
    return {"date": d, "home_team": home, "away_team": away,
            "home_xg": 1.5, "away_xg": 0.8, "home_goals": 2, "away_goals": 1}


def _history(*fixtures: dict) -> dict:
    return {"fixtures": {str(i): f for i, f in enumerate(fixtures)}, "last_updated": None}


def test_stale_history_returns_none():
    old = _history(_fixture(170), _fixture(177), _fixture(184))
    with patch.object(xgc, "_load_history", return_value=old):
        assert xgc.get_team_xg_profile("Derby") is None


def test_recent_history_returns_profile_and_drops_old():
    mixed = _history(_fixture(5), _fixture(12), _fixture(200, home="Stoke", away="Derby"))
    with patch.object(xgc, "_load_history", return_value=mixed):
        profile = xgc.get_team_xg_profile("Derby")
    assert profile is not None
    assert profile.matches_available == 2
    assert profile.xg_for_5g == 1.5
