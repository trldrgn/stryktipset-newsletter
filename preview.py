"""
Re-render the newsletter HTML from saved snapshot — no API calls needed.

Usage:
  python preview.py                              # uses latest snapshot
  python preview.py --send                       # re-render + send test email
  python preview.py --snapshot                   # save current pipeline data as snapshot
                                                 # (run after --dry-run to capture full data)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from config import DATA_DIR, PREDICTIONS_DIR
from email_sender.gmail import send_newsletter
from email_sender.renderer import render_newsletter
from models.match import (
    FormResult,
    H2HResult,
    MarketSignals,
    Match,
    MatchPrediction,
    Outcome,
    PlayerAbsence,
    ScheduleContext,
    SelectionType,
    TeamStats,
    WeeklyReport,
)
from utils.logger import get_logger

logger = get_logger(__name__)

SNAPSHOT_DIR = DATA_DIR / "snapshots"
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)


def save_snapshot(
    draw_number: int,
    report: WeeklyReport,
    matches: list[Match],
) -> Path:
    """Save the full pipeline state as a JSON snapshot for preview.py."""

    def _team_stats_to_dict(ts: TeamStats | None) -> dict | None:
        if ts is None:
            return None
        return {
            "team_name": ts.team_name,
            "team_id": ts.team_id,
            "league_position": ts.league_position,
            "league_points": ts.league_points,
            "form_last5": [
                {"opponent": r.opponent, "home_or_away": r.home_or_away,
                 "goals_for": r.goals_for, "goals_against": r.goals_against,
                 "result": r.result.value, "xg_for": r.xg_for, "xg_against": r.xg_against}
                for r in ts.form_last5
            ],
            "form_last5_home_only": [
                {"opponent": r.opponent, "home_or_away": r.home_or_away,
                 "goals_for": r.goals_for, "goals_against": r.goals_against,
                 "result": r.result.value, "xg_for": r.xg_for, "xg_against": r.xg_against}
                for r in ts.form_last5_home_only
            ],
            "form_last5_away_only": [
                {"opponent": r.opponent, "home_or_away": r.home_or_away,
                 "goals_for": r.goals_for, "goals_against": r.goals_against,
                 "result": r.result.value, "xg_for": r.xg_for, "xg_against": r.xg_against}
                for r in ts.form_last5_away_only
            ],
            "xg_for_avg": ts.xg_for_avg,
            "xg_against_avg": ts.xg_against_avg,
            "injuries": [
                {"player_name": a.player_name, "position": a.position, "status": a.status.value}
                for a in ts.injuries
            ],
            "schedule": {
                "days_since_last_match": ts.schedule.days_since_last_match,
                "last_match_competition": ts.schedule.last_match_competition,
                "matches_last_14_days": ts.schedule.matches_last_14_days,
            } if ts.schedule else None,
            "manager_name": ts.manager_name,
            "manager_weeks_in_post": ts.manager_weeks_in_post,
        }

    def _market_to_dict(m: MarketSignals | None) -> dict | None:
        if m is None:
            return None
        return {
            "odds_home": m.odds_home, "odds_draw": m.odds_draw, "odds_away": m.odds_away,
            "public_pct_home": m.public_pct_home, "public_pct_draw": m.public_pct_draw,
            "public_pct_away": m.public_pct_away,
            "newspaper_tips_home": m.newspaper_tips_home,
            "newspaper_tips_draw": m.newspaper_tips_draw,
            "newspaper_tips_away": m.newspaper_tips_away,
        }

    snapshot = {
        "draw_number": draw_number,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "executive_summary": report.executive_summary,
        "value_radar": report.value_radar,
        "matches": [
            {
                "game_number": m.game_number,
                "draw_number": m.draw_number,
                "home_team": m.home_team,
                "away_team": m.away_team,
                "league": m.league,
                "country": m.country,
                "kickoff": m.kickoff.isoformat() if m.kickoff else None,
                "market": _market_to_dict(m.market),
                "home_stats": _team_stats_to_dict(m.home_stats),
                "away_stats": _team_stats_to_dict(m.away_stats),
                "h2h": [
                    {"date": h.date, "home_team": h.home_team, "away_team": h.away_team,
                     "home_goals": h.home_goals, "away_goals": h.away_goals, "venue": h.venue}
                    for h in m.h2h
                ],
            }
            for m in matches
        ],
        "predictions": [
            {
                "game_number": p.game_number,
                "home_team": p.home_team,
                "away_team": p.away_team,
                "predicted_outcomes": [o.value for o in p.predicted_outcomes],
                "selection_type": p.selection_type.value,
                "confidence": p.confidence,
                "analysis": p.analysis,
                "key_factors": p.key_factors,
                "risk_flags": p.risk_flags,
                "value_note": p.value_note,
            }
            for p in report.predictions
        ],
    }

    path = SNAPSHOT_DIR / f"draw_{draw_number}.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Snapshot saved to %s", path)
    return path


def _load_from_snapshot(path: Path) -> tuple[WeeklyReport, list[Match]]:
    """Load full pipeline state from a snapshot JSON."""
    raw = json.loads(path.read_text(encoding="utf-8"))

    def _parse_form(form_list: list[dict]) -> list[FormResult]:
        return [
            FormResult(
                opponent=r["opponent"], home_or_away=r["home_or_away"],
                goals_for=r["goals_for"], goals_against=r["goals_against"],
                result=Outcome(r["result"]),
                xg_for=r.get("xg_for"), xg_against=r.get("xg_against"),
            )
            for r in form_list
        ]

    def _parse_team_stats(d: dict | None) -> TeamStats | None:
        if d is None:
            return None
        ts = TeamStats(team_name=d["team_name"], team_id=d.get("team_id"))
        ts.league_position = d.get("league_position")
        ts.league_points = d.get("league_points")
        ts.form_last5 = _parse_form(d.get("form_last5", []))
        ts.form_last5_home_only = _parse_form(d.get("form_last5_home_only", []))
        ts.form_last5_away_only = _parse_form(d.get("form_last5_away_only", []))
        ts.xg_for_avg = d.get("xg_for_avg")
        ts.xg_against_avg = d.get("xg_against_avg")
        ts.injuries = [
            PlayerAbsence(player_name=a["player_name"], position=a["position"])
            for a in d.get("injuries", [])
        ]
        if d.get("schedule"):
            ts.schedule = ScheduleContext(**d["schedule"])
        ts.manager_name = d.get("manager_name", "")
        ts.manager_weeks_in_post = d.get("manager_weeks_in_post")
        return ts

    def _parse_market(d: dict | None) -> MarketSignals | None:
        if d is None:
            return None
        return MarketSignals(**d)

    matches: list[Match] = []
    for m in raw["matches"]:
        match = Match(
            game_number=m["game_number"],
            draw_number=m["draw_number"],
            home_team=m["home_team"],
            away_team=m["away_team"],
            league=m["league"],
            country=m["country"],
        )
        if m.get("kickoff"):
            match.kickoff = datetime.fromisoformat(m["kickoff"])
        match.market = _parse_market(m.get("market"))
        match.home_stats = _parse_team_stats(m.get("home_stats"))
        match.away_stats = _parse_team_stats(m.get("away_stats"))
        match.h2h = [H2HResult(**h) for h in m.get("h2h", [])]
        matches.append(match)

    predictions: list[MatchPrediction] = []
    for p in raw["predictions"]:
        predictions.append(MatchPrediction(
            game_number=p["game_number"],
            home_team=p["home_team"],
            away_team=p["away_team"],
            predicted_outcomes=[Outcome(o) for o in p["predicted_outcomes"]],
            selection_type=SelectionType(p["selection_type"]),
            confidence=p["confidence"],
            analysis=p.get("analysis", ""),
            key_factors=p.get("key_factors", []),
            risk_flags=p.get("risk_flags", []),
            value_note=p.get("value_note", ""),
        ))

    singles = [p.game_number for p in predictions if p.selection_type == SelectionType.SINGLE]
    doubles = [p.game_number for p in predictions if p.selection_type == SelectionType.DOUBLE]
    full = [p.game_number for p in predictions if p.selection_type == SelectionType.FULL]

    report = WeeklyReport(
        draw_number=raw["draw_number"],
        generated_at=datetime.now(timezone.utc),
        predictions=predictions,
        singles=singles,
        doubles=doubles,
        full_game=full[0] if full else None,
        total_rows=768,
        total_cost_sek=768,
        executive_summary=raw.get("executive_summary", ""),
        value_radar=raw.get("value_radar", []),
    )

    return report, matches


def _load_from_predictions(path: Path) -> tuple[WeeklyReport, list[Match]]:
    """Fallback: load from predictions-only JSON (no match stats)."""
    raw = json.loads(path.read_text(encoding="utf-8"))

    predictions: list[MatchPrediction] = []
    matches: list[Match] = []

    for p in raw["predictions"]:
        predictions.append(MatchPrediction(
            game_number=p["game_number"],
            home_team=p["home_team"],
            away_team=p["away_team"],
            predicted_outcomes=[Outcome(o) for o in p["predicted_outcomes"]],
            selection_type=SelectionType(p["selection_type"]),
            confidence=p["confidence"],
            analysis=p.get("analysis", ""),
            key_factors=p.get("key_factors", []),
            risk_flags=p.get("risk_flags", []),
            value_note=p.get("value_note", ""),
        ))
        matches.append(Match(
            game_number=p["game_number"],
            draw_number=raw["draw_number"],
            home_team=p["home_team"],
            away_team=p["away_team"],
            league="", country="",
        ))

    singles = [p.game_number for p in predictions if p.selection_type == SelectionType.SINGLE]
    doubles = [p.game_number for p in predictions if p.selection_type == SelectionType.DOUBLE]
    full = [p.game_number for p in predictions if p.selection_type == SelectionType.FULL]

    report = WeeklyReport(
        draw_number=raw["draw_number"],
        generated_at=datetime.now(timezone.utc),
        predictions=predictions,
        singles=singles, doubles=doubles,
        full_game=full[0] if full else None,
        total_rows=768, total_cost_sek=768,
        executive_summary=raw.get("executive_summary", ""),
        value_radar=raw.get("value_radar", []),
    )
    return report, matches


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview newsletter from saved data")
    parser.add_argument("json_file", nargs="?", help="Path to snapshot or predictions JSON")
    parser.add_argument("--send", action="store_true", help="Also send test email")
    parser.add_argument("--snapshot", action="store_true",
                        help="Save current pipeline data as snapshot (run after --dry-run)")
    args = parser.parse_args()

    if args.snapshot:
        # Re-run the fetch steps (cached, instant) to capture full Match objects
        from fetchers.svenska_spel import fetch_coupon
        from fetchers.api_football import enrich_all_matches
        from fetchers.football_data import enrich_with_football_data
        from fetchers.understat_xg import enrich_with_understat_xg

        # Find the latest predictions to know which draw
        files = sorted(PREDICTIONS_DIR.glob("draw_*.json"))
        if not files:
            print("No predictions found. Run --dry-run first.")
            sys.exit(1)
        pred_path = files[-1]
        raw = json.loads(pred_path.read_text(encoding="utf-8"))
        draw_number = raw["draw_number"]

        print(f"Building snapshot for draw #{draw_number}...")
        matches = fetch_coupon(draw_number)
        season = matches[0].kickoff.year if matches and matches[0].kickoff else datetime.now().year
        matches = enrich_all_matches(matches, season)
        matches = enrich_with_football_data(matches)
        matches = enrich_with_understat_xg(matches)

        # Load report from predictions
        report, _ = _load_from_predictions(pred_path)
        path = save_snapshot(draw_number, report, matches)
        print(f"Snapshot saved to: {path}")
        return

    # Load data
    if args.json_file:
        path = Path(args.json_file)
    else:
        # Prefer snapshot, fall back to predictions
        snap_files = sorted(SNAPSHOT_DIR.glob("draw_*.json"))
        pred_files = sorted(PREDICTIONS_DIR.glob("draw_*.json"))
        if snap_files:
            path = snap_files[-1]
        elif pred_files:
            path = pred_files[-1]
        else:
            print("No data found. Run --dry-run first.")
            sys.exit(1)

    print(f"Loading from: {path}")

    # Detect if it's a snapshot (has "matches" key) or predictions-only
    raw = json.loads(path.read_text(encoding="utf-8"))
    if "matches" in raw:
        report, matches = _load_from_snapshot(path)
    else:
        report, matches = _load_from_predictions(path)

    subject, html = render_newsletter(report, matches)

    out = f"newsletter_draw_{report.draw_number}.html"
    Path(out).write_text(html, encoding="utf-8")
    print(f"Rendered to: {out} ({len(html)//1024}KB)")

    if args.send:
        sent = send_newsletter(f"[TEST] {subject}", html)
        print(f"Test email sent to {sent} recipient(s).")


if __name__ == "__main__":
    main()
