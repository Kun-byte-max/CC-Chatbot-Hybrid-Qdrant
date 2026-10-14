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

try:
    from backend.services.job_match_client import job_match_client, JobMatchException, CandidateNotIndexedException, JobMatchAuthException
    from backend.config.config import JOB_MATCH_ENABLED
except ModuleNotFoundError:
    from services.job_match_client import job_match_client, JobMatchException, CandidateNotIndexedException, JobMatchAuthException  # type: ignore
    from config.config import JOB_MATCH_ENABLED  # type: ignore

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
        city_id: Optional[int] = None,
        search_query_title: Optional[str] = None,
        search_query_location: Optional[str] = None,
        search_query_work_mode: Optional[str] = None,
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

        recommendations = self.engine.recommend_jobs(
            candidate_profile,
            job_profiles,
            search_query_title=search_query_title,
            search_query_location=search_query_location,
            search_query_work_mode=search_query_work_mode,
        )
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
        city_id: Optional[int] = None,
        search_query_title: Optional[str] = None,
        search_query_location: Optional[str] = None,
        search_query_work_mode: Optional[str] = None,
    ) -> Tuple[List[RecommendationResult], Dict[str, Any], List[RecommendationResult]]:
        """
        Ranks all candidate pool jobs and applies post-ranking pagination slicing.
        Stores complete un-sliced ranked results in session memory before page slicing.
        Returns (page_slice_recommendations, pagination_metadata_dict, all_ranked_recommendations).
        """
        import math
        all_recs = self.get_recommendations_for_profile(
            candidate_profile,
            jobs=jobs,
            top_n=200,
            job_mode=job_mode,
            city_id=city_id,
            search_query_title=search_query_title,
            search_query_location=search_query_location,
            search_query_work_mode=search_query_work_mode,
        )

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

    async def get_job_match_recommendations_async(
        self,
        candidate_id: int,
        candidate_profile: Optional[CandidateProfile] = None,
        limit: int = 11,
        filters: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        page: int = 1,
        per_page: int = 5,
    ) -> Tuple[List[RecommendationResult], Dict[str, Any], List[RecommendationResult], bool]:
        """
        Attempts to retrieve personalized job recommendations from the deployed Job Match API.
        If successful, maps results to RecommendationResult objects and caches in session state.
        If disabled, unindexed (409), unavailable (503/timeout), or returning 0 hits,
        transparently falls back to the internal recommendation engine.
        Returns: (page_slice, pagination_meta, all_recs, used_job_match)
        """
        if not JOB_MATCH_ENABLED:
            log.info("[JobMatch] Feature flag disabled (JOB_MATCH_ENABLED=false). Using fallback engine.")
            slice_recs, meta, all_recs = self.get_paginated_recommendations_for_profile(
                candidate_profile=candidate_profile,
                page=page,
                per_page=per_page,
                session_id=session_id
            )
            return slice_recs, meta, all_recs, False

        try:
            res_data = await job_match_client.find_jobs(candidate_id=candidate_id, limit=limit, filters=filters)
            raw_results = res_data.get("results", [])

            if not raw_results:
                log.info("[JobMatch] 0 recommendations returned from API. Triggering internal engine fallback.")
                slice_recs, meta, all_recs = self.get_paginated_recommendations_for_profile(
                    candidate_profile=candidate_profile,
                    page=page,
                    per_page=per_page,
                    session_id=session_id
                )
                return slice_recs, meta, all_recs, False

            all_recs: List[RecommendationResult] = []
            for item in raw_results:
                if not isinstance(item, dict):
                    continue

                j_id = item.get("job_id")
                payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
                if not j_id and payload.get("job_id"):
                    j_id = payload.get("job_id")

                title = payload.get("job_title") or payload.get("title")

                # STRICT NON-FABRICATION POLICY: Drop incomplete records missing essential fields!
                if not j_id or not title or not str(title).strip():
                    log.warning("[JobMatch] Dropping incomplete record missing essential fields (job_id or title): %s", item)
                    continue

                raw_score = float(item.get("final_score", 0.0))
                # Verified conversion scale: 0.0 - 1.0 float -> 0.0 - 100.0 percentage
                match_score = round(raw_score * 100.0, 1)

                reasons = item.get("reasons") or []
                if reasons:
                    match_reason = f"Match Score: {round(match_score)}% • " + " • ".join(str(r) for r in reasons)
                else:
                    match_reason = f"Match Score: {round(match_score)}%"

                from backend.recommendation.models import (
                    SkillMatchResult, ExperienceMatchResult, LocationMatchResult,
                    SalaryMatchResult, TitleMatchResult, ScoreResult, ExplanationResult
                )

                sk_res = SkillMatchResult(score=match_score, matched_skills=[], missing_skills=[], extra_skills=[], total_required=0, total_matched=0)
                exp_res = ExperienceMatchResult(score=match_score, candidate_experience=0.0, required_experience=0.0, experience_gap=0.0, meets_requirement=True)
                loc_res = LocationMatchResult(score=match_score, candidate_city=None, candidate_state=None, candidate_country=None, job_city=None, job_state=None, job_country=None, work_mode="Work from Office", match_level="exact")
                sal_res = SalaryMatchResult(score=match_score, candidate_expected_salary=None, job_offered_salary=None, normalized_candidate_salary=None, normalized_job_salary=None, salary_difference_percent=0.0, meets_expectation=True)
                tit_res = TitleMatchResult(score=match_score, candidate_title=None, job_title=title, normalized_candidate_title=None, normalized_job_title=title, matched_keywords=[], match_level="exact")
                sc_res = ScoreResult(overall_score=match_score, recommendation_level="Job Match Profile Recommendation", skill_score=match_score, experience_score=match_score, location_score=match_score, salary_score=match_score, title_score=match_score, weight_skill=0.4, weight_experience=0.2, weight_location=0.1, weight_salary=0.15, weight_title=0.15)
                exp_res_obj = ExplanationResult(summary=match_reason, strengths=[], weaknesses=[], recommendation_level="Job Match Profile Recommendation", overall_score=match_score)

                rec = RecommendationResult(
                    overall_score=match_score,
                    recommendation_level="Job Match Profile Recommendation",
                    skill_result=sk_res,
                    experience_result=exp_res,
                    location_result=loc_res,
                    salary_result=sal_res,
                    title_result=tit_res,
                    score_result=sc_res,
                    explanation_result=exp_res_obj,
                    candidate_id=candidate_id,
                    job_id=int(j_id),
                    recommendation_reasons=[str(r) for r in reasons],
                    raw_data=payload,
                )
                all_recs.append(rec)

            if not all_recs:
                log.warning("[JobMatch] All returned records were incomplete. Triggering internal engine fallback.")
                slice_recs, meta, all_recs = self.get_paginated_recommendations_for_profile(
                    candidate_profile=candidate_profile,
                    page=page,
                    per_page=per_page,
                    session_id=session_id
                )
                return slice_recs, meta, all_recs, False

            # Sort deterministically post-mapping
            all_recs.sort(key=lambda r: (-r.overall_score, -int(r.job_id or 0)))
            for idx, r in enumerate(all_recs, start=1):
                r.global_rank = idx

            import math
            total_results = len(all_recs)
            total_pages = max(1, math.ceil(total_results / per_page))
            current_page = max(1, min(page, total_pages))

            if session_id:
                query_key = f"cand_{candidate_id}"
                set_recommendation_session(
                    session_id=session_id,
                    query_key=query_key,
                    ranked_jobs=all_recs,
                    current_page=current_page,
                    page_size=per_page,
                    total_jobs=total_results
                )

            start_index = (current_page - 1) * per_page + 1
            end_index = min(current_page * per_page, total_results)
            page_slice = all_recs[(current_page - 1) * per_page : current_page * per_page]

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

            return page_slice, pagination_meta, all_recs, True

        except JobMatchAuthException as auth_err:
            log.critical("[JobMatch] Auth failure (401/403): %s. Triggering internal engine fallback.", auth_err)
            slice_recs, meta, all_recs = self.get_paginated_recommendations_for_profile(
                candidate_profile=candidate_profile,
                page=page,
                per_page=per_page,
                session_id=session_id
            )
            return slice_recs, meta, all_recs, False

        except (CandidateNotIndexedException, JobMatchException) as jm_ex:
            log.info("[JobMatch] Job Match API unavailable/unindexed (%s). Falling back to internal engine.", jm_ex)
            slice_recs, meta, all_recs = self.get_paginated_recommendations_for_profile(
                candidate_profile=candidate_profile,
                page=page,
                per_page=per_page,
                session_id=session_id
            )
            return slice_recs, meta, all_recs, False

        except Exception as unk_ex:
            log.exception("[JobMatch] Unexpected exception in get_job_match_recommendations_async: %s", unk_ex)
            slice_recs, meta, all_recs = self.get_paginated_recommendations_for_profile(
                candidate_profile=candidate_profile,
                page=page,
                per_page=per_page,
                session_id=session_id
            )
            return slice_recs, meta, all_recs, False

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

    def get_paginated_employer_candidate_recommendations(
        self,
        employer_reqs: Any,
        candidates: List[Any],
        page: int = 1,
        per_page: int = 5,
        session_id: Optional[str] = None,
        search_gen: Optional[int] = None,
    ) -> Tuple[List[Any], Dict[str, Any], List[Any]]:
        """
        Ranks employer candidate recommendations using EmployerRecommendationEngine,
        applies post-ranking pagination slicing, and stores namespaced PaginationState.
        """
        import math
        from backend.recommendation.employer_recommendation_engine import EmployerRecommendationEngine
        from backend.repositories.candidate_repository import CandidateRepository
        from backend.utils.session_state import PaginationState, generate_query_fingerprint, set_pagination_state

        # Candidate ID pre-hydration deduplication & missing candidate ID filtering (Invariants #28 & #29)
        clean_candidates = []
        if candidates:
            cand_ids = [getattr(c, "id", None) or (c.get("id") if isinstance(c, dict) else None) for c in candidates]
            clean_ids = set(CandidateRepository.deduplicate_and_filter_candidate_ids(cand_ids))
            for c in candidates:
                c_id = getattr(c, "id", None) or (c.get("id") if isinstance(c, dict) else None)
                if c_id in clean_ids:
                    clean_candidates.append(c)
                    clean_ids.remove(c_id)

        # Instantiate dedicated employer engine
        emp_engine = EmployerRecommendationEngine()
        all_recs = emp_engine.recommend_candidates(employer_reqs, clean_candidates)

        from backend.recommendation.types import RecommendationStatus, MatchStatus, RecommendationDimension

        total_results = len(all_recs)
        eligible_recs = [
            r for r in all_recs
            if getattr(r, "overall_status", None) != RecommendationStatus.INSUFFICIENT_DATA
            and getattr(r.match_results.get(RecommendationDimension.EXPERIENCE), "status", None) != MatchStatus.MISMATCH
        ]
        eligible_count = len(eligible_recs)

        log.info(
            f"\n[EMPLOYER_RECOMMENDATION_DIAGNOSTICS]\n"
            f"retrieved_count={len(candidates)}\n"
            f"scored_count={total_results}\n"
            f"eligible_count={eligible_count}\n"
            f"returned_count={min(per_page, total_results)}\n"
        )

        query_fp = generate_query_fingerprint(employer_reqs)


        if total_results == 0:
            total_pages = 0
            current_page = 0
            page_slice = []
            start_index = 0
            end_index = 0
        else:
            total_pages = max(1, math.ceil(total_results / per_page))
            current_page = max(1, min(page, total_pages))
            start_index = (current_page - 1) * per_page + 1
            end_index = min(current_page * per_page, total_results)
            page_slice = all_recs[(current_page - 1) * per_page : current_page * per_page]

        if session_id:
            pag_state = PaginationState(
                session_id=session_id,
                recommendation_type="EMPLOYER",
                result_type="CANDIDATES",
                query_fingerprint=query_fp,
                current_page=current_page,
                total_pages=total_pages,
                total_results=total_results,
                result_ids=[rec.candidate_id for rec in all_recs],
                all_recs=all_recs,
            )
            pag_dict = pag_state.__dict__.copy()
            if search_gen is not None:
                pag_dict["search_gen"] = search_gen
            set_pagination_state(session_id, pag_dict, role="employer")

        pagination_meta = {
            "current_page": current_page,
            "per_page": per_page,
            "total_results": total_results,
            "total_pages": total_pages,
            "start_index": start_index,
            "end_index": end_index,
            "has_next": current_page < total_pages if total_pages > 0 else False,
            "has_prev": current_page > 1 if total_pages > 0 else False,
        }
        if search_gen is not None:
            pagination_meta["search_gen"] = search_gen

        return page_slice, pagination_meta, all_recs


# Global singleton instance for easy reuse across chatbot handlers
recommendation_service = RecommendationService()

