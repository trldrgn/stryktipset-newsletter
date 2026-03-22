"""
Stryktipset Newsletter — main entry point.

Usage:
  python main.py                  # Start the scheduler (runs every Saturday 07:30 Stockholm)
  python main.py --run            # Run the newsletter pipeline immediately (for testing)
  python main.py --dry-run        # Run pipeline but don't send email (saves HTML locally)
  python main.py --draw 4945      # Run for a specific draw number
  python main.py --test-email     # Send the most recent newsletter HTML via email (test delivery)
  python main.py --improve        # Print model improvement analysis prompt (monthly use)
  python main.py --collect-xg     # Collect xG data for last 7 days (run Fridays)
  python main.py --backfill-xg    # Backfill xG data for last 5 weeks (first-time setup)
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler

from analysis.claude_analyst import analyse_matches
from analysis.coupon_optimizer import optimise_coupon
from analysis.evaluator import (
    build_improvement_prompt,
    evaluate_last_week,
    save_predictions,
)
from config import (
    NEWSLETTERS_DIR,
    SCHEDULE_DAY,
    SCHEDULE_HOUR,
    SCHEDULE_MINUTE,
    SCHEDULE_TIMEZONE,
)
from email_sender.gmail import send_newsletter
from email_sender.renderer import render_newsletter
from fetchers.api_football import enrich_all_matches
from fetchers.football_data import enrich_with_football_data
from fetchers.perplexity import fetch_all_match_news
from fetchers.understat_xg import enrich_with_understat_xg
from fetchers.svenska_spel import fetch_current_coupon, fetch_coupon
from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

def run_pipeline(draw_number: int | None = None, dry_run: bool = False) -> None:
    """
    Full newsletter pipeline:
      1. Evaluate last week's predictions
      2. Fetch this week's coupon (13 matches)
      3. Enrich with API-Football stats
      4. Fetch Perplexity news context
      5. Analyse with Claude
      6. Optimise coupon (4 singles / 8 doubles / 1 full)
      7. Render HTML email
      8. Send via Gmail
      9. Save this week's predictions for next week's evaluation
    """
    start_time = datetime.now(timezone.utc)
    logger.info("=" * 60)
    logger.info("STRYKTIPSET PIPELINE STARTED at %s", start_time.isoformat())
    if dry_run:
        logger.info("DRY RUN — email will not be sent")
    logger.info("=" * 60)

    # --- Step 1: Evaluate last week ---
    logger.info("[1/9] Evaluating last week's predictions...")
    evaluation = evaluate_last_week()
    if evaluation:
        logger.info(
            "Last week: %d/%d correct (%.1f%%)",
            evaluation.total_correct,
            len(evaluation.evaluations),
            evaluation.accuracy_pct,
        )
    else:
        logger.info("No previous predictions found — skipping evaluation")

    # --- Step 2: Fetch this week's coupon ---
    logger.info("[2/9] Fetching Stryktipset coupon...")
    if draw_number:
        matches = fetch_coupon(draw_number)
        current_draw = draw_number
    else:
        current_draw, matches = fetch_current_coupon()

    logger.info("Draw #%d — %d matches fetched", current_draw, len(matches))

    # Determine season (year of the draw)
    season = matches[0].kickoff.year if matches and matches[0].kickoff else datetime.now().year

    # --- Step 3: API-Football enrichment ---
    logger.info("[3/9] Enriching matches with API-Football stats...")
    matches = enrich_all_matches(matches, season)

    # --- Step 3b: Football-Data.org enrichment (form + standings for PL/Championship/etc.) ---
    logger.info("[3b] Enriching with Football-Data.org (form + standings)...")
    matches = enrich_with_football_data(matches)

    # --- Step 3c: Understat xG enrichment (Big 5 leagues) ---
    logger.info("[3c] Enriching with Understat xG data...")
    matches = enrich_with_understat_xg(matches)

    # --- Step 4: Perplexity news ---
    logger.info("[4/9] Fetching Perplexity news context...")
    matches = fetch_all_match_news(matches)

    # --- Step 5: Claude analysis ---
    logger.info("[5/9] Sending to Claude for analysis...")
    report = analyse_matches(matches, current_draw, evaluation)

    # --- Step 6: Optimise coupon ---
    logger.info("[6/9] Optimising coupon allocation...")
    report = optimise_coupon(report, matches)

    # --- Step 7: Render email ---
    logger.info("[7/9] Rendering HTML email...")
    subject, html_body = render_newsletter(report, matches, evaluation)

    # --- Step 8: Save & send ---
    logger.info("[8/9] Saving and sending newsletter...")
    draw_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    archive_path = NEWSLETTERS_DIR / f"draw_{current_draw}_{draw_date}.html"
    archive_path.write_text(html_body, encoding="utf-8")
    logger.info("Newsletter archived to %s", archive_path)

    if dry_run:
        # Also save a convenient copy in project root for quick preview
        local_path = f"newsletter_draw_{current_draw}.html"
        with open(local_path, "w", encoding="utf-8") as f:
            f.write(html_body)
        logger.info("DRY RUN: HTML also saved to %s", local_path)
        print(f"\nDry run complete. Newsletter saved to: {local_path}")
        print(f"Archived copy: {archive_path}")
    else:
        sent = send_newsletter(subject, html_body)
        logger.info("Newsletter sent to %d recipients", sent)

    # --- Step 9: Save predictions for next week's evaluation ---
    logger.info("[9/9] Saving predictions...")
    save_predictions(current_draw, draw_date, report.predictions)

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    logger.info("=" * 60)
    logger.info("PIPELINE COMPLETE in %.1f seconds", elapsed)
    logger.info("=" * 60)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def start_scheduler() -> None:
    tz = pytz.timezone(SCHEDULE_TIMEZONE)
    scheduler = BlockingScheduler(timezone=tz)

    # Map day string to APScheduler day_of_week value
    day_map = {
        "mon": "mon", "tue": "tue", "wed": "wed", "thu": "thu",
        "fri": "fri", "sat": "sat", "sun": "sun",
    }
    day = day_map.get(SCHEDULE_DAY.lower(), "sat")

    scheduler.add_job(
        run_pipeline,
        trigger="cron",
        day_of_week=day,
        hour=SCHEDULE_HOUR,
        minute=SCHEDULE_MINUTE,
        id="stryktipset_newsletter",
        name="Stryktipset Newsletter",
        misfire_grace_time=3600,   # allow up to 1h late if system was sleeping
    )

    logger.info(
        "Scheduler started. Next run: every %s at %02d:%02d (%s)",
        day.upper(), SCHEDULE_HOUR, SCHEDULE_MINUTE, SCHEDULE_TIMEZONE,
    )
    print(f"\nScheduler running. Next newsletter: every {day.upper()} at "
          f"{SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d} {SCHEDULE_TIMEZONE}")
    print("Press Ctrl+C to stop.\n")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped by user")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stryktipset Newsletter — automated football betting analysis"
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Run the newsletter pipeline now",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run pipeline but save HTML locally instead of sending email",
    )
    parser.add_argument(
        "--improve",
        action="store_true",
        help="Print model improvement analysis prompt for manual Claude session",
    )
    parser.add_argument(
        "--draw",
        type=int,
        metavar="NUMBER",
        help="Run pipeline for a specific draw number (e.g. --draw 4945)",
    )
    parser.add_argument(
        "--collect-xg",
        action="store_true",
        help="Collect xG data from API-Football (run on Fridays)",
    )
    parser.add_argument(
        "--backfill-xg",
        action="store_true",
        help="Backfill xG data for last 5 weeks",
    )
    parser.add_argument(
        "--test-email",
        action="store_true",
        help="Send the most recent newsletter HTML to configured recipients (test delivery)",
    )

    args = parser.parse_args()

    if args.collect_xg or args.backfill_xg:
        from fetchers.xg_collector import collect_xg, _load_history, _request_count, _DAILY_LIMIT
        days = 35 if args.backfill_xg else 7
        logger.info("Collecting xG data for last %d days...", days)
        new = collect_xg(days=days)
        print(f"\nCollected {new} new fixtures.")
        history = _load_history()
        if history["fixtures"]:
            leagues: dict[str, int] = {}
            for f in history["fixtures"].values():
                league = f.get("league", "Unknown")
                leagues[league] = leagues.get(league, 0) + 1
            print("\nxG database:")
            for league, count in sorted(leagues.items(), key=lambda x: -x[1]):
                print(f"  {league}: {count} fixtures")
        return

    if args.improve:
        prompt = build_improvement_prompt()
        print("\n" + "=" * 60)
        print("MODEL IMPROVEMENT PROMPT")
        print("=" * 60)
        print(prompt)
        print("=" * 60)
        print("\nCopy the above and paste it into a Claude conversation to get model improvement recommendations.")
        return

    if args.test_email:
        # Find the most recent archived newsletter
        html_files = sorted(NEWSLETTERS_DIR.glob("draw_*.html"))
        if not html_files:
            # Fall back to project root dry-run files
            from pathlib import Path
            html_files = sorted(Path(".").glob("newsletter_draw_*.html"))
        if not html_files:
            print("No newsletter HTML found. Run --dry-run first.")
            sys.exit(1)
        latest = html_files[-1]
        html_body = latest.read_text(encoding="utf-8")
        subject = f"[TEST] Stryktipset Newsletter — {latest.stem}"
        print(f"Sending test email using: {latest}")
        sent = send_newsletter(subject, html_body)
        print(f"Test email sent to {sent} recipient(s).")
        return

    if args.run or args.dry_run or args.draw:
        draw_number = args.draw or None
        run_pipeline(draw_number=draw_number, dry_run=args.dry_run)
        return

    # Default: start scheduler
    start_scheduler()


if __name__ == "__main__":
    main()
