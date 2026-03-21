"""
Shared pytest fixtures for the Stryktipset newsletter test suite.

All external HTTP calls must be mocked — tests never hit live APIs.
Use saved JSON fixtures from tests/fixtures/ for realistic response data.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Inject dummy env vars before any project module is imported.
# This prevents config.py from raising KeyError on missing .env file.
# Must happen before any project imports below.
# ---------------------------------------------------------------------------
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("PERPLEXITY_API_KEY", "test-perplexity-key")
os.environ.setdefault("API_FOOTBALL_KEY", "test-api-football-key")
os.environ.setdefault("GMAIL_SENDER", "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "test-app-password")
os.environ.setdefault("NEWSLETTER_RECIPIENTS", "test@example.com")

from models.match import (
    FormResult,
    H2HResult,
    InjuryStatus,
    MarketSignals,
    Match,
    MatchPrediction,
    MatchupRisk,
    NewsContext,
    Outcome,
    PlayerAbsence,
    RiskLevel,
    ScheduleContext,
    SelectionType,
    TeamStats,
    WeeklyEvaluation,
    WeeklyReport,
    MatchEvaluation,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# JSON fixture loader
# ---------------------------------------------------------------------------

def load_fixture(name: str) -> dict:
    """Load a JSON fixture file from tests/fixtures/."""
    path = FIXTURES_DIR / name
    if not path.exists():
        pytest.skip(f"Fixture not found: {name}")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Match fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_market() -> MarketSignals:
    return MarketSignals(
        odds_home=2.10,
        odds_draw=3.40,
        odds_away=3.80,
        public_pct_home=55,
        public_pct_draw=25,
        public_pct_away=20,
        newspaper_tips_home=7,
        newspaper_tips_draw=2,
        newspaper_tips_away=1,
    )


@pytest.fixture
def sample_form() -> list[FormResult]:
    return [
        FormResult("Chelsea", "H", 2, 0, Outcome.HOME),
        FormResult("Liverpool", "A", 1, 1, Outcome.DRAW),
        FormResult("Wolves", "H", 3, 1, Outcome.HOME),
        FormResult("Everton", "A", 0, 2, Outcome.AWAY),
        FormResult("Newcastle", "H", 1, 0, Outcome.HOME),
    ]


@pytest.fixture
def sample_team_stats(sample_form) -> TeamStats:
    return TeamStats(
        team_name="Arsenal",
        team_id=42,
        league_position=2,
        league_points=58,
        form_last5=sample_form,
        xg_for_avg=2.1,
        xg_against_avg=0.9,
        xg_overperformance=0.4,
        schedule=ScheduleContext(
            days_since_last_match=5,
            last_match_competition="Premier League",
            matches_last_14_days=2,
        ),
        manager_name="Mikel Arteta",
        manager_weeks_in_post=200,
    )


@pytest.fixture
def fatigued_team_stats(sample_form) -> TeamStats:
    """A team that played 3 days ago — fatigue_flag should be True."""
    return TeamStats(
        team_name="Chelsea",
        team_id=49,
        form_last5=sample_form,
        schedule=ScheduleContext(
            days_since_last_match=3,
            last_match_competition="Europa League",
            matches_last_14_days=5,
        ),
    )


@pytest.fixture
def new_manager_team() -> TeamStats:
    """A team with a brand-new manager — new_manager_bounce should be True."""
    return TeamStats(
        team_name="Southampton",
        team_id=33,
        manager_name="New Boss",
        manager_weeks_in_post=3,
    )


@pytest.fixture
def player_absence_top_scorer() -> PlayerAbsence:
    return PlayerAbsence(
        player_name="Erling Haaland",
        position="ST",
        status=InjuryStatus.OUT,
        season_goals=22,
        season_assists=5,
        is_top_scorer=True,
        replacement_name="Julian Alvarez",
        replacement_quality="adequate",
    )


@pytest.fixture
def player_absence_with_matchup_risk() -> PlayerAbsence:
    return PlayerAbsence(
        player_name="Oleksandr Zinchenko",
        position="LB",
        status=InjuryStatus.DOUBT,
        season_goals=1,
        season_assists=4,
        replacement_name="Kieran Tierney",
        replacement_quality="poor",
        matchup_risk=MatchupRisk(
            opponent_player="Pedro Neto",
            opponent_position="RW",
            opponent_goals=12,
            opponent_assists=7,
            risk_level=RiskLevel.HIGH,
            note="Chelsea RW Neto attacks the exposed LB channel.",
        ),
    )


@pytest.fixture
def sample_match(sample_market, sample_team_stats) -> Match:
    away_stats = TeamStats(
        team_name="Brentford",
        team_id=55,
        league_position=12,
        league_points=32,
        form_last5=[
            FormResult("Arsenal", "A", 0, 2, Outcome.AWAY),
            FormResult("Brighton", "H", 1, 1, Outcome.DRAW),
            FormResult("Fulham", "A", 2, 1, Outcome.HOME),
            FormResult("Wolves", "H", 0, 0, Outcome.DRAW),
            FormResult("Everton", "A", 1, 0, Outcome.HOME),
        ],
        xg_for_avg=1.3,
        xg_against_avg=1.5,
    )
    return Match(
        game_number=1,
        draw_number=4945,
        home_team="Arsenal",
        away_team="Brentford",
        league="Premier League",
        country="England",
        kickoff=datetime(2026, 3, 22, 15, 0, tzinfo=timezone.utc),
        market=sample_market,
        home_stats=sample_team_stats,
        away_stats=away_stats,
        h2h=[
            H2HResult("2025-10-05", "Arsenal", "Brentford", 2, 1),
            H2HResult("2025-02-17", "Brentford", "Arsenal", 1, 2),
        ],
    )


@pytest.fixture
def sample_matches(sample_match) -> list[Match]:
    """13 matches — all filled with minimal data for pipeline tests."""
    matches = []
    for i in range(1, 14):
        m = Match(
            game_number=i,
            draw_number=4945,
            home_team=f"HomeTeam{i}",
            away_team=f"AwayTeam{i}",
            league="Premier League",
            country="England",
            kickoff=datetime(2026, 3, 22, 15 + (i % 3), 0, tzinfo=timezone.utc),
            market=MarketSignals(
                odds_home=2.0 + i * 0.1,
                odds_draw=3.0 + i * 0.05,
                odds_away=3.5 - i * 0.05,
                public_pct_home=50,
                public_pct_draw=25,
                public_pct_away=25,
            ),
        )
        matches.append(m)
    return matches


# ---------------------------------------------------------------------------
# Prediction fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_predictions() -> list[MatchPrediction]:
    """13 predictions with varying confidence scores for optimizer tests."""
    confidences = [0.92, 0.88, 0.85, 0.82, 0.75, 0.72, 0.68, 0.65, 0.60, 0.55, 0.52, 0.50, 0.40]
    predictions = []
    for i, conf in enumerate(confidences, 1):
        predictions.append(MatchPrediction(
            game_number=i,
            home_team=f"HomeTeam{i}",
            away_team=f"AwayTeam{i}",
            predicted_outcomes=[Outcome.HOME, Outcome.DRAW],
            selection_type=SelectionType.DOUBLE,
            confidence=conf,
            analysis=f"Analysis for game {i}.",
            key_factors=[f"Factor A{i}", f"Factor B{i}"],
            risk_flags=[f"Risk {i}"],
        ))
    return predictions


@pytest.fixture
def sample_weekly_report(sample_predictions) -> WeeklyReport:
    return WeeklyReport(
        draw_number=4945,
        generated_at=datetime(2026, 3, 21, 8, 0, tzinfo=timezone.utc),
        predictions=sample_predictions,
        executive_summary="Strong week for home sides. Watch fatigue in midweek fixtures.",
        value_radar=["Game 3 looks mispriced — public over-backing home side."],
    )


# ---------------------------------------------------------------------------
# Evaluation fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_evaluation() -> WeeklyEvaluation:
    evals = [
        MatchEvaluation(
            game_number=1,
            home_team="Arsenal",
            away_team="Brentford",
            our_prediction=[Outcome.HOME],
            selection_type=SelectionType.SINGLE,
            actual_result=Outcome.HOME,
            correct=True,
        ),
        MatchEvaluation(
            game_number=2,
            home_team="Chelsea",
            away_team="Liverpool",
            our_prediction=[Outcome.HOME],
            selection_type=SelectionType.SINGLE,
            actual_result=Outcome.DRAW,
            correct=False,
            post_mortem="High-confidence single failed. Chelsea rotated after UCL.",
        ),
        MatchEvaluation(
            game_number=3,
            home_team="Bayern",
            away_team="Dortmund",
            our_prediction=[Outcome.HOME, Outcome.DRAW, Outcome.AWAY],
            selection_type=SelectionType.FULL,
            actual_result=Outcome.HOME,
            correct=True,
        ),
    ]
    return WeeklyEvaluation(
        draw_number=4944,
        draw_date="2026-03-14",
        evaluations=evals,
        total_correct=2,
        singles_correct=1,
        singles_total=2,
        doubles_correct=0,
        doubles_total=0,
        full_covered=True,
        lessons=["Consider rotation risk before assigning singles to midweek-fatigued teams."],
        feedback_summary="Last week: 2/3 correct (66.7%). Singles: 1/2. Full: covered.",
    )


# ---------------------------------------------------------------------------
# Svenska Spel API response fixtures
# ---------------------------------------------------------------------------

SVENSKA_SPEL_DRAW_RESPONSE = {
    "draw": {
        "productName": "Stryktipset",
        "productId": 1,
        "drawNumber": 4945,
        "drawState": "Open",
        "drawComment": "Stryktipset v. 2026-12",
        "regCloseTime": "2026-03-22T15:59:00+01:00",
        "drawEvents": [
            {
                "eventNumber": i,
                "eventDescription": f"HomeTeam{i} - AwayTeam{i}",
                "startOdds": {"one": "2,10", "x": "3,40", "two": "3,80"},
                "betMetrics": {
                    "values": [
                        {"outcome": "1", "odds": {"odds": "2,10"}, "distribution": {"distribution": "55"}},
                        {"outcome": "X", "odds": {"odds": "3,40"}, "distribution": {"distribution": "25"}},
                        {"outcome": "2", "odds": {"odds": "3,80"}, "distribution": {"distribution": "20"}},
                    ]
                },
                "svenskaFolket": {"one": "55", "x": "25", "two": "20"},
                "tioTidningarsTips": {"one": 7, "x": 2, "two": 1},
                "match": {
                    "matchId": 80000 + i,
                    "matchStart": f"2026-03-22T{15 + (i % 3):02d}:00:00+01:00",
                    "status": "Inte startat",
                    "participants": [
                        {"id": 100 + i, "type": "home", "name": f"HomeTeam{i}", "countryName": "England"},
                        {"id": 200 + i, "type": "away", "name": f"AwayTeam{i}", "countryName": "England"},
                    ],
                    "league": {"id": 1, "name": "Premier League", "country": {"name": "England"}},
                },
            }
            for i in range(1, 14)
        ],
    },
    "error": None,
}


@pytest.fixture
def svenska_spel_draw_response() -> dict:
    return SVENSKA_SPEL_DRAW_RESPONSE


@pytest.fixture
def svenska_spel_result_response() -> dict:
    """A finalized draw with outcomes."""
    resp = {
        "draw": {
            "drawNumber": 4944,
            "drawEvents": [
                {
                    "eventNumber": i,
                    "outcome": ["1", "X", "2"][(i - 1) % 3],
                    "outcomeDescription": ["1", "X", "2"][(i - 1) % 3],
                }
                for i in range(1, 14)
            ],
        },
        "error": None,
    }
    return resp
