"""
Renders the Jinja2 HTML template with the weekly report data.

Display logic (form letters, H2H perspective, implied odds, value flags,
signal chips) is computed here into plain dicts so the email template stays
a layout file. Colours live in THEME — the Dark Teal & Ivory palette (346eur)
plus the fixed signal colours.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader

from models.match import (
    InjuryStatus, Match, MatchPrediction, PlayerAbsence, TeamStats, WeeklyEvaluation, WeeklyReport,
)
from utils.logger import get_logger

logger = get_logger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_STATIC_BASE_URL = "https://trldrgn.github.io/stryktipset-newsletter/static"
_BADGES_DIR = Path(__file__).resolve().parent.parent / "static" / "badges"
_BADGES_BASE_URL = f"{_STATIC_BASE_URL}/badges"
_FOTMOB_CREST_URL = "https://images.fotmob.com/image_resources/logo/teamlogo/{team_id}_small.png"

# Locale-independent English names. strftime('%b'/'%A') would emit Swedish
# names on a Swedish-locale runner and break the email.
_EN_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_EN_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Dark Teal & Ivory (346eur) on Alabaster (346eur neutral) + fixed signal colours. Contrast-checked to WCAG AA.
THEME = {
    "page": "#E6E5E0", "card": "#FFFFFF", "text": "#001219", "muted": "#3F555C", "line": "#D5E6E8",
    "accent": "#005F73", "on_accent": "#FFFFFF", "highlight": "#E9D8A6",
    "o1": "#005F73", "on_o1": "#FFFFFF", "oX": "#E9D8A6", "on_oX": "#001219", "o2": "#94D2BD", "on_o2": "#001219",
    "positive": "#1F7548", "positive_t": "#E2F2E8",
    "caution": "#8C5A10", "caution_t": "#FBEEDB",
    "danger": "#B5302E", "danger_t": "#FBE3E1",
    "special": "#5A43B2", "special_t": "#ECE7FA",
    "yellow_card": "#F5C400", "red_card": "#D32F2F",
    "body": "Helvetica,Arial,sans-serif",   # unquoted: no &#39; escaping bloat
}

_WDL = {"1": "W", "X": "D", "2": "L"}

# Column headers in the stats table: Svenska Spel name -> short name
_SHORT_NAMES = {
    "Manchester City": "Man City", "Manchester United": "Man United", "Queens Park Rangers": "QPR",
    "West Bromwich": "West Brom", "Nottingham": "Forest", "Nottingham Forest": "Forest",
    "Wolverhampton": "Wolves", "Sheffield United": "Sheff Utd", "Sheffield Wednesday": "Sheff Wed",
    "Brighton & Hove Albion": "Brighton", "Crystal Palace": "Palace", "Tottenham": "Spurs",
}


def _short(name: str) -> str:
    if name in _SHORT_NAMES:
        return _SHORT_NAMES[name]
    return name if len(name) <= 14 else name.split()[0]
_VALUE_MIN_POINTS = 3      # odds-implied % minus public % to flag value
_GMAIL_CLIP_WARN_KB = 98   # Gmail hides everything past ~102 KB behind "View entire message"


def _load_badge_mapping() -> dict[str, str]:
    """Load team/competition name -> badge URL mapping."""
    mapping_path = _BADGES_DIR / "mapping.json"
    if not mapping_path.exists():
        return {}
    raw: dict[str, str] = json.loads(mapping_path.read_text(encoding="utf-8"))
    return {name: f"{_BADGES_BASE_URL}/{slug}.png" for name, slug in raw.items()}


def _badge(name: str, stats: Optional[TeamStats], badges: dict[str, str]) -> str:
    """Local badge first; FotMob's crest (by FotMob team id) for teams we have no file for."""
    if name in badges:
        return badges[name]
    if stats and stats.team_id:
        return _FOTMOB_CREST_URL.format(team_id=stats.team_id)
    return ""


# ---------------------------------------------------------------------------
# Per-match view model
# ---------------------------------------------------------------------------

def _form_letters(form) -> str:
    return "".join(_WDL[r.result.value] for r in form)


def _h2h(match: Match) -> list[dict]:
    """Last 5 meetings, newest first, as each side's own W/D/L (mirrored on the card)."""
    out = []
    for h in match.h2h[:5]:
        home_side = h.home_team == match.home_team
        gf, ga = (h.home_goals, h.away_goals) if home_side else (h.away_goals, h.home_goals)
        letter = "W" if gf > ga else "D" if gf == ga else "L"
        out.append({"home": letter, "away": {"W": "L", "L": "W"}.get(letter, "D"), "year": h.date[:4]})
    return out


def _odds(match: Match) -> Optional[dict]:
    m = match.market
    if not m or not (m.odds_home and m.odds_draw and m.odds_away):
        return None
    raw = [1 / m.odds_home, 1 / m.odds_draw, 1 / m.odds_away]
    total = sum(raw)
    implied = [round(p / total * 100) for p in raw]
    public = [m.public_pct_home, m.public_pct_draw, m.public_pct_away]
    has_public = all(p is not None for p in public)

    value = None
    if has_public:
        edges = {o: implied[i] - public[i] for i, o in enumerate("1X2")}
        best = max(edges, key=edges.get)
        if edges[best] >= _VALUE_MIN_POINTS:
            value = (best, edges[best])

    return {
        "cells": [
            {"o": o, "odds": f"{(m.odds_home, m.odds_draw, m.odds_away)[i]:.2f}",
             "implied": implied[i], "public": public[i] if has_public else None,
             "value": value[1] if value and value[0] == o else None}
            for i, o in enumerate("1X2")
        ],
        "public": public if has_public else None,
    }


def _venue_record(stats: Optional[TeamStats], venue: str) -> str:
    rec = (stats.home_record if venue == "home" else stats.away_record) if stats else None
    if not rec:
        return ""
    rank = f" ({_ordinal(rec.rank)})" if rec.rank else ""
    return f"W{rec.wins} D{rec.draws} L{rec.losses}, {rec.goals_for}–{rec.goals_against}{rank}"


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


_MISSING_SHOWN = 3


def _absence_detail(a: PlayerAbsence, banned: bool) -> str:
    """Why the player matters, shortest form first: 'RW, top assister', 'ST, 4G', 'CB, doubt'."""
    bits = [a.position] if a.position else []
    if a.is_top_scorer:
        bits.append("top scorer")
    elif a.is_top_assister:
        bits.append("top assister")
    elif a.season_goals or a.season_assists:
        bits.append(" ".join(f"{n}{k}" for n, k in ((a.season_goals, "G"), (a.season_assists, "A")) if n))
    if a.is_set_piece_taker:
        bits.append("set pieces")
    if banned:
        bits.append("banned")
    elif a.status in (InjuryStatus.DOUBT, InjuryStatus.FIFTY_FIFTY):
        bits.append("doubt")
    return ", ".join(bits)


def _missing(stats: Optional[TeamStats]) -> dict:
    """{'players': [{name, detail, key}], 'more': n, 'empty': text} — key players first."""
    if not stats:
        return {}
    absences = [(a, False) for a in stats.injuries] + [(a, True) for a in stats.suspensions]
    if not absences:
        return {"empty": "Nobody missing"} if stats.absences_checked_source else {}
    critical = {id(a) for a in stats.critical_absences}
    absences.sort(key=lambda ab: (id(ab[0]) not in critical, ab[0].status != InjuryStatus.OUT))
    return {
        "players": [
            {"name": a.player_name, "detail": _absence_detail(a, banned), "key": id(a) in critical}
            for a, banned in absences[:_MISSING_SHOWN]
        ],
        "more": max(0, len(absences) - _MISSING_SHOWN),
    }


def _chips(stats: Optional[TeamStats], absent_count: int) -> list[dict]:
    """Signal chips under a team name: icon + short text, coloured by signal role."""
    if not stats:
        return []
    chips = []
    if stats.form_points_last5 >= 10:
        chips.append(("positive", "▲", "Up-form"))
    elif len(stats.form_last5) >= 3 and stats.form_points_last5 <= 4:
        chips.append(("caution", "▼", "Down-form"))
    # Fatigue and an upcoming cup/European game share one chip to keep the sides balanced
    sched = stats.schedule
    rest = ""
    if stats.fatigue_flag and sched and sched.days_since_last_match is not None:
        days = sched.days_since_last_match
        rest = f"{days} day{'s' if days != 1 else ''} rest"
    upcoming = sched.upcoming_big_match if sched else ""
    if rest or upcoming:
        chips.append(("caution", "⚡", ", ".join(x for x in (rest, upcoming) if x)))
    if stats.new_manager_bounce:
        chips.append(("special", "★", "New manager"))
    absences = stats.injuries + stats.suspensions
    if len(absences) >= 5 or absent_count >= 5:
        chips.append(("danger", "✚", f"{max(len(absences), absent_count)} missing"))
    else:
        for a in stats.critical_absences[:1]:
            chips.append(("danger", "✚", f"{a.player_name} out"))
    return [{"kind": k, "icon": i, "text": t} for k, i, t in chips]


def _rows(h: Optional[TeamStats], a: Optional[TeamStats]) -> list[dict]:
    """Two-column stat rows; a row is dropped when neither side has data."""
    def xg(ts):
        return f"{ts.xg_for_avg:.2f} / {ts.xg_against_avg:.2f}" if ts and ts.xg_for_avg is not None else ""

    def xpts(ts):
        if not ts or ts.season_xpoints is None or ts.league_points is None:
            return ""
        return f"{ts.league_points} vs {ts.season_xpoints:.0f} xPts"

    rows = [
        {"label": "Form", "form": True,
         "home": _form_letters(h.form_last5) if h else "", "away": _form_letters(a.form_last5) if a else ""},
        {"label": "Venue record", "home": _venue_record(h, "home"), "away": _venue_record(a, "away")},
        {"label": "xG for / against", "home": xg(h), "away": xg(a)},
        {"label": "Points vs xPts", "home": xpts(h), "away": xpts(a)},
        {"label": "Missing", "missing": True, "home": _missing(h), "away": _missing(a)},
    ]
    return [r for r in rows if r["home"] or r["away"]]


def _kickoff(match: Match, tz: ZoneInfo) -> str:
    if not match.kickoff:
        return ""
    k = match.kickoff.astimezone(tz)
    return f"{_EN_WEEKDAYS[k.weekday()]} {k.strftime('%H:%M')}"


def _card(pred: MatchPrediction, match: Optional[Match], badges: dict[str, str], tz: ZoneInfo) -> dict:
    h = match.home_stats if match else None
    a = match.away_stats if match else None
    paras = [p for p in pred.analysis.split("\n\n") if p.strip()]
    return {
        "n": pred.game_number,
        "home": pred.home_team, "away": pred.away_team,
        "home_short": _short(pred.home_team),
        "away_short": _short(pred.away_team),
        "home_badge": _badge(pred.home_team, h, badges),
        "away_badge": _badge(pred.away_team, a, badges),
        "league": match.league if match else "",
        "league_badge": badges.get(match.league, "") if match else "",
        "when": _kickoff(match, tz) if match else "",
        "picks": [o.value for o in pred.predicted_outcomes],
        "type": pred.selection_type.value,
        "confidence": round(pred.confidence * 100),
        "home_chips": _chips(h, pred.home_absent_count),
        "away_chips": _chips(a, pred.away_absent_count),
        "rows": _rows(h, a),
        "h2h": _h2h(match) if match else [],
        "referee": match.referee if match else "",
        "ref_yellows": match.referee_yellows_avg if match else None,
        "ref_reds": match.referee_reds_avg if match else None,
        "odds": _odds(match) if match else None,
        "analysis": paras[0] if paras else "",
        "why": pred.key_factors[:2],
        "risks": pred.risk_flags[:2],
    }


def _last_week(evaluation: Optional[WeeklyEvaluation]) -> Optional[dict]:
    if not evaluation:
        return None
    return {
        "draw": evaluation.draw_number,
        "correct": evaluation.total_correct,
        "total": len(evaluation.evaluations),
        "market": evaluation.market_correct,
        "singles": f"{evaluation.singles_correct}/{evaluation.singles_total}",
        "doubles": f"{evaluation.doubles_correct}/{evaluation.doubles_total}",
        "rows": [
            {"n": e.game_number, "home": e.home_team, "away": e.away_team,
             "picks": [o.value for o in e.our_prediction], "result": e.actual_result.value, "hit": e.correct}
            for e in sorted(evaluation.evaluations, key=lambda e: e.game_number)
        ],
    }


def _compact(html: str) -> str:
    """Drop indentation and blank lines between tags. Gmail clips emails over ~102 KB."""
    html = re.sub(r"\n[ \t]+", "\n", html)          # indentation renders as nothing in HTML
    return re.sub(r"\n{2,}", "\n", html)


def render_newsletter(
    report: WeeklyReport,
    matches: list[Match],
    evaluation: WeeklyEvaluation | None = None,
) -> tuple[str, str]:
    """
    Render the HTML newsletter.
    Returns (subject, html_body).
    """
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("report.html")

    stockholm = ZoneInfo("Europe/Stockholm")
    now = datetime.now(stockholm)
    week_number = now.isocalendar()[1]

    matches_by_game: dict[int, Match] = {m.game_number: m for m in matches}
    badge_urls = _load_badge_mapping()
    predictions = sorted(report.predictions, key=lambda p: p.game_number)
    cards = [_card(p, matches_by_game.get(p.game_number), badge_urls, stockholm) for p in predictions]

    tpl_vars = dict(
        c=THEME,
        report=report,
        cards=cards,
        draw_number=report.draw_number,
        week_number=week_number,
        year=now.year,
        generated_date=f"{now.day} {_EN_MONTHS[now.month]} {now.year}, {now.strftime('%H:%M')}",
        executive_summary=report.executive_summary,
        value_radar=report.value_radar,
        last_week=_last_week(evaluation),
        whistle_url=f"{_STATIC_BASE_URL}/icons/whistle.png",
    )
    html = _compact(template.render(**tpl_vars))
    size_kb = len(html.encode("utf-8")) / 1024
    if size_kb > _GMAIL_CLIP_WARN_KB:
        logger.warning("Newsletter HTML is %.0f KB — Gmail clips at ~102 KB ('View entire message')", size_kb)

    subject = (
        f"Stryktipset v.{week_number} #{report.draw_number} — "
        f"{len(report.singles)} singles · {len(report.doubles)} doubles · 1 full · {report.total_cost_sek} SEK"
    )

    logger.info("Newsletter rendered. Subject: %s", subject)
    return subject, html
