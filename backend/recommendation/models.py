"""
models.py — Data models for Recommendation Engine V1 skeleton.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CandidateProfile:
    """
    Represents a candidate profile for recommendation scoring.
    """
    id: int
    name: str = "Candidate"
    skills: List[str] = field(default_factory=list)
    experience_years: float = 0.0
    location: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    expected_salary: Optional[float] = None
    current_title: Optional[str] = None
    raw_data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class JobProfile:
    """
    Represents a job posting profile for recommendation scoring.
    """
    id: int
    title: str = "Job Title"
    company_name: Optional[str] = None
    required_skills: List[str] = field(default_factory=list)
    required_experience_years: float = 0.0
    location: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    job_mode: int = 1  # 1=Office, 2=Remote, 3=Hybrid
    offered_salary_min: Optional[float] = None
    offered_salary_max: Optional[float] = None
    raw_data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MatchBreakdown:
    """
    Individual component scores for recommendation matching.
    """
    skill_score: float = 0.0
    experience_score: float = 0.0
    location_score: float = 0.0
    salary_score: float = 0.0
    title_score: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillMatchResult:
    """
    Structured outcome of SkillMatcher evaluation.
    """
    score: float
    matched_skills: List[str]
    missing_skills: List[str]
    extra_skills: List[str]
    total_required: int
    total_matched: int


@dataclass
class ExperienceMatchResult:
    """
    Structured outcome of ExperienceMatcher evaluation.
    """
    score: float
    candidate_experience: float
    required_experience: float
    experience_gap: float
    meets_requirement: bool


@dataclass
class LocationMatchResult:
    """
    Structured outcome of LocationMatcher evaluation.
    """
    score: float
    candidate_city: Optional[str]
    candidate_state: Optional[str]
    candidate_country: Optional[str]
    job_city: Optional[str]
    job_state: Optional[str]
    job_country: Optional[str]
    work_mode: str
    match_level: str


@dataclass
class SalaryMatchResult:
    """
    Structured outcome of SalaryMatcher evaluation.
    """
    score: float
    candidate_expected_salary: Any
    job_offered_salary: Any
    normalized_candidate_salary: Optional[float]
    normalized_job_salary: Optional[float]
    salary_difference_percent: float
    meets_expectation: bool


@dataclass
class TitleMatchResult:
    """
    Structured outcome of TitleMatcher evaluation.
    """
    score: float
    candidate_title: Optional[str]
    job_title: Optional[str]
    normalized_candidate_title: Optional[str]
    normalized_job_title: Optional[str]
    matched_keywords: List[str]
    match_level: str


@dataclass(frozen=True)
class ScoreResult:
    """
    Structured output of ScoreCalculator combining matcher scores.
    """
    overall_score: float
    recommendation_level: str
    skill_score: float
    experience_score: float
    location_score: float
    salary_score: float
    title_score: float
    weight_skill: float
    weight_experience: float
    weight_location: float
    weight_salary: float
    weight_title: float


@dataclass(frozen=True)
class ExplanationResult:
    """
    Structured output of ExplanationGenerator.
    """
    summary: str
    strengths: List[str]
    weaknesses: List[str]
    recommendation_level: str
    overall_score: float
    search_reasons: List[str] = field(default_factory=list)
    recommendation_reasons: List['RecommendationReason'] = field(default_factory=list)


@dataclass
class RecommendationReason:
    """
    Structured, typed reason item for UI card bullet points.
    """
    type: str  # "skill" | "experience" | "location" | "title" | "salary" | "overall"
    label: str  # "Skills" | "Experience" | "Location" | "Role Match" | "Salary" | "Overall Fit"
    message: str  # Human-readable sentence
    evidence: Optional[Any] = None  # Internal debug evidence dictionary or list (JSON-serializable)


@dataclass
class RecommendationResult:
    """
    Final output model for recommendation orchestration.
    """
    overall_score: float
    recommendation_level: str
    skill_result: SkillMatchResult
    experience_result: ExperienceMatchResult
    location_result: LocationMatchResult
    salary_result: SalaryMatchResult
    title_result: TitleMatchResult
    score_result: ScoreResult
    explanation_result: ExplanationResult
    candidate_id: Optional[int] = None
    job_id: Optional[int] = None
    global_rank: Optional[int] = None
    search_reasons: List[str] = field(default_factory=list)
    recommendation_reasons: List[RecommendationReason] = field(default_factory=list)
    raw_data: Dict[str, Any] = field(default_factory=dict)



