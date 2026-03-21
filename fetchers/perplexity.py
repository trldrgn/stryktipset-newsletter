"""
Fetches latest team news, xG context, and injury updates via Perplexity Sonar API.

Cost efficiency:
  - 1 query per match (combining both teams + all topics)
  - Uses the cheapest 'sonar' model
  - Structured prompt ensures compact, useful responses
  - Results are NOT cached (news must be fresh each run)

What we ask per match:
  - Latest injury and availability news for both teams
  - xG stats from recent games (FBref context)
  - Rotation/fatigue risk (upcoming fixtures, cup games)
  - Manager press conference quotes on team selection
  - Any relevant tactical or morale context
"""

from __future__ import annotations

from datetime import datetime, timezone

import requests

from config import PERPLEXITY_API_KEY, PERPLEXITY_BASE_URL, PERPLEXITY_MODEL
from models.match import Match, NewsContext
from utils.logger import get_logger

logger = get_logger(__name__)


def _build_query(match: Match) -> str:
    """
    Build a single focused query for one match covering both teams.
    Compact and specific to minimise token cost while maximising signal.
    """
    kickoff_str = ""
    if match.kickoff:
        kickoff_str = match.kickoff.strftime("%d %B %Y")

    return (
        f"Football match preview: {match.home_team} vs {match.away_team} "
        f"({match.league}{', ' + kickoff_str if kickoff_str else ''}). "
        f"Provide concise factual updates on: "
        f"1) Current injury list and doubtful players for both teams "
        f"2) Recent xG statistics (last 3-5 games) for both teams — cite FBref or similar "
        f"3) Rotation/fatigue risk — did either team play in the last 3 days? "
        f"Any upcoming high-priority fixture causing likely rotation? "
        f"4) Manager press conference — any quotes on team selection or player availability? "
        f"5) Squad morale, recent form narrative, or any unusual context (new manager, dressing room issues). "
        f"Be factual and brief. If data is unavailable say so."
    )


def _call_perplexity(query: str) -> str:
    """
    Single Perplexity Sonar API call. Returns the response text.
    """
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
                    "You are a football analyst assistant. Provide concise, factual, "
                    "data-driven answers. Always cite sources briefly (e.g. 'FBref', "
                    "'BBC Sport', 'club official'). If you cannot find recent data, "
                    "say so clearly rather than speculating."
                ),
            },
            {"role": "user", "content": query},
        ],
        "max_tokens": 600,
        "temperature": 0.1,   # low temperature for factual retrieval
        "return_citations": True,
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


def _parse_response(raw: str, team_name: str) -> NewsContext:
    """
    Wrap the raw Perplexity response in a NewsContext.
    The response already covers both teams — we keep the full text in summary
    and try to extract team-specific sections.
    """
    return NewsContext(
        team_name=team_name,
        summary=raw,
        retrieved_at=datetime.now(timezone.utc),
    )


def fetch_match_news(match: Match) -> Match:
    """
    Fetch news context for one match. Populates match.home_news and match.away_news.
    Both are set from the same API response (one call per match).
    """
    logger.info(
        "Fetching Perplexity news for [%d] %s vs %s",
        match.game_number, match.home_team, match.away_team,
    )

    query = _build_query(match)

    try:
        raw_response = _call_perplexity(query)
        logger.debug("Perplexity response for %s vs %s: %s...", match.home_team, match.away_team, raw_response[:120])

        # Both home_news and away_news hold the combined response —
        # Claude will read it and extract what's relevant for each team
        match.home_news = _parse_response(raw_response, match.home_team)
        match.away_news = _parse_response(raw_response, match.away_team)

    except requests.HTTPError as e:
        logger.error("Perplexity HTTP error for %s vs %s: %s", match.home_team, match.away_team, e)
        match.home_news = NewsContext(team_name=match.home_team, summary="News unavailable (API error).")
        match.away_news = NewsContext(team_name=match.away_team, summary="News unavailable (API error).")
    except Exception as e:
        logger.error("Unexpected Perplexity error for %s vs %s: %s", match.home_team, match.away_team, e)
        match.home_news = NewsContext(team_name=match.home_team, summary="News unavailable.")
        match.away_news = NewsContext(team_name=match.away_team, summary="News unavailable.")

    return match


def fetch_all_match_news(matches: list[Match]) -> list[Match]:
    """
    Fetch news for all 13 matches. One Perplexity call per match = 13 total.
    """
    logger.info("Fetching Perplexity news for %d matches", len(matches))
    for i, match in enumerate(matches, 1):
        logger.info("Perplexity query %d/%d", i, len(matches))
        fetch_match_news(match)
    logger.info("Perplexity news fetch complete")
    return matches
