"""
Sends all 13 enriched matches to Claude in a single API call.
Returns structured predictions + editorial analysis for the newsletter.

Design principles:
  - ONE API call for all 13 matches (cost efficient)
  - Structured JSON response — easier to parse than free-text
  - Last week's evaluation is injected as context
  - Claude produces both the analysis TEXT and the prediction DATA
  - If JSON parsing fails, we fall back to raw text extraction
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, CLAUDE_MAX_TOKENS
from models.match import (
    Match,
    MatchPrediction,
    WeeklyReport,
    WeeklyEvaluation,
    Outcome,
    SelectionType,
)
from utils.logger import get_logger

logger = get_logger(__name__)

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _format_form(form_list) -> str:
    if not form_list:
        return "N/A"
    return " ".join(
        f"{r.result.value}({r.goals_for}-{r.goals_against})" for r in form_list
    )


def _format_h2h(h2h_list) -> str:
    if not h2h_list:
        return "No H2H data"
    lines = []
    for h in h2h_list[:5]:
        lines.append(f"{h.date}: {h.home_team} {h.home_goals}-{h.away_goals} {h.away_team}")
    return " | ".join(lines)


def _format_absences(absences) -> str:
    if not absences:
        return "None reported"
    parts = []
    for a in absences:
        desc = f"{a.player_name} ({a.position}, {a.status.value})"
        if a.is_top_scorer:
            desc += " ⚡TOP SCORER"
        if a.is_top_assister:
            desc += " ⚡TOP ASSISTER"
        if a.is_set_piece_taker:
            desc += " [set piece taker]"
        if a.matchup_risk and a.matchup_risk.risk_level.value in ("high", "critical"):
            desc += f" ⚠️ MATCHUP RISK: {a.matchup_risk.note}"
        parts.append(desc)
    return "; ".join(parts)


def _format_match_block(match: Match) -> str:
    """Format one match's data as a compact text block for the prompt."""
    m = match.market
    h = match.home_stats
    a = match.away_stats

    odds_str = "N/A"
    dist_str = ""
    tips_str = ""
    if m:
        odds_str = f"1:{m.odds_home}  X:{m.odds_draw}  2:{m.odds_away}"
        if m.public_pct_home is not None:
            dist_str = f"Public: {m.public_pct_home}% / {m.public_pct_draw}% / {m.public_pct_away}%"
        if m.newspaper_tips_home + m.newspaper_tips_draw + m.newspaper_tips_away > 0:
            tips_str = f"Newspapers (of 10): 1:{m.newspaper_tips_home}  X:{m.newspaper_tips_draw}  2:{m.newspaper_tips_away}"

    home_pos = f"P{h.league_position}" if h and h.league_position else "?"
    away_pos = f"P{a.league_position}" if a and a.league_position else "?"

    home_form = _format_form(h.form_last5) if h else "N/A"
    away_form = _format_form(a.form_last5) if a else "N/A"
    home_form_home = _format_form(h.form_last5_home_only) if h else "N/A"
    away_form_away = _format_form(a.form_last5_away_only) if a else "N/A"

    home_xg = f"xGF:{h.xg_for_avg:.2f} xGA:{h.xg_against_avg:.2f}" if h and h.xg_for_avg else "xG: N/A"
    away_xg = f"xGF:{a.xg_for_avg:.2f} xGA:{a.xg_against_avg:.2f}" if a and a.xg_for_avg else "xG: N/A"

    home_fatigue = " ⚠️FATIGUE" if h and h.fatigue_flag else ""
    away_fatigue = " ⚠️FATIGUE" if a and a.fatigue_flag else ""

    home_injuries = _format_absences(h.injuries + h.suspensions) if h else "N/A"
    away_injuries = _format_absences(a.injuries + a.suspensions) if a else "N/A"

    home_intl = ", ".join(h.intl_call_ups) if h and h.intl_call_ups else "None"
    away_intl = ", ".join(a.intl_call_ups) if a and a.intl_call_ups else "None"

    home_manager = ""
    if h and h.new_manager_bounce:
        home_manager = f" [NEW MANAGER {h.manager_name} — {h.manager_weeks_in_post}wks, bounce effect possible]"
    away_manager = ""
    if a and a.new_manager_bounce:
        away_manager = f" [NEW MANAGER {a.manager_name} — {a.manager_weeks_in_post}wks, bounce effect possible]"

    news = ""
    if match.home_news and match.home_news.summary:
        news = f"\nLATEST NEWS:\n{match.home_news.summary}"

    return f"""
GAME {match.game_number}: {match.home_team} vs {match.away_team}
League: {match.league} ({match.country}) | Kickoff: {match.kickoff.strftime('%a %d %b %H:%M') if match.kickoff else 'TBD'}
Odds: {odds_str} | {dist_str} | {tips_str}

HOME — {match.home_team} ({home_pos}){home_fatigue}{home_manager}
  Form (all): {home_form} | Form (home): {home_form_home}
  {home_xg}
  Injuries/Suspensions: {home_injuries}
  Intl call-ups missing: {home_intl}

AWAY — {match.away_team} ({away_pos}){away_fatigue}{away_manager}
  Form (all): {away_form} | Form (away): {away_form_away}
  {away_xg}
  Injuries/Suspensions: {away_injuries}
  Intl call-ups missing: {away_intl}

H2H (last 5): {_format_h2h(match.h2h)}
{news}
""".strip()


def _build_system_prompt(evaluation: WeeklyEvaluation | None) -> str:
    feedback = ""
    if evaluation and evaluation.feedback_summary:
        feedback = f"""
LAST WEEK'S PERFORMANCE — USE THIS TO CALIBRATE:
{evaluation.feedback_summary}

Key lessons to apply this week:
{chr(10).join(f'- {l}' for l in evaluation.lessons)}
"""

    return f"""You are an expert football analyst producing a weekly Stryktipset betting newsletter.

Your role:
- Analyse each of the 13 matches using ALL provided data (form, xG, injuries, H2H, market signals, news)
- Write editorial-quality analysis — not bullet points. Real narrative, like a respected football journalist.
- Give a clear, reasoned prediction for each match
- Be especially alert to: fatigue/rotation flags, matchup risks from injuries, new manager effects,
  motivation differences, xG over/underperformance, and contrarian signals vs the public betting %

INJURY ANALYSIS DEPTH:
- Don't just note injuries — evaluate the IMPACT. A missing LB matters more if the opponent has a
  top-scoring RW. A missing top scorer is huge. A missing squad player is not.
- Always consider the replacement quality.
- PRIMARY source for injuries is the LATEST NEWS section (from Perplexity web search, current week).
  The structured "Injuries/Suspensions" field may show "None reported" when the stats API had no data
  — this is a DATA GAP, not confirmation the team is fully fit. Always check LATEST NEWS first.
- If LATEST NEWS confirms a clean bill of health, say "no significant absences". Do NOT write
  phrases like "no injury news which is surprising/concerning" or "unusual that no injuries reported".
  A fully fit squad is simply good news for that team — treat it as such.
- If injury data is genuinely unavailable from both sources, say "injury data unavailable" and move on.
  Do not speculate or editorialize about the absence of data.

DATA QUALITY NOTES:
- xG showing N/A is common for lower-league teams (Championship, League One) and when the stats API
  is restricted. Do not comment on the absence of xG — just use form, odds, and news instead.
- "Form: N/A" means the stats API had no data. Use market signals and news to compensate.
- When structured stats are sparse, weight the LATEST NEWS and market odds more heavily.
{feedback}
CRITICAL RULES:
- You MUST return valid JSON exactly as specified — the system depends on it for parsing
- Confidence scores: 0.9+ = single candidate, 0.5 = full candidate
- Be honest about uncertainty — it's better to double a game than single a 50/50
- Do not invent statistics — if data says N/A, say so in your analysis"""


def _build_user_prompt(matches: list[Match], draw_number: int) -> str:
    match_blocks = "\n\n" + "="*60 + "\n\n".join(
        _format_match_block(m) for m in matches
    )

    return f"""Analyse these 13 Stryktipset matches for draw #{draw_number} and return your analysis as JSON.

{match_blocks}

Return ONLY valid JSON in this exact structure (no markdown, no extra text):
{{
  "executive_summary": "2-3 sentence overview of this week's coupon — what stands out, any dominant themes",
  "value_radar": ["game X looks mispriced because...", "..."],
  "matches": [
    {{
      "game_number": 1,
      "home_team": "...",
      "away_team": "...",
      "analysis": "3-4 paragraph editorial analysis. Cover form, H2H, key stats, injuries with matchup context, tactical angle, and prediction reasoning.",
      "key_factors": ["Factor 1", "Factor 2", "Factor 3"],
      "risk_flags": ["Risk 1", "Risk 2"],
      "predicted_outcomes": ["1"],
      "confidence": 0.85,
      "value_note": "optional: if odds look mispriced vs your assessment"
    }}
  ]
}}

For predicted_outcomes use only "1", "X", "2".
Confidence is 0.0-1.0. Higher = more certain.
Include all 13 matches in the matches array, numbered 1-13."""


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_response(raw: str, matches: list[Match], draw_number: int) -> WeeklyReport:
    """Parse Claude's JSON response into a WeeklyReport."""
    # Strip any accidental markdown code fences
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse Claude JSON response: %s\nRaw:\n%s", e, raw[:500])
        raise ValueError(f"Claude returned invalid JSON: {e}") from e

    predictions: list[MatchPrediction] = []
    for m_data in data.get("matches", []):
        game_num = m_data["game_number"]
        predicted_raw = m_data.get("predicted_outcomes", ["1", "X", "2"])
        predicted = [Outcome(o) for o in predicted_raw]
        confidence = float(m_data.get("confidence", 0.5))

        # Determine selection type from number of predicted outcomes
        n = len(predicted)
        if n == 1:
            sel_type = SelectionType.SINGLE
        elif n == 3:
            sel_type = SelectionType.FULL
        else:
            sel_type = SelectionType.DOUBLE

        predictions.append(MatchPrediction(
            game_number=game_num,
            home_team=m_data.get("home_team", ""),
            away_team=m_data.get("away_team", ""),
            predicted_outcomes=predicted,
            selection_type=sel_type,
            confidence=confidence,
            analysis=m_data.get("analysis", ""),
            key_factors=m_data.get("key_factors", []),
            risk_flags=m_data.get("risk_flags", []),
            value_note=m_data.get("value_note", ""),
        ))

    predictions.sort(key=lambda p: p.game_number)

    report = WeeklyReport(
        draw_number=draw_number,
        generated_at=datetime.now(timezone.utc),
        predictions=predictions,
        executive_summary=data.get("executive_summary", ""),
        value_radar=data.get("value_radar", []),
    )

    return report


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analyse_matches(
    matches: list[Match],
    draw_number: int,
    evaluation: WeeklyEvaluation | None = None,
) -> WeeklyReport:
    """
    Send all 13 matches to Claude in one call. Returns a WeeklyReport.
    """
    logger.info("Sending %d matches to Claude (%s)", len(matches), CLAUDE_MODEL)

    system_prompt = _build_system_prompt(evaluation)
    user_prompt = _build_user_prompt(matches, draw_number)

    logger.debug("System prompt length: %d chars", len(system_prompt))
    logger.debug("User prompt length: %d chars", len(user_prompt))

    message = _client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw_response = message.content[0].text
    logger.info(
        "Claude response received. Input tokens: %d, Output tokens: %d",
        message.usage.input_tokens,
        message.usage.output_tokens,
    )

    report = _parse_response(raw_response, matches, draw_number)
    logger.info("Parsed %d predictions from Claude", len(report.predictions))

    return report
