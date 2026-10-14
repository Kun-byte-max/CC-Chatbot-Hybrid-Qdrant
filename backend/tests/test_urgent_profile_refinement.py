import pytest
import asyncio
from unittest.mock import patch, MagicMock

from backend.services.search_parser import parse_search_query, normalize_search_target
from backend.utils.session_state import merge_employee_search_query, update_employee_session_cas, reset_search_context
from backend.schemas.search_query import SearchQuery, SearchTarget
from backend.services.hybrid_job_search import execute_hybrid_job_search, NormalizedJob


# ============================================================================
# GROUP A — Parser / Routing Tests
# ============================================================================

def test_group_a_direct_urgent_search_parser():
    """Verify direct urgent query parses JOBS target and urgent=True."""
    async def _test():
        parsed = await parse_search_query("show me jobs with urgent hiring", role="employee")
        assert parsed.get("search_target") == "JOBS"
        assert parsed.get("urgent") is True
    asyncio.run(_test())


def test_group_a_profile_skills_triggers():
    """Verify various profile-oriented follow-up queries activate use_profile_skills."""
    async def _test():
        queries = [
            "show me jobs according to my skills",
            "show me urgent jobs according to my skills",
            "find the urgent jobs matching my profile",
            "which of these urgent jobs are suitable for me?",
            "jobs based on my profile",
            "jobs suitable for me",
        ]
        for q in queries:
            parsed = await parse_search_query(q, role="employee")
            assert parsed.get("search_target") == "JOBS"
            assert parsed.get("use_profile_skills") is True or parsed.get("skill_source") == "PROFILE"
    asyncio.run(_test())


def test_group_a_explicit_skill_vs_profile_skill():
    """Verify explicit skill ('Python') is keyword vs profile query ('according to my skills')."""
    async def _test():
        p_explicit = await parse_search_query("show me urgent Python jobs", role="employee")
        assert p_explicit.get("keyword") == "Python"
        assert p_explicit.get("urgent") is True
        assert p_explicit.get("use_profile_skills") is not True

        p_profile = await parse_search_query("show me urgent jobs according to my skills", role="employee")
        assert p_profile.get("urgent") is True
        assert p_profile.get("use_profile_skills") is True
    asyncio.run(_test())


def test_group_a_candidate_hiring_queries_remain_people_search():
    """Verify hiring queries with candidate nouns remain PEOPLE_SEARCH."""
    async def _test():
        cand_queries = [
            "find candidates for hiring",
            "find candidates to hire",
            "find professionals to hire",
            "find developers to hire",
        ]
        for q in cand_queries:
            parsed = await parse_search_query(q, role="employee")
            assert parsed.get("search_target") == "PEOPLE_SEARCH"
    asyncio.run(_test())


# ============================================================================
# GROUP B — Multi-turn Session State Tests
# ============================================================================

def test_group_b_urgent_preserved_on_profile_followup():
    """Verify Turn 1 urgent=True is preserved into Turn 2 profile skill search."""
    turn1_sq = SearchQuery(search_target=SearchTarget.JOBS, urgent=True)
    turn2_sq = SearchQuery(search_target=SearchTarget.JOBS, urgent=None)

    merged = merge_employee_search_query(previous_query=turn1_sq, current_turn_query=turn2_sq, prompt_category="ADDITIVE")
    assert merged.urgent is True


def test_group_b_new_search_resets_urgent_context():
    """Verify an independent NEW_SEARCH resets urgent context when prompt_category is RESET."""
    turn1_sq = SearchQuery(search_target=SearchTarget.JOBS, urgent=True)
    turn2_sq = SearchQuery(search_target=SearchTarget.JOBS, title="Accountant", urgent=None)

    merged = merge_employee_search_query(previous_query=turn1_sq, current_turn_query=turn2_sq, prompt_category="RESET")
    assert merged.urgent is None
    assert merged.title == "Accountant"


def test_group_b_session_cas_preserves_urgent_for_profile_turn():
    """Verify update_employee_session_cas retains urgent=True for profile follow-ups."""
    session_id = "test_sess_urgent_prof_101"
    reset_search_context(session_id, role="employee")

    # Turn 1: urgent search
    sq1 = SearchQuery(search_target=SearchTarget.JOBS, urgent=True)
    s1 = update_employee_session_cas(session_id, sq1, prompt_category="RESET", role="employee")
    assert s1.get("search_context", {}).get("urgent") is True

    # Turn 2: profile follow-up
    sq2 = SearchQuery(search_target=SearchTarget.JOBS, urgent=None)
    s2 = update_employee_session_cas(session_id, sq2, prompt_category="ADDITIVE", role="employee")
    assert s2.get("search_context", {}).get("urgent") is True


# ============================================================================
# GROUP C — Hybrid Contract & Leakage Prevention
# ============================================================================

def _patch_search_jobs(mock_fn):
    """Helper context manager to patch search_jobs across all imported module namespaces."""
    import sys
    patches = [
        patch("backend.services.search_client.search_jobs", new=mock_fn),
        patch("backend.services.hybrid_job_search.search_jobs", new=mock_fn),
    ]
    if "backend.api.employee" in sys.modules:
        patches.append(patch("backend.api.employee.search_jobs", new=mock_fn))
    return patches


def test_group_c_non_urgent_leakage_prevention_in_hybrid_search():
    """
    Verify highly vector-similar non-urgent jobs (urgent=0) are excluded by Step 3 gate
    when an urgent profile search is active, and only confirmed urgent jobs are returned.
    """
    async def _test():
        # Mock Meilisearch returning 1 urgent job (Job 101)
        mock_meili_hits = [
            {
                "id": 101,
                "job_title": "Python Developer (Urgent)",
                "urgent": 1,
                "location": "Delhi",
                "job_mode_name": "Office",
                "experience": "2 years",
                "salary": "800000",
            }
        ]

        # Mock JobMatch/Qdrant returning a non-urgent job with VERY HIGH vector score (Job 102) and an urgent job (Job 103)
        mock_jm_hits = [
            {
                "job_id": 102,
                "title": "Senior Python Developer (Non-Urgent)",
                "final_score": 0.98,
                "payload": {
                    "job_id": 102,
                    "job_title": "Senior Python Developer (Non-Urgent)",
                    "urgent": 0,
                    "location": "Delhi",
                }
            },
            {
                "job_id": 103,
                "title": "Lead Python Engineer (Urgent)",
                "final_score": 0.85,
                "payload": {
                    "job_id": 103,
                    "job_title": "Lead Python Engineer (Urgent)",
                    "urgent": 1,
                    "location": "Delhi",
                }
            }
        ]

        async def _mock_search_jobs(**kwargs):
            return {"success": True, "data": mock_meili_hits}

        patches = _patch_search_jobs(_mock_search_jobs)
        for p in patches:
            p.start()

        try:
            with patch("backend.services.hybrid_job_search.job_match_client.find_jobs", return_value={"status": "success", "results": mock_jm_hits}):

                hits, telemetry = await execute_hybrid_job_search(
                    last_user_msg="show me jobs according to my skills",
                    candidate_id=41031,
                    parsed={"search_target": "JOBS", "urgent": True, "use_profile_skills": True},
                    base_filters={"urgent": 1},
                    used_profile_fallback=True,
                    profile_terms=["Python"],
                    limit=10,
                )

                returned_ids = [h["id"] for h in hits]
                # Job 102 (urgent=0) MUST BE EXCLUDED despite high vector score (0.98)
                assert 102 not in returned_ids
                # Jobs 101 and 103 (urgent=1) MUST be included
                assert 101 in returned_ids
                assert 103 in returned_ids
                assert telemetry["final_count"] == 2
        finally:
            for p in patches:
                p.stop()

    asyncio.run(_test())


# ============================================================================
# GROUP D — End-to-End Multi-Turn Integration
# ============================================================================

def test_group_d_end_to_end_urgent_to_profile_refinement():
    """
    Verify complete multi-turn flow:
    Turn 1: "show me jobs with urgent hiring" -> urgent=True
    Turn 2: "show me jobs according to my skills" -> urgent=True + use_profile_skills=True
    """
    async def _test():
        from backend.api.employee import handle_employee_context

        session_id = "test_e2e_urgent_profile_999"
        reset_search_context(session_id, role="employee")

        mock_profile_data = {
            "status": True,
            "data": {
                "id": "41031",
                "full_name": "Test Candidate",
                "skills": ["Python", "FastAPI"],
                "all_Skill": [{"skill": "Python"}, {"skill": "FastAPI"}],
            }
        }

        captured_calls = []

        async def mock_execute_hybrid_job_search(**kwargs):
            captured_calls.append(kwargs)
            return [
                {
                    "id": 501,
                    "job_title": "Urgent Python Engineer",
                    "urgent": 1,
                    "location": "Delhi",
                }
            ], {"final_count": 1, "search_mode": "PROFILE_RECOMMENDATION"}

        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=mock_profile_data), \
             patch("backend.api.employee.execute_hybrid_job_search", side_effect=mock_execute_hybrid_job_search):

            # Turn 1
            res1 = await handle_employee_context(
                "show me jobs with urgent hiring",
                session_id=session_id,
                candidate_id="41031",
                role="employee",
            )
            assert res1 is not None

            # Turn 2
            res2 = await handle_employee_context(
                "show me jobs according to my skills",
                session_id=session_id,
                candidate_id="41031",
                role="employee",
            )
            assert res2 is not None

            assert len(captured_calls) >= 2
            turn2_call = captured_calls[-1]

            # Verify urgent=1 is preserved in base_filters and parsed dict
            assert turn2_call["base_filters"].get("urgent") == 1
            assert turn2_call["parsed"].get("urgent") is True
            # Verify profile recommendation flag / fallback is active
            assert turn2_call["parsed"].get("use_profile_skills") is True or turn2_call["used_profile_fallback"] is True

    asyncio.run(_test())
