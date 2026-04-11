"""
Renders the Jinja2 HTML template with the weekly report data.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader

from models.match import Match, WeeklyReport, WeeklyEvaluation
from utils.logger import get_logger

logger = get_logger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_BADGES_DIR = Path(__file__).resolve().parent.parent / "static" / "badges"
_BADGES_BASE_URL = "https://trldrgn.github.io/stryktipset-newsletter/static/badges"

# Locale-independent English month abbreviations. strftime('%b') would emit
# Swedish month names on a Swedish-locale runner and break the email header.
_EN_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _load_badge_mapping() -> dict[str, str]:
    """Load team/competition name -> badge URL mapping."""
    mapping_path = _BADGES_DIR / "mapping.json"
    if not mapping_path.exists():
        return {}
    raw: dict[str, str] = json.loads(mapping_path.read_text(encoding="utf-8"))
    return {name: f"{_BADGES_BASE_URL}/{slug}.png" for name, slug in raw.items()}


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
    )
    template = env.get_template("report.html")

    stockholm = ZoneInfo("Europe/Stockholm")
    now = datetime.now(stockholm)
    week_number = now.isocalendar()[1]

    matches_by_game: dict[int, Match] = {m.game_number: m for m in matches}
    badge_urls = _load_badge_mapping()

    tpl_vars = dict(
        report=report,
        predictions=sorted(report.predictions, key=lambda p: p.game_number),
        draw_number=report.draw_number,
        week_number=week_number,
        year=now.year,
        generated_date=f"{now.day:02d} {_EN_MONTHS[now.month]} {now.year}, {now.strftime('%H:%M %Z')}",
        executive_summary=report.executive_summary,
        value_radar=report.value_radar,
        evaluation=evaluation,
        matches_by_game=matches_by_game,
        badge_urls=badge_urls,
        tz=stockholm,
    )
    html = template.render(**tpl_vars)

    subject = (
        f"Stryktipset v.{week_number} #{report.draw_number} — "
        f"{len(report.singles)} singles · {len(report.doubles)} doubles · 1 full · {report.total_cost_sek} SEK"
    )

    logger.info("Newsletter rendered. Subject: %s", subject)
    return subject, html
