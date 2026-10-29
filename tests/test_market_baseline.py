"""Tests for the odds-only 4 singles / 8 doubles / 1 full baseline coupon."""

from analysis.evaluator import market_baseline_correct
from models.match import Outcome


def _odds(fav: float) -> tuple[float, float, float]:
    """Home favourite at `fav`, draw second, away third."""
    return (fav, 3.4, 5.0)


def test_baseline_allocation_and_scoring():
    # Games 1-4 have the strongest favourites (singles), 5-12 doubles, 13 full
    odds = {g: _odds(1.2 + 0.1 * g) for g in range(1, 14)}
    results = {g: Outcome.HOME for g in range(1, 14)}
    results[1] = Outcome.DRAW      # single on 1 misses
    results[5] = Outcome.DRAW      # double 1X hits
    results[6] = Outcome.AWAY      # double 1X misses
    results[13] = Outcome.AWAY     # full always hits
    assert market_baseline_correct(odds, results) == 11


def test_baseline_needs_complete_data():
    odds = {g: _odds(1.5) for g in range(1, 14)}
    results = {g: Outcome.HOME for g in range(1, 13)}   # game 13 missing
    assert market_baseline_correct(odds, results) is None
    odds[2] = (None, 3.4, 5.0)
    assert market_baseline_correct(odds, {g: Outcome.HOME for g in range(1, 14)}) is None
