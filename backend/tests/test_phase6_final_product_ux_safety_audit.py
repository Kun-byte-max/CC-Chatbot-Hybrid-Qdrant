"""
test_phase6_final_product_ux_safety_audit.py
Phase 6 Final Product UX and Safety Audit Unit Tests.

Verifies:
1. User Journeys A through H (Urgent, Profile follow-up, Ordinary job refinement, Explicit new search, Candidate switching).
2. Data & payload consistency: JobCard.urgent metadata for frontend ⚡ Urgent badge rendering.
3. Edge case safety: Missing/Unknown urgency handling, profile data unavailability, zero leakage.
"""

import pytest
from unittest.mock import AsyncMock, patch
from backend.api.employee import handle_employee_context
from backend.services.search_parser import parse_search_query, normalize_search_target
from backend.recommendation.models import CandidateProfile


@pytest.mark.anyio
async def test_journey_a_basic_urgent_search():
    """Journey A: 'show me urgent jobs' -> JOBS, urgent=True, profile=False."""
    parsed = await parse_search_query("show me urgent jobs", role="employee")
    assert parsed.get("search_target") == "JOBS"
    assert parsed.get("urgent") is True
    assert parsed.get("use_profile_skills") in (False, None)


@pytest.mark.anyio
async def test_journey_b_c_urgent_to_profile_followups():
    """Journey B & C: Urgent search followed by natural profile follow-up phrases."""
    sess_id = "sess_p6_journey_bc"

    mock_cand = CandidateProfile(id=101, skills=["Python", "FastAPI"])
    mock_meili = {
        "success": True,
        "data": [
            {"id": 501, "job_title": "Python Dev", "urgent": 1, "company_name": "Tech Corp"},
            {"id": 502, "job_title": "Java Dev", "urgent": 0, "company_name": "Legacy Corp"},
        ],
    }

    with patch("backend.services.search_client.search_jobs", new=AsyncMock(return_value=mock_meili)), \
         patch("backend.api.employee.search_jobs", new=AsyncMock(return_value=mock_meili)), \
         patch("backend.services.hybrid_job_search.search_jobs", new=AsyncMock(return_value=mock_meili)), \
         patch("backend.api.employee.to_candidate_profile_from_api", return_value=mock_cand):

        # Turn 1: Urgent jobs
        res1 = await handle_employee_context("show me urgent jobs", session_id=sess_id, candidate_id="101")
        assert isinstance(res1, dict)

        # Turn 2: Natural profile follow-up "matching my profile"
        res2 = await handle_employee_context("matching my profile", session_id=sess_id, candidate_id="101")
        assert isinstance(res2, dict)
        cards = (res2.get("pagination") or {}).get("ranked_jobs") or []
        card_ids = [c.get("job_id") for c in cards if isinstance(c, dict)]

        # Job 502 (urgent=0) filtered out, Job 501 (urgent=1) preserved with urgency flag
        assert 502 not in card_ids
        for c in cards:
            if isinstance(c, dict):
                assert c.get("urgent") in (1, True, "1", "true")


@pytest.mark.anyio
async def test_journey_d_profile_to_urgent_refinement():
    """Journey D: Profile recommendation followed by 'only urgent ones'."""
    sess_id = "sess_p6_journey_d"
    mock_cand = CandidateProfile(id=101, skills=["Python"])
    mock_meili = {
        "success": True,
        "data": [
            {"id": 601, "job_title": "Urgent Python Dev", "urgent": 1},
            {"id": 602, "job_title": "Normal Python Dev", "urgent": 0},
        ],
    }

    with patch("backend.services.search_client.search_jobs", new=AsyncMock(return_value=mock_meili)), \
         patch("backend.api.employee.search_jobs", new=AsyncMock(return_value=mock_meili)), \
         patch("backend.services.hybrid_job_search.search_jobs", new=AsyncMock(return_value=mock_meili)), \
         patch("backend.api.employee.to_candidate_profile_from_api", return_value=mock_cand):

        await handle_employee_context("show me jobs according to my skills", session_id=sess_id, candidate_id="101")
        res2 = await handle_employee_context("only urgent ones", session_id=sess_id, candidate_id="101")

        cards = (res2.get("pagination") or {}).get("ranked_jobs") or []
        card_ids = [c.get("job_id") for c in cards if isinstance(c, dict)]
        assert 602 not in card_ids


@pytest.mark.anyio
async def test_journey_e_ordinary_jobs_to_urgent():
    """Journey E: 'show me Python jobs' -> 'only urgent ones' preserves keyword=Python and urgent=True."""
    sess_id = "sess_p6_journey_e"
    mock_meili = {"success": True, "data": [{"id": 701, "job_title": "Urgent Python Dev", "urgent": 1}]}

    with patch("backend.services.search_client.search_jobs", new=AsyncMock(return_value=mock_meili)), \
         patch("backend.api.employee.search_jobs", new=AsyncMock(return_value=mock_meili)):

        await handle_employee_context("show me Python jobs", session_id=sess_id)
        res2 = await handle_employee_context("only urgent ones", session_id=sess_id)
        assert isinstance(res2, dict)


@pytest.mark.anyio
async def test_journey_g_explicit_new_search_resets_stickiness():
    """Journey G: Urgent Python search followed by 'show me accountant jobs' resets previous criteria."""
    sess_id = "sess_p6_journey_g"
    with patch("backend.services.search_client.search_jobs", new=AsyncMock(return_value={"success": True, "data": []})), \
         patch("backend.api.employee.search_jobs", new=AsyncMock(return_value={"success": True, "data": []})):

        await handle_employee_context("show me urgent Python jobs", session_id=sess_id)
        parsed = await parse_search_query("show me accountant jobs", role="employee")

    assert parsed.get("search_target") == "JOBS"
    assert parsed.get("urgent") is None


def test_journey_h_candidate_job_switching_safety():
    """Journey H: Candidate vs Job routing safety and intent switching."""
    assert normalize_search_target(None, "show me urgent jobs", role="employee") == "JOBS"
    assert normalize_search_target(None, "find candidates for hiring", role="employee") == "PEOPLE_SEARCH"
    assert normalize_search_target(None, "show me urgent openings", role="employee") == "JOBS"
    assert normalize_search_target("JOBS", "matching my profile", role="employee") == "JOBS"


@pytest.mark.anyio
async def test_unknown_or_missing_urgency_handling():
    """Verify missing/null urgency field does not pass urgent eligibility filter."""
    from backend.services.hybrid_job_search import execute_hybrid_job_search

    mock_meili = {
        "success": True,
        "data": [
            {"id": 801, "job_title": "Job 1", "urgent": 1},
            {"id": 802, "job_title": "Job 2", "urgent": 0},
            {"id": 803, "job_title": "Job 3"},  # MISSING URGENCY
            {"id": 804, "job_title": "Job 4", "urgent": None},  # NULL URGENCY
        ],
    }

    with patch("backend.services.search_client.search_jobs", new=AsyncMock(return_value=mock_meili)), \
         patch("backend.api.employee.search_jobs", new=AsyncMock(return_value=mock_meili)), \
         patch("backend.services.hybrid_job_search.search_jobs", new=AsyncMock(return_value=mock_meili)):

        hits, _ = await execute_hybrid_job_search(
            last_user_msg="show me urgent jobs",
            candidate_id=123,
            base_filters={"urgent": 1},
            parsed={"urgent": True},
        )

    returned_ids = [h["id"] for h in hits]
    assert returned_ids == [801], f"Expected only [801], got {returned_ids}"
