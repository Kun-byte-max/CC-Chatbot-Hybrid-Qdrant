"""
test_phase5_e2e_urgent_profile_integration.py
Phase 5 End-to-End Integration Hardening Test Suite.

Verifies the complete integration journey from parser -> session merge -> employee orchestration -> candidate profile fetching -> hybrid retrieval -> post-fusion urgent gate -> JobCard payload -> frontend metadata.
"""

import pytest
from unittest.mock import AsyncMock, patch
from backend.api.employee import handle_employee_context
from backend.services.search_parser import parse_search_query
from backend.recommendation.models import CandidateProfile


@pytest.mark.anyio
async def test_scenario_a_e2e_urgent_to_profile_refinement():
    """Scenario A: Turn 1 'show me jobs with urgent hiring' -> Turn 2 'for me'."""
    sess_id = "sess_e2e_scenario_a"

    # Mock candidate profile with skills
    mock_candidate = CandidateProfile(
        id=41031,
        name="Kartik Sharma",
        skills=["Python", "FastAPI", "SQL"],
        current_title="Software Engineer",
    )

    # Mock Meilisearch hits
    mock_meili = {
        "success": True,
        "data": [
            {
                "id": 101,
                "job_title": "Python FastAPI Developer",
                "company_name": "Tech Corp",
                "urgent": 1,
                "job_mode_name": "Office",
            },
            {
                "id": 102,
                "job_title": "Senior Python Architect",
                "company_name": "Cloud Corp",
                "urgent": 1,
                "job_mode_name": "Remote",
            },
            {
                "id": 103,
                "job_title": "Java Developer",
                "company_name": "Legacy Corp",
                "urgent": 0,  # NON-URGENT
                "job_mode_name": "Office",
            },
        ],
    }

    # Mock JobMatch vector hits
    mock_jobmatch = {
        "success": True,
        "results": [
            {
                "job_id": 103,  # Non-urgent high fit (0.98)
                "final_score": 0.98,
                "payload": {"job_id": 103, "job_title": "Java Developer", "urgent": 0},
            },
            {
                "job_id": 101,  # Urgent high fit (0.92)
                "final_score": 0.92,
                "payload": {"job_id": 101, "job_title": "Python FastAPI Developer", "urgent": 1},
            },
            {
                "job_id": 102,  # Urgent medium fit (0.85)
                "final_score": 0.85,
                "payload": {"job_id": 102, "job_title": "Senior Python Architect", "urgent": 1},
            },
        ],
    }

    with patch("backend.services.search_client.search_jobs", new=AsyncMock(return_value=mock_meili)), \
         patch("backend.api.employee.search_jobs", new=AsyncMock(return_value=mock_meili)), \
         patch("backend.services.hybrid_job_search.search_jobs", new=AsyncMock(return_value=mock_meili)), \
         patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new=AsyncMock(return_value=mock_jobmatch)), \
         patch("backend.services.hybrid_job_search.JOB_MATCH_ENABLED", True), \
         patch("backend.api.employee.to_candidate_profile_from_api", return_value=mock_candidate):

        # Turn 1: Direct Urgent Search
        res1 = await handle_employee_context(
            last_user_msg="show me jobs with urgent hiring",
            session_id=sess_id,
            candidate_id="41031",
        )
        assert isinstance(res1, dict)
        assert "db_context" in res1

        # Turn 2: Short profile follow-up "for me"
        res2 = await handle_employee_context(
            last_user_msg="for me",
            session_id=sess_id,
            candidate_id="41031",
        )
        assert isinstance(res2, dict)

        pag_meta = res2.get("pagination") or {}
        cards = pag_meta.get("ranked_jobs") or []

        # Zero leakage check: Job 103 (urgent=0) MUST NOT be present in cards
        card_ids = [c.get("job_id") for c in cards if isinstance(c, dict)]
        assert 103 not in card_ids, "Non-urgent job 103 MUST NOT leak into final payload!"

        # Urgency metadata check on returned cards
        for card in cards:
            if isinstance(card, dict):
                assert card.get("urgent") in (1, True, "1", "true"), f"Job card missing confirmed urgency: {card}"


@pytest.mark.anyio
async def test_scenario_c_profile_to_urgent_refinement():
    """Scenario C: Turn 1 'show me jobs according to my skills' -> Turn 2 'only urgent ones'."""
    sess_id = "sess_e2e_scenario_c"

    mock_candidate = CandidateProfile(
        id=41031,
        skills=["Python", "FastAPI"],
    )

    mock_meili = {
        "success": True,
        "data": [
            {"id": 201, "job_title": "Python Dev", "urgent": 1},
            {"id": 202, "job_title": "FastAPI Dev", "urgent": 0},
        ],
    }

    with patch("backend.services.search_client.search_jobs", new=AsyncMock(return_value=mock_meili)), \
         patch("backend.api.employee.search_jobs", new=AsyncMock(return_value=mock_meili)), \
         patch("backend.services.hybrid_job_search.search_jobs", new=AsyncMock(return_value=mock_meili)), \
         patch("backend.api.employee.to_candidate_profile_from_api", return_value=mock_candidate):

        # Turn 1
        res1 = await handle_employee_context(
            last_user_msg="show me jobs according to my skills",
            session_id=sess_id,
            candidate_id="41031",
        )
        assert isinstance(res1, dict)

        # Turn 2: "only urgent ones"
        res2 = await handle_employee_context(
            last_user_msg="only urgent ones",
            session_id=sess_id,
            candidate_id="41031",
        )
        assert isinstance(res2, dict)
        pag_meta = res2.get("pagination") or {}
        cards = pag_meta.get("ranked_jobs") or []

        # Job 202 (urgent=0) must be filtered out
        card_ids = [c.get("job_id") for c in cards if isinstance(c, dict)]
        assert 202 not in card_ids


@pytest.mark.anyio
async def test_scenario_e_explicit_new_search_resets_urgent_context():
    """Scenario E: Urgent search followed by explicit new search resets urgent criteria."""
    sess_id = "sess_e2e_scenario_e"

    with patch("backend.services.search_client.search_jobs", new=AsyncMock(return_value={"success": True, "data": []})), \
         patch("backend.api.employee.search_jobs", new=AsyncMock(return_value={"success": True, "data": []})):
        # Turn 1: Urgent Python Search
        await handle_employee_context("show me urgent Python jobs", session_id=sess_id)

        # Turn 2: Explicit New Search "show me accountant jobs"
        parsed3 = await parse_search_query("show me accountant jobs", role="employee")

    assert parsed3.get("search_target") == "JOBS"
    assert parsed3.get("urgent") is None, "Explicit new search MUST reset urgent criteria to None"


@pytest.mark.anyio
async def test_hybrid_engine_source_failure_resilience():
    """Verify that if JobMatch fails with 503/Timeout, Meilisearch branch continues and preserves urgent gate."""
    mock_meili = {
        "success": True,
        "data": [
            {"id": 301, "job_title": "Urgent Accountant", "urgent": 1},
            {"id": 302, "job_title": "Normal Accountant", "urgent": 0},
        ],
    }

    with patch("backend.services.search_client.search_jobs", new=AsyncMock(return_value=mock_meili)), \
         patch("backend.api.employee.search_jobs", new=AsyncMock(return_value=mock_meili)), \
         patch("backend.services.hybrid_job_search.search_jobs", new=AsyncMock(return_value=mock_meili)), \
         patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new=AsyncMock(side_effect=Exception("503 Service Unavailable"))), \
         patch("backend.services.hybrid_job_search.JOB_MATCH_ENABLED", True):

        from backend.services.hybrid_job_search import execute_hybrid_job_search
        hits, meta = await execute_hybrid_job_search(
            last_user_msg="show me urgent accountant jobs",
            candidate_id=123,
            base_filters={"urgent": 1},
            parsed={"urgent": True},
        )

    hit_ids = [h["id"] for h in hits]
    assert 301 in hit_ids
    assert 302 not in hit_ids, "Surviving Meilisearch branch MUST STILL enforce urgent gate!"
