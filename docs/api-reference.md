# API Reference

All external APIs used by the Stryktipset Newsletter pipeline.

---

## 1. API-Football (api-sports.io)

**Base URL:** `https://v3.football.api-sports.io`
**Auth:** `x-apisports-key` header
**Docs:** https://www.api-football.com/documentation-v3

### Free Tier Constraints

| Constraint | Limit |
|---|---|
| Requests per day | **100** (hard cap, resets at midnight UTC) |
| Requests per minute | **10** |
| Historical data | Current season only |
| Date queries | Last **2-3 days** only (older dates return access error) |
| League+date combo | **Not supported** on free tier (requires season param) |
| `last` parameter | Inconsistent on free tier — use `from`/`to` date ranges instead |
| `season` parameter | Some endpoints block explicit season on free tier |
| Live scores | Not available |
| Leagues | All 800+ leagues available (no league restriction, only volume + season limits) |
| Data freshness | Updates within ~15 min of real events |
| Upgrade path | Pro plan (~$20/month) → 7,500 req/day + all seasons |

### Response Format

Every response follows this structure:
```json
{
  "get": "fixtures",
  "parameters": { ... },
  "errors": [],
  "results": 10,
  "paging": { "current": 1, "total": 1 },
  "response": [ ... ]
}
```

- `errors` can be `[]` or `{"rateLimit": "..."}` — our code handles both
- Response headers include `x-ratelimit-requests-limit` and `x-ratelimit-requests-remaining`
- Null values are common (goals, stats fields can be `null`, not `0`)
- All dates in UTC unless `timezone` param specified
- Fixture statuses: `NS` (not started), `FT` (finished), `PST` (postponed), `CANC` (cancelled)

### Endpoints We Use

#### `GET /fixtures`
Find fixtures by date, team, or league.

```
# By date (returns ALL global fixtures — we filter client-side)
/fixtures?date=2026-03-20

# By H2H pair
/fixtures/headtoheads?h2h={team1_id}-{team2_id}&last=5
```

**Free tier gotcha:** `?date=X&league=Y` requires a `season` param which then ignores the date filter. Our workaround: query by date only, filter by league ID in Python.

**Free tier gotcha:** Date queries only work for the last 2-3 days. Older dates return `"Free plans do not have access to this date."` The xG collector handles this gracefully and stops.

**Budget:** ~5 calls per pipeline run (batch by kickoff date).

#### `GET /fixtures/statistics`
Per-fixture team statistics including **expected goals (xG)**.

```
/fixtures/statistics?fixture={fixture_id}
```

Returns statistics array per team. Key stat: `"type": "expected_goals"` with float value.
Used by the xG collector for Championship/League One/Allsvenskan.

**Budget:** ~24 calls per xG collection run (12 Championship + 12 League One fixtures).

#### `GET /fixtures/headtoheads`
Head-to-head history between two teams.

```
/fixtures/headtoheads?h2h={team1_id}-{team2_id}&last=5
```

**Budget:** 13 calls (one per match).

#### `GET /standings`
League table for a given league and season.

```
/standings?league={league_id}&season={year}
```

Cached per league — multiple matches in the same league share one call.

**Budget:** ~5 calls per run.

#### `GET /injuries`
Current injuries/suspensions for a fixture.

```
/injuries?fixture={fixture_id}
```

**Budget:** 13 calls per run.

#### `GET /players`
Season statistics for specific players (used for injury impact assessment).

```
/players?id={player_id}&season={year}
```

Only called for players flagged as key absences (top scorer, set piece taker, etc.).

**Budget:** ~10 calls per run (only for impactful absences).

### Our API-Football League IDs

| League | ID | xG Source |
|---|---|---|
| Premier League | 39 | Understat |
| Championship | 40 | API-Football collector |
| League One | 41 | API-Football collector |
| Bundesliga | 78 | Understat |
| La Liga | 140 | Understat |
| Serie A | 135 | Understat |
| Ligue 1 | 61 | Understat |
| Allsvenskan | 113 | API-Football collector |

### Total Budget Per Saturday Pipeline

| Step | Calls |
|---|---|
| Fixture search (by date) | ~5 |
| H2H per match | 13 |
| Standings (cached per league) | ~5 |
| Injuries per fixture | 13 |
| Player stats (key absences only) | ~10 |
| **Total** | **~46-59** |

The xG collector runs separately (Tue/Fri) using its own daily quota: ~31 calls per run.

### Endpoints We Don't Use (But Could)

| Endpoint | Cost | Notes |
|---|---|---|
| `GET /predictions` | 13 calls | API-Football's own win probability — could cross-reference with Claude |
| `GET /fixtures/events` | 13 calls | Goals, cards, subs — more detail than we currently need |
| `GET /teams/statistics` | ~5 calls | Season aggregates (form string, clean sheets) — partly covered by Football-Data.org |
| `GET /fixtures/lineups` | 13 calls | Starting XI — **always empty at 08:00**, populates ~60 min before kickoff |

---

## 2. Football-Data.org

**Base URL:** `https://api.football-data.org/v4`
**Auth:** `X-Auth-Token` header
**Docs:** https://www.football-data.org/documentation/quickstart

### Free Tier Constraints

| Constraint | Limit |
|---|---|
| Requests per minute | **10** |
| Competitions | [12 leagues](https://www.football-data.org/coverage) — all major European |
| Seasons | Current season only |
| Update frequency | ~15 min after full time |

### Endpoints We Use

#### `GET /competitions/{code}/standings`
Full league table with position, points, wins, draws, losses, goals for/against.

```
/competitions/PL/standings
/competitions/ELC/standings
```

#### `GET /competitions/{code}/matches?status=FINISHED&limit=100`
Recent results — used to compute last-5 form and days-since-last-match.

### Competition Codes

| League | Code |
|---|---|
| Premier League | PL |
| Championship | ELC |
| League One | EL1 |
| League Two | EL2 |
| Bundesliga | BL1 |
| 2. Bundesliga | BL2 |
| La Liga | PD |
| Serie A | SA |
| Ligue 1 | FL1 |
| Eredivisie | DED |
| Primeira Liga | PPL |

### Budget Per Run

~10-15 calls (standings + matches per league, cached 7 days).
Rate limited to 6.5s between calls.

---

## 3. Understat

**Base URL:** https://understat.com (web scraping)
**Auth:** None (public website)
**Python library:** `understatapi` (sync client)

### What It Provides

- **Per-match xG** — the strongest predictive signal we have
- xG for (expected goals scored) and xG against (expected goals conceded)
- Last 5 completed matches per team
- Computed: overperformance (actual goals minus xG)

### Coverage

| League | Understat Code |
|---|---|
| Premier League | EPL |
| La Liga | La_Liga |
| Bundesliga | Bundesliga |
| Serie A | Serie_A |
| Ligue 1 | Ligue_1 |

**NOT covered:** Championship, League One, Allsvenskan, Eredivisie, Primeira Liga — these use our API-Football xG collector instead.

### Rate Limiting

1.5s between requests (polite scraping, not an official API).
Responses cached 7 days.

### Budget Per Run

~20-30 calls (league team lists + per-team match data, many cached).

---

## 4. Perplexity Sonar

**Base URL:** `https://api.perplexity.ai`
**Auth:** `Authorization: Bearer {key}` header
**Model:** `sonar` (cheapest tier)

### What It Provides

Real-time web search for each match, prioritized:
1. **Confirmed injuries/suspensions** (critical)
2. **Rotation/fatigue risk** (important)
3. **Manager press conference quotes** (nice-to-have)
4. **Tactical/morale context** (nice-to-have)

### Constraints

- One query per match = **13 calls per run**
- NOT cached (news must be fresh)
- Rate limited: 2s between calls
- Parallelized: 4 workers
- Max tokens per response: 800

### Cost

Sonar is the cheapest Perplexity model. 13 queries per week is minimal usage.

---

## 5. Claude (Anthropic)

**Base URL:** `https://api.anthropic.com`
**Auth:** `x-api-key` header
**Model:** `claude-sonnet-4-6` (configurable via `CLAUDE_MODEL`)

### How We Use It

**One call per pipeline run** — all 13 matches sent in a single prompt.

- Extended thinking enabled (10,000 token budget)
- Temperature: 1 (required when thinking is enabled)
- Max output tokens: 16,000
- Response format: JSON (parsed by `claude_analyst.py`)

### Output Structure

Returns `WeeklyReport` JSON with per-match:
- Predicted outcome (1/X/2) with confidence 0.0-1.0
- Alternative outcomes ranked by likelihood
- Reasoning text
- Key factors

### Cost

One Sonnet call with ~8-12K input tokens + 10K thinking + up to 16K output.
Roughly $0.10-0.20 per weekly run.

---

## 6. Svenska Spel

**Base URL:** `https://api.spela.svenskaspel.se/draw/1/stryktipset`
**Auth:** None (public API)

### Endpoints

#### `GET /draws/upcoming`
Returns the current week's Stryktipset draw with all 13 matches, odds, and kickoff times.

#### `GET /draws/{draw_number}`
Returns a specific draw by number (used with `--draw` flag).

### Response Data

- Draw number, close time
- 13 matches with: home team, away team, league, country, kickoff time
- Svenska Spel odds (1/X/2 distribution)

No rate limiting. No authentication. Always returns current season data.

---

## 7. Gmail SMTP

**Host:** `smtp.gmail.com:587` (STARTTLS)
**Auth:** App Password (16-character code from Google Account)

### Setup

1. Create a dedicated Gmail account
2. Enable 2-Step Verification at myaccount.google.com → Security
3. Generate App Password at Security → App Passwords → Mail
4. Set `GMAIL_SENDER` and `GMAIL_APP_PASSWORD` in `.env`

### Limits

- Gmail SMTP: 500 emails/day for regular accounts
- We send to a small subscriber list — well within limits

---

## Data Flow Summary

```
Saturday 08:00 Stockholm (pipeline):
  Svenska Spel → 13 matches
  API-Football → H2H, injuries, standings        (~59 calls)
  Football-Data.org → form, league position       (~15 calls)
  Understat → xG for Big 5 leagues                (~25 calls)
  xG History → xG for Championship/League One     (local JSON, no API calls)
  Perplexity → live news per match                (13 calls)
  Claude → analysis + predictions                 (1 call)
  Gmail → send newsletter                         (1 call)

Tuesday + Friday 20:00 UTC (xG collector):
  API-Football → fixture discovery + xG stats     (~31 calls)
  → Saves to data/xg/xg_history.json
```

---

## Environment Variables

| Variable | Required | Used By |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Claude analysis |
| `PERPLEXITY_API_KEY` | Yes | Match news |
| `API_FOOTBALL_KEY` | Yes | Stats + xG collector |
| `FOOTBALL_DATA_API_KEY` | No* | Form + standings |
| `GMAIL_SENDER` | Yes | Email sending |
| `GMAIL_APP_PASSWORD` | Yes | Email sending |
| `NEWSLETTER_RECIPIENTS` | Yes | Comma-separated email list |
| `CLAUDE_MODEL` | No | Default: `claude-sonnet-4-6` |
| `CLAUDE_MAX_TOKENS` | No | Default: `16000` |
| `CLAUDE_THINKING_BUDGET` | No | Default: `10000` |
| `PERPLEXITY_MODEL` | No | Default: `sonar` |
| `CACHE_TTL_SECONDS` | No | Default: 604800 (7 days) |

*Football-Data.org enrichment is skipped gracefully if key is empty.

---

## GitHub Actions Secrets

Add these in repo Settings → Secrets and variables → Actions:

```
ANTHROPIC_API_KEY
PERPLEXITY_API_KEY
API_FOOTBALL_KEY
FOOTBALL_DATA_API_KEY
GMAIL_SENDER
GMAIL_APP_PASSWORD
NEWSLETTER_RECIPIENTS
```
