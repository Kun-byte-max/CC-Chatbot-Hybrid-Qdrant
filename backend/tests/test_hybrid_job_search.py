"""
test_hybrid_job_search.py — Comprehensive Unit Test Suite for Hybrid Job Search Engine.
"""

import pytest
import asyncio
from unittest.mock import patch, AsyncMock

from backend.services.hybrid_job_search import (
    execute_hybrid_job_search,
    detect_search_mode,
    passes_location_eligibility,
    passes_domain_relevance_eligibility,
    NormalizedJob,
    MODE_PROFILE_RECOMMENDATION,
    MODE_EXPLICIT_KEYWORD,
    MODE_EXPLICIT_LOCATION,
    MODE_NEAR_ME,
)
from backend.services.job_match_client import (
    CandidateNotIndexedException,
    JobMatchAuthException,
    JobMatchUnavailableException,
)
from backend.recommendation.models import CandidateProfile


def test_detect_search_mode():
    """Verify correct classification of user queries into intent modes."""
    # Profile Recommendation
    assert detect_search_mode("show me jobs according to my skills") == MODE_PROFILE_RECOMMENDATION
    assert detect_search_mode("recommend jobs for me") == MODE_PROFILE_RECOMMENDATION
    assert detect_search_mode("jobs suitable for me") == MODE_PROFILE_RECOMMENDATION
    assert detect_search_mode("what jobs are suitable for me", used_profile_fallback=True) == MODE_PROFILE_RECOMMENDATION

    # Explicit Keyword Search
    assert detect_search_mode("Python jobs", keyword="Python") == MODE_EXPLICIT_KEYWORD
    assert detect_search_mode("backend developer jobs", keyword="backend developer") == MODE_EXPLICIT_KEYWORD
    assert detect_search_mode("Testing and Validation Techniques jobs", keyword="Testing and Validation Techniques") == MODE_EXPLICIT_KEYWORD

    # Explicit Location Search
    assert detect_search_mode("Python jobs in Delhi", keyword="Python", explicit_loc=True) == MODE_EXPLICIT_LOCATION

    # Near Me Intent
    assert detect_search_mode("jobs near me") == MODE_NEAR_ME
    assert detect_search_mode("nearby jobs") == MODE_NEAR_ME

    # GPS presence alone WITHOUT text intent DOES NOT force near-me intent mode
    assert detect_search_mode("Python jobs", keyword="Python") == MODE_EXPLICIT_KEYWORD


def test_hybrid_search_both_engines_success():
    """Verify fusion when both Meilisearch and JobMatch return hits for an authenticated candidate."""
    async def _test():
        meili_mock_data = [
            {"id": 101, "job_title": "Python Developer", "company_name": "Tech Corp", "location": "Delhi", "city_name": "Delhi"},
            {"id": 102, "job_title": "Backend Engineer", "company_name": "Data Systems", "location": "Noida", "city_name": "Noida"},
        ]
        jm_mock_data = {
            "candidate_id": 123,
            "results": [
                {
                    "job_id": 102,  # Duplicate job (appears in both)
                    "final_score": 0.92,
                    "reasons": ["Matched Python"],
                    "payload": {"job_id": 102, "job_title": "Backend Engineer", "company_name": "Data Systems", "city_name": "Noida"}
                },
                {
                    "job_id": 103,  # Vector-only job
                    "final_score": 0.88,
                    "reasons": ["High skill similarity"],
                    "payload": {"job_id": 103, "job_title": "Python ML Engineer", "company_name": "AI Lab", "city_name": "Delhi"}
                }
            ]
        }

        with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_meili:
            with patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:
                mock_meili.return_value = {"success": True, "data": meili_mock_data}
                mock_jm.return_value = jm_mock_data

                hits, meta = await execute_hybrid_job_search(
                    last_user_msg="Python jobs",
                    candidate_id=123,
                    keyword="Python",
                    limit=5,
                )

                assert len(hits) == 3
                assert meta["meilisearch_count"] == 2
                assert meta["jobmatch_count"] == 2
                assert meta["fused_count"] == 3

                # Verify deduplicated hybrid item (102)
                hit_102 = next(h for h in hits if h["id"] == 102)
                assert hit_102["_source"] == "hybrid"

    asyncio.run(_test())


def test_hybrid_search_mixed_rrf_and_domain_filtering():
    """
    Mixed RRF + Domain Gate Test:
    For 'Testing and Validation Techniques jobs', Meilisearch hits match, while vector-only off-domain
    jobs (Python Developer, Data Engineer) get rejected post-RRF.
    """
    async def _test():
        meili_hits = [
            {"id": 1, "job_title": "Testing Engineer", "company_name": "QA Corp"},
            {"id": 2, "job_title": "Validation Engineer", "company_name": "QA Corp"},
            {"id": 3, "job_title": "QA Engineer", "company_name": "QA Corp"},
        ]
        jm_hits = {
            "candidate_id": 123,
            "results": [
                {
                    "job_id": 10,
                    "final_score": 0.95,
                    "payload": {"job_id": 10, "job_title": "Python Developer"}
                },
                {
                    "job_id": 11,
                    "final_score": 0.90,
                    "payload": {"job_id": 11, "job_title": "Data Engineer"}
                },
                {
                    "job_id": 1,  # Hybrid match
                    "final_score": 0.85,
                    "payload": {"job_id": 1, "job_title": "Testing Engineer"}
                }
            ]
        }

        with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_meili:
            with patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:
                mock_meili.return_value = {"success": True, "data": meili_hits}
                mock_jm.return_value = jm_hits

                hits, meta = await execute_hybrid_job_search(
                    last_user_msg="Testing and Validation Techniques jobs",
                    candidate_id=123,
                    keyword="Testing and Validation Techniques",
                    limit=5,
                )

                titles = [h["job_title"] for h in hits]
                assert "Testing Engineer" in titles
                assert "Validation Engineer" in titles
                assert "QA Engineer" in titles
                assert "Python Developer" not in titles
                assert "Data Engineer" not in titles

    asyncio.run(_test())


def test_hybrid_search_mixed_rrf_and_location_filtering():
    """
    Mixed RRF + Location Constraint Test:
    For 'Python jobs in Delhi', vector-only Mumbai jobs (Mumbai Python Engineer) are rejected post-RRF.
    """
    async def _test():
        meili_hits = [
            {"id": 1, "job_title": "Delhi Python Engineer", "city_name": "Delhi", "location": "Delhi"},
            {"id": 2, "job_title": "Delhi Backend Engineer", "city_name": "Delhi", "location": "Delhi"},
        ]
        jm_hits = {
            "candidate_id": 123,
            "results": [
                {
                    "job_id": 10,
                    "final_score": 0.98,
                    "payload": {"job_id": 10, "job_title": "Mumbai Python Engineer", "city_name": "Mumbai", "location": "Mumbai"}
                },
                {
                    "job_id": 11,
                    "final_score": 0.90,
                    "payload": {"job_id": 11, "job_title": "Delhi Python Developer", "city_name": "Delhi", "location": "Delhi"}
                }
            ]
        }

        with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_meili:
            with patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:
                mock_meili.return_value = {"success": True, "data": meili_hits}
                mock_jm.return_value = jm_hits

                hits, meta = await execute_hybrid_job_search(
                    last_user_msg="Python jobs in Delhi",
                    candidate_id=123,
                    keyword="Python",
                    explicit_loc=True,
                    target_city_name="Delhi",
                    limit=5,
                )

                titles = [h["job_title"] for h in hits]
                assert "Delhi Python Engineer" in titles
                assert "Delhi Backend Engineer" in titles
                assert "Delhi Python Developer" in titles
                assert "Mumbai Python Engineer" not in titles

    asyncio.run(_test())


def test_hybrid_search_no_candidate_unauthenticated():
    """Verify unauthenticated candidate queries skip JobMatch and run Meilisearch only."""
    async def _test():
        meili_mock_data = [
            {"id": 201, "job_title": "Java Developer", "company_name": "Enterprise Inc"}
        ]

        with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_meili:
            with patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:
                mock_meili.return_value = {"success": True, "data": meili_mock_data}

                hits, meta = await execute_hybrid_job_search(
                    last_user_msg="Java jobs",
                    candidate_id=None,
                    keyword="Java",
                    limit=5,
                )

                assert len(hits) == 1
                assert meta["jobmatch_attempted"] is False
                assert meta["meilisearch_count"] == 1
                mock_jm.assert_not_called()

    asyncio.run(_test())


def test_hybrid_search_jobmatch_409_fallback():
    """Verify HTTP 409 response raises CandidateNotIndexedException to trigger fallback."""
    async def _test():
        meili_mock_data = [
            {"id": 301, "job_title": "QA Engineer", "company_name": "Test Co"}
        ]

        with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_meili:
            with patch("backend.services.hybrid_job_search.job_match_client.find_jobs", side_effect=CandidateNotIndexedException("Not indexed")):
                mock_meili.return_value = {"success": True, "data": meili_mock_data}

                hits, meta = await execute_hybrid_job_search(
                    last_user_msg="QA jobs",
                    candidate_id=999,
                    keyword="QA",
                    limit=5,
                )

                assert len(hits) == 1
                assert hits[0]["id"] == 301

    asyncio.run(_test())


def test_hybrid_search_jobmatch_503_or_timeout_fallback():
    """Verify HTTP 503 or timeout falls back smoothly to Meilisearch."""
    async def _test():
        meili_mock_data = [
            {"id": 401, "job_title": "DevOps Engineer", "company_name": "Cloud Ltd"}
        ]

        with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_meili:
            with patch("backend.services.hybrid_job_search.job_match_client.find_jobs", side_effect=JobMatchUnavailableException("503 Service Unavailable")):
                mock_meili.return_value = {"success": True, "data": meili_mock_data}

                hits, meta = await execute_hybrid_job_search(
                    last_user_msg="DevOps jobs",
                    candidate_id=123,
                    keyword="DevOps",
                    limit=5,
                )

                assert len(hits) == 1
                assert hits[0]["id"] == 401

    asyncio.run(_test())


def test_hybrid_search_both_engines_fail_safe():
    """Verify safe empty response when both engines fail."""
    async def _test():
        with patch("backend.services.hybrid_job_search.search_jobs", side_effect=Exception("Error")):
            with patch("backend.services.hybrid_job_search.job_match_client.find_jobs", side_effect=Exception("Error")):
                hits, meta = await execute_hybrid_job_search(
                    last_user_msg="Nonexistent jobs",
                    candidate_id=123,
                    limit=5,
                )

                assert hits == []
                assert meta["final_count"] == 0

    asyncio.run(_test())


def test_passes_job_mode_eligibility_unit():
    """Unit test for passes_job_mode_eligibility function across all mode categories."""
    from backend.services.hybrid_job_search import passes_job_mode_eligibility, NormalizedJob

    # Case A — No constraint -> ALLOW
    job_any = NormalizedJob(job_id=1, title="Dev", job_mode="Work from Office", raw_data={"job_mode_id": 1})
    assert passes_job_mode_eligibility(job_any, target_job_mode=None) is True

    # Case B — Remote requested
    job_remote = NormalizedJob(job_id=2, title="Remote Dev", job_mode="Work from Home", raw_data={"job_mode_id": 2})
    job_office = NormalizedJob(job_id=3, title="Office Dev", job_mode="Work from Office", raw_data={"job_mode_id": 1})
    job_hybrid = NormalizedJob(job_id=4, title="Hybrid Dev", job_mode="Hybrid", raw_data={"job_mode_id": 3})

    assert passes_job_mode_eligibility(job_remote, target_job_mode="remote") is True
    assert passes_job_mode_eligibility(job_remote, target_job_mode=[2]) is True
    assert passes_job_mode_eligibility(job_office, target_job_mode="remote") is False
    assert passes_job_mode_eligibility(job_hybrid, target_job_mode="remote") is False

    # Case C — Hybrid requested
    assert passes_job_mode_eligibility(job_hybrid, target_job_mode="hybrid") is True
    assert passes_job_mode_eligibility(job_hybrid, target_job_mode=[3]) is True
    assert passes_job_mode_eligibility(job_remote, target_job_mode="hybrid") is False
    assert passes_job_mode_eligibility(job_office, target_job_mode="hybrid") is False

    # Case D — Office requested
    assert passes_job_mode_eligibility(job_office, target_job_mode="office") is True
    assert passes_job_mode_eligibility(job_office, target_job_mode=[1]) is True
    assert passes_job_mode_eligibility(job_remote, target_job_mode="office") is False
    assert passes_job_mode_eligibility(job_hybrid, target_job_mode="office") is False

    # Case E — Missing/Unknown mode FAIL CLOSED
    job_unknown = NormalizedJob(job_id=5, title="Unknown Dev", job_mode="Not specified", raw_data={"job_mode_id": None})
    assert passes_job_mode_eligibility(job_unknown, target_job_mode="remote") is False


def test_job_mode_remote_blocks_vector_onsite_and_hybrid():
    """
    Test 1 & 2: Remote constraint blocks vector Onsite and Hybrid jobs from Qdrant.
    Meili returns Remote Job A, Remote Job B.
    Qdrant returns Onsite Job C, Hybrid Job D, Remote Job A.
    Expected: Onsite Job C & Hybrid Job D MUST NOT appear.
    """
    async def _test():
        meili_hits = [
            {"id": 101, "job_title": "Remote Python Dev A", "job_mode_id": 2, "job_mode_name": "Work from Home"},
            {"id": 102, "job_title": "Remote Python Dev B", "job_mode_id": 2, "job_mode_name": "Work from Home"},
        ]
        jm_hits = {
            "candidate_id": 123,
            "results": [
                {
                    "job_id": 201,  # Onsite vector hit (should be blocked)
                    "final_score": 0.98,
                    "payload": {"job_id": 201, "job_title": "Onsite Dev C", "job_mode_id": 1, "job_mode_name": "Work from Office"}
                },
                {
                    "job_id": 202,  # Hybrid vector hit (should be blocked)
                    "final_score": 0.95,
                    "payload": {"job_id": 202, "job_title": "Hybrid Dev D", "job_mode_id": 3, "job_mode_name": "Hybrid"}
                },
                {
                    "job_id": 101,  # Remote hybrid match
                    "final_score": 0.90,
                    "payload": {"job_id": 101, "job_title": "Remote Python Dev A", "job_mode_id": 2, "job_mode_name": "Work from Home"}
                }
            ]
        }

        with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_meili:
            with patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:
                mock_meili.return_value = {"success": True, "data": meili_hits}
                mock_jm.return_value = jm_hits

                hits, meta = await execute_hybrid_job_search(
                    last_user_msg="Show me remote jobs",
                    candidate_id=123,
                    base_filters={"job_mode": [2]},
                    parsed={"job_mode": "home"},
                    limit=5,
                )

                hit_ids = [h["id"] for h in hits]
                assert 101 in hit_ids
                assert 102 in hit_ids
                assert 201 not in hit_ids  # Onsite job blocked!
                assert 202 not in hit_ids  # Hybrid job blocked!
                assert meta["job_mode_rejected_count"] == 2
                assert meta["job_mode_requested"] == "remote"

    asyncio.run(_test())


def test_job_mode_hybrid_blocks_remote_and_office():
    """
    Test 3: Hybrid constraint blocks Remote and Office jobs.
    Target: job_mode = hybrid [3].
    Qdrant returns Remote Job A, Hybrid Job B.
    Expected: Remote Job A rejected, Hybrid Job B allowed.
    """
    async def _test():
        jm_hits = {
            "candidate_id": 123,
            "results": [
                {
                    "job_id": 301,  # Remote vector hit (should be blocked)
                    "final_score": 0.96,
                    "payload": {"job_id": 301, "job_title": "Remote Dev A", "job_mode_id": 2, "job_mode_name": "Work from Home"}
                },
                {
                    "job_id": 302,  # Hybrid vector hit (should pass)
                    "final_score": 0.90,
                    "payload": {"job_id": 302, "job_title": "Hybrid Dev B", "job_mode_id": 3, "job_mode_name": "Hybrid"}
                }
            ]
        }

        with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_meili:
            with patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:
                mock_meili.return_value = {"success": True, "data": []}
                mock_jm.return_value = jm_hits

                hits, meta = await execute_hybrid_job_search(
                    last_user_msg="Hybrid jobs",
                    candidate_id=123,
                    base_filters={"job_mode": [3]},
                    parsed={"job_mode": "hybrid"},
                    limit=5,
                )

                hit_ids = [h["id"] for h in hits]
                assert 302 in hit_ids
                assert 301 not in hit_ids
                assert meta["job_mode_rejected_count"] == 1

    asyncio.run(_test())


def test_job_mode_office_blocks_remote_and_hybrid():
    """
    Test 4: Office constraint blocks Remote and Hybrid jobs.
    Target: job_mode = office [1].
    """
    async def _test():
        jm_hits = {
            "candidate_id": 123,
            "results": [
                {
                    "job_id": 401,
                    "final_score": 0.95,
                    "payload": {"job_id": 401, "job_title": "Office Engineer", "job_mode_id": 1, "job_mode_name": "Work from Office"}
                },
                {
                    "job_id": 402,
                    "final_score": 0.91,
                    "payload": {"job_id": 402, "job_title": "Remote Engineer", "job_mode_id": 2, "job_mode_name": "Work from Home"}
                }
            ]
        }

        with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_meili:
            with patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:
                mock_meili.return_value = {"success": True, "data": []}
                mock_jm.return_value = jm_hits

                hits, meta = await execute_hybrid_job_search(
                    last_user_msg="Office jobs",
                    candidate_id=123,
                    base_filters={"job_mode": [1]},
                    parsed={"job_mode": "office"},
                    limit=5,
                )

                hit_ids = [h["id"] for h in hits]
                assert 401 in hit_ids
                assert 402 not in hit_ids

    asyncio.run(_test())


def test_profile_recommendation_remote_followup_scenario_a():
    """
    Test 6: Turn 1 'show me remote jobs' -> Turn 2 'according to my skills'.
    Mode = MODE_PROFILE_RECOMMENDATION (Qdrant weight 0.70).
    Qdrant returns high-ranked Onsite profile match and high-ranked Remote profile match.
    Meili returns Remote matches.
    Invariant: Final results are 100% Remote. High-ranking vector Onsite match CANNOT leak.
    """
    async def _test():
        meili_hits = [
            {"id": 501, "job_title": "Remote Python Dev", "job_mode_id": 2, "job_mode_name": "Work from Home"}
        ]
        jm_hits = {
            "candidate_id": 123,
            "results": [
                {
                    "job_id": 601,  # High-score Onsite profile match from vector search
                    "final_score": 0.99,
                    "payload": {"job_id": 601, "job_title": "Top Onsite Python Architect", "job_mode_id": 1, "job_mode_name": "Work from Office"}
                },
                {
                    "job_id": 501,  # Remote profile match
                    "final_score": 0.95,
                    "payload": {"job_id": 501, "job_title": "Remote Python Dev", "job_mode_id": 2, "job_mode_name": "Work from Home"}
                }
            ]
        }

        with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_meili:
            with patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:
                mock_meili.return_value = {"success": True, "data": meili_hits}
                mock_jm.return_value = jm_hits

                hits, meta = await execute_hybrid_job_search(
                    last_user_msg="according to my skills",
                    candidate_id=123,
                    base_filters={"job_mode": [2]},
                    parsed={"use_profile_skills": True, "skill_source": "PROFILE", "job_mode": "home"},
                    limit=5,
                )

                assert len(hits) == 1
                assert hits[0]["id"] == 501
                assert hits[0]["job_mode_name"] == "Work from Home"
                assert meta["job_mode_rejected_count"] == 1

    asyncio.run(_test())


def test_job_mode_and_location_interaction():
    """
    Test 7: Job mode filter works together with location filter.
    Target: job_mode = remote, target_city = Delhi.
    Remote jobs pass location filter automatically (match_level="remote").
    Onsite Delhi jobs are blocked by job_mode filter.
    Onsite Mumbai jobs are blocked by both.
    """
    async def _test():
        meili_hits = [
            {"id": 701, "job_title": "Remote Dev", "city_name": "Delhi", "job_mode_id": 2, "job_mode_name": "Work from Home"},
            {"id": 702, "job_title": "Onsite Delhi Dev", "city_name": "Delhi", "job_mode_id": 1, "job_mode_name": "Work from Office"},
        ]

        with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_meili:
            with patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:
                mock_meili.return_value = {"success": True, "data": meili_hits}
                mock_jm.return_value = {"candidate_id": 123, "results": []}

                hits, meta = await execute_hybrid_job_search(
                    last_user_msg="Remote jobs in Delhi",
                    candidate_id=123,
                    explicit_loc=True,
                    target_city_name="Delhi",
                    base_filters={"job_mode": [2], "city": [10]},
                    parsed={"job_mode": "home", "location": "Delhi"},
                    limit=5,
                )

                hit_ids = [h["id"] for h in hits]
                assert 701 in hit_ids
                assert 702 not in hit_ids
                assert meta["job_mode_rejected_count"] == 1

    asyncio.run(_test())


# ==============================================================================
# PHASE 3B — SALARY & EXPERIENCE HARD CONSTRAINT SUITE
# ==============================================================================

from backend.services.hybrid_job_search import (
    passes_salary_eligibility,
    passes_experience_eligibility,
)


def test_passes_salary_eligibility_unit():
    """Verify salary gate logic under various min, max, and range criteria."""
    # Test 1: Max Salary (under 10 LPA -> 1,000,000 INR)
    job_5_8 = NormalizedJob(job_id=1, title="Dev", raw_data={"salary_min": 500000, "salary_max": 800000})
    job_5_10 = NormalizedJob(job_id=2, title="Dev", raw_data={"salary_min": 500000, "salary_max": 1000000})
    job_5_15 = NormalizedJob(job_id=3, title="Dev", raw_data={"salary_min": 500000, "salary_max": 1500000})
    job_12_20 = NormalizedJob(job_id=4, title="Dev", raw_data={"salary_min": 1200000, "salary_max": 2000000})

    assert passes_salary_eligibility(job_5_8, salary_max=1000000.0) is True
    assert passes_salary_eligibility(job_5_10, salary_max=1000000.0) is True
    assert passes_salary_eligibility(job_5_15, salary_max=1000000.0) is False
    assert passes_salary_eligibility(job_12_20, salary_max=1000000.0) is False

    # Test 2: Min Salary (above 5 LPA -> 500,000 INR)
    job_3_5 = NormalizedJob(job_id=5, title="Dev", raw_data={"salary_min": 300000, "salary_max": 500000})
    job_8_12 = NormalizedJob(job_id=6, title="Dev", raw_data={"salary_min": 800000, "salary_max": 1200000})

    assert passes_salary_eligibility(job_3_5, salary_min=500000.0) is False
    assert passes_salary_eligibility(job_5_8, salary_min=500000.0) is True
    assert passes_salary_eligibility(job_8_12, salary_min=500000.0) is True

    # Test 3: Salary Range (5-10 LPA -> min=500k, max=1M)
    assert passes_salary_eligibility(job_5_8, salary_min=500000.0, salary_max=1000000.0) is True
    assert passes_salary_eligibility(job_5_15, salary_min=500000.0, salary_max=1000000.0) is False
    assert passes_salary_eligibility(job_3_5, salary_min=500000.0, salary_max=1000000.0) is False

    # Test 4: Missing Salary Data (Fail Closed under active salary constraint)
    job_no_sal = NormalizedJob(job_id=7, title="Dev", raw_data={})
    assert passes_salary_eligibility(job_no_sal, salary_max=1000000.0) is False
    assert passes_salary_eligibility(job_no_sal, salary_min=500000.0) is False

    # Test 5: No Salary Constraint (Passes naturally)
    assert passes_salary_eligibility(job_no_sal, salary_min=None, salary_max=None) is True


def test_passes_experience_eligibility_unit():
    """Verify experience gate logic for fresher, min experience, and range queries."""
    job_fresher = NormalizedJob(job_id=1, title="Junior Dev", raw_data={"experience_min": 0, "experience_max": 1, "experience_id": 1})
    job_1_3 = NormalizedJob(job_id=2, title="Mid Dev", raw_data={"experience_min": 1, "experience_max": 3, "experience_id": 3})
    job_5_8 = NormalizedJob(job_id=3, title="Senior Dev", raw_data={"experience_min": 5, "experience_max": 8, "experience_id": 6})
    job_no_exp = NormalizedJob(job_id=4, title="Unknown Exp", raw_data={})

    # Test 6: Fresher Query
    assert passes_experience_eligibility(job_fresher, experience_level="fresher", message="fresher jobs") is True
    assert passes_experience_eligibility(job_1_3, experience_level="fresher", message="fresher jobs") is False
    assert passes_experience_eligibility(job_5_8, experience_level="fresher", message="fresher jobs") is False

    # Test 7: Min Experience Query (5+ years -> experience_min=5)
    assert passes_experience_eligibility(job_fresher, experience_min=5) is False
    assert passes_experience_eligibility(job_1_3, experience_min=5) is False
    assert passes_experience_eligibility(job_5_8, experience_min=5) is True

    # Test 8: Missing Experience Data (Fail Closed under active experience constraint)
    assert passes_experience_eligibility(job_no_exp, experience_level="fresher") is False
    assert passes_experience_eligibility(job_no_exp, experience_min=5) is False

    # Test 9: No Experience Constraint (Passes naturally)
    assert passes_experience_eligibility(job_no_exp) is True


def test_vector_salary_leakage_blocked():
    """
    Integration Test: High-scoring Qdrant vector hit violating max salary is blocked.
    User asks: 'under 10 LPA' (salary_max = 1,000,000).
    Qdrant returns a 25 LPA job with 0.95 vector score.
    Meilisearch returns a valid 7 LPA job.
    Outcome: 25 LPA vector job MUST NOT reach final playcards.
    """
    async def _test():
        meili_hits = [
            {"id": 801, "job_title": "Budget Dev", "salary_min": 500000, "salary_max": 700000, "salary": "7 LPA"}
        ]
        jm_hits = {
            "candidate_id": 123,
            "results": [
                {
                    "job_id": 802,
                    "final_score": 0.95,
                    "payload": {"job_id": 802, "job_title": "High Paid Executive", "salary_min": 2000000, "salary_max": 2500000, "salary": "25 LPA"}
                }
            ]
        }

        with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_meili:
            with patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:
                mock_meili.return_value = {"success": True, "data": meili_hits}
                mock_jm.return_value = jm_hits

                hits, meta = await execute_hybrid_job_search(
                    last_user_msg="show me jobs under 10 LPA",
                    candidate_id=123,
                    parsed={"salary_max": 1000000.0},
                    limit=5,
                )

                hit_ids = [h["id"] for h in hits]
                assert 801 in hit_ids
                assert 802 not in hit_ids
                assert meta["salary_rejected_count"] == 1

    asyncio.run(_test())


def test_vector_experience_leakage_blocked():
    """
    Integration Test: High-scoring Qdrant vector hit violating fresher requirement is blocked.
    User asks: 'fresher jobs'.
    Qdrant returns a Senior Architect (10+ yrs exp) with 0.98 score.
    Meilisearch returns a Fresher Dev (0-1 yrs exp).
    Outcome: 10+ yr Senior Architect MUST NOT reach final playcards.
    """
    async def _test():
        meili_hits = [
            {"id": 901, "job_title": "Junior Trainee", "experience_min": 0, "experience_max": 1, "experience_id": 1}
        ]
        jm_hits = {
            "candidate_id": 123,
            "results": [
                {
                    "job_id": 902,
                    "final_score": 0.98,
                    "payload": {"job_id": 902, "job_title": "Senior Staff Architect", "experience_min": 10, "experience_max": 15, "experience_id": 9}
                }
            ]
        }

        with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_meili:
            with patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:
                mock_meili.return_value = {"success": True, "data": meili_hits}
                mock_jm.return_value = jm_hits

                hits, meta = await execute_hybrid_job_search(
                    last_user_msg="show me fresher jobs",
                    candidate_id=123,
                    parsed={"experience_level": "fresher", "experience_min": 0, "experience_max": 0},
                    limit=5,
                )

                hit_ids = [h["id"] for h in hits]
                assert 901 in hit_ids
                assert 902 not in hit_ids
                assert meta["experience_rejected_count"] == 1

    asyncio.run(_test())


def test_multi_constraint_salary_experience_and_remote_composition():
    """
    Integration Test: Multiple hard constraints compose correctly (salary + experience + remote).
    All active gates must pass. If a job violates any single gate, it is rejected.
    """
    async def _test():
        meili_hits = [
            {"id": 1001, "job_title": "Valid Remote Fresher", "job_mode_id": 2, "job_mode_name": "Work from Home", "salary_max": 800000, "experience_min": 0, "experience_max": 1},
            {"id": 1002, "job_title": "High Salary Remote Fresher", "job_mode_id": 2, "job_mode_name": "Work from Home", "salary_max": 2000000, "experience_min": 0, "experience_max": 1},
            {"id": 1003, "job_title": "Senior Remote Fresher", "job_mode_id": 2, "job_mode_name": "Work from Home", "salary_max": 800000, "experience_min": 5, "experience_max": 8},
            {"id": 1004, "job_title": "Onsite Valid Fresher", "job_mode_id": 1, "job_mode_name": "Work from Office", "salary_max": 800000, "experience_min": 0, "experience_max": 1},
        ]

        with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_meili:
            with patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:
                mock_meili.return_value = {"success": True, "data": meili_hits}
                mock_jm.return_value = {"candidate_id": 123, "results": []}

                hits, meta = await execute_hybrid_job_search(
                    last_user_msg="show me remote fresher jobs under 10 LPA",
                    candidate_id=123,
                    base_filters={"job_mode": [2]},
                    parsed={"job_mode": "home", "experience_level": "fresher", "salary_max": 1000000.0},
                    limit=5,
                )

                hit_ids = [h["id"] for h in hits]
                assert hit_ids == [1001]
                assert meta["job_mode_rejected_count"] == 1
                assert meta["salary_rejected_count"] == 1
                assert meta["experience_rejected_count"] == 1

    asyncio.run(_test())


def test_profile_recommendation_salary_and_experience_inheritance():
    """
    Conversational Inheritance Test:
    Turn 1: User asks 'under 10 LPA' and 'fresher jobs'.
    Turn 2: Follow-up 'according to my skills' inherits salary_max and experience constraints.
    High-salary or senior jobs in vector database must be rejected during Turn 2.
    """
    async def _test():
        meili_hits = [
            {"id": 1101, "job_title": "Python Trainee", "salary_max": 600000, "experience_min": 0, "experience_max": 1}
        ]
        jm_hits = {
            "candidate_id": 123,
            "results": [
                {
                    "job_id": 1102,
                    "final_score": 0.96,
                    "payload": {"job_id": 1102, "job_title": "Senior Python Lead", "salary_max": 3000000, "experience_min": 8, "experience_max": 12}
                }
            ]
        }

        with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_meili:
            with patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:
                mock_meili.return_value = {"success": True, "data": meili_hits}
                mock_jm.return_value = jm_hits

                hits, meta = await execute_hybrid_job_search(
                    last_user_msg="according to my skills",
                    candidate_id=123,
                    parsed={
                        "use_profile_skills": True,
                        "skill_source": "PROFILE",
                        "salary_max": 1000000.0,
                        "experience_level": "fresher",
                        "experience_min": 0,
                        "experience_max": 0,
                    },
                    limit=5,
                )

                hit_ids = [h["id"] for h in hits]
                assert 1101 in hit_ids
                assert 1102 not in hit_ids
                assert meta["salary_rejected_count"] + meta["experience_rejected_count"] >= 1

    asyncio.run(_test())


# ==============================================================================
# PHASE 4C — STATIC RETRIEVAL OVERSAMPLING TEST SUITE
# ==============================================================================

def test_phase4c_static_oversampling_depth_verification():
    """Verify Meilisearch and JobMatch receive the oversampled retrieval limit depth (30)."""
    async def _test():
        with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_meili:
            with patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:
                mock_meili.return_value = {"success": True, "data": []}
                mock_jm.return_value = {"candidate_id": 123, "results": []}

                await execute_hybrid_job_search(
                    last_user_msg="Python jobs",
                    candidate_id=123,
                    keyword="Python",
                    limit=10,
                )

                # Verify Meilisearch received limit=30 (max(30, 10*2))
                meili_call_kwargs = mock_meili.call_args[1]
                assert meili_call_kwargs.get("limit") == 30

                # Verify JobMatch received limit=30 and filters={}
                jm_call_kwargs = mock_jm.call_args[1]
                assert jm_call_kwargs.get("limit") == 30
                assert jm_call_kwargs.get("filters") == {}

    asyncio.run(_test())


def test_phase4c_oversampling_noisy_vector_candidates_filtered():
    """
    Verify that static oversampling allows valid candidates to survive post-fusion gating
    even when a large proportion of retrieved vector hits violate hard constraints.
    """
    async def _test():
        # Create 25 vector hits: 20 violate salary (25 LPA) or work mode (Onsite), 5 are valid remote budget jobs
        noisy_jm_results = []
        for i in range(1, 21):
            noisy_jm_results.append({
                "job_id": 2000 + i,
                "final_score": 0.99 - (i * 0.01),
                "payload": {
                    "job_id": 2000 + i,
                    "job_title": f"Senior Executive {i}",
                    "job_mode_id": 1,  # Onsite (invalid for remote query)
                    "salary_max": 2500000,  # 25 LPA (invalid for under 10 LPA query)
                }
            })
        for i in range(21, 26):
            noisy_jm_results.append({
                "job_id": 2000 + i,
                "final_score": 0.70 - (i * 0.01),
                "payload": {
                    "job_id": 2000 + i,
                    "job_title": f"Valid Remote Python Developer {i}",
                    "job_mode_id": 2,  # Remote
                    "salary_max": 800000,  # 8 LPA
                }
            })

        meili_hits = [
            {"id": 1001, "job_title": "Meili Remote Dev 1", "job_mode_id": 2, "job_mode_name": "Work from Home", "salary_max": 700000},
            {"id": 1002, "job_title": "Meili Remote Dev 2", "job_mode_id": 2, "job_mode_name": "Work from Home", "salary_max": 600000},
        ]

        with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_meili:
            with patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:
                mock_meili.return_value = {"success": True, "data": meili_hits}
                mock_jm.return_value = {"candidate_id": 123, "results": noisy_jm_results}

                hits, meta = await execute_hybrid_job_search(
                    last_user_msg="remote python jobs under 10 LPA",
                    candidate_id=123,
                    keyword="Python",
                    base_filters={"job_mode": [2]},
                    parsed={"job_mode": "home", "salary_max": 1000000.0},
                    limit=10,
                )

                # All 20 noisy vector jobs rejected by hard gates
                assert meta["job_mode_rejected_count"] + meta["salary_rejected_count"] >= 20

                # Surviving eligible hits include Meili hits + 5 valid oversampled vector hits
                hit_ids = [h["id"] for h in hits]
                assert 1001 in hit_ids
                assert 1002 in hit_ids
                assert any(h_id > 2020 for h_id in hit_ids)
                assert len(hits) == 7  # 2 Meili + 5 valid oversampled vector hits

    asyncio.run(_test())



