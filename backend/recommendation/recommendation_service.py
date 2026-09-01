"""
recommendation_service.py — Service layer for orchestrating DB data retrieval and RecommendationEngine execution.
"""

import logging
import re
from typing import List, Optional, Dict, Any, Tuple

try:
    from backend.recommendation.recommendation_engine import RecommendationEngine
    from backend.recommendation.models import RecommendationResult, CandidateProfile, JobProfile
    from backend.recommendation.mappers import to_candidate_profile, to_candidate_profile_from_api, to_job_profile
    from backend.repositories.candidate_repository import CandidateRepository
    from backend.repositories.job_repository import JobRepository
    from backend.utils.session_state import (
        set_recommendation_session,
        get_recommendation_session,
        update_last_referenced_job_id
    )
except ModuleNotFoundError:
    from recommendation.recommendation_engine import RecommendationEngine  # type: ignore
    from recommendation.models import RecommendationResult, CandidateProfile, JobProfile  # type: ignore
    from recommendation.mappers import to_candidate_profile, to_candidate_profile_from_api, to_job_profile  # type: ignore
    from repositories.candidate_repository import CandidateRepository  # type: ignore
    from repositories.job_repository import JobRepository  # type: ignore
    from utils.session_state import (  # type: ignore
        set_recommendation_session,
        get_recommendation_session,
        update_last_referenced_job_id
    )

log = logging.getLogger(__name__)

ORDINAL_MAP = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10
}


def parse_followup_rank_or_id(user_message: Optional[str]) -> Tuple[Optional[int], bool]:
    """
    Parses conversational follow-up message to extract explicit 1-based rank or determine if ambiguous.
    Returns (explicit_rank, is_ambiguous_contextual).
    """
    if not user_message:
        return None, True

    msg_lower = user_message.lower().strip()

    # Check word ordinals first: e.g. "second", "seventh", "2nd", "7th"
    for word, val in ORDINAL_MAP.items():
        if re.search(r'\b' + re.escape(word) + r'\b', msg_lower):
            return val, False

    # Digits regex: e.g. "7th", "job 3", "#5", "21st", "rank 2"
    digit_match = re.search(r'\b(?:job|rank|position|#)?\s*(\d+)(?:st|nd|rd|th)?\b', msg_lower)
    if digit_match:
        try:
            rank_val = int(digit_match.group(1))
            return rank_val, False
        except ValueError:
            pass

    return None, True


def get_ordinal_suffix(n: int) -> str:
    """Returns clean grammatical ordinal string e.g. 1st, 2nd, 3rd, 21st, 22nd."""
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


class RecommendationService:
    """
    Orchestration service connecting database repositories to the core RecommendationEngine.
    """

    def __init__(self, engine: Optional[RecommendationEngine] = None) -> None:
        """Instantiate RecommendationEngine once for optimal performance."""
        self.engine = engine or RecommendationEngine()

    def get_recommendations_for_profile(
        self,
        candidate_profile: CandidateProfile,
        jobs: Optional[List[Dict[str, Any]]] = None,
        top_n: int = 5,
        job_mode: Optional[int] = None,
        city_id: Optional[int] = None
    ) -> List[RecommendationResult]:
        """
        Ranks candidate recommendations for a CandidateProfile constructed from API data.
        If 'jobs' is provided (e.g. from explicit DB search filters), ranks those jobs.
        Otherwise uses JobRepository.get_jobs_for_skills to perform skill-aware job retrieval.
        Does NOT use CandidateRepository for candidate skills.
        """
        if candidate_profile is None:
            log.warning("No candidate_profile provided to get_recommendations_for_profile")
            return []

        cand_skills = candidate_profile.skills if candidate_profile else []
        log.info("[Recommendation] Candidate skills: %s", cand_skills)
        jobs_db = jobs
        if jobs_db is None:
            if cand_skills:
                log.info("[Recommendation] Skill-aware job retrieval: candidate_skills_count=%d, job_mode=%s, city_id=%s", len(cand_skills), job_mode, city_id)
                jobs_db = JobRepository.get_jobs_for_skills(cand_skills, limit=200, job_mode=job_mode, city_id=city_id)
                if not jobs_db:
                    log.info("[Recommendation] No skill matches found for candidate skills %s. Returning empty recommendations.", cand_skills)
                    return []

            else:
                log.info("[Recommendation] Profile has no skills. Loading active jobs fallback.")
                jobs_db = JobRepository.get_active_jobs(limit=200)

        log.info("[Recommendation] Jobs retrieved: count=%d", len(jobs_db or []))

        if not jobs_db:
            log.info("No jobs available for recommendation ranking.")
            return []

        job_profiles: List[JobProfile] = []
        for j in jobs_db:
            try:
                job_profiles.append(to_job_profile(j))
            except Exception as ex:
                log.warning("Skipping job mapping in recommendation: %s", ex)

        log.info("[Recommendation] Jobs mapped: count=%d", len(job_profiles))

        if not job_profiles:
            return []

        recommendations = self.engine.recommend_jobs(candidate_profile, job_profiles)
        if cand_skills and jobs is None:
            filtered_recs = []
            for r in recommendations:
                has_skill_match = bool(r.skill_result and r.skill_result.score > 0)
                has_title_match = bool(r.title_result and r.title_result.match_level in ("exact", "contains", "keyword_match"))
                if has_skill_match or has_title_match or r.overall_score >= 40.0:
                    filtered_recs.append(r)
            if filtered_recs:
                recommendations = filtered_recs
            for idx, res in enumerate(recommendations, start=1):
                res.global_rank = idx

        log.info("[Recommendation] Jobs scored: count=%d", len(recommendations))

        final_recs = recommendations[:top_n]
        log.info("[Recommendation] Recommendations returned: count=%d", len(final_recs))
        for i, r in enumerate(final_recs, 1):
            log.info(
                "[Recommendation] Top %d: Job ID %s matched skills=%s, overall_score=%.1f",
                i, r.job_id, r.skill_result.matched_skills, r.overall_score
            )

        return final_recs

    def get_recommendations_from_api_profile(
        self,
        api_data: Dict[str, Any],
        user_id: Optional[int] = None,
        jobs: Optional[List[Dict[str, Any]]] = None,
        top_n: int = 5
    ) -> List[RecommendationResult]:
        """
        Maps user-profile API JSON payload to CandidateProfile and ranks candidate recommendations.
        """
        candidate_profile = to_candidate_profile_from_api(api_data, user_id=user_id)
        return self.get_recommendations_for_profile(candidate_profile, jobs=jobs, top_n=top_n)

    def get_paginated_recommendations_for_profile(
        self,
        candidate_profile: CandidateProfile,
        jobs: Optional[List[Dict[str, Any]]] = None,
        page: int = 1,
        per_page: int = 5,
        session_id: Optional[str] = None,
        job_mode: Optional[int] = None,
        city_id: Optional[int] = None
    ) -> Tuple[List[RecommendationResult], Dict[str, Any], List[RecommendationResult]]:
        """
        Ranks all candidate pool jobs and applies post-ranking pagination slicing.
        Stores complete un-sliced ranked results in session memory before page slicing.
        Returns (page_slice_recommendations, pagination_metadata_dict, all_ranked_recommendations).
        """
        import math
        all_recs = self.get_recommendations_for_profile(candidate_profile, jobs=jobs, top_n=200, job_mode=job_mode, city_id=city_id)

        total_results = len(all_recs)
        total_pages = max(1, math.ceil(total_results / per_page))
        current_page = max(1, min(page, total_pages))

        if session_id and all_recs:
            query_key = f"cand_{candidate_profile.id if hasattr(candidate_profile, 'id') else 'default'}"
            set_recommendation_session(
                session_id=session_id,
                query_key=query_key,
                ranked_jobs=all_recs,
                current_page=current_page,
                page_size=per_page,
                total_jobs=total_results
            )

        if total_results > 0:
            start_index = (current_page - 1) * per_page + 1
            end_index = min(current_page * per_page, total_results)
            page_slice = all_recs[(current_page - 1) * per_page : current_page * per_page]
        else:
            start_index = 0
            end_index = 0
            page_slice = []

        pagination_meta = {
            "current_page": current_page,
            "per_page": per_page,
            "total_results": total_results,
            "total_pages": total_pages,
            "start_index": start_index,
            "end_index": end_index,
            "has_next": current_page < total_pages,
            "has_prev": current_page > 1,
        }

        return page_slice, pagination_meta, all_recs

    def get_recommendation_by_rank_or_id(
        self,
        session_id: str,
        user_message: Optional[str] = None,
        target_rank: Optional[int] = None,
        target_job_id: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Resolves pre-computed RecommendationResult snapshot from active recommendation session state.
        Enforces Precedence Hierarchy:
        1. Explicit conversational reference in user_message (always wins over UI job_id)
        2. UI-selected target_job_id (when message lacks explicit ordinal)
        3. Direct target_rank argument
        4. Ambiguous fallback: last_referenced_job_id -> Current-page top job -> Global rank 1 fallback
        
        Zero DB search, zero re-scoring, zero API retrieval.
        """
        rec_session = get_recommendation_session(session_id)
        if not rec_session or not rec_session.get("ranked_jobs"):
            return {
                "success": False,
                "error": "I don't have a recent job recommendation list to refer to."
            }

        ranked_jobs: List[RecommendationResult] = rec_session["ranked_jobs"]
        total_jobs = len(ranked_jobs)
        page_size = rec_session.get("page_size", 5)
        current_page = rec_session.get("current_page", 1)
        last_ref_id = rec_session.get("last_referenced_job_id")

        explicit_rank, _ = parse_followup_rank_or_id(user_message) if user_message else (target_rank, False)

        resolved_snapshot: Optional[RecommendationResult] = None

        # Priority 1: Explicit conversational rank reference (always wins over UI job_id)
        if explicit_rank is not None:
            if explicit_rank <= 0:
                return {"success": False, "error": f"Rank #{explicit_rank} is invalid. Please specify a valid rank number."}
            if explicit_rank > total_jobs:
                ord_str = get_ordinal_suffix(explicit_rank)
                return {"success": False, "error": f"I don't have a {ord_str} recommended job in the current results."}
            for r in ranked_jobs:
                if getattr(r, "global_rank", None) == explicit_rank:
                    resolved_snapshot = r
                    break
            if not resolved_snapshot and 1 <= explicit_rank <= total_jobs:
                resolved_snapshot = ranked_jobs[explicit_rank - 1]

            for r in ranked_jobs:
                if getattr(r, "global_rank", None) == explicit_rank:
                    resolved_snapshot = r
                    break
            if not resolved_snapshot and 1 <= explicit_rank <= total_jobs:
                resolved_snapshot = ranked_jobs[explicit_rank - 1]

        # Priority 2: UI-selected target_job_id (when message lacks explicit conversational rank)
        elif target_job_id is not None:
            try:
                t_id = int(target_job_id)
                for r in ranked_jobs:
                    if getattr(r, "job_id", None) == t_id or (hasattr(r, "job") and getattr(r.job, "id", None) == t_id):
                        resolved_snapshot = r
                        break
            except (ValueError, TypeError):
                pass

        # Priority 3: Most recently referenced job (last_referenced_job_id)
        if resolved_snapshot is None and last_ref_id is not None:
            for r in ranked_jobs:
                if getattr(r, "job_id", None) == last_ref_id:
                    resolved_snapshot = r
                    break

        # Priority 4: Current page first job fallback (dynamic page_size from session context)
        if resolved_snapshot is None and current_page > 1:
            computed_rank = (current_page - 1) * page_size + 1
            if 1 <= computed_rank <= total_jobs:
                resolved_snapshot = ranked_jobs[computed_rank - 1]

        # Priority 5: Global Rank #1 Fallback
        if resolved_snapshot is None and total_jobs > 0:
            resolved_snapshot = ranked_jobs[0]

        if resolved_snapshot is None:
            return {"success": False, "error": "Could not resolve the requested job recommendation."}

        # Update last_referenced_job_id in active session context
        if getattr(resolved_snapshot, "job_id", None):
            update_last_referenced_job_id(session_id, resolved_snapshot.job_id)

        return {
            "success": True,
            "recommendation": resolved_snapshot,
            "global_rank": getattr(resolved_snapshot, "global_rank", 1),
            "job_id": getattr(resolved_snapshot, "job_id", None),
            "recommendation_explanation": getattr(resolved_snapshot.explanation_result, "summary", ""),
            "recommendation_reasons": getattr(resolved_snapshot, "recommendation_reasons", []),
        }

    def get_candidate_recommendations(self, candidate_id: int, top_n: int = 5) -> List[RecommendationResult]:
        """
        Loads candidate & active jobs from database repositories, maps to domain profiles,
        invokes RecommendationEngine.recommend_jobs(), and returns the top_n results.

        :param candidate_id: ID of candidate in the database.
        :param top_n: Maximum number of top recommendations to return (default: 5).
        :return: List of RecommendationResult objects sorted by match score.
        """
        if candidate_id is None or candidate_id <= 0:
            log.warning("Invalid candidate_id provided to RecommendationService: %s", candidate_id)
            return []

        # 1. Load Candidate DB Data
        cand_db = CandidateRepository.get_candidate_by_id(candidate_id)
        if not cand_db:
            log.warning("No candidate found in database for ID %s", candidate_id)
            return []

        # 2. Map Candidate DB entity to CandidateProfile
        candidate_profile = to_candidate_profile(cand_db)

        # 3. Load Active Jobs DB Data
        active_jobs_db = JobRepository.get_active_jobs(limit=50)
        if not active_jobs_db:
            log.info("No active jobs found in database for recommendations.")
            return []

        # 4. Map Job DB records to JobProfile list
        job_profiles: List[JobProfile] = [to_job_profile(j) for j in active_jobs_db]

        # 5. Execute Recommendation Engine & return top_n
        recommendations = self.engine.recommend_jobs(candidate_profile, job_profiles)
        return recommendations[:top_n]

    def recommend_for_candidate(self, candidate_id: int, top_n: int = 5) -> List[RecommendationResult]:
        """Alias for get_candidate_recommendations."""
        return self.get_candidate_recommendations(candidate_id, top_n=top_n)

    def recommend_jobs_for_candidate(self, candidate_id: int, top_n: int = 5) -> List[RecommendationResult]:
        """Alias for get_candidate_recommendations."""
        return self.get_candidate_recommendations(candidate_id, top_n=top_n)


# Global singleton instance for easy reuse across chatbot handlers
recommendation_service = RecommendationService()
