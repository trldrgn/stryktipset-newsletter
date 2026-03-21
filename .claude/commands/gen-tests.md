Generate pytest unit tests for the module specified in $ARGUMENTS.

First, read the target file thoroughly. Then read any related files it imports from `models/` to understand the data structures. Check `tests/` for existing test patterns and `tests/fixtures/` for available JSON fixtures.

Generate a complete test file that covers:

## What to test

### Happy path
- Normal successful execution with realistic input data
- Verify the return type matches the declared return annotation
- Verify key fields are populated correctly on output dataclasses

### Edge cases specific to this project
- **Empty API responses** (`{"response": []}`) — should return empty list/None, not crash
- **Missing optional fields** in API JSON (e.g. no `injuries`, no `xG`, no `startOdds`) — should not KeyError
- **Swedish decimal strings** (`"2,55"`) converted correctly to float `2.55`
- **Draw with no open games** (Svenska Spel returns empty `draws` array)
- **API-Football 100 req/day limit hit** — should raise RuntimeError or log and continue gracefully
- **Perplexity API HTTP error** — should return fallback NewsContext with "unavailable" message, not crash pipeline
- **Claude returns malformed JSON** — `claude_analyst.py` should raise ValueError, not silently produce empty report
- **No previous predictions file** — evaluator should return None, not crash
- **Fixture ID not found** — `find_fixture_id()` should return None, not raise

### Coupon math (if testing coupon_optimizer.py)
- 13 predictions → exactly 4 singles, 8 doubles, 1 full
- Total rows = 1^4 × 2^8 × 3^1 = 768
- Highest 4 confidence scores become singles
- Lowest confidence score becomes full
- If Claude returns 1 outcome for a double game, optimizer correctly adds a second outcome from market data

### Data model properties (if testing models/match.py)
- `TeamStats.fatigue_flag` is True when days_since_last_match <= 3
- `TeamStats.fatigue_flag` is True when matches_last_14_days >= 4
- `TeamStats.new_manager_bounce` is True when manager_weeks_in_post <= 8
- `TeamStats.critical_absences` only returns players with high/critical matchup risk OR top scorer/assister flags
- `MarketSignals.implied_prob_home` = 1/odds, rounds to 4 decimal places
- `WeeklyEvaluation.accuracy_pct` calculates correctly

## Test structure requirements

```python
# Standard imports
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Always mock external HTTP — never make real calls in tests
# Use responses library or unittest.mock.patch for requests.get/post

# Load fixtures from tests/fixtures/ where available
FIXTURES_DIR = Path(__file__).parent / "fixtures"
```

- Use `pytest.fixture` for shared setup
- Use `@pytest.mark.parametrize` for multiple input variants (e.g. different outcome strings)
- Mock `requests.get` / `requests.post` with saved JSON from `tests/fixtures/`
- If a fixture JSON doesn't exist yet, create a minimal realistic one and save it to `tests/fixtures/`
- Use `tmp_path` fixture for any tests that write to disk (cache, predictions, results)
- Group tests in classes by method/function being tested

## Output
Write the complete test file to `tests/test_{module_name}.py`.
After writing, run `pytest tests/test_{module_name}.py -v` to verify all tests pass.
Fix any failures before finishing.
