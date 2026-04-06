"""
Fetches fixture-anchored match previews from Perplexity Sonar.

Role (after Tier 2B rewrite): narrative context + media perspective only.
Structured injury truth comes from API-Football and Sofascore; Perplexity
is used to corroborate and add editorial colour, never as the sole source
for a named injured player.

Key design points:
  - One query per match (parallelized, 4 workers).
  - Fixture-anchored prompt: asks specifically about THIS fixture on THIS
    date so old H2H write-ups don't leak in.
  - API-side filtering: `search_after_date_filter` + `search_before_date_filter`
    pin the search window to [kickoff-7d, kickoff+1d], and
    `search_domain_filter` whitelists trusted outlets.
  - Response is split into [PREVIEW] / [ABSENCES — home] / [ABSENCES — away]
    sections so home_news and away_news receive different summaries (the old
    bug was that both held the same blob).
  - Every query + response is written to data/perplexity_traces/draw_{N}/
    for offline review.
"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from config import DATA_DIR, PERPLEXITY_API_KEY, PERPLEXITY_BASE_URL, PERPLEXITY_MODEL
from models.match import Match, NewsContext
from utils.logger import get_logger

logger = get_logger(__name__)

# Rate limiter — 2s between calls (conservative safety margin)
_last_call_at: float = 0.0
_MIN_CALL_INTERVAL = 2.0
_rate_lock = threading.Lock()

# Parallelization — 4 workers keeps us well within any rate limit
_MAX_WORKERS = 4

# Perplexity enforces max 20 domains in search_domain_filter.
TRUSTED_FOOTBALL_DOMAINS: list[str] = [
    # English-language
    "bbc.co.uk",
    "skysports.com",
    "theguardian.com",
    "theathletic.com",
    "espn.com",
    "reuters.com",
    "telegraph.co.uk",
    # Spain
    "marca.com",
    "as.com",
    "mundodeportivo.com",
    # Italy
    "gazzetta.it",
    "corrieredellosport.it",
    "tuttosport.com",
    # Germany
    "kicker.de",
    "bild.de",
    # France
    "lequipe.fr",
    # Club-official fallbacks
    "uefa.com",
    "premierleague.com",
]

_DOMAIN_RE = re.compile(r"([a-z0-9-]+\.(?:com|co\.uk|de|fr|it|es|eu|tv|net))")


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _build_query(match: Match, kickoff_date: str) -> str:
    """Fixture-anchored preview query. Forces the model to treat the kickoff
    date as the centre of gravity rather than "latest team news"."""
    return (
        f"Match preview: {match.home_team} vs {match.away_team} — "
        f"{match.league}, kickoff {kickoff_date}.\n\n"
        "Only reference articles and reports that specifically preview THIS "
        f"fixture on {kickoff_date}. Do NOT use older articles about either "
        "team's general season situation. Do NOT use reports about other "
        "fixtures between these teams (no H2H history). Do NOT cite articles "
        f"dated more than 7 days before {kickoff_date}.\n\n"
        "Return exactly three sections, each 3-5 sentences:\n\n"
        "[PREVIEW] What are the leading English and local-language football "
        "media outlets saying about this fixture specifically? Expected "
        "lineup, tactical angle, or narrative thread. Cite outlets by name.\n\n"
        f"[ABSENCES — {match.home_team}] Players confirmed OUT or DOUBTFUL "
        "for THIS fixture according to this week's press conference or "
        "official club statements. For each: name, position, status, reason. "
        "If the club reports a full squad or no new injury news surfaced for "
        "this week's fixture, say \"none reported\". Do NOT include players "
        "from injury reports older than 7 days unless the source explicitly "
        "re-confirms for THIS fixture.\n\n"
        f"[ABSENCES — {match.away_team}] Same format.\n\n"
        f"End with a line: \"Total confirmed out: {match.home_team} N, "
        f"{match.away_team} M.\""
    )


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def _call_sonar(query: str, kickoff: Optional[datetime]) -> str:
    """Single Perplexity Sonar call with rate limiting, date + domain filters."""
    global _last_call_at

    with _rate_lock:
        elapsed = time.time() - _last_call_at
        if elapsed < _MIN_CALL_INTERVAL:
            time.sleep(_MIN_CALL_INTERVAL - elapsed)
        _last_call_at = time.time()

    kdate = (kickoff or datetime.now(timezone.utc)).date()
    after = (kdate - timedelta(days=7)).strftime("%m/%d/%Y")
    before = (kdate + timedelta(days=1)).strftime("%m/%d/%Y")

    headers = {
        "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "model": PERPLEXITY_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a football analyst assistant. Produce preview "
                    "content focused strictly on the specific fixture asked "
                    "about, on the specific date given. Cite outlets briefly. "
                    "If you cannot find recent preview coverage, say so "
                    "clearly rather than padding with stale H2H or season "
                    "narrative."
                ),
            },
            {"role": "user", "content": query},
        ],
        "max_tokens": 900,
        "temperature": 0.1,
        "search_after_date_filter": after,
        "search_before_date_filter": before,
        "search_domain_filter": TRUSTED_FOOTBALL_DOMAINS,
    }

    resp = requests.post(
        f"{PERPLEXITY_BASE_URL}/chat/completions",
        headers=headers,
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _extract_section(text: str, marker: str) -> str:
    """Extract text between [marker] and the next [section] or end of string."""
    pattern = re.compile(
        rf"\[{re.escape(marker)}[^\]]*\](.*?)(?=\n\[[^\]]+\]|$)",
        re.DOTALL,
    )
    m = pattern.search(text)
    return m.group(1).strip() if m else ""


def _extract_count(text: str, team_name: str) -> Optional[int]:
    """Parse the 'Total confirmed out' line for a given team."""
    pattern = re.compile(
        rf"{re.escape(team_name)}\D*(\d+)",
        re.IGNORECASE,
    )
    m = pattern.search(text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _extract_domains(text: str) -> list[str]:
    """Deduped list of domain-like strings found in the response."""
    found = _DOMAIN_RE.findall(text.lower())
    seen: list[str] = []
    for d in found:
        if d not in seen:
            seen.append(d)
    return seen


def _parse_response(
    raw: str,
    match: Match,
    after_iso: str,
    before_iso: str,
) -> tuple[NewsContext, NewsContext]:
    """
    Split a single Sonar response into distinct home and away NewsContexts.
    Falls back to full-blob-for-both + warning if section markers are missing.
    """
    preview = _extract_section(raw, "PREVIEW")
    home_abs = _extract_section(raw, f"ABSENCES — {match.home_team}") \
        or _extract_section(raw, f"ABSENCES - {match.home_team}")
    away_abs = _extract_section(raw, f"ABSENCES — {match.away_team}") \
        or _extract_section(raw, f"ABSENCES - {match.away_team}")

    # Pull the "Total confirmed out" tail line if present
    total_line = ""
    for line in raw.splitlines()[::-1]:
        if "total confirmed out" in line.lower():
            total_line = line.strip()
            break

    if not (preview or home_abs or away_abs):
        logger.warning(
            "Perplexity response for %s vs %s did not match expected section "
            "format — falling back to raw blob for both teams",
            match.home_team, match.away_team,
        )
        home_summary = away_summary = raw
    else:
        parts_home = [p for p in (preview, home_abs, total_line) if p]
        parts_away = [p for p in (preview, away_abs, total_line) if p]
        home_summary = "\n\n".join(parts_home)
        away_summary = "\n\n".join(parts_away)

    domains = _extract_domains(raw)
    home_count = _extract_count(total_line, match.home_team) if total_line else None
    away_count = _extract_count(total_line, match.away_team) if total_line else None

    now = datetime.now(timezone.utc)
    home_ctx = NewsContext(
        team_name=match.home_team,
        summary=home_summary,
        retrieved_at=now,
        source_domains=domains,
        perplexity_absent_count=home_count,
        query_window_start=after_iso,
        query_window_end=before_iso,
    )
    away_ctx = NewsContext(
        team_name=match.away_team,
        summary=away_summary,
        retrieved_at=now,
        source_domains=domains,
        perplexity_absent_count=away_count,
        query_window_start=after_iso,
        query_window_end=before_iso,
    )
    return home_ctx, away_ctx


# ---------------------------------------------------------------------------
# Trace logging — offline diagnostic asset
# ---------------------------------------------------------------------------

def _log_query_trace(
    match: Match,
    query: str,
    raw: str,
    home_ctx: NewsContext,
    away_ctx: NewsContext,
) -> None:
    """Write the query + response to data/perplexity_traces/draw_{N}/game_{M}.json."""
    try:
        trace_dir = DATA_DIR / "perplexity_traces" / f"draw_{match.draw_number}"
        trace_dir.mkdir(parents=True, exist_ok=True)
        trace_file = trace_dir / f"game_{match.game_number}.json"
        payload = {
            "match": f"{match.home_team} vs {match.away_team}",
            "kickoff": match.kickoff.isoformat() if match.kickoff else None,
            "query": query,
            "filters": {
                "search_after_date_filter": home_ctx.query_window_start,
                "search_before_date_filter": home_ctx.query_window_end,
                "search_domain_filter": TRUSTED_FOOTBALL_DOMAINS,
            },
            "response": raw,
            "parsed": {
                "home_summary": home_ctx.summary,
                "away_summary": away_ctx.summary,
                "home_absent_count": home_ctx.perplexity_absent_count,
                "away_absent_count": away_ctx.perplexity_absent_count,
            },
            "source_domains": home_ctx.source_domains,
        }
        trace_file.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.debug("Could not write Perplexity trace for game %d: %s",
                     match.game_number, e)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def fetch_match_news(match: Match) -> Match:
    logger.info(
        "Fetching Perplexity preview for [%d] %s vs %s",
        match.game_number, match.home_team, match.away_team,
    )

    # ISO date keeps the prompt locale-independent (runners may be localised
    # to Swedish, and %A/%B would leak Swedish day/month names into the prompt).
    kickoff_date = match.kickoff.strftime("%Y-%m-%d") if match.kickoff else "this week"
    query = _build_query(match, kickoff_date)

    kdate = (match.kickoff or datetime.now(timezone.utc)).date()
    after_iso = (kdate - timedelta(days=7)).isoformat()
    before_iso = (kdate + timedelta(days=1)).isoformat()

    try:
        raw = _call_sonar(query, match.kickoff)
        logger.debug(
            "Perplexity response for %s vs %s: %s...",
            match.home_team, match.away_team, raw[:120],
        )
        home_ctx, away_ctx = _parse_response(raw, match, after_iso, before_iso)
        match.home_news = home_ctx
        match.away_news = away_ctx
        _log_query_trace(match, query, raw, home_ctx, away_ctx)

    except requests.HTTPError as e:
        logger.error(
            "Perplexity HTTP error for %s vs %s: %s",
            match.home_team, match.away_team, e,
        )
        match.home_news = NewsContext(
            team_name=match.home_team, summary="News unavailable (API error).",
        )
        match.away_news = NewsContext(
            team_name=match.away_team, summary="News unavailable (API error).",
        )
    except Exception as e:
        logger.error(
            "Unexpected Perplexity error for %s vs %s: %s",
            match.home_team, match.away_team, e,
        )
        match.home_news = NewsContext(team_name=match.home_team, summary="News unavailable.")
        match.away_news = NewsContext(team_name=match.away_team, summary="News unavailable.")

    return match


def fetch_all_match_news(matches: list[Match]) -> list[Match]:
    logger.info(
        "Fetching Perplexity previews for %d matches (%d workers)",
        len(matches), _MAX_WORKERS,
    )

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_match_news, match): match
            for match in matches
        }
        for i, future in enumerate(as_completed(futures), 1):
            match = futures[future]
            try:
                future.result()
                logger.info(
                    "Perplexity %d/%d complete: %s vs %s",
                    i, len(matches), match.home_team, match.away_team,
                )
            except Exception as e:
                logger.error(
                    "Perplexity worker failed for %s vs %s: %s",
                    match.home_team, match.away_team, e,
                )

    logger.info("Perplexity news fetch complete")
    return matches
