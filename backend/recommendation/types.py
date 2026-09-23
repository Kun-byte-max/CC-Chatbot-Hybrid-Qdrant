"""
Type definitions and data transfer objects for Phase 2 Employer Candidate Recommendation & Ranking.
"""
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, Any


class MatchStatus(str, Enum):
    """
    Dimension-Level Matcher Statuses ONLY.
    Returned by individual matchers (Title, Skill, Experience, Location, Salary).
    """
    NOT_REQUESTED = "NOT_REQUESTED"
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNKNOWN = "UNKNOWN"


class RecommendationStatus(str, Enum):
    """
    Overall Candidate Recommendation Result Statuses ONLY.
    Evaluated by ScoreCalculator for overall candidate recommendation quality.
    """
    CALCULATED = "CALCULATED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class RecommendationDimension(str, Enum):
    """Supported candidate recommendation scoring dimensions."""
    TITLE = "title"
    SKILL = "skill"
    EXPERIENCE = "experience"
    LOCATION = "location"
    SALARY = "salary"


@dataclass
class MatchResult:
    """Detailed match outcome for a single recommendation dimension."""
    dimension: RecommendationDimension
    status: MatchStatus
    score: Optional[float]  # None if NOT_REQUESTED or UNKNOWN; float in [0.0, 100.0] if MATCH or MISMATCH
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EmployerCandidateRecommendationResult:
    """Overall candidate recommendation result evaluated by EmployerRecommendationEngine."""
    candidate_id: str
    overall_status: RecommendationStatus
    final_score: Optional[float]  # None if overall_status is INSUFFICIENT_DATA; float in [0.0, 100.0] if CALCULATED
    confidence_score: float  # Ratio of evaluated weights to requested weights [0.0, 1.0]
    match_results: Dict[RecommendationDimension, MatchResult]
    explanation: str
    scored_criteria_count: int
    total_requested_criteria_count: int
