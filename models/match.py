"""
Data models for the Stryktipset newsletter system.

All dataclasses used across fetchers, analysis, and email layers are defined here.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Outcome(str, Enum):
    HOME = "1"
    DRAW = "X"
    AWAY = "2"


class SelectionType(str, Enum):
    SINGLE = "single"   # 1 outcome — most confident
    DOUBLE = "double"   # 2 outcomes
    FULL = "full"       # all 3 outcomes — most uncertain


class InjuryStatus(str, Enum):
    OUT = "out"
    DOUBT = "doubt"
    FIFTY_FIFTY = "50-50"
    AVAILABLE = "available"


class MotivationLevel(str, Enum):
    TITLE_RACE = "title_race"
    EUROPEAN_SPOT = "european_spot"
    MID_TABLE = "mid_table"
    RELEGATION = "relegation"
    CUP_DISTRACTION = "cup_distraction"  # heavy rotation risk


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# Opponent matchup risk for an injured player
# ---------------------------------------------------------------------------

@dataclass
class MatchupRisk:
    """
    When a player is injured, assess whether the opponent has a direct
    threat that exploits the gap — e.g. missing LB vs top-scoring RW.
    """
    opponent_player: str
    opponent_position: str          # e.g. "RW", "ST", "CAM"
    opponent_goals: int = 0
    opponent_assists: int = 0
    risk_level: RiskLevel = RiskLevel.LOW
    note: str = ""                  # human-readable explanation


# ---------------------------------------------------------------------------
# Injured / suspended player
# ---------------------------------------------------------------------------

@dataclass
class PlayerAbsence:
    """
    Represents a missing player and contextual impact assessment.
    Position-agnostic value scoring — a top-scoring LW matters as much
    as a striker; a WB absence matters if the opponent's RW is dominant.
    """
    player_name: str
    position: str                   # e.g. "LB", "CAM", "ST"
    status: InjuryStatus = InjuryStatus.OUT

    # Output value — what do we lose?
    season_goals: int = 0
    season_assists: int = 0
    season_key_passes_per90: float = 0.0
    season_progressive_carries_per90: float = 0.0
    is_top_scorer: bool = False
    is_top_assister: bool = False
    is_set_piece_taker: bool = False    # corners, free kicks, penalties
    is_captain: bool = False
    is_press_trigger: bool = False      # tactical role in high press

    # Replacement quality
    replacement_name: Optional[str] = None
    replacement_quality: str = "unknown"  # "like_for_like" / "adequate" / "poor" / "unknown"

    # Compound risk: does opponent exploit this gap?
    matchup_risk: Optional[MatchupRisk] = None

    # Data provenance — which fetcher produced this record? Lets the analyst
    # prompt show Claude which source each player comes from so it can weigh
    # conflicting reports (e.g. API-Football stale vs Sofascore fresh).
    source: str = ""                    # "api-football" / "sofascore" / ""

    @property
    def impact_summary(self) -> str:
        parts = []
        if self.is_top_scorer:
            parts.append("top scorer")
        if self.is_top_assister:
            parts.append("top assister")
        if self.is_set_piece_taker:
            parts.append("set piece taker")
        if self.season_goals >= 5:
            parts.append(f"{self.season_goals}G")
        if self.season_assists >= 5:
            parts.append(f"{self.season_assists}A")
        return ", ".join(parts) if parts else "squad player"


# ---------------------------------------------------------------------------
# Form entry (one past result)
# ---------------------------------------------------------------------------

@dataclass
class FormResult:
    opponent: str
    home_or_away: str       # "H" or "A"
    goals_for: int
    goals_against: int
    result: Outcome         # 1/X/2 from THIS team's perspective: 1=win, X=draw, 2=loss
    competition: str = ""   # e.g. "Premier League", "UCL", "EFL Cup" — shown in prompt
    xg_for: Optional[float] = None
    xg_against: Optional[float] = None

    @property
    def points(self) -> int:
        if self.result == Outcome.HOME:
            return 3
        if self.result == Outcome.DRAW:
            return 1
        return 0


# ---------------------------------------------------------------------------
# Head-to-head record entry
# ---------------------------------------------------------------------------

@dataclass
class H2HResult:
    date: str               # ISO date string
    home_team: str
    away_team: str
    home_goals: int
    away_goals: int
    venue: str = ""         # stadium name if available
    competition: str = ""   # "Premier League" / "FA Cup" / "UCL" — lets the
                            # prompt distinguish cup meetings from league ones


# ---------------------------------------------------------------------------
# Schedule / fatigue context
# ---------------------------------------------------------------------------

@dataclass
class ScheduleContext:
    days_since_last_match: Optional[int] = None
    last_match_competition: str = ""   # "Premier League" / "UCL" / "FA Cup"
    matches_last_14_days: int = 0
    upcoming_big_match: str = ""       # e.g. "UCL semi-final in 3 days" — rotation risk


# ---------------------------------------------------------------------------
# Full stats bundle for one team in a match
# ---------------------------------------------------------------------------

@dataclass
class TeamStats:
    team_name: str
    team_id: Optional[int] = None          # API-Football team ID

    # Standings
    league_position: Optional[int] = None
    league_points: Optional[int] = None
    motivation: Optional[MotivationLevel] = None

    # Form (last 5 — all competitions unless specified)
    form_last5: list[FormResult] = field(default_factory=list)
    form_last5_home_only: list[FormResult] = field(default_factory=list)    # for home team
    form_last5_away_only: list[FormResult] = field(default_factory=list)    # for away team

    # xG averages (last 5) — populated by Perplexity news parsing when available
    xg_for_avg: Optional[float] = None
    xg_against_avg: Optional[float] = None

    # Absences
    injuries: list[PlayerAbsence] = field(default_factory=list)
    suspensions: list[PlayerAbsence] = field(default_factory=list)
    intl_call_ups: list[str] = field(default_factory=list)   # player names away on intl duty

    # Schedule
    schedule: Optional[ScheduleContext] = None

    # Coaching
    manager_name: str = ""
    manager_weeks_in_post: Optional[int] = None

    # Fixture stats averages (last 5 completed games)
    shots_on_target_avg: Optional[float] = None   # shots on target per game
    shots_total_avg: Optional[float] = None
    possession_avg: Optional[float] = None        # avg ball possession %
    corners_avg: Optional[float] = None

    # Disciplinary averages (season-to-date from football-data.co.uk CSV)
    avg_yellows_home: Optional[float] = None      # avg yellows when playing at home
    avg_yellows_away: Optional[float] = None      # avg yellows when playing away

    # Lineup context (published ~1h before kickoff)
    formation: str = ""                           # e.g. "4-3-3"
    starting_xi: list[str] = field(default_factory=list)  # player names

    @property
    def new_manager_bounce(self) -> bool:
        return self.manager_weeks_in_post is not None and self.manager_weeks_in_post <= 8

    @property
    def fatigue_flag(self) -> bool:
        if self.schedule is None:
            return False
        days = self.schedule.days_since_last_match
        recent = self.schedule.matches_last_14_days
        # days=0 is almost always a data bug (fixture matched itself, or wrong team)
        # — require strong corroboration before flagging fatigue
        if days == 0:
            return recent >= 3
        if days is not None and 1 <= days <= 3:
            return True
        return recent >= 4

    @property
    def form_points_last5(self) -> int:
        return sum(r.points for r in self.form_last5)

    @property
    def critical_absences(self) -> list[PlayerAbsence]:
        """Returns absences with high/critical matchup risk or top scorer/assister status."""
        return [
            p for p in self.injuries + self.suspensions
            if p.is_top_scorer
            or p.is_top_assister
            or p.is_set_piece_taker
            or (p.matchup_risk and p.matchup_risk.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL))
        ]


# ---------------------------------------------------------------------------
# News context from Perplexity Sonar
# ---------------------------------------------------------------------------

@dataclass
class NewsContext:
    team_name: str
    summary: str = ""               # 2–4 sentence summary of latest news
    injury_updates: str = ""        # any last-minute injury news
    press_conference_notes: str = ""
    squad_morale: str = ""
    retrieved_at: Optional[datetime] = None
    # Perplexity fixture-anchored preview metadata (Tier 2B rewrite)
    source_domains: list[str] = field(default_factory=list)
    perplexity_absent_count: Optional[int] = None
    query_window_start: str = ""    # ISO date (kickoff - 7d)
    query_window_end: str = ""      # ISO date (kickoff + 1d)


# ---------------------------------------------------------------------------
# Market signals (from Svenska Spel API — free)
# ---------------------------------------------------------------------------

@dataclass
class MarketSignals:
    odds_home: Optional[float] = None       # 1
    odds_draw: Optional[float] = None       # X
    odds_away: Optional[float] = None       # 2

    public_pct_home: Optional[int] = None   # % of public bets on home
    public_pct_draw: Optional[int] = None
    public_pct_away: Optional[int] = None

    newspaper_tips_home: int = 0            # how many of 10 newspapers tip home
    newspaper_tips_draw: int = 0
    newspaper_tips_away: int = 0

    @property
    def implied_prob_home(self) -> Optional[float]:
        if self.odds_home:
            return round(1 / self.odds_home, 4)
        return None

    @property
    def implied_prob_draw(self) -> Optional[float]:
        if self.odds_draw:
            return round(1 / self.odds_draw, 4)
        return None

    @property
    def implied_prob_away(self) -> Optional[float]:
        if self.odds_away:
            return round(1 / self.odds_away, 4)
        return None

    @property
    def market_favourite(self) -> Optional[Outcome]:
        probs = {
            Outcome.HOME: self.implied_prob_home,
            Outcome.DRAW: self.implied_prob_draw,
            Outcome.AWAY: self.implied_prob_away,
        }
        valid = {k: v for k, v in probs.items() if v is not None}
        return max(valid, key=valid.get) if valid else None


# ---------------------------------------------------------------------------
# Core match — as fetched from Svenska Spel
# ---------------------------------------------------------------------------

@dataclass
class Match:
    """
    A single Stryktipset game. Populated in stages:
      1. svenska_spel.py fills the base fields + market signals
      2. api_football.py fills home_stats + away_stats
      3. perplexity.py fills home_news + away_news
    """
    game_number: int                    # 1–13
    draw_number: int
    home_team: str
    away_team: str
    league: str
    country: str
    kickoff: Optional[datetime] = None
    api_football_fixture_id: Optional[int] = None   # resolved during stats fetch
    referee: str = ""                               # assigned referee name
    referee_avg_cards: Optional[float] = None       # referee's avg total cards/game this season

    # Populated progressively
    market: Optional[MarketSignals] = None
    home_stats: Optional[TeamStats] = None
    away_stats: Optional[TeamStats] = None
    h2h: list[H2HResult] = field(default_factory=list)
    home_news: Optional[NewsContext] = None
    away_news: Optional[NewsContext] = None


# ---------------------------------------------------------------------------
# Claude's output for one match
# ---------------------------------------------------------------------------

@dataclass
class MatchPrediction:
    game_number: int
    home_team: str
    away_team: str

    # Core prediction
    predicted_outcomes: list[Outcome]       # 1–3 outcomes we select
    selection_type: SelectionType           # single / double / full
    confidence: float                       # 0.0–1.0

    # Analysis text (Claude-generated, editorial quality)
    analysis: str = ""                      # 3–5 paragraph match write-up
    key_factors: list[str] = field(default_factory=list)   # bullet points driving the pick
    risk_flags: list[str] = field(default_factory=list)    # reasons this could go wrong
    value_note: str = ""    # if odds look mispriced vs our model

    # Absent player counts (from Claude's analysis of all sources)
    home_absent_count: int = 0              # total confirmed out/doubtful for home team
    away_absent_count: int = 0              # total confirmed out/doubtful for away team

    # For evaluation later
    actual_result: Optional[Outcome] = None
    correct: Optional[bool] = None


# ---------------------------------------------------------------------------
# Full weekly analysis report (Claude's complete output)
# ---------------------------------------------------------------------------

@dataclass
class WeeklyReport:
    draw_number: int
    generated_at: datetime
    predictions: list[MatchPrediction] = field(default_factory=list)

    # Coupon allocation summary
    singles: list[int] = field(default_factory=list)    # game numbers
    doubles: list[int] = field(default_factory=list)
    full_game: Optional[int] = None
    total_rows: int = 0
    total_cost_sek: int = 0

    # Coupon quality
    volatile_week: bool = False  # True if <4 singles have confidence ≥0.80

    # Claude's overall commentary
    executive_summary: str = ""
    value_radar: list[str] = field(default_factory=list)  # mispriced games

    def get_prediction(self, game_number: int) -> Optional[MatchPrediction]:
        return next((p for p in self.predictions if p.game_number == game_number), None)


# ---------------------------------------------------------------------------
# Evaluation — how last week's predictions did
# ---------------------------------------------------------------------------

@dataclass
class MatchEvaluation:
    game_number: int
    home_team: str
    away_team: str
    our_prediction: list[Outcome]
    selection_type: SelectionType
    actual_result: Outcome
    correct: bool
    post_mortem: str = ""           # Claude-generated explanation of what we missed


@dataclass
class WeeklyEvaluation:
    draw_number: int
    draw_date: str                  # ISO date
    evaluations: list[MatchEvaluation] = field(default_factory=list)

    total_correct: int = 0
    singles_correct: int = 0
    singles_total: int = 0
    doubles_correct: int = 0
    doubles_total: int = 0
    full_covered: bool = False       # did full selection cover the actual result (always True)

    # Narrative summary fed back into next week's Claude prompt
    feedback_summary: str = ""
    lessons: list[str] = field(default_factory=list)   # bullet points for Claude context

    @property
    def accuracy_pct(self) -> float:
        if not self.evaluations:
            return 0.0
        return round(self.total_correct / len(self.evaluations) * 100, 1)

    @property
    def singles_accuracy_pct(self) -> float:
        if self.singles_total == 0:
            return 0.0
        return round(self.singles_correct / self.singles_total * 100, 1)
