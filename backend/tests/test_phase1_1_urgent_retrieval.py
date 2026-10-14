"""
test_phase1_1_urgent_retrieval.py — Phase 1.1 Urgent Retrieval Enforcement Verification Tests
"""

import pytest
import asyncio
import sys
from unittest.mock import AsyncMock, patch

from backend.services.hybrid_job_search import execute_hybrid_job_search
from backend.services.search_parser import parse_search_query
from backend.schemas.search_criteria import SearchCriteria
from backend.schemas.search_query import SearchQuery, SearchTarget
from backend.utils.session_state import (
    set_candidate_search_criteria,
    get_candidate_search_criteria,
    merge_employee_search_query,
)


def _patch_search_jobs(mock_fn):
    """Helper context manager to patch search_jobs across all imported module namespaces."""
    patches = [
        patch("backend.services.search_client.search_jobs", new=mock_fn),
        patch("backend.services.hybrid_job_search.search_jobs", new=mock_fn),
    ]
    if "backend.api.employee" in sys.modules:
        patches.append(patch("backend.api.employee.search_jobs", new=mock_fn))
    return patches


def test_core_mixed_urgent_non_urgent_filtering():
    """Verify that execute_hybrid_job_search strictly excludes urgent=0 jobs when urgent=1 filter is active."""
    mock_meili_hits = [
        {"id": 101, "job_title": "Python Developer", "urgent": 1, "company_name": "CollarCheck"},
        {"id": 102, "job_title": "Java Developer", "urgent": 0, "company_name": "CollarCheck"},
        {"id": 103, "job_title": "React Developer", "urgent": 1, "company_name": "CollarCheck"},
        {"id": 104, "job_title": "DevOps Engineer", "urgent": 0, "company_name": "CollarCheck"},
    ]

    async def _run():
        mock_search = AsyncMock(return_value={"success": True, "data": mock_meili_hits, "total": 4})
        patches = _patch_search_jobs(mock_search)
        for p in patches:
            p.start()
        try:
            hits, telemetry = await execute_hybrid_job_search(
                last_user_msg="show me urgent jobs",
                keyword="developer",
                base_filters={"urgent": 1},
                parsed={"urgent": True},
            )

            returned_ids = [h["id"] for h in hits]
            assert 101 in returned_ids, "Urgent Job 101 missing"
            assert 103 in returned_ids, "Urgent Job 103 missing"
            assert 102 not in returned_ids, "Non-urgent Job 102 leaked!"
            assert 104 not in returned_ids, "Non-urgent Job 104 leaked!"
            assert all(h.get("urgent") == 1 for h in hits)
        finally:
            for p in patches:
                p.stop()

    asyncio.run(_run())


def test_hybrid_search_qdrant_leakage_prevention():
    """Verify that high-scoring non-urgent jobs from Qdrant/JobMatch cannot leak through post-fusion gate."""
    meili_hits = [
        {"id": 201, "job_title": "Python Engineer", "urgent": 1, "company_name": "Partner A"},
        {"id": 202, "job_title": "Python Developer", "urgent": 0, "company_name": "Partner B"},
    ]
    jm_hits = [
        {
            "job_id": 203,
            "final_score": 0.98,  # High vector match score
            "payload": {"job_id": 203, "title": "Senior Python Engineer", "urgent": 0, "company_name": "Partner C"},
        },
        {
            "job_id": 204,
            "final_score": 0.85,
            "payload": {"job_id": 204, "title": "Lead Python Architect", "urgent": 1, "company_name": "Partner D"},
        },
    ]

    async def _run():
        mock_meili = AsyncMock(return_value={"success": True, "data": meili_hits})
        patches = _patch_search_jobs(mock_meili)
        for p in patches:
            p.start()

        with patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm, \
             patch("backend.services.hybrid_job_search.JOB_MATCH_ENABLED", True):

            mock_jm.return_value = {"results": jm_hits}
            try:
                hits, telemetry = await execute_hybrid_job_search(
                    last_user_msg="show me jobs with urgent hiring",
                    candidate_id=42,
                    keyword="python",
                    base_filters={"urgent": 1},
                    parsed={"urgent": True},
                )

                returned_ids = [h["id"] for h in hits]
                assert 201 in returned_ids, "Urgent Meili hit 201 should be allowed"
                assert 204 in returned_ids, "Urgent Qdrant hit 204 should be allowed"
                assert 202 not in returned_ids, "Non-urgent Meili hit 202 must be rejected"
                assert 203 not in returned_ids, "High-scoring non-urgent Qdrant hit 203 MUST BE REJECTED (leakage test)"
            finally:
                for p in patches:
                    p.stop()

    asyncio.run(_run())


def test_missing_or_null_urgency_field_handling():
    """Verify that jobs with urgent=None or missing urgent key are safely excluded from urgent searches."""
    hits_with_missing_urgency = [
        {"id": 301, "job_title": "Frontend Engineer", "urgent": 1},
        {"id": 302, "job_title": "Backend Engineer", "urgent": None},
        {"id": 303, "job_title": "Fullstack Engineer"},  # Missing key
    ]

    async def _run():
        mock_search = AsyncMock(return_value={"success": True, "data": hits_with_missing_urgency})
        patches = _patch_search_jobs(mock_search)
        for p in patches:
            p.start()
        try:
            hits, telemetry = await execute_hybrid_job_search(
                last_user_msg="show me urgent jobs",
                base_filters={"urgent": 1},
                parsed={"urgent": True},
            )

            returned_ids = [h["id"] for h in hits]
            assert returned_ids == [301], f"Expected only confirmed urgent job 301, got {returned_ids}"
        finally:
            for p in patches:
                p.stop()

    asyncio.run(_run())


def test_normal_job_search_preserves_both_urgent_and_non_urgent():
    """Verify that standard non-urgent job queries do NOT activate strict urgent filtering."""
    mixed_hits = [
        {"id": 401, "job_title": "Python Dev", "urgent": 1},
        {"id": 402, "job_title": "Python Engineer", "urgent": 0},
    ]

    async def _run():
        mock_search = AsyncMock(return_value={"success": True, "data": mixed_hits})
        patches = _patch_search_jobs(mock_search)
        for p in patches:
            p.start()
        try:
            hits, telemetry = await execute_hybrid_job_search(
                last_user_msg="show me Python jobs",
                keyword="python",
                base_filters={},
                parsed={"urgent": False},
            )

            returned_ids = [h["id"] for h in hits]
            assert 401 in returned_ids and 402 in returned_ids, "Normal job search must return both urgent and non-urgent jobs"
        finally:
            for p in patches:
                p.stop()

    asyncio.run(_run())


def test_multi_turn_urgent_refinement_enforcement():
    """Verify multi-turn refinement 'show me Python jobs' -> 'only urgent ones' retains Python keyword and enforces urgent=1."""
    sq1 = SearchQuery(title="python", skills=["Python"])
    sq2 = SearchQuery(urgent=True)
    merged = merge_employee_search_query(sq1, sq2, prompt_category="ADDITIVE")

    assert merged.title == "python"
    assert merged.urgent is True

    mixed_hits = [
        {"id": 501, "job_title": "Python Dev", "urgent": 1},
        {"id": 502, "job_title": "Python Architect", "urgent": 0},
    ]

    async def _run():
        mock_search = AsyncMock(return_value={"success": True, "data": mixed_hits})
        patches = _patch_search_jobs(mock_search)
        for p in patches:
            p.start()
        try:
            base_filters = {}
            if merged.urgent:
                base_filters["urgent"] = 1

            hits, telemetry = await execute_hybrid_job_search(
                last_user_msg="only urgent ones",
                keyword=merged.title,
                base_filters=base_filters,
                parsed={"urgent": merged.urgent},
            )

            returned_ids = [h["id"] for h in hits]
            assert returned_ids == [501]
        finally:
            for p in patches:
                p.stop()

    asyncio.run(_run())


def test_candidate_to_urgent_job_switch_retrieval_flow():
    """Verify that switching from active candidate search to 'show me jobs with urgent hiring' returns job cards with urgent=1."""
    session_id = "test_phase1_1_cand_switch"
    set_candidate_search_criteria(session_id, SearchCriteria(skills=["Python"], target=SearchTarget.PEOPLE_SEARCH), role="employee")

    async def _run():
        q = "show me jobs with urgent hiring"
        parsed = await parse_search_query(q, role="employee", session_id=session_id)
        assert parsed.get("search_target") == "JOBS"
        assert parsed.get("urgent") is True

        mock_hits = [{"id": 601, "job_title": "Urgent Python Developer", "urgent": 1}]
        with patch("backend.api.employee.execute_hybrid_job_search", new_callable=AsyncMock) as mock_hybrid:
            mock_hybrid.return_value = (mock_hits, {})

            from backend.api.employee import handle_employee_context
            resp = await handle_employee_context(
                last_user_msg=q,
                session_id=session_id,
                parsed_query=parsed,
            )

            assert resp.get("result_type") == "jobs"
            assert resp.get("results") is not None
            assert len(resp["results"]) == 1
            assert resp["results"][0].job_id == 601

    asyncio.run(_run())


def test_candidate_hiring_semantics_safety():
    """Verify that candidate queries containing 'hiring' do NOT activate urgent job search."""
    async def _run():
        parsed = await parse_search_query("find candidates for hiring", role="employee")
        assert parsed.get("search_target") == "PEOPLE_SEARCH"
        assert parsed.get("urgent") is not True

    asyncio.run(_run())


def test_deduplication_and_urgent_status_consistency():
    """Verify that when same job appears in both sources, merged normalized job preserves confirmed urgent status."""
    meili_hit = {"id": 701, "job_title": "Fullstack Dev", "urgent": 1, "company_name": "Partner X"}
    jm_hit = {
        "job_id": 701,
        "final_score": 0.90,
        "payload": {"job_id": 701, "title": "Fullstack Dev", "urgent": 1, "company_name": "Partner X"},
    }

    async def _run():
        mock_meili = AsyncMock(return_value={"success": True, "data": [meili_hit]})
        patches = _patch_search_jobs(mock_meili)
        for p in patches:
            p.start()

        with patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm, \
             patch("backend.services.hybrid_job_search.JOB_MATCH_ENABLED", True):

            mock_jm.return_value = {"results": [jm_hit]}
            try:
                hits, telemetry = await execute_hybrid_job_search(
                    last_user_msg="show me urgent jobs",
                    candidate_id=10,
                    base_filters={"urgent": 1},
                    parsed={"urgent": True},
                )

                assert len(hits) == 1
                assert hits[0]["id"] == 701
                assert hits[0]["_source"] == "hybrid"
                assert hits[0]["urgent"] == 1
            finally:
                for p in patches:
                    p.stop()

    asyncio.run(_run())


def test_fallback_path_preserves_urgent_invariant():
    """Verify that when JobMatch vector service throws an exception, Meilisearch fallback still enforces urgent=1."""
    meili_hits = [
        {"id": 801, "job_title": "Urgent Data Analyst", "urgent": 1},
        {"id": 802, "job_title": "Normal Data Analyst", "urgent": 0},
    ]

    async def _run():
        mock_meili = AsyncMock(return_value={"success": True, "data": meili_hits})
        patches = _patch_search_jobs(mock_meili)
        for p in patches:
            p.start()

        with patch("backend.services.hybrid_job_search.job_match_client.find_jobs", side_effect=Exception("Qdrant Timeout")), \
             patch("backend.services.hybrid_job_search.JOB_MATCH_ENABLED", True):

            try:
                hits, telemetry = await execute_hybrid_job_search(
                    last_user_msg="find urgent hiring jobs",
                    candidate_id=5,
                    base_filters={"urgent": 1},
                    parsed={"urgent": True},
                )

                assert len(hits) == 1
                assert hits[0]["id"] == 801
                assert hits[0]["urgent"] == 1
            finally:
                for p in patches:
                    p.stop()

    asyncio.run(_run())
