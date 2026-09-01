"""
recommendation_engine.py — Master Recommendation Engine Orchestrator.
Orchestrates all matchers, ScoreCalculator, and ExplanationGenerator.
"""

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

    def recommend(
        self,
        candidate: CandidateProfile,
        job: JobProfile,
        profile_status: Optional[Any] = None,
        search_mode: Optional[Any] = None,
        search_query_title: Optional[str] = None,
        search_query_location: Optional[str] = None,
        search_query_work_mode: Optional[str] = None,
    ) -> RecommendationResult:
        """
        Executes full evaluation pipeline for a candidate and job profile pair.
        """
        if candidate is None:
            raise ValueError("CandidateProfile cannot be None.")
        if job is None:
            raise ValueError("JobProfile cannot be None.")

        # 1. Skill Match
        skill_res = self.skill_matcher.evaluate_profiles(candidate, job)

        # 2. Experience Match
        exp_res = self.experience_matcher.evaluate_profiles(candidate, job)

        # 3. Location Match
        loc_res = self.location_matcher.evaluate_profiles(candidate, job)

        # 4. Salary Match
        sal_res = self.salary_matcher.evaluate_profiles(candidate, job)

        # 5. Title Match
        title_res = self.title_matcher.evaluate_profiles(candidate, job)

        # 6. Score Calculation
        score_res = self.score_calculator.calculate_score(
            skill_result=skill_res,
            experience_result=exp_res,
            location_result=loc_res,
            salary_result=sal_res,
            title_result=title_res,
            profile_status=profile_status,
            search_mode=search_mode,
        )

        # 7. Explanation Generation
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

        # 8. Construct & Return RecommendationResult
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

        results: List[RecommendationResult] = [
            self.recommend(
                candidate=candidate,
                job=job,
                profile_status=profile_status,
                search_mode=search_mode,
                search_query_title=search_query_title,
                search_query_location=search_query_location,
                search_query_work_mode=search_query_work_mode,
            )
            for job in jobs
        ]

        # DETERMINISTIC SORT by overall_score descending, then job_id descending tie-breaker
        results.sort(key=lambda r: (-r.overall_score, -int(r.job_id or 0)))

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
        results.sort(key=lambda r: (-r.overall_score, -int(r.candidate_id or 0)))
        return results

