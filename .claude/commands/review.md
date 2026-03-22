Review the Python code in $ARGUMENTS for quality, correctness, and alignment with this project's conventions.

If $ARGUMENTS is empty, review all recently changed Python files (use `git diff --name-only HEAD~3` to find them).

Read each file thoroughly before evaluating. Also read `models/match.py` for context on data structures, and `config.py` for configuration patterns.

## Pythonic Quality
- Are type hints present on all function signatures and return types?
- Is `from __future__ import annotations` at the top of each module?
- Are dataclasses used instead of plain dicts for structured data crossing module boundaries?
- Are list/dict comprehensions used where they improve readability (but not at the cost of clarity)?
- Are f-strings used consistently?
- Is there any dead code, unused imports, or unreachable branches?

## Project Conventions
- Does the module call `get_logger(__name__)` from `utils/logger.py`?
- Are API keys read from `config.py`, never hardcoded?
- Do any new API-Football calls use the `@cached(...)` decorator?
- Does the code fail gracefully — catching exceptions and returning partial data rather than crashing the pipeline?
- Are prompts to Claude kept compact (no unnecessary verbosity that wastes tokens)?
- Are directory paths imported from `config.py` (PREDICTIONS_DIR, NEWSLETTERS_DIR, etc.), not hardcoded?

## Error Handling
- Are HTTP errors caught specifically (`requests.HTTPError`) before generic `Exception`?
- Is there appropriate logging at each catch site (not silent swallowing)?
- Are there any places where a crash in one match's processing would kill the whole 13-match run?
- Are file I/O operations using `encoding="utf-8"` explicitly?

## Security
- No secrets, API keys, or tokens in code or log output
- No shell injection risks (f-strings in subprocess calls etc.)
- User-controlled input sanitised before use in API calls

## API Budget Awareness (critical)
- If this touches API-Football: does it respect the 100 req/day limit?
- Are new calls going through the cache decorator?
- Could any new call cause duplicate requests for the same data (e.g. fetching standings per match instead of per league)?

## Data Integrity
- Are prediction files and performance history written atomically (not corrupted on crash)?
- Are JSON files written with `ensure_ascii=False` for proper Unicode handling?
- Are Optional fields checked before access (e.g. `match.kickoff` could be None)?

## Output
For each issue found: show the file, line number, problematic code, explain why it's a problem, and provide a corrected version.
Separate issues into: **Must fix** (bugs, security, API budget, data loss) and **Should fix** (style, conventions).
Do not suggest changes that add complexity without clear benefit.
