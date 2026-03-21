Review the Python code in $ARGUMENTS for quality, correctness, and alignment with this project's conventions.

Read the file(s) specified, then evaluate against these criteria:

## Pythonic Quality
- Are type hints present on all function signatures and return types?
- Are dataclasses used instead of plain dicts for structured data crossing module boundaries?
- Is `match`/`case` used where appropriate (Python 3.12)?
- Are list/dict comprehensions used where they improve readability (but not at the cost of clarity)?
- Are f-strings used consistently?
- Is there any dead code, unused imports, or unreachable branches?

## Project Conventions
- Does the module call `get_logger(__name__)` from `utils/logger.py`?
- Are API keys read from `config.py`, never hardcoded?
- Do any new API-Football calls use the `@cached(...)` decorator?
- Does the code fail gracefully — catching exceptions and returning partial data rather than crashing the pipeline?
- Are prompts to Claude kept compact (no unnecessary verbosity that wastes tokens)?

## Error Handling
- Are HTTP errors caught specifically (`requests.HTTPError`) before generic `Exception`?
- Is there appropriate logging at each catch site (not silent swallowing)?
- Are there any places where a crash in one match's processing would kill the whole 13-match run?

## Security
- No secrets, API keys, or tokens in code or log output
- No shell injection risks (f-strings in subprocess calls etc.)
- User-controlled input sanitised before use in API calls

## API Budget Awareness (critical)
- If this touches API-Football: does it respect the 100 req/day limit?
- Are new calls going through the cache decorator?
- Could any new call cause duplicate requests for the same data (e.g. fetching standings per match instead of per league)?

## Suggestions
For each issue found: show the problematic code, explain why it's a problem, and provide a corrected version.
Separate issues into: **Must fix** (bugs, security, API budget) and **Should fix** (style, conventions).
Do not suggest changes that add complexity without clear benefit.
