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

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'CandidateProfile':
        if not isinstance(d, dict):
            return cls(id=0)

        cand_id = d.get("id") or d.get("user_id") or d.get("candidate_id") or 0
        if isinstance(cand_id, str) and cand_id.isdigit():
            cand_id = int(cand_id)
        elif not isinstance(cand_id, int):
            cand_id = 0

        name = d.get("full_name") or d.get("name") or d.get("username") or "Candidate"

        skills = []
        raw_skills = d.get("skills") or d.get("skill_names") or d.get("exp_skills") or d.get("all_Skill")
        if isinstance(raw_skills, list):
            for s in raw_skills:
                if isinstance(s, dict):
                    val = s.get("skill") or s.get("name") or s.get("title")
                    if val and str(val).strip():
                        skills.append(str(val).strip())
                elif str(s).strip():
                    skills.append(str(s).strip())
        elif isinstance(raw_skills, str):
            skills = [s.strip() for s in raw_skills.split(",") if s.strip()]

        exp_years = d.get("total_experience_years") or d.get("experience_years") or 0.0
        try:
            exp_years = float(exp_years)
        except (ValueError, TypeError):
            exp_years = 0.0

        city = d.get("city_name") or d.get("city")
        state = d.get("state_name") or d.get("state")
        country = d.get("country_name") or d.get("country")
        location = ", ".join([str(p) for p in [city, state, country] if p]) or None

        current_title = d.get("current_position_name") or d.get("designation") or d.get("title")

        exp_sal = None
        if d.get("expected_salary") is not None:
            try:
                exp_sal = float(d["expected_salary"])
            except (ValueError, TypeError):
                exp_sal = None

        return cls(
            id=cand_id,
            name=str(name),
            skills=skills,
            experience_years=exp_years,
            location=location,
            city=str(city) if city else None,
            state=str(state) if state else None,
            country=str(country) if country else None,
            expected_salary=exp_sal,
            current_title=str(current_title) if current_title else None,
            raw_data=d
        )


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

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'JobProfile':
        if not isinstance(d, dict):
            return cls(id=0)

        j_id = d.get("id") or d.get("job_id") or 0
        if isinstance(j_id, str) and j_id.isdigit():
            j_id = int(j_id)
        elif not isinstance(j_id, int):
            j_id = 0

        title = d.get("job_title") or d.get("title") or "Job Title"
        company_name = d.get("company_name") or d.get("company")

        req_skills = []
        raw_skills = d.get("required_skills") or d.get("skills") or d.get("skill_names")
        if isinstance(raw_skills, list):
            req_skills = [str(s).strip() for s in raw_skills if str(s).strip()]
        elif isinstance(raw_skills, str):
            req_skills = [s.strip() for s in raw_skills.split(",") if s.strip()]

        req_exp = d.get("required_experience_years") or d.get("min_experience_years") or d.get("experience_years") or 0.0
        try:
            req_exp = float(req_exp)
        except (ValueError, TypeError):
            req_exp = 0.0

        city = d.get("city_name") or d.get("city")
        state = d.get("state_name") or d.get("state")
        country = d.get("country_name") or d.get("country")
        location = d.get("location") or (", ".join([str(p) for p in [city, state, country] if p]) or None)

        sal_min = None
        if d.get("offered_salary_min") is not None:
            try:
                sal_min = float(d["offered_salary_min"])
            except (ValueError, TypeError):
                sal_min = None

        sal_max = None
        if d.get("offered_salary_max") is not None:
            try:
                sal_max = float(d["offered_salary_max"])
            except (ValueError, TypeError):
                sal_max = None

        return cls(
            id=j_id,
            title=str(title),
            company_name=str(company_name) if company_name else None,
            required_skills=req_skills,
            required_experience_years=req_exp,
            location=location,
            city=str(city) if city else None,
            state=str(state) if state else None,
            country=str(country) if country else None,
            job_mode=int(d.get("job_mode", 1)),
            offered_salary_min=sal_min,
            offered_salary_max=sal_max,
            raw_data=d
        )


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



