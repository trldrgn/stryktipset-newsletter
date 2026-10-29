"""
FotMob — primary source for xG, absences, H2H, tables, referee and schedule.

FotMob's web app reads its data from public JSON endpoints (no key):
  GET /api/data/matches?date=YYYYMMDD   → every fixture on a date (resolve ids)
  GET /api/data/matchDetails?matchId=N  → unavailable players, H2H, referee, coach, xG
  GET /api/data/leagues?id=N            → tables: all / home / away / xg
  GET /api/data/teams?id=N              → squad (positions, goals) + all-competition fixtures

Unofficial and unversioned — the path has moved before (/api/leagues → /api/data/leagues).
Every response is checked for JSON and failures degrade per field: this module
only fills fields that are still empty, and every later fetcher (CSV, Understat,
FPL, Sofascore snapshot) also only fills what is still empty. So if FotMob
fails for one field, only that field falls back to the backup source.

Coverage (verified 2026-10-29, also from GitHub Actions runners): xG for PL,
Championship, League One, League Two, Allsvenskan and the Big 5. No xG for
Superettan or the National League.

Be polite: ~1 request/second, everything cached, only the coupon's 13 games.
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Callable, Optional

import requests

from models.match import (
    FormResult,
    H2HResult,
    InjuryStatus,
    Match,
    Outcome,
    PlayerAbsence,
    ScheduleContext,
    TeamStats,
    VenueRecord,
)
from utils.cache import get_cache
from utils.logger import get_logger

logger = get_logger(__name__)

_BASE_URL = "https://www.fotmob.com/api/data/"
_TIMEOUT = 20
_MIN_INTERVAL_S = 1.0           # polite gap between live requests
_MAX_CONSECUTIVE_FAILURES = 4   # then stop calling FotMob for the rest of the run

# Cache TTLs — availability must be fresh; finished matches never change
_TTL_LIVE = 3 * 3600            # fixtures list, pre-match details, tables, squads
_TTL_FINISHED = 60 * 24 * 3600  # matchDetails of finished games (rolling xG)

_MAX_INJURIES_PER_TEAM = 15     # safety ceiling only: every absent player reaches Claude and the reader
_FORM_GAMES = 5
_XG_GAMES = 5
_H2H_MAX = 5
_MIN_NAME_SCORE = 0.75

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "application/json",
})

_last_request_at = 0.0
_consecutive_failures = 0


class FotMobUnavailable(Exception):
    """Raised once FotMob has failed repeatedly — callers skip remaining calls."""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _get_json(url: str, params: dict[str, Any]) -> Optional[dict]:
    """One live request. Returns None on any non-JSON / non-200 response."""
    global _last_request_at, _consecutive_failures
    if _consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
        raise FotMobUnavailable(f"{_consecutive_failures} consecutive failures")

    wait = _MIN_INTERVAL_S - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()

    try:
        resp = _SESSION.get(url, params=params, timeout=_TIMEOUT)
        content_type = resp.headers.get("content-type", "")
        if resp.status_code != 200 or "json" not in content_type:
            # HTML here means a moved endpoint or a bot challenge — not data
            logger.warning(
                "FotMob %s %s → HTTP %d (%s)", url, params, resp.status_code, content_type,
            )
            _consecutive_failures += 1
            return None
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("FotMob %s %s failed: %s", url, params, e)
        _consecutive_failures += 1
        return None

    _consecutive_failures = 0
    return data if isinstance(data, dict) else None


def _cached_get(key: str, ttl: int, path: str, params: dict[str, Any]) -> Optional[dict]:
    """`path` is relative to /api/data/, or an absolute URL (data.fotmob.com stats)."""
    store = get_cache()
    value = store.get(key)
    if value is not None:
        return value
    url = path if path.startswith("https://") else _BASE_URL + path
    value = _get_json(url, params)
    if value is not None:
        store.set(key, value, expire=ttl)
    return value


def _fixtures_on(date_str: str) -> Optional[dict]:
    return _cached_get(f"fotmob_matches_{date_str}", _TTL_LIVE, "matches", {"date": date_str})


def _match_details(match_id: int, finished: bool = False) -> Optional[dict]:
    ttl = _TTL_FINISHED if finished else _TTL_LIVE
    prefix = "fotmob_md_final" if finished else "fotmob_md"
    return _cached_get(f"{prefix}_{match_id}", ttl, "matchDetails", {"matchId": match_id})


def _league(league_id: int) -> Optional[dict]:
    return _cached_get(f"fotmob_league_{league_id}", _TTL_LIVE, "leagues", {"id": league_id})


def _team(team_id: int) -> Optional[dict]:
    return _cached_get(f"fotmob_team_{team_id}", _TTL_LIVE, "teams", {"id": team_id})


# League-wide player stat files: (file name, key in player_index entries)
_PLAYER_STAT_FILES = (("goals.json", "goals"), ("goal_assist.json", "assists"))


def _league_player_stats(league: Optional[dict]) -> dict[str, dict]:
    """{'goals': stat file, 'assists': stat file} for a league; missing ones omitted.
    Squad goal counts are 0 for EFL teams, so these files are the reliable source."""
    out: dict[str, dict] = {}
    for entry in ((league or {}).get("stats") or {}).get("players") or []:
        url = entry.get("fetchAllUrl") or ""
        for file_name, key in _PLAYER_STAT_FILES:
            if url.endswith("/" + file_name) and key not in out:
                data = _cached_get(f"fotmob_stat_{url}", _TTL_LIVE, url, {})
                if data:
                    out[key] = data
    return out


# ---------------------------------------------------------------------------
# Team-name resolution (Svenska Spel name → FotMob fixture)
# ---------------------------------------------------------------------------

_DROP_TOKENS = {"fc", "afc", "cf", "sc", "the"}
_TOKEN_ALIASES = {"utd": "united", "man": "manchester", "nottm": "nottingham", "sheff": "sheffield"}
_NAME_ALIASES = {
    "qpr": "queens park rangers",
    "wolves": "wolverhampton wanderers",
    "athletic bilbao": "athletic club",
    # Svenska Spel names national teams in Swedish (Nations League / qualifiers)
    "spanien": "spain", "tyskland": "germany", "italien": "italy", "frankrike": "france",
    "nederlanderna": "netherlands", "belgien": "belgium", "skottland": "scotland",
    "schweiz": "switzerland", "sverige": "sweden", "norge": "norway", "danmark": "denmark",
    "osterrike": "austria", "turkiet": "turkey", "grekland": "greece", "polen": "poland",
    "tjeckien": "czechia", "kroatien": "croatia", "serbien": "serbia", "rumanien": "romania",
    "ungern": "hungary", "irland": "ireland", "nordirland": "northern ireland",
    "cypern": "cyprus", "bulgarien": "bulgaria", "ukraina": "ukraine", "slovenien": "slovenia",
    "slovakien": "slovakia", "nordmakedonien": "north macedonia", "island": "iceland",
    "albanien": "albania", "bosnien och hercegovina": "bosnia and herzegovina",
    "montenegro": "montenegro", "georgien": "georgia", "armenien": "armenia",
    "azerbajdzjan": "azerbaijan", "litauen": "lithuania", "lettland": "latvia",
    "estland": "estonia", "vitryssland": "belarus", "moldavien": "moldova",
}


# Letters NFKD can't strip to ASCII ('Bodø' would otherwise lose its 'o')
_FOLD = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "ß": "ss", "ł": "l", "Ł": "L", "đ": "d"})


def _tokens(name: str) -> list[str]:
    text = unicodedata.normalize("NFKD", name.translate(_FOLD)).encode("ascii", "ignore").decode().lower()
    text = text.replace("&", " and ").replace("'", "")
    text = _NAME_ALIASES.get(text.strip(), text)
    tokens = [_TOKEN_ALIASES.get(t, t) for t in re.split(r"[^a-z0-9]+", text) if t]
    return [t for t in tokens if t not in _DROP_TOKENS]


def _name_score(ours: str, theirs: str) -> float:
    """1.0 exact; 0.9 when every token of the shorter name prefixes a token of the
    longer ('Hull' ~ 'Hull City', 'Sheffield W' ~ 'Sheffield Wednesday'); else a
    fuzzy ratio over the tokens that DIFFER — so a shared city name can't make
    'Manchester City' look like 'Manchester United'."""
    a, b = _tokens(ours), _tokens(theirs)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if all(any(t.startswith(s) for t in long_) for s in short):
        return 0.9
    only_a = " ".join(t for t in a if t not in b)
    only_b = " ".join(t for t in b if t not in a)
    return SequenceMatcher(None, only_a, only_b).ratio()


def _team_names(side: dict) -> list[str]:
    return [n for n in (side.get("name"), side.get("longName"), side.get("shortName")) if n]


def _best_fixture(match: Match, day: dict) -> Optional[tuple[float, int]]:
    best: Optional[tuple[float, int]] = None
    for league in day.get("leagues", []):
        for fx in league.get("matches", []):
            home = max((_name_score(match.home_team, n) for n in _team_names(fx.get("home", {}))), default=0)
            away = max((_name_score(match.away_team, n) for n in _team_names(fx.get("away", {}))), default=0)
            score = min(home, away)
            if score >= _MIN_NAME_SCORE and (best is None or score > best[0]):
                best = (score, int(fx["id"]))
    return best


def _resolve_fixture_ids(matches: list[Match]) -> dict[int, int]:
    """game_number → FotMob match id. Tries the kickoff date, then ±1 day."""
    days: dict[str, Optional[dict]] = {}
    resolved: dict[int, int] = {}
    for match in matches:
        if match.kickoff is None:
            continue
        best: Optional[tuple[float, int]] = None
        for offset in (0, -1, 1):
            date_str = (match.kickoff + timedelta(days=offset)).strftime("%Y%m%d")
            if date_str not in days:
                days[date_str] = _fixtures_on(date_str)
            if days[date_str] and (cand := _best_fixture(match, days[date_str])):
                if best is None or cand[0] > best[0]:
                    best = cand
            if best and best[0] >= 0.9:
                break
        if best:
            resolved[match.game_number] = best[1]
        else:
            logger.warning("FotMob: no fixture found for game %d %s vs %s",
                           match.game_number, match.home_team, match.away_team)
    return resolved


# ---------------------------------------------------------------------------
# Parsing helpers (pure — unit tested against saved responses)
# ---------------------------------------------------------------------------

def _parse_utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_score(score_str: Optional[str]) -> Optional[tuple[int, int]]:
    if not score_str:
        return None
    m = re.match(r"\s*(\d+)\s*-\s*(\d+)", score_str)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _is_friendly(fixture: dict) -> bool:
    return "friendl" in ((fixture.get("tournament") or {}).get("name") or "").lower()


def player_index(team: Optional[dict], team_id: int, stat_files: dict[str, dict]) -> dict[int, dict]:
    """
    player id → {"position", "goals", "assists"} for one team. Positions come
    from the squad; goals/assists from the league stat files when present
    (squad counts are 0 outside the PL), else from the squad.
    """
    index: dict[int, dict] = {}
    for g in ((team or {}).get("squad") or {}).get("squad") or []:
        if g.get("title") == "coach":
            continue
        for m in g.get("members", []):
            if "id" in m:
                index[m["id"]] = {
                    "position": (m.get("positionIdsDesc") or "").split(",")[0],
                    "goals": m.get("goals") or 0,
                    "assists": m.get("assists") or 0,
                }

    for key, data in stat_files.items():
        rows = [
            r for top in data.get("TopLists") or [] for r in top.get("StatList") or []
            if r.get("TeamId") == team_id
        ]
        for entry in index.values():
            entry[key] = 0   # league file is authoritative: not listed = 0 this season
        for r in rows:
            pid = r.get("ParticipantId") or r.get("ParticiantId")   # FotMob's own typo
            entry = index.setdefault(pid, {"position": "", "goals": 0, "assists": 0})
            entry[key] = int(r.get("StatValue") or 0)
    return index


def _absence_status(unavailability: dict, kickoff: Optional[datetime]) -> InjuryStatus:
    expected = (unavailability.get("expectedReturn") or "").lower()
    if expected == "doubtful":
        return InjuryStatus.DOUBT
    if expected == "back in training":
        return InjuryStatus.FIFTY_FIFTY
    back = _parse_utc(unavailability.get("expectedReturnDate"))
    if back and kickoff and back <= kickoff:
        return InjuryStatus.DOUBT   # due back before kickoff, but still listed
    return InjuryStatus.OUT


def parse_absences(
    lineup_team: Optional[dict], players: dict[int, dict], kickoff: Optional[datetime],
) -> Optional[tuple[list[PlayerAbsence], list[PlayerAbsence]]]:
    """
    (injuries, suspensions) from matchDetails lineup.{home,away}Team.unavailable.
    `players` comes from player_index().

    None when the key is missing — that is "no data", not "fully fit". Observed
    2026-10-29: FotMob omits the key both for leagues it doesn't track
    (Superettan, National League, League Two) and for some tracked teams with
    nobody out, so a missing key can never be read as "fully fit".
    """
    if not lineup_team or "unavailable" not in lineup_team:
        return None

    # A "top scorer" with 1 goal is one of many tied (Spurs: 7 scorers on 1) — not a signal
    top_goals = max((p["goals"] for p in players.values()), default=0)
    top_assists = max((p["assists"] for p in players.values()), default=0)
    top_goals = top_goals if top_goals >= 2 else -1
    top_assists = top_assists if top_assists >= 2 else -1

    ranked: list[tuple[tuple[int, int], PlayerAbsence, str]] = []
    for p in lineup_team.get("unavailable") or []:
        u = p.get("unavailability") or {}
        info = players.get(p.get("id"), {})
        g, a = info.get("goals", 0), info.get("assists", 0)
        absence = PlayerAbsence(
            player_name=p.get("name", "Unknown"),
            position=info.get("position", ""),
            status=_absence_status(u, kickoff),
            season_goals=g,
            season_assists=a,
            is_top_scorer=g == top_goals,
            is_top_assister=a == top_assists,
            source="fotmob",
            expected_return=u.get("expectedReturn") or "",
        )
        ranked.append(((g + a, p.get("marketValue") or 0), absence, u.get("type") or "injury"))

    ranked.sort(key=lambda r: r[0], reverse=True)
    injuries = [a for _, a, kind in ranked if kind != "suspension"][:_MAX_INJURIES_PER_TEAM]
    suspensions = [a for _, a, kind in ranked if kind == "suspension"]
    return injuries, suspensions


def _table_rows(league: Optional[dict], key: str) -> list[dict]:
    """Rows of one table tab ('all' / 'home' / 'away' / 'xg'); handles grouped leagues."""
    rows: list[dict] = []
    for block in (league or {}).get("table") or []:
        data = block.get("data") or {}
        tables = [data] if "table" in data else data.get("tables") or []
        for t in tables:
            rows.extend((t.get("table") or {}).get(key) or [])
    return rows


def _row_for(rows: list[dict], team_id: int) -> Optional[dict]:
    return next((r for r in rows if r.get("id") == team_id or r.get("teamId") == team_id), None)


def parse_venue_record(row: Optional[dict]) -> Optional[VenueRecord]:
    if not row:
        return None
    score = _parse_score(row.get("scoresStr"))
    if score is None:
        return None
    return VenueRecord(
        played=row.get("played", 0), wins=row.get("wins", 0), draws=row.get("draws", 0),
        losses=row.get("losses", 0), goals_for=score[0], goals_against=score[1],
        points=row.get("pts", 0), rank=row.get("idx"),
    )


def competitive_results(team: Optional[dict], team_id: int, before: datetime) -> list[dict]:
    """Finished non-friendly fixtures before `before`, newest first."""
    fixtures = (((team or {}).get("fixtures") or {}).get("allFixtures") or {}).get("fixtures") or []
    out = []
    for f in fixtures:
        status = f.get("status") or {}
        when = _parse_utc(status.get("utcTime"))
        if not status.get("finished") or status.get("cancelled") or _is_friendly(f) or when is None:
            continue
        if when < before:
            out.append(f)
    out.sort(key=lambda f: f["status"]["utcTime"], reverse=True)
    return out


def _to_form_result(f: dict, team_id: int) -> Optional[FormResult]:
    home, away = f.get("home") or {}, f.get("away") or {}
    score = _parse_score((f.get("status") or {}).get("scoreStr"))
    if score is None:
        return None
    is_home = home.get("id") == team_id
    gf, ga = (score[0], score[1]) if is_home else (score[1], score[0])
    result = Outcome.HOME if gf > ga else Outcome.DRAW if gf == ga else Outcome.AWAY
    return FormResult(
        opponent=(away if is_home else home).get("name", "?"),
        home_or_away="H" if is_home else "A",
        goals_for=gf, goals_against=ga, result=result,
        competition=(f.get("tournament") or {}).get("name", ""),
    )


def parse_form(results: list[dict], team_id: int, venue: Optional[str] = None) -> list[FormResult]:
    form: list[FormResult] = []
    for f in results:
        r = _to_form_result(f, team_id)
        if r and (venue is None or r.home_or_away == venue):
            form.append(r)
        if len(form) == _FORM_GAMES:
            break
    return form


def parse_schedule(
    team: Optional[dict], team_id: int, kickoff: datetime, league_id: Optional[int] = None,
) -> Optional[ScheduleContext]:
    """
    Rest days and congestion across ALL competitions (cups and Europe included).
    upcoming_big_match flags a cup/European game within 4 days — the rotation
    risk — not a routine midweek game in the match's own league.
    """
    results = competitive_results(team, team_id, kickoff)
    if not results:
        return None
    last = results[0]
    last_at = _parse_utc(last["status"]["utcTime"])
    window = kickoff - timedelta(days=14)

    upcoming = ""
    fixtures = (((team or {}).get("fixtures") or {}).get("allFixtures") or {}).get("fixtures") or []
    for f in fixtures:
        when = _parse_utc((f.get("status") or {}).get("utcTime"))
        tournament = f.get("tournament") or {}
        if (
            when and kickoff + timedelta(hours=3) < when <= kickoff + timedelta(days=4)
            and not _is_friendly(f) and tournament.get("leagueId") != league_id
        ):
            days = (when - kickoff).days or 1
            upcoming = f"{tournament.get('name', '?')} in {days}d"
            break

    return ScheduleContext(
        days_since_last_match=(kickoff - last_at).days if last_at else None,
        last_match_competition=(last.get("tournament") or {}).get("name", ""),
        matches_last_14_days=sum(
            1 for f in results if (w := _parse_utc(f["status"]["utcTime"])) and w >= window
        ),
        upcoming_big_match=upcoming,
    )


def parse_match_xg(details: Optional[dict], team_id: int) -> Optional[tuple[float, float]]:
    """(xG for, xG against) for `team_id` from a finished game's matchDetails."""
    if not details:
        return None
    general = details.get("general") or {}
    is_home = (general.get("homeTeam") or {}).get("id") == team_id
    if not is_home and (general.get("awayTeam") or {}).get("id") != team_id:
        return None
    periods = (((details.get("content") or {}).get("stats") or {}).get("Periods") or {})
    for group in (periods.get("All") or {}).get("stats") or []:
        for item in group.get("stats") or []:
            values = item.get("stats") or []
            if item.get("key") == "expected_goals" and len(values) == 2 and None not in values:
                home_xg, away_xg = float(values[0]), float(values[1])
                return (home_xg, away_xg) if is_home else (away_xg, home_xg)
    return None


def parse_h2h(h2h: Optional[dict], home_team: str, away_team: str, home_id: Optional[int]) -> list[H2HResult]:
    """Finished meetings (all competitions except friendlies), newest first."""
    out: list[H2HResult] = []
    for m in (h2h or {}).get("matches") or []:
        status = m.get("status") or {}
        score = _parse_score(status.get("scoreStr"))
        when = _parse_utc((m.get("time") or {}).get("utcTime"))
        league = (m.get("league") or {}).get("name", "")
        if not status.get("finished") or score is None or when is None or "friendl" in league.lower():
            continue
        was_home = str((m.get("home") or {}).get("id")) == str(home_id)
        out.append(H2HResult(
            date=when.date().isoformat(),
            home_team=home_team if was_home else away_team,
            away_team=away_team if was_home else home_team,
            home_goals=score[0], away_goals=score[1],
            competition=league,
        ))
    out.sort(key=lambda r: r.date, reverse=True)
    return out[:_H2H_MAX]


def parse_referee(details: Optional[dict]) -> tuple[str, Optional[float], Optional[float]]:
    """
    (name, yellows per match, reds per match) from matchFacts.infoBox.Referee.
    FotMob gives yellows per match but reds only as a total, so reds are
    divided by the referee's match count.
    """
    ref = ((((details or {}).get("content") or {}).get("matchFacts") or {}).get("infoBox") or {}).get("Referee") or {}
    name = ref.get("text") or ""
    stats = {s.get("type"): s for s in ref.get("stats") or []}

    def per_match(kind: str) -> Optional[float]:
        s = stats.get(kind)
        if not s or s.get("value") is None:
            return None
        if s.get("valueType") == "perMatch":
            return round(float(s["value"]), 2)
        matches = (stats.get("matches") or {}).get("value")
        return round(float(s["value"]) / matches, 2) if matches else None

    return name, per_match("yellowCards"), per_match("redCards")


# ---------------------------------------------------------------------------
# Field fillers — each fills one field only if empty, and fails alone
# ---------------------------------------------------------------------------

def _fill(field: str, coverage: Counter, fn: Callable[[], bool]) -> None:
    try:
        if fn():
            coverage[field] += 1
    except FotMobUnavailable:
        raise
    except Exception as e:
        logger.warning("FotMob: %s failed: %s", field, e)


def _fill_absences(stats: TeamStats, lineup_team: Optional[dict], players: dict[int, dict],
                   kickoff: Optional[datetime]) -> bool:
    if stats.injuries or stats.suspensions or stats.absences_checked_source:
        return False
    parsed = parse_absences(lineup_team, players, kickoff)
    if parsed is None:
        return False
    stats.injuries, stats.suspensions = parsed
    stats.absences_checked_source = "fotmob"
    return True


def _fill_table(stats: TeamStats, league: Optional[dict], team_id: int) -> bool:
    filled = False
    row = _row_for(_table_rows(league, "all"), team_id)
    if row and stats.league_position is None:
        stats.league_position, stats.league_points = row.get("idx"), row.get("pts")
        filled = True
    if stats.home_record is None:
        stats.home_record = parse_venue_record(_row_for(_table_rows(league, "home"), team_id))
        filled |= stats.home_record is not None
    if stats.away_record is None:
        stats.away_record = parse_venue_record(_row_for(_table_rows(league, "away"), team_id))
        filled |= stats.away_record is not None
    return filled


def _fill_season_xg(stats: TeamStats, league: Optional[dict], team_id: int) -> bool:
    if stats.season_xg_for is not None:
        return False
    row = _row_for(_table_rows(league, "xg"), team_id)
    if not row or row.get("xg") is None:
        return False
    stats.season_played = row.get("played")
    stats.season_xg_for = round(row["xg"], 2)
    stats.season_xg_against = round(row.get("xgConceded") or 0.0, 2)
    stats.season_xpoints = round(row.get("xPoints") or 0.0, 1)
    return True


def _fill_form(stats: TeamStats, results: list[dict], team_id: int) -> bool:
    if stats.form_last5:
        return False
    stats.form_last5 = parse_form(results, team_id)
    if not stats.form_last5_home_only:
        stats.form_last5_home_only = parse_form(results, team_id, venue="H")
    if not stats.form_last5_away_only:
        stats.form_last5_away_only = parse_form(results, team_id, venue="A")
    return bool(stats.form_last5)


def _fill_schedule(stats: TeamStats, team: Optional[dict], team_id: int,
                   kickoff: Optional[datetime], league_id: Optional[int]) -> bool:
    if stats.schedule is not None or kickoff is None:
        return False
    stats.schedule = parse_schedule(team, team_id, kickoff, league_id)
    return stats.schedule is not None


def _fill_rolling_xg(stats: TeamStats, results: list[dict], team_id: int) -> bool:
    """Last-5 xG from finished matchDetails (cached long — they never change)."""
    if stats.xg_for_avg is not None:
        return False
    from fetchers.xg_collector import TeamXGProfile

    xgf: list[float] = []
    xga: list[float] = []
    gf: list[int] = []
    ga: list[int] = []
    for f in results[: _XG_GAMES + 3]:   # a few spare: some cup games have no xG
        xg = parse_match_xg(_match_details(int(f["id"]), finished=True), team_id)
        form = _to_form_result(f, team_id)
        if xg is None or form is None:
            continue
        xgf.append(xg[0]); xga.append(xg[1])
        gf.append(form.goals_for); ga.append(form.goals_against)
        if len(xgf) == _XG_GAMES:
            break
    if len(xgf) < 3:
        return False

    n = len(xgf)
    profile = TeamXGProfile(
        team_name=stats.team_name, matches_available=n,
        xg_for_5g=round(sum(xgf) / n, 2), xg_against_5g=round(sum(xga) / n, 2),
        goals_for_5g=round(sum(gf) / n, 2), goals_against_5g=round(sum(ga) / n, 2),
    )
    stats.xg_for_avg, stats.xg_against_avg = profile.xg_for_5g, profile.xg_against_5g
    stats._xg_profile = profile
    return True


def _fill_manager(stats: TeamStats, lineup_team: Optional[dict]) -> bool:
    coach = (lineup_team or {}).get("coach") or {}
    if stats.manager_name or not coach.get("name"):
        return False
    stats.manager_name = coach["name"]
    return True


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

_FIELDS = (
    "absences", "xg_5g", "season_xg", "table", "form", "schedule", "manager", "h2h", "referee",
)


def _enrich_match(match: Match, fotmob_id: int, coverage: Counter) -> None:
    details = _match_details(fotmob_id)
    if not details:
        return
    general = details.get("general") or {}
    content = details.get("content") or {}
    league_id = general.get("parentLeagueId") or general.get("leagueId")
    league = _league(int(league_id)) if league_id else None
    kickoff = match.kickoff or _parse_utc(general.get("matchTimeUTCDate"))
    lineup = content.get("lineup") or {}
    try:
        stat_files = _league_player_stats(league)
    except FotMobUnavailable:
        raise
    except Exception as e:   # only costs the goals/assists tags on absences
        logger.warning("FotMob: league player stats failed: %s", e)
        stat_files = {}

    for side in ("home", "away"):
        attr = f"{side}_stats"
        team_name = match.home_team if side == "home" else match.away_team
        stats: Optional[TeamStats] = getattr(match, attr)
        if stats is None:
            stats = TeamStats(team_name=team_name)
            setattr(match, attr, stats)

        team_id = (general.get(f"{side}Team") or {}).get("id")
        if team_id is None:
            continue
        team_id = int(team_id)
        stats.team_id = stats.team_id or team_id
        team = _team(team_id)
        lineup_team = lineup.get(f"{side}Team")
        results = competitive_results(team, team_id, kickoff) if kickoff else []

        _fill("absences", coverage, lambda: _fill_absences(
            stats, lineup_team, player_index(team, team_id, stat_files), kickoff))
        _fill("table", coverage, lambda: _fill_table(stats, league, team_id))
        _fill("season_xg", coverage, lambda: _fill_season_xg(stats, league, team_id))
        _fill("form", coverage, lambda: _fill_form(stats, results, team_id))
        _fill("schedule", coverage, lambda: _fill_schedule(
            stats, team, team_id, kickoff, int(league_id) if league_id else None))
        _fill("xg_5g", coverage, lambda: _fill_rolling_xg(stats, results, team_id))
        _fill("manager", coverage, lambda: _fill_manager(stats, lineup_team))

    def _h2h() -> bool:
        if match.h2h:
            return False
        home_id = (general.get("homeTeam") or {}).get("id")
        match.h2h = parse_h2h(content.get("h2h"), match.home_team, match.away_team, home_id)
        return bool(match.h2h)

    def _referee() -> bool:
        if match.referee:
            return False
        match.referee, yellows, reds = parse_referee(details)
        match.referee_yellows_avg, match.referee_reds_avg = yellows, reds
        if yellows is not None:
            match.referee_avg_cards = round(yellows + (reds or 0), 2)
        return bool(match.referee)

    _fill("h2h", coverage, _h2h)
    _fill("referee", coverage, _referee)


def enrich_with_fotmob(matches: list[Match]) -> list[Match]:
    """
    Fill every field FotMob has, per field and only where still empty.
    Later fetchers fill whatever is left, so a failure here costs one field,
    not the whole source. Logs a coverage line so silent gaps are visible.
    """
    global _consecutive_failures
    _consecutive_failures = 0
    coverage: Counter = Counter()
    resolved: dict[int, int] = {}

    try:
        resolved = _resolve_fixture_ids(matches)
        for match in matches:
            fotmob_id = resolved.get(match.game_number)
            if fotmob_id is None:
                continue
            try:
                _enrich_match(match, fotmob_id, coverage)
            except FotMobUnavailable:
                raise
            except Exception as e:
                logger.warning("FotMob: game %d failed: %s", match.game_number, e)
    except FotMobUnavailable as e:
        logger.error("FotMob unreachable (%s) — remaining fields fall back to backup sources", e)

    teams, games = 2 * len(matches), len(matches)
    per_team = {"absences", "xg_5g", "season_xg", "table", "form", "schedule", "manager"}
    parts = [f"fixtures {len(resolved)}/{games}"] + [
        f"{f} {coverage[f]}/{teams if f in per_team else games}" for f in _FIELDS
    ]
    log = logger.info if len(resolved) == games else logger.warning
    if not resolved and matches:
        log = logger.error
    log("FotMob coverage: %s", " | ".join(parts))
    return matches
