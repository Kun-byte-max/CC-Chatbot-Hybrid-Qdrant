"""
employer_recommendation_engine.py — Dedicated Phase 2 Employer Candidate Recommendation Engine.
Ranks candidate profiles against employer search requirements using 4-state matcher taxonomies,
dynamic weight renormalization, zero-denominator protections, and canonical tie-breaker keys.
"""

import re
import logging
from typing import List, Dict, Any, Optional, Tuple
from backend.recommendation.types import (
    MatchStatus,
    RecommendationStatus,
    RecommendationDimension,
    MatchResult,
    EmployerCandidateRecommendationResult,
)
from backend.recommendation.title_matcher import TitleMatcher
from backend.recommendation.skill_matcher import SkillMatcher
from backend.recommendation.experience_matcher import ExperienceMatcher
from backend.recommendation.location_matcher import LocationMatcher
from backend.recommendation.salary_matcher import SalaryMatcher
from backend.recommendation.score_calculator import ScoreCalculator
from backend.recommendation.explanation_generator import ExplanationGenerator
from backend.recommendation.mappers import map_doc_to_standard_candidate

log = logging.getLogger(__name__)


def candidate_rank_key(
    rec_res: EmployerCandidateRecommendationResult, cand: Any
) -> Tuple[Any, ...]:
    """
    Canonical deterministic tie-breaking rank key function for Employer Candidate Recommendation (Section 12).
    Tie-breaking hierarchy:
    1. final_score (Descending; INSUFFICIENT_DATA sorts to bottom via 999.0)
    2. title_score (Descending)
    3. skill_score (Descending)
    4. exp_score (Descending)
    5. loc_score (Descending)
    6. sal_score (Descending)
    7. confidence_score (Descending)
    8. verified_sort (Metadata-only tie-breaker: Verified=0, Unverified=1)
    9. rating (Metadata-only tie-breaker: Descending)
    10. cand_id_sort (Hybrid numeric/string candidate ID tie-breaker: (0, int_id) or (1, str_id))
    """
    score_sort = -round(rec_res.final_score, 2) if rec_res.final_score is not None else 999.0

    def get_dim_score(dim: RecommendationDimension) -> float:
        res = rec_res.match_results.get(dim)
        if res and res.status in (MatchStatus.MATCH, MatchStatus.MISMATCH) and res.score is not None:
            return round(res.score, 2)
        return -1.0

    title_score = -get_dim_score(RecommendationDimension.TITLE)
    skill_score = -get_dim_score(RecommendationDimension.SKILL)
    exp_score = -get_dim_score(RecommendationDimension.EXPERIENCE)
    loc_score = -get_dim_score(RecommendationDimension.LOCATION)
    sal_score = -get_dim_score(RecommendationDimension.SALARY)
    confidence = -round(rec_res.confidence_score, 4)
    verified_sort = 0 if getattr(cand, "verified_employment", False) else 1

    rating_val = getattr(cand, "star_rating_avg", None) or getattr(cand, "rating", None)
    rating = -(float(rating_val) if rating_val is not None else -1.0)

    cand_raw_id = getattr(cand, "id", None) or getattr(cand, "candidate_id", None) or rec_res.candidate_id
    try:
        cand_id_sort = (0, int(cand_raw_id))
    except (ValueError, TypeError):
        cand_id_sort = (1, str(cand_raw_id) if cand_raw_id is not None else "")

    return (
        score_sort,
        title_score,
        skill_score,
        exp_score,
        loc_score,
        sal_score,
        confidence,
        verified_sort,
        rating,
        cand_id_sort,
    )


class EmployerRecommendationEngine:
    """
    Dedicated Phase 2 Candidate Recommendation Engine.
    Evaluates, ranks, and sorts candidate profiles against employer search requirements.
    """

    def __init__(self):
        self.title_matcher = TitleMatcher()
        self.skill_matcher = SkillMatcher()
        self.experience_matcher = ExperienceMatcher()
        self.location_matcher = LocationMatcher()
        self.salary_matcher = SalaryMatcher()
        self.score_calculator = ScoreCalculator()
        self.explanation_generator = ExplanationGenerator()

    def _match_title(self, req_role: Optional[str], cand_title: Optional[str]) -> MatchResult:
        """Evaluates Title matching dimension using 4-state MatchStatus taxonomy."""
        if not req_role or not str(req_role).strip():
            return MatchResult(
                dimension=RecommendationDimension.TITLE,
                status=MatchStatus.NOT_REQUESTED,
                score=None,
                details={"reason": "No target role requested by employer."},
            )

        if not cand_title or not str(cand_title).strip():
            return MatchResult(
                dimension=RecommendationDimension.TITLE,
                status=MatchStatus.UNKNOWN,
                score=None,
                details={"reason": "Candidate profile has no current designation/title data."},
            )

        r_norm = str(req_role).strip().lower()
        c_norm = str(cand_title).strip().lower()

        # Token extraction
        GENERIC_ROLE_TOKENS = {"developer", "engineer", "manager", "analyst", "executive", "specialist", "consultant", "lead", "head", "director", "vp", "officer", "associate", "business", "data"}
        SYNONYM_TOKENS = {
            "developer": {"developer", "engineer", "coder", "programmer"},
            "engineer": {"developer", "engineer", "coder", "programmer"},
            "manager": {"manager", "lead", "head"},
            "executive": {"executive", "officer", "associate"},
        }

        r_tokens = set(re.sub(r"[\-_+.]+", " ", r_norm).split())
        c_tokens = set(re.sub(r"[\-_+.]+", " ", c_norm).split())

        # 1. Tier 5: Explicitly Unrelated Domain Mismatch -> Score = 0.0
        DISTINCT_DOMAIN_MISMATCHES = [
            ({"scientist", "analyst"}, {"entry", "operator", "typist"}),
            ({"analyst"}, {"head", "executive", "director", "vp"}),
            ({"hr", "recruiter", "talent"}, {"sales", "marketing", "developer", "engineer", "accountant"}),
            ({"sales"}, {"hr", "recruiter", "developer", "engineer", "accountant"}),
            ({"accountant", "finance"}, {"hr", "sales", "developer", "engineer"}),
        ]
        for req_domain, cand_forbidden in DISTINCT_DOMAIN_MISMATCHES:
            if r_tokens.intersection(req_domain) and c_tokens.intersection(cand_forbidden):
                return MatchResult(
                    dimension=RecommendationDimension.TITLE,
                    status=MatchStatus.MISMATCH,
                    score=0.0,
                    details={"req_role": req_role, "cand_title": cand_title, "reason": "Explicitly unrelated domain mismatch (Tier 5)"},
                )

        # Non-generic specific domain tokens (e.g. "node.js", "python", "react", "hr", "sales", "data", "business", "tax")
        specific_req_tokens = r_tokens - {"developer", "engineer", "manager", "executive", "officer", "associate", "lead"}

        # 2. Tier 1: Exact / Specific Title Match -> Score = 100.0
        if r_norm == c_norm or (specific_req_tokens and specific_req_tokens.issubset(c_tokens) and r_tokens.intersection({"developer", "engineer"}).intersection(c_tokens)):
            return MatchResult(
                dimension=RecommendationDimension.TITLE,
                status=MatchStatus.MATCH,
                score=100.0,
                details={"req_role": req_role, "cand_title": cand_title, "tier": "Tier 1: Exact/Specific Match"},
            )

        # 3. Tier 2: Strong Related Title (e.g. Node.js Developer ↔ Node.js Engineer, Frontend Dev ↔ React Dev) -> Score = 85.0
        if specific_req_tokens and specific_req_tokens.intersection(c_tokens):
            return MatchResult(
                dimension=RecommendationDimension.TITLE,
                status=MatchStatus.MATCH,
                score=85.0,
                details={"req_role": req_role, "cand_title": cand_title, "tier": "Tier 2: Strong Related Title"},
            )

        # 4. Tier 3: Generic Role Match (e.g. Node.js Developer ↔ Developer/Engineer, HR Manager ↔ HR Executive) -> Score = 40.0
        # Generic role words alone CANNOT produce a score >= 70%
        has_generic_overlap = False
        for r_t in r_tokens:
            syns = SYNONYM_TOKENS.get(r_t, {r_t})
            if syns.intersection(c_tokens):
                has_generic_overlap = True
                break

        if has_generic_overlap:
            return MatchResult(
                dimension=RecommendationDimension.TITLE,
                status=MatchStatus.MISMATCH,
                score=40.0,
                details={"req_role": req_role, "cand_title": cand_title, "tier": "Tier 3: Generic Role Match"},
            )

        # 5. Tier 4: Weak / Irrelevant Match -> Score = 0.0
        return MatchResult(
            dimension=RecommendationDimension.TITLE,
            status=MatchStatus.MISMATCH,
            score=0.0,
            details={"req_role": req_role, "cand_title": cand_title, "tier": "Tier 4: Weak/Irrelevant Match"},
        )

    def _match_skills(
        self, req_skills: Optional[List[str]], cand_skills: Optional[List[str]]
    ) -> MatchResult:
        """Evaluates Skill matching dimension using 4-state MatchStatus taxonomy."""
        if not req_skills:
            return MatchResult(
                dimension=RecommendationDimension.SKILL,
                status=MatchStatus.NOT_REQUESTED,
                score=None,
                details={"reason": "No skills requested by employer."},
            )

        clean_req = [str(s).strip().lower() for s in req_skills if s and str(s).strip()]
        if not clean_req:
            return MatchResult(
                dimension=RecommendationDimension.SKILL,
                status=MatchStatus.NOT_REQUESTED,
                score=None,
                details={"reason": "No non-empty skills requested by employer."},
            )

        if not cand_skills:
            return MatchResult(
                dimension=RecommendationDimension.SKILL,
                status=MatchStatus.UNKNOWN,
                score=None,
                details={"reason": "Candidate profile contains no skill data."},
            )

        from backend.services.normalization_service import normalize_skill_name

        norm_req = [normalize_skill_name(s).lower() for s in clean_req]
        norm_cand = [normalize_skill_name(s).lower() for s in cand_skills if s and str(s).strip()]
        cand_set = set(norm_cand)

        matched_count = 0
        matched_skills = []

        for req_raw, req_norm in zip(clean_req, norm_req):
            if req_norm in cand_set:
                matched_count += 1
                matched_skills.append(req_raw)

        overlap_ratio = matched_count / len(clean_req) if clean_req else 0.0
        score = round(overlap_ratio * 100.0, 2)
        status = MatchStatus.MATCH if score >= 70.0 else MatchStatus.MISMATCH

        return MatchResult(
            dimension=RecommendationDimension.SKILL,
            status=status,
            score=score,
            details={
                "requested_skills": clean_req,
                "matched_skills": matched_skills,
                "matched_count": matched_count,
                "total_requested": len(clean_req),
            },
        )

    def _match_experience(
        self, req_min_exp: Optional[float], cand_exp_yrs: Optional[float]
    ) -> MatchResult:
        """
        Evaluates Experience matching dimension using seniority fit depth curve (Section 10.3 & Test 15).
        Δ = cand_exp - req_min_exp
        Δ = 0.0      -> 90.0 (MATCH)
        0 < Δ <= 3.0 -> 100.0 (MATCH)
        3 < Δ <= 5.0 -> 95.0 (MATCH)
        Δ > 5.0      -> 80.0 (MATCH)
        """
        if req_min_exp is None or float(req_min_exp) <= 0.0:
            return MatchResult(
                dimension=RecommendationDimension.EXPERIENCE,
                status=MatchStatus.NOT_REQUESTED,
                score=None,
                details={"reason": "No minimum experience requested by employer."},
            )

        req_min = float(req_min_exp)

        if cand_exp_yrs is None:
            return MatchResult(
                dimension=RecommendationDimension.EXPERIENCE,
                status=MatchStatus.UNKNOWN,
                score=None,
                details={"reason": "Candidate profile has no experience history data."},
            )

        cand_exp = float(cand_exp_yrs)
        if cand_exp < req_min:
            # Phase 1 Filter Violation Protection
            return MatchResult(
                dimension=RecommendationDimension.EXPERIENCE,
                status=MatchStatus.MISMATCH,
                score=0.0,
                details={"reason": "Candidate experience is below minimum required.", "delta": round(cand_exp - req_min, 2)},
            )

        delta = round(cand_exp - req_min, 4)

        if delta == 0.0:
            score = 90.0
        elif 0.0 < delta <= 3.0:
            score = 100.0
        elif 3.0 < delta <= 5.0:
            score = 95.0
        else:  # delta > 5.0
            score = 80.0

        return MatchResult(
            dimension=RecommendationDimension.EXPERIENCE,
            status=MatchStatus.MATCH,
            score=score,
            details={
                "required_min": req_min,
                "candidate_exp": cand_exp,
                "delta": delta,
            },
        )

    def recommend_candidates(
        self,
        employer_reqs: Any,
        candidates: List[Any],
    ) -> List[EmployerCandidateRecommendationResult]:
        """
        Main public recommendation entry point for Phase 2 Employer Candidate Recommendation.
        Evaluates, ranks, and returns the complete deterministic ranked candidate list.
        """
        if not candidates:
            return []

        # Requirement Extraction (Unwrap ProvenanceValue objects if present)
        req_role_obj = getattr(employer_reqs, "role", None) or (employer_reqs.get("role") if isinstance(employer_reqs, dict) else None)
        req_role = getattr(req_role_obj, "value", req_role_obj)

        req_skills_obj = getattr(employer_reqs, "skills", None) or (employer_reqs.get("skills") if isinstance(employer_reqs, dict) else None)
        req_skills = getattr(req_skills_obj, "value", req_skills_obj)

        req_exp_obj = getattr(employer_reqs, "experience", None) or getattr(employer_reqs, "min_exp", None) or (employer_reqs.get("min_exp") if isinstance(employer_reqs, dict) else None)
        req_exp = getattr(req_exp_obj, "min", req_exp_obj)

        req_loc = getattr(employer_reqs, "location", None) or (employer_reqs.get("location") if isinstance(employer_reqs, dict) else None)
        req_budget = getattr(employer_reqs, "budget", None) or (employer_reqs.get("budget") if isinstance(employer_reqs, dict) else None)
        req_mode = getattr(employer_reqs, "work_mode", None) or (employer_reqs.get("work_mode") if isinstance(employer_reqs, dict) else None)

        results = []

        for cand in candidates:
            # Normalize candidate profile object if candidate is a dict or unmapped object
            if isinstance(cand, dict):
                std_cand = map_doc_to_standard_candidate(cand)
            elif not hasattr(cand, "title") or not hasattr(cand, "experience_years"):
                # Handle candidate object missing standard attributes
                cand_dict = cand.__dict__ if hasattr(cand, "__dict__") else {}
                std_cand = map_doc_to_standard_candidate(cand_dict)
            else:
                std_cand = cand

            # Extract candidate ID
            cand_id = getattr(std_cand, "candidate_id", None) or getattr(std_cand, "id", None)
            if isinstance(cand, dict):
                cand_id = cand_id or cand.get("id") or cand.get("user_id") or cand.get("candidate_id")

            # Skip candidate if missing valid ID (Invariant #29)
            if cand_id is None or not str(cand_id).strip():
                log.warning("Candidate profile missing valid ID during Phase 2 scoring, skipping")
                continue

            # Candidate field extraction from standardized candidate object
            c_title = getattr(std_cand, "title", None) or getattr(std_cand, "current_title", None)
            c_skills = getattr(std_cand, "skills", None)
            c_exp = getattr(std_cand, "experience_years", None)

            c_city = getattr(std_cand, "city", None)
            c_state = getattr(std_cand, "state", None)
            c_country = getattr(std_cand, "country", None)

            c_sal = getattr(std_cand, "expected_salary", None) or (cand.get("expected_salary") if isinstance(cand, dict) else None)

            # Matcher evaluations
            res_title = self._match_title(req_role, c_title)
            res_skill = self._match_skills(req_skills, c_skills)
            res_exp = self._match_experience(req_exp, c_exp)
            res_loc = self.location_matcher.match_employer_location(
                req_location=req_loc,
                req_work_mode=req_mode,
                candidate_city=c_city,
                candidate_state=c_state,
                candidate_country=c_country,
            )
            res_sal = self.salary_matcher.match_employer_salary(
                employer_budget=req_budget,
                candidate_salary=c_sal,
            )

            match_results = {
                RecommendationDimension.TITLE: res_title,
                RecommendationDimension.SKILL: res_skill,
                RecommendationDimension.EXPERIENCE: res_exp,
                RecommendationDimension.LOCATION: res_loc,
                RecommendationDimension.SALARY: res_sal,
            }

            # Structured Explanation
            fname = getattr(cand, "fname", "") or (cand.get("fname", "") if isinstance(cand, dict) else "")
            lname = getattr(cand, "lname", "") or (cand.get("lname", "") if isinstance(cand, dict) else "")
            c_name = f"{fname} {lname}".strip() or f"Candidate {cand_id}"

            explanation_parts = []
            if res_title.status == MatchStatus.MATCH:
                explanation_parts.append(f"Matching title ({c_title or req_role})")
            if res_skill.status == MatchStatus.MATCH:
                explanation_parts.append(f"Matched {res_skill.details.get('matched_count', 0)} requested skill(s)")
            if res_exp.status == MatchStatus.MATCH:
                explanation_parts.append(f"{c_exp or 0} yrs experience meets requirement ({req_exp} yrs)")
            if res_loc.status == MatchStatus.MATCH:
                explanation_parts.append("Location requirement satisfied")

            explanation_text = f"Candidate {c_name}: " + ("; ".join(explanation_parts) if explanation_parts else "Evaluated against requested criteria.")

            # Calculate composite score
            rec_result = self.score_calculator.calculate_employer_candidate_score(
                candidate_id=str(cand_id),
                match_results=match_results,
                explanation=explanation_text,
            )

            # Pair result with candidate object for sorting
            results.append((rec_result, cand))

        # Deterministic Ranking (Section 12)
        results.sort(key=lambda pair: candidate_rank_key(pair[0], pair[1]))

        # Return list of ranked recommendation results
        return [pair[0] for pair in results]
