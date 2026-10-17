"""Tests for the local Sofascore snapshot (collect locally, apply in the pipeline)."""

import json
from datetime import datetime, timezone

import pytest

import fetchers.sofascore_collector as sc
from analysis.claude_analyst import _injury_source_tag
from models.match import InjuryStatus, Match, PlayerAbsence, TeamStats


@pytest.fixture(autouse=True)
def _tmp_snapshot_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "SOFASCORE_DIR", tmp_path)


def _make_match(n: int = 5, home: str = "Birmingham", away: str = "Queens Park Rangers") -> Match:
    m = Match(
        game_number=n, draw_number=4975, home_team=home, away_team=away,
        league="Championship", country="England",
        kickoff=datetime(2026, 10, 17, 14, 0, tzinfo=timezone.utc),
    )
    m.home_stats = TeamStats(team_name=home)
    m.away_stats = TeamStats(team_name=away)
    return m


def _side(xg: float | None = None, absences: list | None = None, checked: bool = False) -> dict:
    return {
        "xg": None if xg is None else {
            "xg_for_5g": xg, "xg_against_5g": 1.1,
            "goals_for_5g": 1.4, "goals_against_5g": 0.8, "matches_available": 5,
        },
        "absences": absences or [],
        "absences_checked": checked,
    }


def _write_snapshot(games: list[dict], collected_at: str | None = None) -> None:
    data = {
        "draw_number": 4975,
        "collected_at": collected_at or datetime.now(timezone.utc).isoformat(),
        "games": games,
    }
    sc.snapshot_path(4975).write_text(json.dumps(data), encoding="utf-8")


def _game(home_side: dict | None = None, away_side: dict | None = None, n: int = 5,
          home: str = "Birmingham", away: str = "Queens Park Rangers") -> dict:
    return {"game_number": n, "home_team": home, "away_team": away,
            "home": home_side or _side(), "away": away_side or _side()}


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def test_apply_fills_xg_and_absences():
    _write_snapshot([_game(
        home_side=_side(xg=1.8, absences=[{"player_name": "Bielik", "position": "M", "status": "out"}]),
        away_side=_side(xg=1.2, checked=True),
    )])
    m = _make_match()
    sc.apply_sofascore_snapshot([m], 4975)

    assert m.home_stats.xg_for_avg == 1.8
    assert m.home_stats._xg_profile.goals_for_5g == 1.4
    assert [a.player_name for a in m.home_stats.injuries] == ["Bielik"]
    assert m.home_stats.injuries[0].status == InjuryStatus.OUT
    assert m.home_stats.injuries[0].source == "sofascore"
    assert m.away_stats.xg_for_avg == 1.2
    assert m.away_stats.injuries == []
    assert m.away_stats.absences_checked_source == "sofascore"


def test_apply_never_overwrites_existing_data():
    _write_snapshot([_game(home_side=_side(xg=1.8, absences=[{"player_name": "X", "status": "out"}]))])
    m = _make_match()
    m.home_stats.xg_for_avg = 2.5
    existing = [PlayerAbsence(player_name="Y", position="D", source="fpl")]
    m.home_stats.injuries = existing
    sc.apply_sofascore_snapshot([m], 4975)
    assert m.home_stats.xg_for_avg == 2.5
    assert m.home_stats.injuries == existing


def test_apply_skips_mismatched_game():
    # Same game number but different teams (e.g. coupon changed) — ignore it
    _write_snapshot([_game(home_side=_side(xg=1.8), home="Derby")])
    m = _make_match()
    sc.apply_sofascore_snapshot([m], 4975)
    assert m.home_stats.xg_for_avg is None


def test_missing_snapshot_leaves_matches_unchanged():
    m = _make_match()
    assert sc.apply_sofascore_snapshot([m], 4975) == [m]
    assert m.home_stats.xg_for_avg is None


def test_corrupt_snapshot_is_tolerated():
    sc.snapshot_path(4975).write_text("{not json", encoding="utf-8")
    m = _make_match()
    assert sc.apply_sofascore_snapshot([m], 4975) == [m]


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------

def test_build_snapshot_round_trip():
    from fetchers.xg_collector import TeamXGProfile

    m = _make_match()
    m.home_stats.xg_for_avg, m.home_stats.xg_against_avg = 1.7, 0.9
    m.home_stats._xg_profile = TeamXGProfile(
        team_name="Birmingham", matches_available=5,
        xg_for_5g=1.7, xg_against_5g=0.9, goals_for_5g=2.0, goals_against_5g=0.6,
    )
    m.home_stats.injuries = [PlayerAbsence(player_name="Bielik", position="M",
                                           status=InjuryStatus.DOUBT, source="sofascore")]
    m.home_stats.absences_checked_source = "sofascore"
    m.away_stats.absences_checked_source = "sofascore"

    snap = sc.build_snapshot(4975, [m])
    sc.snapshot_path(4975).write_text(json.dumps(snap), encoding="utf-8")

    fresh = _make_match()
    sc.apply_sofascore_snapshot([fresh], 4975)
    assert fresh.home_stats.xg_for_avg == 1.7
    assert fresh.home_stats._xg_profile.goals_against_5g == 0.6
    assert fresh.home_stats.injuries[0].status == InjuryStatus.DOUBT
    assert fresh.away_stats.absences_checked_source == "sofascore"
    assert fresh.away_stats.xg_for_avg is None


# ---------------------------------------------------------------------------
# Prompt tag: "checked, none missing" vs "no data"
# ---------------------------------------------------------------------------

def test_injury_source_tag():
    ts = TeamStats(team_name="Man City")
    assert _injury_source_tag(ts) == "[structured empty]"
    ts.absences_checked_source = "fpl"
    assert _injury_source_tag(ts) == "[checked: fpl — none missing]"
    ts.injuries = [PlayerAbsence(player_name="Rodri", position="MID", source="fpl")]
    assert _injury_source_tag(ts) == ""
    assert _injury_source_tag(None) == "[structured empty]"


# ---------------------------------------------------------------------------
# Blocked collection must not write (or overwrite) a snapshot
# ---------------------------------------------------------------------------

def test_blocked_collection_raises_and_keeps_existing_file(monkeypatch):
    import contextlib

    import fetchers.sofascore as sofa
    import fetchers.sofascore_browser as sb
    import fetchers.svenska_spel as ss

    _write_snapshot([_game(home_side=_side(xg=1.8))])
    good = sc.snapshot_path(4975).read_text(encoding="utf-8")

    monkeypatch.setattr(ss, "fetch_coupon", lambda n: [_make_match()])
    monkeypatch.setattr(sb, "browser_session", lambda: contextlib.nullcontext(lambda path: None))
    monkeypatch.setattr(sofa, "enrich_with_sofascore_xg", lambda ms: ms)
    monkeypatch.setattr(sofa, "enrich_with_sofascore_absences", lambda ms: ms)

    with pytest.raises(sc.SofascoreCollectionEmpty):
        sc.collect_sofascore_snapshot(4975)
    assert sc.snapshot_path(4975).read_text(encoding="utf-8") == good
