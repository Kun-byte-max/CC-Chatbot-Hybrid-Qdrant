"""
recommendation_engine.py — Master Recommendation Engine Orchestrator.
Orchestrates all matchers, ScoreCalculator, and ExplanationGenerator.
"""

import re
from typing import List, Optional, Any

try:
    from backend.recommendation.skill_matcher import SkillMatcher
    from backend.recommendation.experience_matcher import ExperienceMatcher
    from backend.recommendation.location_matcher import LocationMatcher
    from backend.recommendation.salary_matcher import SalaryMatcher
    from backend.recommendation.title_matcher import TitleMatcher
    from backend.recommendation.score_calculator import ScoreCalculator
    from backend.recommendation.explanation_generator import ExplanationGenerator
    from backend.recommendation.models import (
        CandidateProfile,
        JobProfile,
        RecommendationResult,
    )
except ModuleNotFoundError:
    from recommendation.skill_matcher import SkillMatcher  # type: ignore
    from recommendation.experience_matcher import ExperienceMatcher  # type: ignore
    from recommendation.location_matcher import LocationMatcher  # type: ignore
    from recommendation.salary_matcher import SalaryMatcher  # type: ignore
    from recommendation.title_matcher import TitleMatcher  # type: ignore
    from recommendation.score_calculator import ScoreCalculator  # type: ignore
    from recommendation.explanation_generator import ExplanationGenerator  # type: ignore
    from recommendation.models import (  # type: ignore
        CandidateProfile,
        JobProfile,
        RecommendationResult,
    )


class RecommendationEngine:
    """
    Main orchestration class for the V1 Recommendation System.
    Combines Skill, Experience, Location, Salary, Title matchers, ScoreCalculator, and ExplanationGenerator.
    """

    def __init__(self) -> None:
        """Initialize and store reusable instances of all matchers and engine components."""
        self.skill_matcher = SkillMatcher()
        self.experience_matcher = ExperienceMatcher()
        self.location_matcher = LocationMatcher()
        self.salary_matcher = SalaryMatcher()
        self.title_matcher = TitleMatcher()
        self.score_calculator = ScoreCalculator()
        self.explanation_generator = ExplanationGenerator()

    def _evaluate_domain_gate(
        self,
        candidate: CandidateProfile,
        job: JobProfile,
        skill_res: Any,
        title_res: Any,
        search_query_title: Optional[str] = None,
        search_query_location: Optional[str] = None,
        search_query_work_mode: Optional[str] = None,
    ) -> bool:
        """
        Evaluates Domain Relevance Gate and Hard Constraints.
        Precedence: Search Query Term > Profile Skills > Candidate Title.
        Also enforces hard location and work_mode filtering when explicitly supplied.
        Returns True if domain gate and hard constraints passed, False if rejected.
        """
        import logging
        from backend.recommendation.skill_matcher import canonicalize_skill
        log = logging.getLogger(__name__)

        # Hard Filter: Explicit Work Mode
        if search_query_work_mode:
            norm_query_mode = search_query_work_mode.strip().lower()
            if "remote" in norm_query_mode or "wfh" in norm_query_mode:
                j_mode = self.location_matcher._normalize_work_mode(getattr(job, "job_mode", None))
                if j_mode != "remote":
                    log.info("[HARD_FILTER] Job %s rejected: work_mode mismatch (requested Remote, got %s)", getattr(job, "id", None), j_mode)
                    return False

        # Hard Filter: Explicit Location
        if search_query_location:
            req_loc = self.location_matcher._normalize_text(search_query_location)
            j_mode = self.location_matcher._normalize_work_mode(getattr(job, "job_mode", None))
            if req_loc and j_mode != "remote":
                j_city = self.location_matcher._normalize_text(getattr(job, "city", None))
                j_state = self.location_matcher._normalize_text(getattr(job, "state", None))
                j_country = self.location_matcher._normalize_text(getattr(job, "country", None))
                j_has_loc = any([j_city, j_state, j_country])
                if j_has_loc:
                    matched_loc = False
                    if j_city and (req_loc == j_city or req_loc in j_city or j_city in req_loc):
                        matched_loc = True
                    elif j_state and (req_loc == j_state or req_loc in j_state or j_state in req_loc):
                        matched_loc = True
                    elif j_country and (req_loc == j_country or req_loc in j_country or j_country in req_loc):
                        matched_loc = True
                    
                    if not matched_loc:
                        log.info("[HARD_FILTER] Job %s rejected: location mismatch (requested %s, job city=%s)", getattr(job, "id", None), search_query_location, j_city)
                        return False

        query_term = (search_query_title or "").strip()
        cand_skills = candidate.skills if (candidate and candidate.skills) else []
        cand_title = (
            getattr(candidate, "current_title", None)
            or getattr(candidate, "title", None)
            or getattr(candidate, "designation", None)
            or ""
        )
        if isinstance(cand_title, str):
            cand_title = cand_title.strip()
        else:
            cand_title = ""

        skill_source = "QUERY" if query_term else ("PROFILE" if (cand_skills or cand_title) else "FALLBACK")

        # 1. Explicit Query Skill Precedence (Tests F, P, Q, R, S)
        if query_term:
            q_title_res = self.title_matcher._match_titles(query_term, getattr(job, "title", ""))
            query_match = bool(
                q_title_res
                and getattr(q_title_res, "match_level", "none") in ("exact", "contains", "keyword_match")
            )
            if not query_match and getattr(job, "required_skills", None):
                q_words = set(re.findall(r"\w+", query_term.lower()))
                is_explicit_role = bool(q_words & (self.title_matcher.GENERIC_ROLE_SUFFIXES | self.title_matcher.SENIORITY_WORDS))
                if not is_explicit_role:
                    q_term_lower = query_term.lower()
                    q_canon = canonicalize_skill(query_term).lower()
                    for req_sk in job.required_skills:
                        req_sk_canon = canonicalize_skill(str(req_sk)).lower()
                        req_str_lower = str(req_sk).lower()
                        if q_term_lower == req_str_lower or q_canon == req_sk_canon or q_term_lower in req_str_lower or req_str_lower in q_term_lower:
                            query_match = True
                            break
            domain_gate_passed = query_match
        elif cand_skills or cand_title:
            # 2. Profile Skills or Candidate Title Relevance (Tests A, B, C, E, G-O)
            skill_match = bool(skill_res and skill_res.score > 0)
            title_match = bool(
                title_res
                and getattr(title_res, "match_level", "none") in ("exact", "contains", "keyword_match")
            )
            # If job required_skills is empty, check if any candidate skill matches job title or description with token boundaries
            if not skill_match and not title_match and not getattr(job, "required_skills", None) and cand_skills:
                raw_d = getattr(job, "raw_data", {}) or {}
                j_desc = raw_d.get("job_description") or raw_d.get("description") or getattr(job, "description", "") or ""
                job_text = f"{getattr(job, 'title', '')} {j_desc}".lower()
                for c_sk in cand_skills:
                    c_sk_clean = str(c_sk).strip().lower()
                    if len(c_sk_clean) >= 2:
                        # Allow punctuation at end of skill (like c++, c#) or standard word boundaries
                        pattern = r'(?:^|\s|[\(\[\{,;:])' + re.escape(c_sk_clean) + r'(?:$|\s|[\)\}\],;:!\?])'
                        if re.search(pattern, job_text) or (c_sk_clean.isalnum() and re.search(r'\b' + re.escape(c_sk_clean) + r'\b', job_text)):
                            skill_match = True
                            break

            domain_gate_passed = skill_match or title_match
        else:
            # 3. No query term and no profile signals -> General search fallback (Test D)
            domain_gate_passed = True

        gate_status = "PASS" if domain_gate_passed else "REJECT"
        log.info(
            "[QUERY_TRACE] query_skill=%r skill_source=%r",
            query_term, skill_source
        )
        log.info(
            "[DOMAIN_GATE] job_id=%s job_title=%r skill_source=%s query_term=%r skill_score=%.1f title_score=%.1f candidate_skills=%s domain_gate=%s",
            getattr(job, "id", None), getattr(job, "title", "Job"),
            skill_source, query_term,
            skill_res.score if skill_res else 0.0,
            title_res.score if title_res else 0.0,
            cand_skills,
            gate_status
        )

        return domain_gate_passed

    def recommend(
        self,
        candidate: CandidateProfile,
        job: JobProfile,
        profile_status: Optional[Any] = None,
        search_mode: Optional[Any] = None,
        search_query_title: Optional[str] = None,
        search_query_location: Optional[str] = None,
        search_query_work_mode: Optional[str] = None,
    ) -> Optional[RecommendationResult]:
        """
        Executes full evaluation pipeline for a candidate and job profile pair.
        Returns None if job fails the Domain Relevance Gate or explicit hard filters.
        """
        if candidate is None:
            raise ValueError("CandidateProfile cannot be None.")
        if job is None:
            raise ValueError("JobProfile cannot be None.")

        # 1. Skill Match
        skill_res = self.skill_matcher.evaluate_profiles(candidate, job)

        # 2. Title Match
        title_res = self.title_matcher.evaluate_profiles(candidate, job)

        # 3. ★ DOMAIN RELEVANCE GATE & HARD FILTERS ★
        if not self._evaluate_domain_gate(
            candidate=candidate,
            job=job,
            skill_res=skill_res,
            title_res=title_res,
            search_query_title=search_query_title,
            search_query_location=search_query_location,
            search_query_work_mode=search_query_work_mode,
        ):
            return None

        # Align Title and Skill scores with explicit query term if provided
        if search_query_title:
            q_title_res = self.title_matcher._match_titles(search_query_title, job.title)
            if q_title_res.score > title_res.score:
                title_res = q_title_res

            # Only inflate skill score if title is relevant to query term
            if getattr(q_title_res, "match_level", "none") in ("exact", "contains", "keyword_match"):
                from backend.recommendation.skill_matcher import canonicalize_skill
                q_canon = canonicalize_skill(search_query_title).lower()
                q_lower = search_query_title.lower()
                matched_q_skill = False
                for req_sk in (job.required_skills or []):
                    req_sk_canon = canonicalize_skill(str(req_sk)).lower()
                    req_str_lower = str(req_sk).lower()
                    if q_lower == req_str_lower or q_canon == req_sk_canon or q_lower in req_str_lower or req_str_lower in q_lower:
                        matched_q_skill = True
                        break
                if matched_q_skill and skill_res.score < 100.0:
                    skill_res.score = max(skill_res.score, 90.0)
                    if search_query_title not in (skill_res.matched_skills or []):
                        skill_res.matched_skills = list(skill_res.matched_skills or []) + [search_query_title]

        # 4. Experience Match
        exp_res = self.experience_matcher.evaluate_profiles(candidate, job)

        # 5. Location Match
        loc_res = self.location_matcher.evaluate_profiles(candidate, job)

        # 6. Salary Match
        sal_res = self.salary_matcher.evaluate_profiles(candidate, job)

        # 7. Score Calculation
        score_res = self.score_calculator.calculate_score(
            skill_result=skill_res,
            experience_result=exp_res,
            location_result=loc_res,
            salary_result=sal_res,
            title_result=title_res,
            profile_status=profile_status,
            search_mode=search_mode,
        )

        # 8. Explanation Generation
        exp_gen_res = self.explanation_generator.generate_explanation(
            score_result=score_res,
            skill_result=skill_res,
            experience_result=exp_res,
            location_result=loc_res,
            salary_result=sal_res,
            title_result=title_res,
            search_query_title=search_query_title,
            search_query_location=search_query_location,
            search_query_work_mode=search_query_work_mode,
        )

        # 9. Construct & Return RecommendationResult
        candidate_id = getattr(candidate, "id", None)
        job_id = getattr(job, "id", None)

        return RecommendationResult(
            overall_score=score_res.overall_score,
            recommendation_level=score_res.recommendation_level,
            skill_result=skill_res,
            experience_result=exp_res,
            location_result=loc_res,
            salary_result=sal_res,
            title_result=title_res,
            score_result=score_res,
            explanation_result=exp_gen_res,
            candidate_id=candidate_id,
            job_id=job_id,
            search_reasons=getattr(exp_gen_res, "search_reasons", []),
            recommendation_reasons=getattr(exp_gen_res, "recommendation_reasons", []),
            raw_data=getattr(job, "raw_data", {}) or {},
        )

    def recommend_jobs(
        self,
        candidate: CandidateProfile,
        jobs: List[JobProfile],
        profile_status: Optional[Any] = None,
        search_mode: Optional[Any] = None,
        search_query_title: Optional[str] = None,
        search_query_location: Optional[str] = None,
        search_query_work_mode: Optional[str] = None,
    ) -> List[RecommendationResult]:
        """
        Ranks and returns job recommendations for a candidate sorted deterministically by
        overall score descending with job_id tie-breaker.
        """
        if candidate is None:
            raise ValueError("CandidateProfile cannot be None.")
        if not jobs:
            return []

        results: List[RecommendationResult] = []
        for job in jobs:
            rec_res = self.recommend(
                candidate=candidate,
                job=job,
                profile_status=profile_status,
                search_mode=search_mode,
                search_query_title=search_query_title,
                search_query_location=search_query_location,
                search_query_work_mode=search_query_work_mode,
            )
            if rec_res is not None:
                results.append(rec_res)

        # DETERMINISTIC SORT by overall_score descending, then skill_score descending secondary key (preserving insertion order for complete ties)
        results.sort(
            key=lambda r: (
                -r.overall_score,
                -float(getattr(r.score_result, "skill_score", getattr(r.skill_result, "score", 0.0))),
            )
        )

        # Assign 1-based global_rank strictly post-sort
        for idx, res in enumerate(results, start=1):
            res.global_rank = idx

        return results

    def recommend_candidates(
        self,
        job: JobProfile,
        candidates: List[CandidateProfile],
    ) -> List[RecommendationResult]:
        """
        Ranks and returns candidate recommendations for a job sorted by overall score descending.
        """
        if job is None:
            raise ValueError("JobProfile cannot be None.")
        if not candidates:
            return []

        results: List[RecommendationResult] = [
            self.recommend(candidate, job) for candidate in candidates
        ]
        results = [r for r in results if r is not None]
        results.sort(
            key=lambda r: (
                -r.overall_score,
                -float(getattr(r.score_result, "skill_score", getattr(r.skill_result, "score", 0.0))),
            )
        )
        return results

