"""
test_urgent_hiring_routing.py — Comprehensive Unit & Integration Tests for Urgent Hiring Job Search
"""

import pytest
import asyncio
from backend.services.search_parser import parse_search_query, normalize_search_target
from backend.schemas.search_criteria import SearchCriteria
from backend.schemas.search_query import SearchQuery, SearchTarget
from backend.utils.session_state import (
    set_candidate_search_criteria,
    get_candidate_search_criteria,
    merge_employee_search_query,
)


def test_group_a_direct_urgent_job_queries():
    """Test Group A: Direct urgent job queries extract search_target=JOBS and urgent=True."""
    urgent_queries = [
        "show me urgent jobs",
        "show me urgent hiring jobs",
        "show me jobs with urgent hiring",
        "find urgent hiring jobs",
        "find jobs hiring urgently",
        "show urgent openings",
        "show urgent positions",
    ]
    async def _run():
        for q in urgent_queries:
            parsed = await parse_search_query(q, role="employee")
            assert parsed.get("search_target") == "JOBS", f"Failed search_target for query: {q}"
            assert parsed.get("urgent") is True, f"Failed urgent=True for query: {q}"

    asyncio.run(_run())


def test_group_b_normal_job_queries_remain_normal():
    """Test Group B: Normal job queries extract search_target=JOBS and urgent!=True."""
    normal_queries = [
        "show me jobs",
        "show me Python jobs",
        "show me Python jobs in Delhi",
        "find backend developer jobs",
    ]
    async def _run():
        for q in normal_queries:
            parsed = await parse_search_query(q, role="employee")
            assert parsed.get("search_target") == "JOBS", f"Failed search_target for query: {q}"
            assert parsed.get("urgent") is not True, f"Unexpected urgent=True for normal query: {q}"

    asyncio.run(_run())


def test_group_c_candidate_queries_remain_candidate_searches():
    """Test Group C: Candidate queries with hiring terms extract search_target=PEOPLE_SEARCH and urgent!=True."""
    candidate_queries = [
        "find candidates for hiring",
        "find candidates to hire",
        "find professionals to hire",
        "find developers to hire",
        "show me candidate profiles",
    ]
    async def _run():
        for q in candidate_queries:
            parsed = await parse_search_query(q, role="employee")
            assert parsed.get("search_target") == "PEOPLE_SEARCH", f"Failed search_target for candidate query: {q}"
            assert parsed.get("urgent") is not True, f"Unexpected urgent=True for candidate query: {q}"

    asyncio.run(_run())


def test_group_d_candidate_to_urgent_job_switch():
    """Test Group D: Active candidate context switches to JOBS search when receiving urgent job query."""
    session_id = "test_session_cand_to_urgent_job"
    cand_criteria = SearchCriteria(skills=["Python"], search_target=SearchTarget.PEOPLE_SEARCH)
    set_candidate_search_criteria(session_id, cand_criteria, role="employee")

    # Verify candidate context is active
    active_cand = get_candidate_search_criteria(session_id, role="employee")
    assert active_cand is not None

    async def _run():
        # Process urgent job query
        q = "show me jobs with urgent hiring"
        parsed = await parse_search_query(q, role="employee", session_id=session_id)
        assert parsed.get("search_target") == "JOBS"
        assert parsed.get("urgent") is True

        # Test handle_employee_context routing
        from backend.api.employee import handle_employee_context
        resp = await handle_employee_context(
            last_user_msg=q,
            session_id=session_id,
            parsed_query=parsed,
        )
        assert resp.get("result_type") == "jobs"

    asyncio.run(_run())


def test_group_e_job_to_urgent_refinement():
    """Test Group E: Job search turn followed by 'only urgent ones' preserves skills and adds urgent=True."""
    sq1 = SearchQuery(title="python", skills=["Python"])
    sq2 = SearchQuery(urgent=True)
    merged = merge_employee_search_query(sq1, sq2, prompt_category="ADDITIVE")

    assert merged.title == "python"
    assert "Python" in (merged.skills or [])
    assert merged.urgent is True


def test_group_f_candidate_refinement_remains_candidate_search():
    """Test Group F: Candidate query refinement does not accidentally trigger job search."""
    session_id = "test_session_cand_refine"
    cand_criteria = SearchCriteria(skills=["Python"], search_target=SearchTarget.PEOPLE_SEARCH)
    set_candidate_search_criteria(session_id, cand_criteria, role="employee")

    async def _run():
        q = "find candidates to hire"
        parsed = await parse_search_query(q, role="employee", session_id=session_id)
        assert parsed.get("search_target") == "PEOPLE_SEARCH"

    asyncio.run(_run())


def test_group_g_explicit_job_noun_precedence():
    """Test Group G: Co-occurrence of hiring with job nouns gives precedence to JOBS search."""
    queries = [
        "show me jobs with urgent hiring",
        "find urgent jobs",
        "find jobs hiring urgently",
        "show urgent openings",
        "show urgent positions",
    ]
    for q in queries:
        target = normalize_search_target(None, q, role="employee")
        assert target == "JOBS", f"normalize_search_target failed for query: {q}"
