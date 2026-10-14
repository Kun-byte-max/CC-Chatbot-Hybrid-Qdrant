"""
test_phase3_urgent_profile_relevance_quality.py
Phase 3 Quality & Relevance Hardening Tests for Urgent Job + Profile Search.

Verifies:
1. HARD eligibility vs RELEVANCE signal: urgent=True strictly filters out non-urgent jobs, while profile vector similarity ranks the eligible urgent jobs.
2. Search mode weights: MODE_PROFILE_RECOMMENDATION scales vector jobmatch weight to 0.70 and meilisearch weight to 0.30.
3. Zero-leakage invariant: Non-urgent jobs with 99% profile match are strictly excluded from urgent profile search.
"""

import pytest
from unittest.mock import AsyncMock, patch
from backend.services.hybrid_job_search import (
    execute_hybrid_job_search,
    detect_search_mode,
    get_mode_weights,
    MODE_PROFILE_RECOMMENDATION,
)
from backend.recommendation.models import CandidateProfile


@pytest.mark.anyio
async def test_urgent_profile_relevance_ranking_and_zero_leakage():
    """
    Verify that:
    1. Non-urgent jobs are excluded regardless of how high their vector score is (Job D).
    2. Urgent jobs are ranked according to their profile match score (Job A > Job C > Job B).
    """
    candidate_prof = CandidateProfile(
        id=101,
        skills=["Python", "FastAPI", "SQL"],
        current_title="Python Developer",
    )

    # Mock Meilisearch hits (Keyword hits)
    meili_hits = {
        "success": True,
        "data": [
            {
                "id": 10,
                "job_title": "Python Backend Developer",
                "company_name": "Tech Corp",
                "urgent": 1,
                "city_name": "Delhi",
            },
            {
                "id": 20,
                "job_title": "Java Spring Developer",
                "company_name": "Legacy Corp",
                "urgent": 1,
                "city_name": "Delhi",
            },
            {
                "id": 30,
                "job_title": "Python Machine Learning Engineer",
                "company_name": "AI Corp",
                "urgent": 1,
                "city_name": "Delhi",
            },
            {
                "id": 40,
                "job_title": "Lead Python FastAPI Architect",
                "company_name": "Top Tech",
                "urgent": 0,  # NOT URGENT
                "city_name": "Delhi",
            },
        ],
    }

    # Mock JobMatch hits (Vector similarity search hits)
    jobmatch_hits = {
        "success": True,
        "results": [
            {
                "job_id": 40,  # Non-urgent but highest vector fit (0.99)
                "final_score": 0.99,
                "payload": {
                    "job_id": 40,
                    "job_title": "Lead Python FastAPI Architect",
                    "company_name": "Top Tech",
                    "urgent": 0,
                },
            },
            {
                "job_id": 10,  # Urgent & high vector fit (0.92)
                "final_score": 0.92,
                "payload": {
                    "job_id": 10,
                    "job_title": "Python Backend Developer",
                    "company_name": "Tech Corp",
                    "urgent": 1,
                },
            },
            {
                "job_id": 30,  # Urgent & medium vector fit (0.85)
                "final_score": 0.85,
                "payload": {
                    "job_id": 30,
                    "job_title": "Python Machine Learning Engineer",
                    "company_name": "AI Corp",
                    "urgent": 1,
                },
            },
            {
                "job_id": 20,  # Urgent & lower vector fit (0.45)
                "final_score": 0.45,
                "payload": {
                    "job_id": 20,
                    "job_title": "Java Spring Developer",
                    "company_name": "Legacy Corp",
                    "urgent": 1,
                },
            },
        ],
    }

    with patch(
        "backend.services.hybrid_job_search.search_jobs",
        new=AsyncMock(return_value=meili_hits),
    ), patch(
        "backend.services.search_client.search_jobs",
        new=AsyncMock(return_value=meili_hits),
    ), patch(
        "backend.api.employee.search_jobs",
        new=AsyncMock(return_value=meili_hits),
    ), patch(
        "backend.services.hybrid_job_search.job_match_client.find_jobs",
        new=AsyncMock(return_value=jobmatch_hits),
    ), patch(
        "backend.services.hybrid_job_search.JOB_MATCH_ENABLED", True
    ):
        hits, meta = await execute_hybrid_job_search(
            last_user_msg="show me jobs according to my skills",
            candidate_id=101,
            candidate_profile=candidate_prof,
            base_filters={"urgent": 1},
            parsed={"urgent": True, "use_profile_skills": True},
            used_profile_fallback=True,
            profile_terms=["Python", "FastAPI", "SQL"],
        )

    # 1. Zero leakage verification
    returned_ids = [h["id"] for h in hits]
    assert 40 not in returned_ids, "Non-urgent Job 40 MUST NOT be returned despite highest vector match score!"

    # 2. Urgent filter verification
    for hit in hits:
        assert str(hit.get("urgent")) in ("1", "true", "True"), f"Returned non-urgent hit: {hit}"

    # 3. Profile relevance ordering verification
    # Job 10 (vector score 0.92, rank 2) should rank higher than Job 30 (0.85, rank 3), which ranks higher than Job 20 (0.45, rank 4)
    assert returned_ids == [10, 30, 20], f"Expected relevance ordering [10, 30, 20], got {returned_ids}"
    assert meta["search_mode"] == MODE_PROFILE_RECOMMENDATION


def test_profile_search_mode_weights():
    """Verify that MODE_PROFILE_RECOMMENDATION allocates higher weight to JobMatch vector matches."""
    mode = detect_search_mode(
        last_user_msg="show me jobs according to my skills",
        keyword=None,
        parsed={"use_profile_skills": True},
    )
    assert mode == MODE_PROFILE_RECOMMENDATION

    meili_w, jobmatch_w = get_mode_weights(mode)
    assert (jobmatch_w, meili_w) == (0.70, 0.30), "Profile mode should assign 0.70 weight to vector match and 0.30 to keyword match"
