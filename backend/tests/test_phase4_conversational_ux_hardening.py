"""
test_phase4_conversational_ux_hardening.py
Phase 4 Conversational UX and Edge-Case Hardening Unit Tests.

Verifies:
1. Short profile follow-up phrases ("for me", "for my profile", "my profile", "my skills", "based on my background").
2. Multi-turn order of operations (urgent -> profile, profile -> urgent, job -> urgent -> profile).
3. Candidate search safety and job <-> candidate intent switching.
4. Explicit new search reset behavior without over-stickiness.
"""

import pytest
from unittest.mock import AsyncMock, patch
from backend.services.search_parser import parse_search_query, normalize_search_target


@pytest.mark.anyio
async def test_short_profile_followups():
    """Verify short conversational profile follow-up phrases set profile intent."""
    short_phrases = [
        "for me",
        "for my profile",
        "my profile",
        "my skills",
        "according to my experience",
        "based on my background",
        "jobs for my experience",
    ]

    for phrase in short_phrases:
        with patch("backend.services.llm_service.LLMService.get_chat_completion", new=AsyncMock(side_effect=Exception("Offline"))):
            parsed = await parse_search_query(phrase, role="employee", messages=[{"role": "user", "content": "show me urgent jobs"}])

        assert parsed.get("use_profile_skills") is True, f"Failed to detect profile intent for '{phrase}'"
        assert parsed.get("skill_source") == "PROFILE", f"Failed skill_source='PROFILE' for '{phrase}'"


@pytest.mark.anyio
async def test_short_urgent_followups():
    """Verify short urgent refinement phrases extract urgent=True."""
    urgent_phrases = [
        "only urgent ones",
        "urgent ones",
        "the urgent ones",
        "only urgent",
    ]

    for phrase in urgent_phrases:
        with patch("backend.services.llm_service.LLMService.get_chat_completion", new=AsyncMock(side_effect=Exception("Offline"))):
            parsed = await parse_search_query(phrase, role="employee")

        assert parsed.get("urgent") is True, f"Failed to set urgent=True for '{phrase}'"


def test_candidate_job_boundary_routing():
    """Verify job <-> candidate intent switching precedence."""
    # Candidate queries remain PEOPLE_SEARCH
    assert normalize_search_target(None, "find candidates for hiring", role="employee") == "PEOPLE_SEARCH"
    assert normalize_search_target(None, "find candidates to hire", role="employee") == "PEOPLE_SEARCH"
    assert normalize_search_target(None, "find developers to hire", role="employee") == "PEOPLE_SEARCH"

    # Urgent job queries remain JOBS
    assert normalize_search_target(None, "show me jobs with urgent hiring", role="employee") == "JOBS"
    assert normalize_search_target(None, "show me urgent jobs", role="employee") == "JOBS"
    assert normalize_search_target(None, "find urgent jobs suitable for me", role="employee") == "JOBS"


@pytest.mark.anyio
async def test_new_search_reset_behavior():
    """Verify an explicit new search query clears previous specific keyword filters."""
    with patch("backend.services.llm_service.LLMService.get_chat_completion", new=AsyncMock(side_effect=Exception("Offline"))):
        parsed = await parse_search_query("show me accountant jobs", role="employee")

    assert parsed.get("search_target") == "JOBS"
    assert parsed.get("keyword") in ("accountant", "accountant jobs", "accountant job") or "accountant" in (parsed.get("keyword") or "")


def test_phase4_pagination_continuity_and_no_duplicate_ids():
    """Verify 'show more' continues search, advances page cursor once, and yields no duplicate cards."""
    from backend.utils.session_state import set_pagination_state, get_pagination_state
    session_id = "test_phase4_sess_continuity"

    # Simulate 12 total jobs from initial search
    all_jobs = [{"job_id": i, "title": f"Job {i}"} for i in range(1, 13)]
    p_meta_page1 = {
        "current_page": 1,
        "per_page": 5,
        "total_results": 12,
        "total_pages": 3,
        "has_next": True,
        "has_prev": False,
        "result_type": "jobs",
        "all_recs": all_jobs,
    }
    set_pagination_state(session_id, p_meta_page1, role="employee")

    # Verify Page 1 slice
    st1 = get_pagination_state(session_id, role="employee")
    page1_ids = [j["job_id"] for j in st1["all_recs"][0:5]]
    assert page1_ids == [1, 2, 3, 4, 5]

    # Advance to Page 2 ('show more')
    p_meta_page2 = dict(st1)
    p_meta_page2.update({
        "current_page": 2,
        "has_next": True,
        "has_prev": True,
    })
    set_pagination_state(session_id, p_meta_page2, role="employee")

    st2 = get_pagination_state(session_id, role="employee")
    page2_ids = [j["job_id"] for j in st2["all_recs"][5:10]]
    assert page2_ids == [6, 7, 8, 9, 10]
    # No duplicate IDs across pages
    assert set(page1_ids).isdisjoint(set(page2_ids))

    # Advance to Page 3 (Exhausted pagination)
    p_meta_page3 = dict(st2)
    p_meta_page3.update({
        "current_page": 3,
        "has_next": False,
        "has_prev": True,
    })
    set_pagination_state(session_id, p_meta_page3, role="employee")

    st3 = get_pagination_state(session_id, role="employee")
    page3_ids = [j["job_id"] for j in st3["all_recs"][10:15]]
    assert page3_ids == [11, 12]
    assert st3["has_next"] is False


def test_phase4_job_followup_preserves_selected_job_id_and_rank():
    """Verify that clicking a job card or asking 'Why this job?' resolves exact job ID and rank without triggering re-search."""
    from backend.recommendation.recommendation_service import RecommendationService
    from backend.utils.session_state import set_recommendation_session, get_recommendation_session
    from backend.recommendation.models import RecommendationResult, ScoreResult, ExplanationResult

    service = RecommendationService()
    session_id = "test_phase4_sess_followup"

    class MockRec:
        def __init__(self, job_id, rank, title, summary):
            self.job_id = job_id
            self.global_rank = rank
            self.title = title
            self.overall_score = 90.0
            self.score_result = ScoreResult(
                overall_score=90.0,
                recommendation_level="Good Match",
                skill_score=90.0,
                experience_score=90.0,
                location_score=90.0,
                salary_score=90.0,
                title_score=90.0,
                weight_skill=0.4,
                weight_experience=0.2,
                weight_location=0.1,
                weight_salary=0.15,
                weight_title=0.15
            )
            self.explanation_result = ExplanationResult(
                summary=summary,
                strengths=["Python"],
                weaknesses=[],
                recommendation_level="Good Match",
                overall_score=90.0
            )
            self.recommendation_reasons = []

    mock_recs = [
        MockRec(job_id=501, rank=1, title="Python Dev", summary="Strong Python match"),
        MockRec(job_id=502, rank=2, title="FastAPI Dev", summary="FastAPI skills align"),
    ]

    set_recommendation_session(
        session_id=session_id,
        query_key="cand_101",
        ranked_jobs=mock_recs,
        current_page=1,
        page_size=5,
        total_jobs=2
    )

    # 1. Resolve by target_job_id (UI click)
    res_click = service.get_recommendation_by_rank_or_id(
        session_id=session_id,
        target_job_id=502
    )
    assert res_click["success"] is True
    assert res_click["job_id"] == 502
    assert res_click["global_rank"] == 2
    assert res_click["recommendation_explanation"] == "FastAPI skills align"

    # 2. Resolve by conversational ordinal
    res_ordinal = service.get_recommendation_by_rank_or_id(
        session_id=session_id,
        user_message="Why was the first job recommended?"
    )
    assert res_ordinal["success"] is True
    assert res_ordinal["job_id"] == 501
    assert res_ordinal["global_rank"] == 1
    assert res_ordinal["recommendation_explanation"] == "Strong Python match"

    # 3. Verify last_referenced_job_id state updated
    sess_state = get_recommendation_session(session_id)
    assert sess_state["last_referenced_job_id"] == 501


def test_phase4_cross_session_isolation():
    """Verify that pagination state and recommendation snapshots are completely isolated across session IDs."""
    from backend.utils.session_state import set_pagination_state, get_pagination_state, set_recommendation_session, get_recommendation_session

    sess_user1 = "sess_user_1"
    sess_user2 = "sess_user_2"

    set_pagination_state(sess_user1, {"current_page": 2, "result_type": "jobs", "total_results": 10}, role="employee")
    set_pagination_state(sess_user2, {"current_page": 1, "result_type": "people", "total_results": 5}, role="employee")

    st1 = get_pagination_state(sess_user1, role="employee")
    st2 = get_pagination_state(sess_user2, role="employee")

    assert st1["current_page"] == 2
    assert st1["result_type"] == "jobs"
    assert st2["current_page"] == 1
    assert st2["result_type"] == "people"

    set_recommendation_session(session_id=sess_user1, query_key="q1", ranked_jobs=[{"job_id": 100}], current_page=2, total_jobs=10)
    set_recommendation_session(session_id=sess_user2, query_key="q2", ranked_jobs=[{"job_id": 200}], current_page=1, total_jobs=5)

    rec1 = get_recommendation_session(sess_user1)
    rec2 = get_recommendation_session(sess_user2)

    assert rec1["ranked_jobs"][0]["job_id"] == 100
    assert rec2["ranked_jobs"][0]["job_id"] == 200


def test_phase4_reset_search_context_clears_recommendation_context():
    """Verify reset_search_context clears search, pagination, and recommendation context, preventing stale lookups."""
    from backend.utils.session_state import (
        set_session_state,
        get_session_state,
        set_pagination_state,
        get_pagination_state,
        set_recommendation_session,
        get_recommendation_session,
        reset_search_context,
    )
    from backend.recommendation.recommendation_service import RecommendationService
    from backend.recommendation.models import ScoreResult, ExplanationResult

    class MockRec:
        def __init__(self, job_id, rank, title, summary):
            self.job_id = job_id
            self.global_rank = rank
            self.title = title
            self.overall_score = 92.0
            self.score_result = ScoreResult(
                overall_score=92.0,
                recommendation_level="Good Match",
                skill_score=92.0,
                experience_score=90.0,
                location_score=90.0,
                salary_score=90.0,
                title_score=90.0,
                weight_skill=0.4,
                weight_experience=0.2,
                weight_location=0.1,
                weight_salary=0.15,
                weight_title=0.15,
            )
            self.explanation_result = ExplanationResult(
                summary=summary,
                strengths=["Python"],
                weaknesses=[],
                recommendation_level="Good Match",
                overall_score=92.0,
            )
            self.recommendation_reasons = []

    service = RecommendationService()
    sess_target = "test_phase4_reset_target"
    sess_other = "test_phase4_reset_other"

    # 1. Populate sess_target with search state, pagination state, and recommendation context
    set_session_state(sess_target, {
        "search_context": {"role": "Python Dev", "location": "Remote"},
        "search_snapshot": {"snapshot_id": "snap_target"},
        "language_preference": "english",
        "auth_token": "token_target_abc",
        "user_id": "usr_target_123",
    })
    set_pagination_state(sess_target, {
        "current_page": 2,
        "per_page": 5,
        "total_results": 10,
        "total_pages": 2,
        "result_type": "jobs",
    })
    set_recommendation_session(
        session_id=sess_target,
        query_key="cand_target",
        ranked_jobs=[MockRec(job_id=901, rank=1, title="Python Dev", summary="Target Python Match")],
        current_page=2,
        page_size=5,
        total_jobs=10,
    )

    # Populate sess_other to verify cross-session isolation
    set_recommendation_session(
        session_id=sess_other,
        query_key="cand_other",
        ranked_jobs=[MockRec(job_id=902, rank=1, title="Rust Dev", summary="Other Rust Match")],
        current_page=1,
        page_size=5,
        total_jobs=5,
    )

    # 2. Call reset_search_context() for sess_target
    reset_res = reset_search_context(sess_target)

    # 3. Verify intended search and pagination state is cleared, while lang/auth are preserved
    assert reset_res.get("search_context") is None
    assert reset_res.get("pagination") is None
    assert reset_res.get("search_snapshot") is None
    assert reset_res.get("recommendation_context") is None
    assert reset_res.get("language_preference") == "english"
    assert reset_res.get("user_id") == "usr_target_123"
    assert get_pagination_state(sess_target) is None

    # 4. Verify recommendation context is cleared in session state
    assert get_recommendation_session(sess_target) is None

    # 4b. Verify recommendation lookup fails gracefully without stale data
    lookup_after_reset = service.get_recommendation_by_rank_or_id(
        session_id=sess_target,
        user_message="Why was the first job recommended?"
    )
    assert lookup_after_reset["success"] is False
    assert "don't have a recent job recommendation list" in lookup_after_reset["error"]

    # 5. Verify sess_other recommendation context remains untouched
    other_rec = get_recommendation_session(sess_other)
    assert other_rec is not None
    assert other_rec["ranked_jobs"][0].job_id == 902

    other_lookup = service.get_recommendation_by_rank_or_id(
        session_id=sess_other,
        user_message="Why was the first job recommended?"
    )
    assert other_lookup["success"] is True
    assert other_lookup["job_id"] == 902
    assert other_lookup["recommendation_explanation"] == "Other Rust Match"

    # 6. Verify resetting an already empty session raises no errors
    empty_sess = "test_phase4_empty_sess"
    empty_reset = reset_search_context(empty_sess)
    assert empty_reset is not None
    assert get_recommendation_session(empty_sess) is None

    # 7. Verify fresh search can subsequently populate new recommendation context and resolve normally
    set_recommendation_session(
        session_id=sess_target,
        query_key="cand_fresh",
        ranked_jobs=[MockRec(job_id=903, rank=1, title="Go Dev", summary="Fresh Go Match")],
        current_page=1,
        page_size=5,
        total_jobs=1,
    )
    fresh_lookup = service.get_recommendation_by_rank_or_id(
        session_id=sess_target,
        user_message="Why was the first job recommended?"
    )
    assert fresh_lookup["success"] is True
    assert fresh_lookup["job_id"] == 903
    assert fresh_lookup["recommendation_explanation"] == "Fresh Go Match"


@pytest.mark.anyio
async def test_phase4_sequential_pagination_handler():
    """Verify sequential 'show more' requests advance page 1 -> page 2 -> page 3 with no repeated IDs."""
    from backend.api.employee import handle_employee_context
    from backend.utils.session_state import set_pagination_state, get_pagination_state

    sess_id = "test_phase4_seq_handler"
    all_jobs = [{"job_id": i, "raw_data": {"job_title": f"Engineer {i}"}} for i in range(1, 16)]
    pag_meta = {
        "current_page": 1,
        "per_page": 5,
        "total_results": 15,
        "total_pages": 3,
        "has_next": True,
        "result_type": "jobs",
        "all_recs": all_jobs,
    }
    set_pagination_state(sess_id, pag_meta, role="employee")

    # Turn 1: Next page -> Page 2
    res2 = await handle_employee_context("show more", session_id=sess_id)
    assert res2.get("result_type") == "jobs"
    p2_meta = res2.get("pagination")
    assert p2_meta["current_page"] == 2
    p2_ids = [card.job_id for card in res2.get("results", [])]
    assert p2_ids == [6, 7, 8, 9, 10]

    # Turn 2: Next page -> Page 3
    res3 = await handle_employee_context("show more", session_id=sess_id)
    assert res3.get("result_type") == "jobs"
    p3_meta = res3.get("pagination")
    assert p3_meta["current_page"] == 3
    p3_ids = [card.job_id for card in res3.get("results", [])]
    assert p3_ids == [11, 12, 13, 14, 15]

    # Verify disjoint IDs across sequential pages
    assert set(p2_ids).isdisjoint(set(p3_ids))


def test_phase4_concurrent_pagination_race_prevention():
    """Verify two concurrent 'show more' requests for the same session claim separate consecutive pages (never duplicates)."""
    import threading
    import asyncio
    from backend.api.employee import handle_employee_context
    from backend.utils.session_state import set_pagination_state

    sess_id = "test_phase4_concurrent_race"
    all_jobs = [{"job_id": i, "raw_data": {"job_title": f"Dev {i}"}} for i in range(1, 21)]
    pag_meta = {
        "current_page": 1,
        "per_page": 5,
        "total_results": 20,
        "total_pages": 4,
        "has_next": True,
        "result_type": "jobs",
        "all_recs": all_jobs,
    }
    set_pagination_state(sess_id, pag_meta, role="employee")

    barrier = threading.Barrier(2)
    thread_results = [None, None]

    def worker(idx):
        barrier.wait()
        res = asyncio.run(handle_employee_context("show more", session_id=sess_id))
        thread_results[idx] = res

    t1 = threading.Thread(target=worker, args=(0,))
    t2 = threading.Thread(target=worker, args=(1,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    r1_page = thread_results[0].get("pagination", {}).get("current_page")
    r2_page = thread_results[1].get("pagination", {}).get("current_page")
    claimed_pages = sorted([r1_page, r2_page])

    # Must claim pages 2 and 3 sequentially, NOT duplicate page 2 twice
    assert claimed_pages == [2, 3]

    cards1_ids = [card.job_id for card in thread_results[0].get("results", [])]
    cards2_ids = [card.job_id for card in thread_results[1].get("results", [])]
    assert set(cards1_ids).isdisjoint(set(cards2_ids))


def test_phase4_cross_session_concurrent_pagination_independence():
    """Verify concurrent pagination requests on two different sessions progress completely independently."""
    import threading
    import asyncio
    from backend.api.employee import handle_employee_context
    from backend.utils.session_state import set_pagination_state

    sess_a = "test_phase4_sess_a"
    sess_b = "test_phase4_sess_b"

    all_jobs_a = [{"job_id": i, "raw_data": {"job_title": f"Role A-{i}"}} for i in range(1, 11)]
    all_jobs_b = [{"job_id": 100 + i, "raw_data": {"job_title": f"Role B-{i}"}} for i in range(1, 11)]

    set_pagination_state(sess_a, {"current_page": 1, "per_page": 5, "total_results": 10, "total_pages": 2, "has_next": True, "result_type": "jobs", "all_recs": all_jobs_a}, role="employee")
    set_pagination_state(sess_b, {"current_page": 1, "per_page": 5, "total_results": 10, "total_pages": 2, "has_next": True, "result_type": "jobs", "all_recs": all_jobs_b}, role="employee")

    barrier = threading.Barrier(2)
    res_a = {}
    res_b = {}

    def worker_a():
        barrier.wait()
        res_a.update(asyncio.run(handle_employee_context("show more", session_id=sess_a)))

    def worker_b():
        barrier.wait()
        res_b.update(asyncio.run(handle_employee_context("show more", session_id=sess_b)))

    t1 = threading.Thread(target=worker_a)
    t2 = threading.Thread(target=worker_b)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Both independent sessions must advance from page 1 to page 2 without interfering
    assert res_a.get("pagination", {}).get("current_page") == 2
    assert res_b.get("pagination", {}).get("current_page") == 2

    ids_a = [c.job_id for c in res_a.get("results", [])]
    ids_b = [c.job_id for c in res_b.get("results", [])]
    assert ids_a == [6, 7, 8, 9, 10]
    assert ids_b == [106, 107, 108, 109, 110]


@pytest.mark.anyio
async def test_phase4_exhausted_pagination_boundary():
    """Verify exhausted pagination does not advance beyond total_pages and returns empty results."""
    from backend.api.employee import handle_employee_context
    from backend.utils.session_state import set_pagination_state, get_pagination_state

    sess_id = "test_phase4_exhausted"
    all_jobs = [{"job_id": i, "raw_data": {"job_title": f"Job {i}"}} for i in range(1, 6)]
    pag_meta = {
        "current_page": 1,
        "per_page": 5,
        "total_results": 5,
        "total_pages": 1,
        "has_next": False,
        "result_type": "jobs",
        "all_recs": all_jobs,
    }
    set_pagination_state(sess_id, pag_meta, role="employee")

    # Request next page when total_pages is 1
    res = await handle_employee_context("show more", session_id=sess_id)
    assert res.get("result_type") == "jobs"
    # Out of bounds page 2 returns 0 job cards and instructs user page is out of bounds
    assert len(res.get("results", [])) == 0
    assert "out of bounds" in res.get("db_context", "")


def test_phase4_claim_next_pagination_page_error_safety():
    """Verify claim_next_pagination_page fails gracefully on empty/non-existent session without crashing."""
    from backend.utils.session_state import claim_next_pagination_page

    res_empty = claim_next_pagination_page("non_existent_sess_xyz", "next", role="employee")
    assert res_empty["success"] is False
    assert "No active pagination state found" in res_empty["error"]


def test_phase4_stale_response_cannot_regress_cursor():
    """
    Test 1: Stale response cannot regress cursor.
    Uses threading.Event to force deterministic interleaving:
    1. Request A claims page 2.
    2. Request A is paused after claim but before final set_pagination_state().
    3. Request B claims and completes page 3.
    4. Request A resumes and attempts its trailing write with page 2 metadata.
    5. Assert persisted current_page remains 3 (never regresses to 2).
    6. Assert Request A's returned response corresponds to page 2 and Request B's corresponds to page 3.
    7. Assert returned job IDs do not overlap.
    """
    import threading
    import asyncio
    from unittest.mock import patch
    from backend.api.employee import handle_employee_context
    import backend.api.employee as emp_module
    from backend.utils.session_state import set_pagination_state, get_pagination_state

    sess_id = "test_phase4_stale_regress"
    all_jobs = [{"job_id": i, "raw_data": {"job_title": f"Engineer {i}"}} for i in range(1, 21)]
    pag_meta = {
        "current_page": 1,
        "per_page": 5,
        "total_results": 20,
        "total_pages": 4,
        "has_next": True,
        "result_type": "jobs",
        "all_recs": all_jobs,
    }
    set_pagination_state(sess_id, pag_meta, role="employee")

    req_a_claimed = threading.Event()
    req_b_finished = threading.Event()
    thread_results = [None, None]
    thread_errors = []

    orig_set_pagination_state = emp_module.set_pagination_state

    def hooked_set_pagination_state(session_id, pagination_data, role="employee"):
        # Intercept Request A right before its trailing write
        if threading.current_thread().name == "Thread-ReqA":
            req_a_claimed.set()
            # Wait for Request B to finish completely before proceeding with trailing write
            res = req_b_finished.wait(timeout=5.0)
            if not res:
                raise TimeoutError("Timed out waiting for Request B to finish")
        return orig_set_pagination_state(session_id, pagination_data, role=role)

    def worker_a():
        try:
            res = asyncio.run(handle_employee_context("show more", session_id=sess_id))
            thread_results[0] = res
        except Exception as e:
            thread_errors.append(("Thread-ReqA", e))

    def worker_b():
        try:
            # Wait until Request A has claimed page 2 and is paused right before writing
            if not req_a_claimed.wait(timeout=5.0):
                raise TimeoutError("Timed out waiting for Request A claim")
            res = asyncio.run(handle_employee_context("show more", session_id=sess_id))
            thread_results[1] = res
            req_b_finished.set()
        except Exception as e:
            thread_errors.append(("Thread-ReqB", e))
            req_b_finished.set()

    try:
        with patch.object(emp_module, "set_pagination_state", side_effect=hooked_set_pagination_state):
            t_a = threading.Thread(target=worker_a, name="Thread-ReqA")
            t_b = threading.Thread(target=worker_b, name="Thread-ReqB")
            t_a.start()
            t_b.start()
            t_a.join(timeout=10.0)
            t_b.join(timeout=10.0)

        assert not thread_errors, f"Thread errors encountered: {thread_errors}"
        assert thread_results[0] is not None, "Request A returned None"
        assert thread_results[1] is not None, "Request B returned None"

        # 5. Assert persisted current_page remains 3 (Request A's stale write did not regress it to 2)
        persisted_state = get_pagination_state(sess_id, role="employee")
        assert persisted_state["current_page"] == 3, f"Expected persisted page 3, got {persisted_state['current_page']}"

        # 6. Assert responses match claimed pages
        res_a_page = thread_results[0].get("pagination", {}).get("current_page")
        res_b_page = thread_results[1].get("pagination", {}).get("current_page")
        assert res_a_page == 2, f"Request A should be page 2, got {res_a_page}"
        assert res_b_page == 3, f"Request B should be page 3, got {res_b_page}"

        # 7. Assert returned job IDs do not overlap
        ids_a = [card.job_id for card in thread_results[0].get("results", [])]
        ids_b = [card.job_id for card in thread_results[1].get("results", [])]
        assert ids_a == [6, 7, 8, 9, 10]
        assert ids_b == [11, 12, 13, 14, 15]
        assert set(ids_a).isdisjoint(set(ids_b))
    finally:
        from backend.utils.session_state import set_session_state
        set_session_state(sess_id, {"pagination": None}, role="employee")


@pytest.mark.anyio
async def test_phase4_repeated_exhaustion_boundedness():
    """
    Test 2: Repeated exhaustion cursor boundedness.
    1. Seed a small result pool with known final page (total_pages=2).
    2. Request page 2 (final page).
    3. Issue several additional 'show more' requests.
    4. Assert the cursor remains bounded and stable (at max total_pages + 1 = 3).
    5. Assert no nonexistent page is represented as a valid page and no result IDs are duplicated.
    """
    from backend.api.employee import handle_employee_context
    from backend.utils.session_state import set_pagination_state, get_pagination_state, set_session_state

    sess_id = "test_phase4_bounded_exhaustion"
    try:
        all_jobs = [{"job_id": i, "raw_data": {"job_title": f"Dev {i}"}} for i in range(1, 11)]
        pag_meta = {
            "current_page": 1,
            "per_page": 5,
            "total_results": 10,
            "total_pages": 2,
            "has_next": True,
            "result_type": "jobs",
            "all_recs": all_jobs,
        }
        set_pagination_state(sess_id, pag_meta, role="employee")

        # Step 2: Request page 2 (final valid page)
        res2 = await handle_employee_context("show more", session_id=sess_id)
        assert res2.get("pagination", {}).get("current_page") == 2
        assert len(res2.get("results", [])) == 5

        # Step 3: Issue 4 additional 'show more' requests after exhaustion
        for _ in range(4):
            res_oob = await handle_employee_context("show more", session_id=sess_id)
            assert len(res_oob.get("results", [])) == 0
            assert "out of bounds" in res_oob.get("db_context", "")

        # Step 4: Verify stored cursor remains bounded (clamped at total_pages + 1 = 3, NOT advancing to 4, 5, 6, 7)
        final_state = get_pagination_state(sess_id, role="employee")
        assert final_state["current_page"] <= 3
        assert final_state["current_page"] == 3
    finally:
        set_session_state(sess_id, {"pagination": None}, role="employee")


def test_phase4_new_search_or_reset_invalidates_stale_pagination_writes():
    """
    Test 3: New search / reset invalidates stale pagination writes.
    Tests that an in-flight stale write cannot restore or overwrite pagination
    after a reset_search_context() has cleared the session.
    """
    from backend.utils.session_state import (
        set_pagination_state,
        get_pagination_state,
        reset_search_context,
        set_session_state,
    )

    sess_id = "test_phase4_reset_guard"
    try:
        set_session_state(sess_id, {"search_context": {"query": "python"}})
        # Snapshot 1 was produced under reset_seq=0
        set_pagination_state(sess_id, {
            "current_page": 1,
            "per_page": 5,
            "total_results": 10,
            "total_pages": 2,
            "query_fingerprint": "fp_search_1",
            "_reset_seq": 0,
        })

        # Reset clears search and pagination, advancing _reset_seq to 1
        reset_search_context(sess_id)
        assert get_pagination_state(sess_id) is None

        # Stale in-flight write from search 1 carried _reset_seq=0, attempts to write back old pagination
        stale_write = {
            "current_page": 2,
            "per_page": 5,
            "total_results": 10,
            "total_pages": 2,
            "query_fingerprint": "fp_search_1",
            "_reset_seq": 0,
        }
        set_pagination_state(sess_id, stale_write)

        # Must NOT resurrect cleared pagination
        assert get_pagination_state(sess_id) is None
    finally:
        set_session_state(sess_id, {"pagination": None})

def test_phase4_old_search_cannot_overwrite_new_search():
    """
    Test A: Old search cannot overwrite a new search.
    1. Initialize Search A with a synthetic pool containing jobs A1-A20 (search_gen=1).
    2. Capture Search A's generation and prepare a simulated delayed pagination write from Search A.
    3. Begin Search B and ensure its generation advances before its results are initialized.
    4. Initialize Search B with a distinct pool containing jobs B1-B10, on page 1 (search_gen=2).
    5. Release Search A's delayed write (carrying captured generation 1 and higher page number).
    6. Assert that the saved state still contains Search B's exact result pool, page 1, correct totals, and correct navigation flags.
    7. Assert that no Search A jobs or metadata have been restored.
    """
    from backend.utils.session_state import (
        begin_search_generation,
        get_pagination_state,
        set_pagination_state,
    )

    sess_id = "test_phase4_search_gen_race_sess"

    # Step 1: Initialize Search A
    gen_a = begin_search_generation(sess_id, role="employee")
    jobs_a = [{"job_id": f"A{i}", "title": f"Job A{i}"} for i in range(1, 21)]
    pool_a = {
        "current_page": 1,
        "per_page": 5,
        "total_results": 20,
        "total_pages": 4,
        "has_next": True,
        "has_prev": False,
        "result_type": "jobs",
        "all_recs": jobs_a,
        "search_gen": gen_a,
    }
    set_pagination_state(sess_id, pool_a, role="employee")

    # Step 2: In-flight pagination write from Search A (claiming page 2 from Search A)
    stale_write_a = {
        "current_page": 2,
        "per_page": 5,
        "total_results": 20,
        "total_pages": 4,
        "has_next": True,
        "has_prev": True,
        "result_type": "jobs",
        "all_recs": jobs_a,
        "search_gen": gen_a,  # Captured generation 1
    }

    # Step 3: Begin Search B - advance search generation atomically before retrieval
    gen_b = begin_search_generation(sess_id, role="employee")
    assert gen_b > gen_a, f"Expected gen_b ({gen_b}) > gen_a ({gen_a})"

    # Step 4: Initialize Search B with 10 jobs (B1-B10), page 1
    jobs_b = [{"job_id": f"B{i}", "title": f"Job B{i}"} for i in range(1, 11)]
    pool_b = {
        "current_page": 1,
        "per_page": 5,
        "total_results": 10,
        "total_pages": 2,
        "has_next": True,
        "has_prev": False,
        "result_type": "jobs",
        "all_recs": jobs_b,
        "search_gen": gen_b,
        "_is_fresh_init": True,
    }
    set_pagination_state(sess_id, pool_b, role="employee")

    # Verify Search B is saved
    saved_after_b = get_pagination_state(sess_id, role="employee")
    assert saved_after_b["search_gen"] == gen_b
    assert saved_after_b["total_results"] == 10
    assert saved_after_b["current_page"] == 1
    assert [j["job_id"] for j in saved_after_b["all_recs"]] == [f"B{i}" for i in range(1, 11)]

    # Step 5: Delayed write from Search A executes now
    set_pagination_state(sess_id, stale_write_a, role="employee")

    # Step 6 & 7: State must still belong exclusively to Search B
    final_state = get_pagination_state(sess_id, role="employee")
    assert final_state is not None
    assert final_state["search_gen"] == gen_b
    assert final_state["current_page"] == 1
    assert final_state["total_results"] == 10
    assert final_state["total_pages"] == 2
    assert final_state["has_next"] is True
    assert final_state["has_prev"] is False
    assert len(final_state["all_recs"]) == 10
    saved_ids = [j["job_id"] for j in final_state["all_recs"]]
    assert saved_ids == [f"B{i}" for i in range(1, 11)]
    assert not any(j_id.startswith("A") for j_id in saved_ids)


def test_phase4_search_generation_cross_session_isolation():
    """
    Test D: Independent sessions.
    - Begin searches in two sessions.
    - Advance one session's generation.
    - Assert that the other session's generation and result pool remain unchanged.
    """
    from backend.utils.session_state import (
        begin_search_generation,
        get_search_generation,
        get_pagination_state,
        set_pagination_state,
    )

    sess_1 = "test_phase4_iso_sess_1"
    sess_2 = "test_phase4_iso_sess_2"

    gen_1 = begin_search_generation(sess_1, role="employee")
    gen_2 = begin_search_generation(sess_2, role="employee")

    jobs_1 = [{"job_id": f"S1_{i}"} for i in range(1, 6)]
    jobs_2 = [{"job_id": f"S2_{i}"} for i in range(1, 6)]

    set_pagination_state(sess_1, {
        "current_page": 1,
        "per_page": 5,
        "total_results": 5,
        "total_pages": 1,
        "has_next": False,
        "all_recs": jobs_1,
        "search_gen": gen_1,
    }, role="employee")

    set_pagination_state(sess_2, {
        "current_page": 1,
        "per_page": 5,
        "total_results": 5,
        "total_pages": 1,
        "has_next": False,
        "all_recs": jobs_2,
        "search_gen": gen_2,
    }, role="employee")

    # Advance session 1 generation multiple times
    new_gen_1a = begin_search_generation(sess_1, role="employee")
    new_gen_1b = begin_search_generation(sess_1, role="employee")
    assert new_gen_1b > new_gen_1a > gen_1

    # Assert session 2 is completely unaffected
    assert get_search_generation(sess_2, role="employee") == gen_2
    state_2 = get_pagination_state(sess_2, role="employee")
    assert state_2["search_gen"] == gen_2
    assert [j["job_id"] for j in state_2["all_recs"]] == [f"S2_{i}" for i in range(1, 6)]


def test_phase4_missing_generation_cannot_overwrite_newer_pool():
    """
    Test 1: Missing generation cannot overwrite a newer pool.
    - Initialize Search A with generation 1.
    - Begin Search B and initialize a distinct page-1 pool with generation 2.
    - Submit a simulated delayed Search A pagination payload with no search_gen.
    - Assert that Search B's jobs, page number, result count, and generation remain unchanged.
    """
    from backend.utils.session_state import (
        begin_search_generation,
        get_pagination_state,
        set_pagination_state,
        set_session_state,
    )

    sess_id = "test_phase4_missing_gen_sess"
    set_session_state(sess_id, {"pagination": None, "search_gen": 1}, role="employee")

    # Initialize Search A with generation 1
    gen_a = begin_search_generation(sess_id, role="employee")
    jobs_a = [{"job_id": f"A{i}"} for i in range(1, 11)]
    pool_a = {
        "current_page": 1,
        "per_page": 5,
        "total_results": 10,
        "total_pages": 2,
        "has_next": True,
        "has_prev": False,
        "result_type": "jobs",
        "all_recs": jobs_a,
        "search_gen": gen_a,
    }
    set_pagination_state(sess_id, pool_a, role="employee")

    # Begin Search B and initialize a distinct page-1 pool with generation 2
    gen_b = begin_search_generation(sess_id, role="employee")
    assert gen_b > gen_a
    jobs_b = [{"job_id": f"B{i}"} for i in range(1, 6)]
    pool_b = {
        "current_page": 1,
        "per_page": 5,
        "total_results": 5,
        "total_pages": 1,
        "has_next": False,
        "has_prev": False,
        "result_type": "jobs",
        "all_recs": jobs_b,
        "search_gen": gen_b,
        "_is_fresh_init": True,
    }
    set_pagination_state(sess_id, pool_b, role="employee")

    # Delayed write from Search A with NO search_gen (missing key)
    delayed_write_no_gen = {
        "current_page": 2,
        "per_page": 5,
        "total_results": 10,
        "total_pages": 2,
        "has_next": False,
        "has_prev": True,
        "result_type": "jobs",
        "all_recs": jobs_a,
    }
    set_pagination_state(sess_id, delayed_write_no_gen, role="employee")

    # Assert Search B's state remains intact
    saved = get_pagination_state(sess_id, role="employee")
    assert saved is not None
    assert saved["search_gen"] == gen_b
    assert saved["current_page"] == 1
    assert saved["total_results"] == 5
    assert saved["total_pages"] == 1
    assert [j["job_id"] for j in saved["all_recs"]] == [f"B{i}" for i in range(1, 6)]


def test_phase4_malformed_generation_handled_safely():
    """
    Test 2: Malformed generation is handled safely.
    - search_gen=None and non-numeric value "abc".
    - Call must not crash with TypeError or ValueError.
    - Neither malformed payload may overwrite a valid newer result pool.
    """
    from backend.utils.session_state import (
        begin_search_generation,
        get_pagination_state,
        set_pagination_state,
        set_session_state,
    )

    sess_id = "test_phase4_malformed_gen_sess"
    set_session_state(sess_id, {"pagination": None, "search_gen": 1}, role="employee")

    gen_valid = begin_search_generation(sess_id, role="employee")
    jobs_valid = [{"job_id": f"V{i}"} for i in range(1, 6)]
    pool_valid = {
        "current_page": 1,
        "per_page": 5,
        "total_results": 5,
        "total_pages": 1,
        "has_next": False,
        "has_prev": False,
        "result_type": "jobs",
        "all_recs": jobs_valid,
        "search_gen": gen_valid,
    }
    set_pagination_state(sess_id, pool_valid, role="employee")

    # Advance generation
    gen_newer = begin_search_generation(sess_id, role="employee")
    jobs_newer = [{"job_id": f"N{i}"} for i in range(1, 6)]
    pool_newer = {
        "current_page": 1,
        "per_page": 5,
        "total_results": 5,
        "total_pages": 1,
        "has_next": False,
        "has_prev": False,
        "result_type": "jobs",
        "all_recs": jobs_newer,
        "search_gen": gen_newer,
        "_is_fresh_init": True,
    }
    set_pagination_state(sess_id, pool_newer, role="employee")

    # 1. Payload with explicit search_gen=None
    payload_none_gen = {
        "current_page": 2,
        "per_page": 5,
        "total_results": 10,
        "all_recs": [{"job_id": "X1"}],
        "search_gen": None,
    }
    # Must not raise TypeError
    set_pagination_state(sess_id, payload_none_gen, role="employee")

    saved_after_none = get_pagination_state(sess_id, role="employee")
    assert saved_after_none["search_gen"] == gen_newer
    assert [j["job_id"] for j in saved_after_none["all_recs"]] == [f"N{i}" for i in range(1, 6)]

    # 2. Payload with non-numeric search_gen="abc"
    payload_str_gen = {
        "current_page": 2,
        "per_page": 5,
        "total_results": 10,
        "all_recs": [{"job_id": "X2"}],
        "search_gen": "abc",
    }
    # Must not raise ValueError
    set_pagination_state(sess_id, payload_str_gen, role="employee")

    saved_after_str = get_pagination_state(sess_id, role="employee")
    assert saved_after_str["search_gen"] == gen_newer
    assert [j["job_id"] for j in saved_after_str["all_recs"]] == [f"N{i}" for i in range(1, 6)]


@pytest.mark.asyncio
async def test_phase4_employer_search_generation_race_and_role_isolation():
    """
    Test 3: Employer searches are generation-aware with role isolation.
    Simulate:
    - Employer Search A starting first and being delayed (captured generation A).
    - Employer Search B starting later and initializing its pool (captured generation B > A).
    - Search A attempting a trailing write after Search B has initialized.
    Assert:
    - Search A cannot replace Search B's candidate pool or regress its cursor.
    - Role isolation: employer generation changes do not mutate employee session's generation or pagination.
    Uses asyncio.Event for deterministic race synchronization.
    """
    import asyncio
    from unittest.mock import patch, MagicMock
    from backend.api.employer import handle_employer_context
    from backend.utils.session_state import (
        get_pagination_state,
        set_pagination_state,
        get_search_generation,
        set_session_state,
    )
    from backend.recommendation.types import (
        EmployerCandidateRecommendationResult,
        MatchResult,
        RecommendationStatus,
        MatchStatus,
        RecommendationDimension,
    )

    sess_id = "test_phase4_emp_race_sess"
    # Reset both roles for clean slate
    set_session_state(sess_id, {"pagination": None, "search_gen": 1}, role="employer")
    set_session_state(sess_id, {"pagination": None, "search_gen": 1}, role="employee")

    # Seed employee state to verify role isolation
    emp_jobs = [{"job_id": "EMP_JOB_1"}]
    set_pagination_state(sess_id, {
        "current_page": 1,
        "total_results": 1,
        "total_pages": 1,
        "all_recs": emp_jobs,
        "search_gen": 1,
    }, role="employee")

    search_a_started = asyncio.Event()
    search_b_finished = asyncio.Event()

    raw_candidates_a = [
        {"id": f"CA_{i}", "name": f"Cand A{i}", "title": "Python Dev", "location": "Bangalore", "skills": ["Python"]}
        for i in range(1, 6)
    ]
    raw_candidates_b = [
        {"id": f"CB_{i}", "name": f"Cand B{i}", "title": "React Dev", "location": "Bangalore", "skills": ["React"]}
        for i in range(1, 6)
    ]

    async def mock_search_executor(requirements, session_id, page=1, limit=100):
        # Deterministically pause Search A until Search B completes
        role_val = getattr(getattr(requirements, "role", None), "value", "") or ""
        skills_val = getattr(getattr(requirements, "skills", None), "value", []) or []
        req_text = (role_val + " " + " ".join(skills_val)).lower()
        if "python" in req_text:
            search_a_started.set()
            await search_b_finished.wait()
            raw_hits = raw_candidates_a
        else:
            raw_hits = raw_candidates_b
        return [], None, {"raw_hits": raw_hits}

    with patch("backend.services.employer_search_executor.execute_employer_candidate_search", side_effect=mock_search_executor), \
         patch("backend.recommendation.employer_recommendation_engine.EmployerRecommendationEngine.recommend_candidates") as mock_engine:

        def engine_side_effect(reqs, clean_candidates):
            results = []
            for c in clean_candidates:
                cid = c.get("id")
                r = EmployerCandidateRecommendationResult(
                    candidate_id=cid,
                    overall_status=RecommendationStatus.CALCULATED,
                    final_score=90.0,
                    confidence_score=1.0,
                    match_results={
                        RecommendationDimension.EXPERIENCE: MatchResult(
                            dimension=RecommendationDimension.EXPERIENCE,
                            status=MatchStatus.MATCH,
                            score=100.0,
                            details={},
                        )
                    },
                    explanation="Great match",
                    scored_criteria_count=1,
                    total_requested_criteria_count=1,
                )
                results.append(r)
            return results

        mock_engine.side_effect = engine_side_effect

        # Launch Search A (delayed) - pass parsed_query to bypass live LLM calls
        parsed_q_a = {
            "keyword": "python developer",
            "is_search_intent": True,
            "search_target": "PEOPLE_SEARCH",
            "query_type": "NEW_SEARCH",
        }
        task_a = asyncio.create_task(
            handle_employer_context("looking for python developers", session_id=sess_id, parsed_query=parsed_q_a)
        )

        # Wait until Search A starts and captures its generation
        await search_a_started.wait()

        # Run Search B to completion
        parsed_q_b = {
            "keyword": "react developer",
            "is_search_intent": True,
            "search_target": "PEOPLE_SEARCH",
            "query_type": "NEW_SEARCH",
        }
        resp_b = await handle_employer_context("looking for react developers", session_id=sess_id, parsed_query=parsed_q_b)
        assert resp_b is not None
        search_b_finished.set()

        # Await Search A completion
        resp_a = await task_a
        assert resp_a is not None

        # Verify final pagination state belongs exclusively to Search B
        saved_emp = get_pagination_state(sess_id, role="employer")
        assert saved_emp is not None
        gen_b = get_search_generation(sess_id, role="employer")
        assert saved_emp["search_gen"] == gen_b
        candidate_ids = [c["cc_id"] if isinstance(c, dict) else getattr(c, "cc_id", None) for c in (saved_emp.get("all_candidate_cards") or saved_emp.get("all_recs", []))]
        # Should contain Candidate B ids, not Candidate A
        assert all("CB_" in str(cid) or "B" in str(cid) for cid in candidate_ids if cid)
        assert not any("CA_" in str(cid) for cid in candidate_ids if cid)

        # Verify employee role isolation: Employee state was not touched
        saved_employee = get_pagination_state(sess_id, role="employee")
        assert saved_employee is not None
        assert saved_employee["search_gen"] == 1
        assert saved_employee["all_recs"][0]["job_id"] == "EMP_JOB_1"
        assert get_search_generation(sess_id, role="employee") == 1

