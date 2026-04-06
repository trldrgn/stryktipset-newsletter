"""Tests for the fixture-anchored Perplexity rewrite (Tier 2B)."""

from datetime import datetime, timezone
from unittest.mock import patch

import fetchers.perplexity as ppx
from models.match import Match


def _make_match() -> Match:
    return Match(
        game_number=1,
        draw_number=4948,
        home_team="Arsenal",
        away_team="Brighton",
        league="Premier League",
        country="England",
        kickoff=datetime(2026, 4, 11, 15, 0, tzinfo=timezone.utc),
    )


_GOOD_RESPONSE = """[PREVIEW] Arsenal host Brighton at the Emirates in what the Guardian
describes as a pivotal run-in fixture. Sky Sports expects Arteta to rotate
in light of the Champions League tie next week.

[ABSENCES — Arsenal] Eberechi Eze (MF, out, calf), Bukayo Saka (FW, doubt,
physical discomfort). Club confirmed at press conference.

[ABSENCES — Brighton] Solly March (MF, out, knee). No other changes reported.

Total confirmed out: Arsenal 2, Brighton 1.

Sources: theguardian.com, skysports.com.
"""


class TestParseResponse:
    def test_home_and_away_summaries_differ(self):
        match = _make_match()
        home_ctx, away_ctx = ppx._parse_response(
            _GOOD_RESPONSE, match, "2026-04-04", "2026-04-12"
        )
        assert home_ctx.summary != away_ctx.summary
        assert "Eberechi Eze" in home_ctx.summary
        assert "Eberechi Eze" not in away_ctx.summary
        assert "Solly March" in away_ctx.summary
        assert "Solly March" not in home_ctx.summary

    def test_preview_shared_between_both(self):
        match = _make_match()
        home_ctx, away_ctx = ppx._parse_response(
            _GOOD_RESPONSE, match, "2026-04-04", "2026-04-12"
        )
        assert "pivotal run-in" in home_ctx.summary
        assert "pivotal run-in" in away_ctx.summary

    def test_absent_count_parsed(self):
        match = _make_match()
        home_ctx, away_ctx = ppx._parse_response(
            _GOOD_RESPONSE, match, "2026-04-04", "2026-04-12"
        )
        assert home_ctx.perplexity_absent_count == 2
        assert away_ctx.perplexity_absent_count == 1

    def test_domains_extracted_and_deduped(self):
        match = _make_match()
        home_ctx, _ = ppx._parse_response(
            _GOOD_RESPONSE, match, "2026-04-04", "2026-04-12"
        )
        assert "theguardian.com" in home_ctx.source_domains
        assert "skysports.com" in home_ctx.source_domains
        # dedup: each listed once
        assert len(home_ctx.source_domains) == len(set(home_ctx.source_domains))

    def test_query_window_stored(self):
        match = _make_match()
        home_ctx, _ = ppx._parse_response(
            _GOOD_RESPONSE, match, "2026-04-04", "2026-04-12"
        )
        assert home_ctx.query_window_start == "2026-04-04"
        assert home_ctx.query_window_end == "2026-04-12"

    def test_malformed_response_falls_back_to_raw_blob(self, caplog):
        match = _make_match()
        raw = "Random unrelated text with no section markers at all."
        with caplog.at_level("WARNING"):
            home_ctx, away_ctx = ppx._parse_response(
                raw, match, "2026-04-04", "2026-04-12"
            )
        assert home_ctx.summary == raw
        assert away_ctx.summary == raw
        assert any("expected section format" in m for m in caplog.messages)


class TestBuildQuery:
    def test_query_mentions_both_teams_and_date(self):
        match = _make_match()
        q = ppx._build_query(match, "Saturday 11 April 2026")
        assert "Arsenal" in q
        assert "Brighton" in q
        assert "Saturday 11 April 2026" in q
        # Explicit H2H exclusion — the Port Vale failure mode
        assert "H2H" in q or "H2H history" in q

    def test_query_asks_for_three_sections(self):
        match = _make_match()
        q = ppx._build_query(match, "Saturday 11 April 2026")
        assert "[PREVIEW]" in q
        assert "[ABSENCES —" in q


class TestCallSonarPayload:
    def test_payload_includes_filters(self):
        captured: dict = {}

        class FakeResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": _GOOD_RESPONSE}}]}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            return FakeResp()

        with patch.object(ppx.requests, "post", side_effect=fake_post):
            kickoff = datetime(2026, 4, 11, 15, 0, tzinfo=timezone.utc)
            ppx._call_sonar("test query", kickoff)

        payload = captured["payload"]
        assert "search_after_date_filter" in payload
        assert "search_before_date_filter" in payload
        assert payload["search_domain_filter"] == ppx.TRUSTED_FOOTBALL_DOMAINS
        # Window: 7d before, 1d after the kickoff date
        assert payload["search_after_date_filter"] == "04/04/2026"
        assert payload["search_before_date_filter"] == "04/12/2026"
