"""Tests for team-perspective form semantics and the hardened fatigue flag.

Draw 4947 (2026-04-04) shipped a newsletter with Arsenal's form badge reading
WWDLW instead of the real WWDWW — a Brighton away win rendered as a loss.
Root cause: FormResult.result was stored match-perspective by both fetchers,
but downstream consumers (FormResult.points, the Jinja wdl map) read it as
team-perspective. These tests lock the convention in place.
"""

from unittest.mock import patch

from fetchers.api_football import _fixture_to_form
from fetchers.football_data import _form_result, _resolve_team
from models.match import FormResult, Outcome, ScheduleContext, TeamStats


# ---------------------------------------------------------------------------
# FormResult.points / form_points_last5 — the UP-FORM / DOWN-FORM driver
# ---------------------------------------------------------------------------

def _fr(result: Outcome, home_or_away: str = "H") -> FormResult:
    return FormResult(
        opponent="Opp",
        home_or_away=home_or_away,
        goals_for=1,
        goals_against=0,
        result=result,
    )


class TestFormResultPoints:
    def test_win_is_three_points(self):
        assert _fr(Outcome.HOME).points == 3

    def test_draw_is_one_point(self):
        assert _fr(Outcome.DRAW).points == 1

    def test_loss_is_zero_points(self):
        assert _fr(Outcome.AWAY).points == 0

    def test_form_points_four_wins_one_draw(self):
        stats = TeamStats(
            team_name="Arsenal",
            form_last5=[
                _fr(Outcome.HOME, "H"),
                _fr(Outcome.HOME, "A"),
                _fr(Outcome.HOME, "H"),
                _fr(Outcome.HOME, "A"),
                _fr(Outcome.DRAW, "H"),
            ],
        )
        assert stats.form_points_last5 == 13

    def test_form_points_away_wins_still_count(self):
        """Regression: before the fix, away wins were read as losses → 0 points.
        This is exactly how Arsenal's Brighton away win became an 'L' badge.
        """
        stats = TeamStats(
            team_name="Arsenal",
            form_last5=[_fr(Outcome.HOME, "A")] * 3,
        )
        assert stats.form_points_last5 == 9


# ---------------------------------------------------------------------------
# api_football._fixture_to_form — team perspective
# ---------------------------------------------------------------------------

class TestApiFootballFixtureToForm:
    def test_team_won_away(self):
        fixture = {
            "fixture": {"status": {"short": "FT"}},
            "league": {"name": "Premier League"},
            "teams": {
                "home": {"id": 10, "name": "Brighton", "winner": False},
                "away": {"id": 42, "name": "Arsenal", "winner": True},
            },
            "goals": {"home": 0, "away": 1},
        }
        fr = _fixture_to_form(fixture, team_id=42)
        assert fr.result == Outcome.HOME  # team-perspective: win
        assert fr.home_or_away == "A"
        assert fr.goals_for == 1
        assert fr.goals_against == 0
        assert fr.competition == "Premier League"
        assert fr.points == 3

    def test_team_lost_at_home(self):
        fixture = {
            "fixture": {"status": {"short": "FT"}},
            "league": {"name": "Premier League"},
            "teams": {
                "home": {"id": 42, "name": "Arsenal", "winner": False},
                "away": {"id": 10, "name": "Man City", "winner": True},
            },
            "goals": {"home": 0, "away": 2},
        }
        fr = _fixture_to_form(fixture, team_id=42)
        assert fr.result == Outcome.AWAY
        assert fr.points == 0

    def test_draw_is_draw(self):
        fixture = {
            "fixture": {"status": {"short": "FT"}},
            "league": {"name": "Premier League"},
            "teams": {
                "home": {"id": 42, "name": "Arsenal", "winner": None},
                "away": {"id": 10, "name": "Chelsea", "winner": None},
            },
            "goals": {"home": 1, "away": 1},
        }
        fr = _fixture_to_form(fixture, team_id=42)
        assert fr.result == Outcome.DRAW
        assert fr.points == 1


# ---------------------------------------------------------------------------
# football_data._form_result — team perspective
# ---------------------------------------------------------------------------

class TestFootballDataFormResult:
    def test_team_won_away(self):
        fixture = {
            "status": "FINISHED",
            "utcDate": "2026-03-15T15:00:00Z",
            "competition": {"name": "Premier League"},
            "homeTeam": {"id": 10, "name": "Brighton"},
            "awayTeam": {"id": 42, "name": "Arsenal"},
            "score": {
                "winner": "AWAY_TEAM",
                "fullTime": {"home": 0, "away": 1},
            },
        }
        fr = _form_result(fixture, team_id=42)
        assert fr is not None
        assert fr.result == Outcome.HOME  # team perspective
        assert fr.home_or_away == "A"
        assert fr.points == 3
        assert fr.competition == "Premier League"

    def test_team_lost_at_home(self):
        fixture = {
            "status": "FINISHED",
            "utcDate": "2026-03-15T15:00:00Z",
            "competition": {"name": "Premier League"},
            "homeTeam": {"id": 42, "name": "Arsenal"},
            "awayTeam": {"id": 10, "name": "Man City"},
            "score": {
                "winner": "AWAY_TEAM",
                "fullTime": {"home": 0, "away": 2},
            },
        }
        fr = _form_result(fixture, team_id=42)
        assert fr is not None
        assert fr.result == Outcome.AWAY
        assert fr.points == 0

    def test_draw(self):
        fixture = {
            "status": "FINISHED",
            "utcDate": "2026-03-15T15:00:00Z",
            "competition": {"name": "Premier League"},
            "homeTeam": {"id": 42, "name": "Arsenal"},
            "awayTeam": {"id": 10, "name": "Chelsea"},
            "score": {
                "winner": "DRAW",
                "fullTime": {"home": 1, "away": 1},
            },
        }
        fr = _form_result(fixture, team_id=42)
        assert fr.result == Outcome.DRAW


# ---------------------------------------------------------------------------
# TeamStats.fatigue_flag — harden against days_since_last_match=0 data bug
# ---------------------------------------------------------------------------

class TestFatigueFlagGuard:
    def test_zero_days_without_corroboration_is_ignored(self):
        """Port Vale (draw 4947) inherited a wrong team's fixtures and ended up
        with days_since_last_match=0. Without corroborating match density,
        that must NOT trip the fatigue flag.
        """
        stats = TeamStats(
            team_name="Port Vale",
            schedule=ScheduleContext(days_since_last_match=0, matches_last_14_days=1),
        )
        assert stats.fatigue_flag is False

    def test_zero_days_with_corroboration_still_fires(self):
        stats = TeamStats(
            team_name="Madrid",
            schedule=ScheduleContext(days_since_last_match=0, matches_last_14_days=4),
        )
        assert stats.fatigue_flag is True

    def test_two_days_ago_is_fatigue(self):
        stats = TeamStats(
            team_name="Chelsea",
            schedule=ScheduleContext(days_since_last_match=2, matches_last_14_days=1),
        )
        assert stats.fatigue_flag is True

    def test_seven_days_ago_no_congestion_not_fatigue(self):
        stats = TeamStats(
            team_name="Southampton",
            schedule=ScheduleContext(days_since_last_match=7, matches_last_14_days=1),
        )
        assert stats.fatigue_flag is False

    def test_dense_schedule_without_recent_match_still_fires(self):
        stats = TeamStats(
            team_name="Madrid",
            schedule=ScheduleContext(days_since_last_match=5, matches_last_14_days=4),
        )
        assert stats.fatigue_flag is True


# ---------------------------------------------------------------------------
# Fail-closed fuzzy team resolution in football_data.py
# ---------------------------------------------------------------------------

def _standings(names: list[str]) -> dict:
    return {
        "standings": [
            {
                "type": "TOTAL",
                "table": [
                    {
                        "position": i + 1,
                        "points": 50 - i,
                        "team": {"id": i + 1, "name": n, "shortName": n, "tla": ""},
                    }
                    for i, n in enumerate(names)
                ],
            }
        ]
    }


class TestResolveTeamFailsClosed:
    def test_exact_match_resolves(self):
        st = _standings(["Arsenal FC", "Manchester City"])
        row = _resolve_team("Arsenal FC", st)
        assert row is not None
        assert row["team"]["name"] == "Arsenal FC"

    def test_port_vale_in_championship_fails_closed(self):
        """Port Vale (League One) must NOT be matched against Championship teams.
        The old threshold (50) accepted a weak word-overlap match and inherited
        the wrong team's fixtures. Threshold is now 80.
        """
        championship = _standings([
            "Norwich City", "Queens Park Rangers", "Derby County",
            "Swansea City", "Blackburn Rovers", "Portsmouth FC",
        ])
        assert _resolve_team("Port Vale", championship) is None

    def test_strong_substring_match_accepted(self):
        st = _standings(["Manchester United", "Manchester City"])
        row = _resolve_team("Manchester United", st)
        assert row is not None
        assert row["team"]["name"] == "Manchester United"
