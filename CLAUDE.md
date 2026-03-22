# Stryktipset Newsletter — Claude Code Guide

## What this project is

An automated Python app that runs every Saturday at 08:00 Europe/Stockholm. It fetches the week's Stryktipset coupon (13 Swedish football pool matches), enriches each game with structured stats and live news, sends everything to Claude for editorial analysis, then emails a premium HTML newsletter to subscribers.

The long-term goal is to **beat bookmaker odds** through systematic signal tracking — not gambling luck. Every prediction is stored and evaluated so the model gets smarter over time.

---

## Architecture at a glance

```
Svenska Spel API → 13 Match objects
       ↓
API-Football (100 req/day free tier) → H2H, injuries, standings
       ↓
Football-Data.org (free tier) → form, league position, points (PL/Championship/etc.)
       ↓
Understat (free, web scraping) → xG for/against averages (Big 5 leagues)
       ↓
Perplexity Sonar (13 queries, parallel) → injury news, rotation risk, press conferences
       ↓
Claude API (1 call, extended thinking) → analysis + predictions JSON
       ↓
Coupon Optimizer → 4 singles / 8 doubles / 1 full = 768 SEK
       ↓
Jinja2 template → HTML email → Gmail SMTP → subscribers
       ↓
Evaluator → score last week → append to data/performance/history.json
```

**Key file map:**
| Concern | File |
|---|---|
| All data models | `models/match.py` |
| Config / env | `config.py` |
| Svenska Spel fetch | `fetchers/svenska_spel.py` |
| API-Football fetch | `fetchers/api_football.py` |
| Football-Data.org | `fetchers/football_data.py` |
| Understat xG | `fetchers/understat_xg.py` |
| Perplexity news | `fetchers/perplexity.py` |
| Claude analysis | `analysis/claude_analyst.py` |
| Coupon allocation | `analysis/coupon_optimizer.py` |
| Weekly scoring | `analysis/evaluator.py` |
| HTML template | `email_sender/templates/report.html` |
| Gmail send | `email_sender/gmail.py` |
| xG collector (lower leagues) | `fetchers/xg_collector.py` |
| Orchestrator + scheduler | `main.py` |
| API documentation | `docs/api-reference.md` |

---

## Running the app

```bash
# Activate venv first
source .venv/Scripts/activate   # Windows
source .venv/bin/activate       # Mac/Linux

# Test run — saves HTML locally, no email sent
python main.py --dry-run

# Test a specific draw number
python main.py --dry-run --draw 4945

# Run the full pipeline and send email
python main.py --run

# Start the weekly scheduler (every Saturday 08:00 Stockholm)
python main.py

# Print model improvement analysis prompt (use monthly)
python main.py --improve

# Collect xG data for last 7 days (run Tue + Fri, or automated via GitHub Actions)
python main.py --collect-xg

# Backfill xG data for last 5 weeks (first-time setup)
python main.py --backfill-xg
```

---

## API constraints — always respect these

### API-Football (free tier)
- **Hard limit: 100 requests/day** — enforced in `fetchers/api_football.py` via `_request_count`
- All responses are **disk-cached** (7 days TTL) via `utils/cache.py` — never bypass the cache
- Current budget per run: ~60–80 calls. Do not add new endpoints without checking the budget
- If you're adding a new API-Football call, use the `@cached(...)` decorator

### Football-Data.org (free tier)
- **Rate limit: 10 req/min** — enforced via 6.5s sleep between calls
- Provides: standings, form (last 5), league position, points
- Covers: PL, Championship, League One, League Two, Bundesliga, 2.Bundesliga, La Liga, Serie A, Ligue 1, Eredivisie, Primeira Liga
- Auth: `X-Auth-Token` header
- Responses cached (7 days TTL)

### Understat (free, web scraping)
- Provides: **xG per match** — the strongest predictive signal we have
- Covers Big 5 only: EPL, La Liga, Bundesliga, Serie A, Ligue 1
- NOT covered: Championship, League One, Eredivisie, Primeira Liga
- Rate limit: 1.5s between requests (polite scraping)
- Responses cached (7 days TTL)
- Python library: `understatapi`

### Claude API
- **One call per pipeline run** (all 13 matches in one prompt) — do not add more calls
- Model: `claude-sonnet-4-6` (set in `.env` / `CLAUDE_MODEL`)
- **Extended thinking enabled** (10k token budget) — improves confidence calibration
- Response must be JSON — the parser in `claude_analyst.py` depends on this

### Perplexity Sonar
- **One query per match** = 13 calls per run (parallelized, 4 workers)
- Rate limited: 2s between calls with thread-safe lock
- Use `sonar` model (cheapest tier) — do not upgrade without cost analysis
- Results are NOT cached (must be fresh each run)
- Focused on: injuries, rotation risk, press conferences (xG now from Understat)

### Gmail
- SMTP via App Password (no Google Cloud Console needed)
- Sender: dedicated Gmail account with 2-Step Verification + App Password

---

## Data models

All dataclasses live in `models/match.py`. Before modifying any fetcher or analyser, read this file — every field is documented.

Key classes:
- `Match` — one game, populated in 5 stages (Svenska Spel → API-Football → Football-Data.org → Understat → Perplexity)
- `TeamStats` — form, standings, xG, injuries, schedule for one team
- `PlayerAbsence` — injured/suspended player with impact assessment and `MatchupRisk`
- `MatchPrediction` — Claude's output for one game
- `WeeklyReport` — full week's predictions + coupon allocation
- `WeeklyEvaluation` — scored last week, used as Claude context + newsletter Section 1

---

## Injury analysis philosophy

Don't just flag "player is injured". Evaluate **impact**:
- Is this player the top scorer, top assister, set piece taker?
- What is the replacement quality?
- Does the opponent have a player who directly exploits the positional gap?
  (e.g. missing LB + opponent has dominant top-scoring RW = `RiskLevel.CRITICAL`)

The `MatchupRisk` dataclass captures this. The `_assess_matchup_risk()` function in `api_football.py` handles the positional matchup logic. Extend the `exploit_map` dict there to add more position pairs.

---

## Coupon system

13 games, 1 SEK per row:
- **4 singles** (1 outcome) — top 4 by Claude confidence score
- **8 doubles** (2 outcomes) — middle 8
- **1 full** (3 outcomes) — least confident game

Total: 1⁴ × 2⁸ × 3¹ = **768 SEK**

Logic in `analysis/coupon_optimizer.py`. Confidence scores come from Claude's JSON response (`"confidence": 0.0–1.0`).

---

## Prediction evaluation and model improvement

Every run saves predictions to `data/predictions/draw_{N}_{date}.json`.
The next run scores them against results and appends to `data/performance/history.json`.

This history is the most valuable asset in the project. It allows `python main.py --improve` to generate a prompt that identifies systematic biases (leagues we're bad at, signals we underweight, overconfident singles etc).

**Never delete `data/performance/history.json`.**

---

## Development conventions

- **Python 3.12** — use `match` statements, `|` union types, `datetime.fromisoformat()`
- **Type hints everywhere** — all function signatures must be typed
- **Dataclasses over dicts** — if data crosses module boundaries, it should be a dataclass
- **Logging over print** — use `get_logger(__name__)` from `utils/logger.py`
- **No hardcoded API keys** — always read from `config.py` which reads from `.env`
- **Fail gracefully** — fetchers must catch exceptions and return partial data, not crash the pipeline
- **Keep Claude's prompt compact** — token cost matters. `_format_match_block()` in `claude_analyst.py` controls what Claude sees. Don't bloat it.

---

## Testing

```bash
source .venv/Scripts/activate
pytest tests/ -v
```

- Tests live in `tests/`
- Saved API response fixtures are in `tests/fixtures/*.json`
- Mock all external HTTP calls with `pytest-mock` / `responses`
- Never make real API calls in tests
- Use `/gen-tests` slash command to generate tests for a module

---

## Custom slash commands

| Command | What it does |
|---|---|
| `/review` | Pythonic code review of changed files |
| `/review-email` | Email template + Jinja2 UX review |
| `/gen-tests` | Generate pytest tests for a module |

---

## Environment setup

**Local development:**
```bash
cp .env.example .env
# Fill in all keys. For Gmail:
#   1. Create a dedicated Gmail account
#   2. Enable 2-Step Verification → myaccount.google.com → Security
#   3. Generate App Password → Security → App Passwords → Mail
#   4. Paste the 16-char code as GMAIL_APP_PASSWORD
python main.py --dry-run   # test without sending email
```

**GitHub Actions (production):**
Add these to repo Settings → Secrets and variables → Actions:
```
ANTHROPIC_API_KEY
PERPLEXITY_API_KEY
API_FOOTBALL_KEY
FOOTBALL_DATA_API_KEY
GMAIL_SENDER
GMAIL_APP_PASSWORD
NEWSLETTER_RECIPIENTS
```

**Workflows:**
| Workflow | Schedule | What it does |
|---|---|---|
| `newsletter.yml` | Saturday 07:00 UTC | Full pipeline → send newsletter → commit predictions |
| `xg-collector.yml` | Tue + Fri 20:00 UTC | Collect xG data for Championship/League One/Allsvenskan |

Both workflows commit data back to the repo automatically.
Both support `workflow_dispatch` for manual triggering.

**Detailed API docs:** See `docs/api-reference.md`

Secrets that must never be committed: `.env`
