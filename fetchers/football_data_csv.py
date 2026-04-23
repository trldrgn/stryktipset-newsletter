"""
Fetches season results from football-data.co.uk CSV files and computes
standings, form, and match-stat averages for every Stryktipset league.

Covers leagues that Football-Data.org's free API does NOT:
  - League One, League Two (English lower leagues)
  - Also covers PL, Championship, Bundesliga, La Liga, Serie A, Ligue 1,
    Eredivisie, Primeira Liga, Allsvenskan (as primary or fallback)

Data available per match (standard CSV):
  FT result, HT result, shots, shots on target, corners, fouls, cards, odds

Allsvenskan uses a different CSV format (fewer columns — no shots/corners).

No API key required. No rate limiting needed. One HTTP call per league.
All results cached with 7-day TTL.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from models.match import FormResult, Match, Outcome, ScheduleContext, TeamStats
from utils.cache import cached
from utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# League mapping: (Svenska Spel league, country) -> CSV code
# ---------------------------------------------------------------------------

_CSV_BASE_URL = "https://www.football-data.co.uk/mmz4281"
_ALLSVENSKAN_URL = "https://www.football-data.co.uk/new/SWE.csv"

_LEAGUE_CSV_MAP: dict[tuple[str, str], str] = {
    ("Premier League", "England"): "E0",
    ("Championship", "England"): "E1",
    ("League One", "England"): "E2",
    ("League Two", "England"): "E3",
    ("Bundesliga", "Germany"): "D1",
    ("2. Bundesliga", "Germany"): "D2",
    ("La Liga", "Spain"): "SP1",
    ("Primera Division", "Spain"): "SP1",
    ("Serie A", "Italy"): "I1",
    ("Ligue 1", "France"): "F1",
    ("Eredivisie", "Netherlands"): "N1",
    ("Primeira Liga", "Portugal"): "P1",
    ("Allsvenskan", "Sweden"): "SWE",
}

# Allsvenskan CSV has different column names
_SWE_COL_MAP = {
    "Home": "HomeTeam",
    "Away": "AwayTeam",
    "HG": "FTHG",
    "AG": "FTAG",
    "Res": "FTR",
}


def _current_season_code() -> str:
    """Return '2526' for the 2025-26 season, etc."""
    now = datetime.now()
    if now.month < 8:
        return f"{(now.year - 1) % 100:02d}{now.year % 100:02d}"
    return f"{now.year % 100:02d}{(now.year + 1) % 100:02d}"


# ---------------------------------------------------------------------------
# CSV fetching and parsing
# ---------------------------------------------------------------------------

@cached(lambda code: f"csv_data_{code}")
def _fetch_csv(code: str) -> list[dict]:
    """Download and parse a football-data.co.uk CSV. Cached 7 days."""
    if code == "SWE":
        url = _ALLSVENSKAN_URL
    else:
        season = _current_season_code()
        url = f"{_CSV_BASE_URL}/{season}/{code}.csv"

    logger.info("Fetching CSV from %s", url)
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()

    text = resp.text.lstrip("\ufeff")  # strip BOM if present
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)

    # Normalise Allsvenskan column names to standard format
    if code == "SWE":
        normalised = []
        for row in rows:
            # Filter to current season and Allsvenskan only
            if row.get("League") != "Allsvenskan":
                continue
            season_val = row.get("Season", "")
            now = datetime.now()
            current_year = str(now.year) if now.month >= 3 else str(now.year - 1)
            if season_val != current_year:
                continue
            norm = {}
            for old_key, new_key in _SWE_COL_MAP.items():
                norm[new_key] = row.get(old_key, "")
            norm["Date"] = row.get("Date", "")
            norm["Time"] = row.get("Time", "")
            # SWE CSV lacks shot/corner columns
            normalised.append(norm)
        rows = normalised

    logger.info("  Parsed %d match rows for %s", len(rows), code)
    return rows


def _parse_date(date_str: str) -> Optional[datetime]:
    """Parse CSV date string. Handles DD/MM/YYYY and DD/MM/YY."""
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


# ---------------------------------------------------------------------------
# League table computation
# ---------------------------------------------------------------------------

def _build_league_table(rows: list[dict]) -> list[dict]:
    """Compute a full league table from season results."""
    teams: dict[str, dict] = {}

    for row in rows:
        home = row.get("HomeTeam", "").strip()
        away = row.get("AwayTeam", "").strip()
        try:
            hg = int(row["FTHG"])
            ag = int(row["FTAG"])
        except (KeyError, ValueError, TypeError):
            continue

        if not home or not away:
            continue

        for team_name, gf, ga, is_home in [
            (home, hg, ag, True),
            (away, ag, hg, False),
        ]:
            if team_name not in teams:
                teams[team_name] = {
                    "team": team_name, "p": 0, "w": 0, "d": 0, "l": 0,
                    "gf": 0, "ga": 0, "pts": 0,
                }
            t = teams[team_name]
            t["p"] += 1
            t["gf"] += gf
            t["ga"] += ga
            if gf > ga:
                t["w"] += 1
                t["pts"] += 3
            elif gf == ga:
                t["d"] += 1
                t["pts"] += 1
            else:
                t["l"] += 1

    ranked = sorted(
        teams.values(),
        key=lambda t: (-t["pts"], -(t["gf"] - t["ga"]), -t["gf"]),
    )
    for i, entry in enumerate(ranked, 1):
        entry["position"] = i

    return ranked


# ---------------------------------------------------------------------------
# Team name resolution
# ---------------------------------------------------------------------------

def _get_unique_teams(rows: list[dict]) -> set[str]:
    teams = set()
    for row in rows:
        h = row.get("HomeTeam", "").strip()
        a = row.get("AwayTeam", "").strip()
        if h:
            teams.add(h)
        if a:
            teams.add(a)
    return teams


def _resolve_team(team_name: str, csv_teams: set[str]) -> Optional[str]:
    """Fuzzy match a Svenska Spel team name to a CSV team name."""
    name_lower = team_name.lower().strip()

    # Exact match
    for csv_name in csv_teams:
        if csv_name.lower() == name_lower:
            return csv_name

    # Substring match (either direction)
    for csv_name in csv_teams:
        csv_lower = csv_name.lower()
        if name_lower in csv_lower or csv_lower in name_lower:
            return csv_name

    # Word overlap for multi-word names
    name_words = {w for w in name_lower.split() if len(w) > 3}
    if name_words:
        best_match = None
        best_overlap = 0
        for csv_name in csv_teams:
            csv_words = {w for w in csv_name.lower().split() if len(w) > 3}
            overlap = len(name_words & csv_words)
            if overlap > best_overlap:
                best_overlap = overlap
                best_match = csv_name
        if best_match and best_overlap > 0:
            return best_match

    return None


# ---------------------------------------------------------------------------
# Form computation
# ---------------------------------------------------------------------------

def _team_matches(team_csv: str, rows: list[dict], venue: Optional[str] = None) -> list[dict]:
    """
    Get all matches for a team, sorted by date descending.
    venue: None=all, "H"=home only, "A"=away only.
    """
    matches = []
    for row in rows:
        home = row.get("HomeTeam", "").strip()
        away = row.get("AwayTeam", "").strip()
        is_home = home == team_csv
        is_away = away == team_csv
        if not is_home and not is_away:
            continue
        if venue == "H" and not is_home:
            continue
        if venue == "A" and not is_away:
            continue
        dt = _parse_date(row.get("Date", ""))
        matches.append({"row": row, "is_home": is_home, "date": dt})

    matches.sort(key=lambda m: m["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return matches


def _build_form(team_csv: str, rows: list[dict], venue: Optional[str] = None) -> list[FormResult]:
    """Build last 5 FormResult objects for a team."""
    matches = _team_matches(team_csv, rows, venue)
    form: list[FormResult] = []

    for m in matches[:5]:
        row = m["row"]
        is_home = m["is_home"]
        try:
            hg = int(row["FTHG"])
            ag = int(row["FTAG"])
        except (KeyError, ValueError, TypeError):
            continue

        if is_home:
            gf, ga = hg, ag
            opponent = row.get("AwayTeam", "").strip()
            h_or_a = "H"
        else:
            gf, ga = ag, hg
            opponent = row.get("HomeTeam", "").strip()
            h_or_a = "A"

        if gf > ga:
            result = Outcome.HOME  # win from this team's perspective
        elif gf == ga:
            result = Outcome.DRAW
        else:
            result = Outcome.AWAY  # loss

        form.append(FormResult(
            opponent=opponent,
            home_or_away=h_or_a,
            goals_for=gf,
            goals_against=ga,
            result=result,
        ))

    return form


# ---------------------------------------------------------------------------
# Shot/corner stats
# ---------------------------------------------------------------------------

def _build_shot_stats(
    team_csv: str, rows: list[dict],
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Compute 5-game averages: (shots_on_target_avg, shots_total_avg, corners_avg).
    Returns (None, None, None) if columns are missing (e.g. Allsvenskan).
    """
    matches = _team_matches(team_csv, rows)[:5]
    if not matches:
        return None, None, None

    # Check if shot columns exist
    first_row = matches[0]["row"]
    if "HS" not in first_row or "HST" not in first_row:
        return None, None, None

    sot_total = 0.0
    shots_total = 0.0
    corners_total = 0.0
    count = 0

    for m in matches:
        row = m["row"]
        is_home = m["is_home"]
        try:
            sot = int(row["HST"] if is_home else row["AST"])
            shots = int(row["HS"] if is_home else row["AS"])
            corners = int(row["HC"] if is_home else row["AC"])
        except (KeyError, ValueError, TypeError):
            continue

        sot_total += sot
        shots_total += shots
        corners_total += corners
        count += 1

    if count == 0:
        return None, None, None

    return (
        round(sot_total / count, 1),
        round(shots_total / count, 1),
        round(corners_total / count, 1),
    )


# ---------------------------------------------------------------------------
# Card / disciplinary stats
# ---------------------------------------------------------------------------

def _build_card_stats(
    team_csv: str, rows: list[dict],
) -> tuple[Optional[float], Optional[float]]:
    """
    Compute season-to-date average yellows/game for a team split by home/away.
    Returns (avg_yellows_home, avg_yellows_away). Returns (None, None) if the
    HY/AY columns are absent (e.g. Allsvenskan CSV).
    """
    if not rows or "HY" not in rows[0]:
        return None, None

    home_yellows, home_games = 0.0, 0
    away_yellows, away_games = 0.0, 0

    for row in rows:
        home = row.get("HomeTeam", "").strip()
        away = row.get("AwayTeam", "").strip()
        try:
            if home == team_csv:
                home_yellows += int(row.get("HY", 0) or 0)
                home_games += 1
            elif away == team_csv:
                away_yellows += int(row.get("AY", 0) or 0)
                away_games += 1
        except (ValueError, TypeError):
            continue

    avg_home = round(home_yellows / home_games, 1) if home_games else None
    avg_away = round(away_yellows / away_games, 1) if away_games else None
    return avg_home, avg_away


def _build_referee_stats(rows: list[dict]) -> dict[str, float]:
    """
    Compute per-referee average total cards/game (Y + R for both sides)
    from season CSV data. Returns {referee_name: avg_cards}.
    """
    if not rows or "Referee" not in rows[0]:
        return {}

    ref_cards: dict[str, list[int]] = {}
    for row in rows:
        ref = row.get("Referee", "").strip()
        if not ref:
            continue
        try:
            total = (
                int(row.get("HY", 0) or 0)
                + int(row.get("HR", 0) or 0)
                + int(row.get("AY", 0) or 0)
                + int(row.get("AR", 0) or 0)
            )
        except (ValueError, TypeError):
            continue
        ref_cards.setdefault(ref, []).append(total)

    return {
        ref: round(sum(games) / len(games), 1)
        for ref, games in ref_cards.items()
        if games
    }


# ---------------------------------------------------------------------------
# Schedule computation
# ---------------------------------------------------------------------------

def _build_schedule(team_csv: str, rows: list[dict]) -> Optional[ScheduleContext]:
    """Compute schedule context from recent matches."""
    matches = _team_matches(team_csv, rows)
    if not matches or matches[0]["date"] is None:
        return None

    last_date = matches[0]["date"]
    days_since = (datetime.now(timezone.utc) - last_date).days

    # Last match competition — CSV doesn't have this, use league name
    last_comp = ""

    # Matches in last 14 days
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    matches_14d = sum(
        1 for m in matches
        if m["date"] is not None and m["date"] >= cutoff
    )

    return ScheduleContext(
        days_since_last_match=days_since,
        last_match_competition=last_comp,
        matches_last_14_days=matches_14d,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def enrich_with_csv_stats(matches: list[Match]) -> list[Match]:
    """
    Enrich matches with standings, form, and match stats from
    football-data.co.uk CSV files.

    Sets fields ONLY if they are currently None/empty (deduplication guard).
    """
    # Group matches by league to minimise CSV downloads
    leagues_needed: dict[str, str] = {}
    for match in matches:
        key = (match.league or "", match.country or "")
        code = _LEAGUE_CSV_MAP.get(key)
        if code and code not in leagues_needed:
            leagues_needed[code] = match.league or ""

    if not leagues_needed:
        logger.info("No CSV-supported leagues in this coupon")
        return matches

    # Pre-fetch CSVs and build tables
    csv_data: dict[str, list[dict]] = {}
    tables: dict[str, list[dict]] = {}
    team_sets: dict[str, set[str]] = {}
    referee_stats: dict[str, dict[str, float]] = {}  # code → {referee: avg_cards}

    for code, league_name in leagues_needed.items():
        try:
            rows = _fetch_csv(code)
            if rows:
                csv_data[code] = rows
                tables[code] = _build_league_table(rows)
                team_sets[code] = _get_unique_teams(rows)
                referee_stats[code] = _build_referee_stats(rows)
                logger.info("  %s: %d matches, %d teams", league_name, len(rows), len(team_sets[code]))
        except Exception as e:
            logger.warning("CSV fetch failed for %s (%s): %s", code, league_name, e)

    # Enrich each match
    enriched_count = 0
    for match in matches:
        key = (match.league or "", match.country or "")
        code = _LEAGUE_CSV_MAP.get(key)
        if not code or code not in csv_data:
            continue

        rows = csv_data[code]
        table = tables[code]
        teams = team_sets[code]

        for is_home in (True, False):
            team_name = match.home_team if is_home else match.away_team
            stats = match.home_stats if is_home else match.away_stats

            if stats is None:
                stats = TeamStats(team_name=team_name)
                if is_home:
                    match.home_stats = stats
                else:
                    match.away_stats = stats

            csv_name = _resolve_team(team_name, teams)
            if csv_name is None:
                logger.debug("CSV: could not resolve '%s' in %s", team_name, code)
                continue

            did_enrich = False

            # --- Standings ---
            if stats.league_position is None:
                for entry in table:
                    if entry["team"] == csv_name:
                        stats.league_position = entry["position"]
                        stats.league_points = entry["pts"]
                        did_enrich = True
                        break

            # --- Form ---
            if not stats.form_last5:
                stats.form_last5 = _build_form(csv_name, rows)
                if stats.form_last5:
                    did_enrich = True

            if not stats.form_last5_home_only:
                stats.form_last5_home_only = _build_form(csv_name, rows, venue="H")

            if not stats.form_last5_away_only:
                stats.form_last5_away_only = _build_form(csv_name, rows, venue="A")

            # --- Shot stats ---
            if stats.shots_on_target_avg is None:
                sot, shots, corners = _build_shot_stats(csv_name, rows)
                if sot is not None:
                    stats.shots_on_target_avg = sot
                    stats.shots_total_avg = shots
                    stats.corners_avg = corners
                    did_enrich = True

            # --- Schedule ---
            if stats.schedule is None:
                sched = _build_schedule(csv_name, rows)
                if sched:
                    stats.schedule = sched
                    did_enrich = True

            # --- Card / disciplinary averages ---
            if stats.avg_yellows_home is None:
                yh, ya = _build_card_stats(csv_name, rows)
                if yh is not None:
                    stats.avg_yellows_home = yh
                    stats.avg_yellows_away = ya
                    did_enrich = True

            if did_enrich:
                enriched_count += 1

        # --- Referee stats (per match, not per team) ---
        if match.referee and match.referee_avg_cards is None:
            ref_lookup = referee_stats.get(code, {})
            # Fuzzy: try exact then partial match on referee name
            avg = ref_lookup.get(match.referee)
            if avg is None:
                ref_lower = match.referee.lower()
                for ref_name, ref_avg in ref_lookup.items():
                    if ref_lower in ref_name.lower() or ref_name.lower() in ref_lower:
                        avg = ref_avg
                        break
            if avg is not None:
                match.referee_avg_cards = avg

    logger.info("CSV enrichment complete: %d teams enriched", enriched_count)
    return matches
