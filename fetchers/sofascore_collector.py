"""
Local Sofascore snapshot: collect on the user's PC, apply in the GitHub pipeline.

Sofascore IP-blocks GitHub runners, but a headless-browser session from a home
connection can read its data API. So:

  1. collect_sofascore_snapshot()  — runs LOCALLY (Task Scheduler, Fri evening +
     Sat morning via scripts/collect_sofascore.ps1). Uses the existing
     fetchers/sofascore.py logic through fetchers/sofascore_browser.py and writes
     data/sofascore/draw_{N}.json, which the script commits and pushes.

  2. apply_sofascore_snapshot()    — runs in the pipeline. Fills xG (Championship,
     League One, ...) and missing players from the snapshot, never overwriting
     data from other sources. A missing snapshot is a warning, not an error.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import DATA_DIR
from models.match import InjuryStatus, Match, PlayerAbsence, TeamStats
from utils.logger import get_logger

logger = get_logger(__name__)

SOFASCORE_DIR = DATA_DIR / "sofascore"
_STALE_AFTER_HOURS = 36


class SofascoreCollectionEmpty(RuntimeError):
    """Collection produced no data (Sofascore blocked) — nothing was written."""


def snapshot_path(draw_number: int) -> Path:
    return SOFASCORE_DIR / f"draw_{draw_number}.json"


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _side_to_dict(stats: Optional[TeamStats]) -> dict:
    if stats is None:
        return {"xg": None, "absences": [], "absences_checked": False}
    profile = getattr(stats, "_xg_profile", None)
    xg = None
    if stats.xg_for_avg is not None:
        xg = {
            "xg_for_5g": stats.xg_for_avg,
            "xg_against_5g": stats.xg_against_avg,
            "goals_for_5g": getattr(profile, "goals_for_5g", None),
            "goals_against_5g": getattr(profile, "goals_against_5g", None),
            "matches_available": getattr(profile, "matches_available", None),
        }
    return {
        "xg": xg,
        "absences": [
            {"player_name": a.player_name, "position": a.position, "status": a.status.value}
            for a in stats.injuries if a.source == "sofascore"
        ],
        "absences_checked": stats.absences_checked_source == "sofascore",
    }


def build_snapshot(draw_number: int, matches: list[Match]) -> dict:
    return {
        "draw_number": draw_number,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "games": [
            {
                "game_number": m.game_number,
                "home_team": m.home_team,
                "away_team": m.away_team,
                "home": _side_to_dict(m.home_stats),
                "away": _side_to_dict(m.away_stats),
            }
            for m in matches
        ],
    }


# ---------------------------------------------------------------------------
# Collect (local only)
# ---------------------------------------------------------------------------

def collect_sofascore_snapshot(draw_number: int | None = None) -> Path:
    """Fetch this draw's Sofascore xG + missing players via a browser session and save them."""
    from fetchers import sofascore
    from fetchers.sofascore_browser import browser_session
    from fetchers.svenska_spel import fetch_coupon, fetch_current_coupon

    if draw_number:
        matches = fetch_coupon(draw_number)
    else:
        draw_number, matches = fetch_current_coupon()
    logger.info("Collecting Sofascore snapshot for draw %d (%d matches)", draw_number, len(matches))

    for m in matches:
        m.home_stats = TeamStats(team_name=m.home_team)
        m.away_stats = TeamStats(team_name=m.away_team)

    with browser_session() as get_json:
        sofascore.use_transport(get_json)
        try:
            sofascore.enrich_with_sofascore_xg(matches)
            sofascore.enrich_with_sofascore_absences(matches)
        finally:
            sofascore.use_transport(None)

    snapshot = build_snapshot(draw_number, matches)
    n_xg = sum(1 for g in snapshot["games"] for s in ("home", "away") if g[s]["xg"])
    n_abs = sum(len(g[s]["absences"]) for g in snapshot["games"] for s in ("home", "away"))
    n_checked = sum(1 for g in snapshot["games"] for s in ("home", "away") if g[s]["absences_checked"])

    # A blocked session yields nothing — never let it overwrite an earlier good snapshot.
    if n_xg == 0 and n_checked == 0:
        raise SofascoreCollectionEmpty(
            f"Sofascore returned no xG and no lineups for draw {draw_number} "
            "(likely blocked) — snapshot not written"
        )

    SOFASCORE_DIR.mkdir(parents=True, exist_ok=True)
    path = snapshot_path(draw_number)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "Sofascore snapshot saved to %s: %d teams with xG, %d absences, %d teams checked",
        path, n_xg, n_abs, n_checked,
    )
    return path


# ---------------------------------------------------------------------------
# Apply (pipeline)
# ---------------------------------------------------------------------------

def _apply_side(stats: TeamStats, side: dict) -> tuple[bool, bool]:
    """Returns (xg_applied, absences_applied)."""
    xg_applied = absences_applied = False

    xg = side.get("xg")
    if xg and stats.xg_for_avg is None and xg.get("xg_for_5g") is not None:
        from fetchers.xg_collector import TeamXGProfile
        stats.xg_for_avg = xg["xg_for_5g"]
        stats.xg_against_avg = xg["xg_against_5g"]
        stats._xg_profile = TeamXGProfile(
            team_name=stats.team_name,
            matches_available=xg.get("matches_available") or 5,
            xg_for_5g=xg["xg_for_5g"],
            xg_against_5g=xg["xg_against_5g"],
            goals_for_5g=xg.get("goals_for_5g"),
            goals_against_5g=xg.get("goals_against_5g"),
        )
        xg_applied = True

    if not stats.injuries:
        absences = side.get("absences") or []
        if absences:
            stats.injuries = [
                PlayerAbsence(
                    player_name=a["player_name"],
                    position=a.get("position", ""),
                    status=InjuryStatus(a.get("status", "out")),
                    source="sofascore",
                )
                for a in absences
            ]
            absences_applied = True
        if (absences or side.get("absences_checked")) and not stats.absences_checked_source:
            stats.absences_checked_source = "sofascore"

    return xg_applied, absences_applied


def apply_sofascore_snapshot(matches: list[Match], draw_number: int) -> list[Match]:
    path = snapshot_path(draw_number)
    if not path.exists():
        logger.warning(
            "No Sofascore snapshot for draw %d (%s) — run scripts/collect_sofascore.ps1 "
            "locally. Continuing without Sofascore xG/absences.", draw_number, path.name,
        )
        return matches

    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        age_h = (
            datetime.now(timezone.utc) - datetime.fromisoformat(snapshot["collected_at"])
        ).total_seconds() / 3600
        log = logger.warning if age_h > _STALE_AFTER_HOURS else logger.info
        log("Sofascore snapshot for draw %d collected %.1fh ago", draw_number, age_h)

        games = {g["game_number"]: g for g in snapshot.get("games", [])}
        n_xg = n_abs = 0
        for match in matches:
            g = games.get(match.game_number)
            if not g or g["home_team"] != match.home_team or g["away_team"] != match.away_team:
                logger.warning("Sofascore snapshot: no matching entry for game %d", match.game_number)
                continue
            for side_key, attr, team in (("home", "home_stats", match.home_team), ("away", "away_stats", match.away_team)):
                stats = getattr(match, attr)
                if stats is None:
                    stats = TeamStats(team_name=team)
                    setattr(match, attr, stats)
                xg_ok, abs_ok = _apply_side(stats, g[side_key])
                n_xg += xg_ok
                n_abs += abs_ok
        logger.info("Sofascore snapshot applied: xG for %d teams, absences for %d teams", n_xg, n_abs)
    except Exception as e:
        logger.warning("Failed to apply Sofascore snapshot %s: %s", path.name, e)

    return matches
