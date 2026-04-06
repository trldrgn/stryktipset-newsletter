"""Tests for the Sofascore structured-absence fallback.

Covers the draw 4947 failure mode: when API-Football silently returns empty
injury lists, Sofascore's lineups payload must fill the gap before Perplexity
becomes the only source of truth.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import fetchers.sofascore as sofa
from models.match import InjuryStatus, Match, PlayerAbsence, TeamStats


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_match(
    home_team: str = "Arsenal",
    away_team: str = "Brighton",
    country: str = "England",
    home_inj: list | None = None,
    away_inj: list | None = None,
) -> Match:
    m = Match(
        game_number=1,
        draw_number=4948,
        home_team=home_team,
        away_team=away_team,
        league="Premier League",
        country=country,
        kickoff=datetime(2026, 4, 11, 15, 0, tzinfo=timezone.utc),
    )
    m.home_stats = TeamStats(team_name=home_team, injuries=home_inj or [])
    m.away_stats = TeamStats(team_name=away_team, injuries=away_inj or [])
    return m


_LINEUPS_PAYLOAD = {
    "confirmed": False,
    "home": {
        "missingPlayers": [
            {
                "player": {"name": "Eberechi Eze", "position": "M"},
                "type": "missing",
                "description": "Calf Injury",
            },
            {
                "player": {"name": "Bukayo Saka", "position": "F"},
                "type": "doubtful",
                "description": "Physical Discomfort",
            },
            {
                "player": {"name": "Morten Hjulmand", "position": "M"},
                "type": "missing",
                "description": "Yellow card accumulation suspension",
            },
        ],
    },
    "away": {
        "missingPlayers": [
            {
                "player": {"name": "Solly March", "position": "M"},
                "type": "missing",
                "description": "Knee Injury",
            },
        ],
    },
}


# ---------------------------------------------------------------------------
# Pure parsing helpers — no network
# ---------------------------------------------------------------------------

class TestBuildAbsences:
    def test_missing_becomes_out(self):
        result = sofa._build_absences_from_sofa_missing(
            _LINEUPS_PAYLOAD["home"]["missingPlayers"]
        )
        assert len(result) == 3
        eze = result[0]
        assert isinstance(eze, PlayerAbsence)
        assert eze.player_name == "Eberechi Eze"
        assert eze.position == "M"
        assert eze.status == InjuryStatus.OUT

    def test_doubtful_becomes_doubt(self):
        result = sofa._build_absences_from_sofa_missing(
            _LINEUPS_PAYLOAD["home"]["missingPlayers"]
        )
        saka = result[1]
        assert saka.player_name == "Bukayo Saka"
        assert saka.status == InjuryStatus.DOUBT

    def test_empty_list_is_empty(self):
        assert sofa._build_absences_from_sofa_missing([]) == []


class TestResolveEventId:
    def test_matches_by_kickoff_date(self):
        match = _make_match()
        events = [
            {
                "id": 9999,
                "startTimestamp": int(datetime(2026, 4, 5, 15, 0, tzinfo=timezone.utc).timestamp()),
                "homeTeam": {"id": 42},
                "awayTeam": {"id": 7},
            },
            {
                "id": 12345,
                "startTimestamp": int(match.kickoff.timestamp()),
                "homeTeam": {"id": 42},
                "awayTeam": {"id": 17},
            },
        ]
        with patch.object(sofa, "_fetch_team_next_events", return_value=events):
            eid = sofa._resolve_event_id(match, home_sofa_id=42, away_sofa_id=17)
            assert eid == 12345

    def test_returns_none_when_no_kickoff(self):
        match = _make_match()
        match.kickoff = None
        eid = sofa._resolve_event_id(match, home_sofa_id=42)
        assert eid is None


# ---------------------------------------------------------------------------
# Public entry point — mock everything below it
# ---------------------------------------------------------------------------

class TestEnrichWithSofascoreAbsences:
    def setup_method(self):
        # Reset module-level blocked flag between tests
        sofa._blocked = False

    def test_fills_only_empty_sides(self):
        pre_filled = [PlayerAbsence(player_name="Existing", position="D")]
        match = _make_match(home_inj=pre_filled, away_inj=[])  # home already has data

        with patch.object(sofa, "_HAS_CURL_CFFI", True), \
             patch.object(sofa, "_resolve_team_sofa_id", side_effect=[42, 17]), \
             patch.object(sofa, "_resolve_event_id", return_value=12345), \
             patch.object(sofa, "_fetch_lineups", return_value=_LINEUPS_PAYLOAD):
            sofa.enrich_with_sofascore_absences([match])

        # Home already had a player — must not be overwritten
        assert len(match.home_stats.injuries) == 1
        assert match.home_stats.injuries[0].player_name == "Existing"
        # Away was empty — should now be filled from Sofascore
        assert len(match.away_stats.injuries) == 1
        assert match.away_stats.injuries[0].player_name == "Solly March"

    def test_noop_when_both_sides_populated(self):
        match = _make_match(
            home_inj=[PlayerAbsence(player_name="H", position="D")],
            away_inj=[PlayerAbsence(player_name="A", position="D")],
        )
        with patch.object(sofa, "_HAS_CURL_CFFI", True), \
             patch.object(sofa, "_resolve_team_sofa_id") as m_resolve:
            sofa.enrich_with_sofascore_absences([match])
            m_resolve.assert_not_called()

    def test_graceful_when_lineups_missing(self):
        match = _make_match()
        with patch.object(sofa, "_HAS_CURL_CFFI", True), \
             patch.object(sofa, "_resolve_team_sofa_id", side_effect=[42, 17]), \
             patch.object(sofa, "_resolve_event_id", return_value=12345), \
             patch.object(sofa, "_fetch_lineups", return_value=None):
            sofa.enrich_with_sofascore_absences([match])
        assert match.home_stats.injuries == []
        assert match.away_stats.injuries == []

    def test_skipped_when_curl_cffi_missing(self):
        match = _make_match()
        with patch.object(sofa, "_HAS_CURL_CFFI", False), \
             patch.object(sofa, "_resolve_team_sofa_id") as m_resolve:
            sofa.enrich_with_sofascore_absences([match])
            m_resolve.assert_not_called()

    def test_blocked_short_circuits(self):
        match1 = _make_match(home_team="Arsenal", away_team="Brighton")
        match2 = _make_match(home_team="Chelsea", away_team="Fulham")

        def _resolve_then_block(name, country):
            # Trip the blocked flag on the first call
            sofa._blocked = True
            return None

        with patch.object(sofa, "_HAS_CURL_CFFI", True), \
             patch.object(sofa, "_resolve_team_sofa_id", side_effect=_resolve_then_block), \
             patch.object(sofa, "_resolve_event_id") as m_event:
            sofa.enrich_with_sofascore_absences([match1, match2])
            # Second match should not trigger further lookups
            m_event.assert_not_called()

    def test_fills_home_and_away_when_both_empty(self):
        match = _make_match()
        with patch.object(sofa, "_HAS_CURL_CFFI", True), \
             patch.object(sofa, "_resolve_team_sofa_id", side_effect=[42, 17]), \
             patch.object(sofa, "_resolve_event_id", return_value=12345), \
             patch.object(sofa, "_fetch_lineups", return_value=_LINEUPS_PAYLOAD):
            sofa.enrich_with_sofascore_absences([match])
        assert len(match.home_stats.injuries) == 3
        assert len(match.away_stats.injuries) == 1
        # Status mapping sanity check
        assert match.home_stats.injuries[0].status == InjuryStatus.OUT
        assert match.home_stats.injuries[1].status == InjuryStatus.DOUBT
