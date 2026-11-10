"""
Phase 2D.1 — Multi-Worker Redis Reliability & Concurrency Tests.

Verifies:
1. Lifespan shutdown cleanly invokes close_redis_pool() in a finally block.
2. Lifespan shutdown handles upstream component errors without leaking Redis connection pools.
3. CAS concurrency in begin_search_generation uses remerge_fn to prevent lost generation updates.
4. Thread-level concurrency under Redis.
5. True multi-process cross-worker integration tests against Redis 7 (with graceful skip when Redis 7 is offline).
6. Docker compose YAML validity and absence of dead legacy imports.
"""

import os
import sys
import time
import socket
import pytest
import asyncio
import threading
from unittest.mock import patch, AsyncMock, MagicMock
import multiprocessing as mp

import fakeredis
import fakeredis.aioredis

from backend.utils.redis_client import (
    set_test_redis_client,
    set_test_sync_redis_client,
    get_sync_redis_client,
    close_redis_pool,
    RedisSessionOwnershipError,
)
from backend.utils.session_state import (
    get_session_state,
    save_session_state_cas,
    begin_search_generation,
    get_search_generation,
    delete_session,
    touch_session_state,
    claim_next_pagination_page,
    set_candidate_search_criteria,
    get_candidate_search_criteria,
    to_redis_key,
    make_composite_key,
)
from backend.schemas.search_criteria import SearchCriteria
from redis.exceptions import WatchError
from backend.utils.conversation_history import (
    append_turn_atomic_async,
    get_conversation_history_snapshot_async,
    clear_conversation_history_async,
    ConversationHistorySnapshot,
    TurnCommitResult,
)
from backend.main import lifespan, app


# ---------------------------------------------------------------------------
# Redis 7 Availability Check for Multi-Process Tests
# ---------------------------------------------------------------------------

def is_live_redis_available(host: str = "127.0.0.1", port: int = 6379, timeout: float = 0.3) -> bool:
    """Quick non-blocking probe to check if a real Redis server is listening."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        result = s.connect_ex((host, port))
        s.close()
        return result == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Multiprocessing Worker Entry Points (Module-level for Windows spawn support)
# ---------------------------------------------------------------------------

def _mp_worker_ownership(session_id: str, user_id: str, out_q: mp.Queue):
    """Worker process that attempts to write to a session."""
    try:
        from backend.utils.session_state import save_session_state_cas
        ok = save_session_state_cas(
            session_id=session_id,
            state_dict={"data": f"written_by_{user_id}"},
            user_id=user_id,
        )
        out_q.put({"status": "ok", "result": ok})
    except Exception as exc:
        out_q.put({"status": "error", "type": type(exc).__name__, "message": str(exc)})


def _mp_worker_begin_search_gen(session_id: str, user_id: str, out_q: mp.Queue):
    """Worker process that calls begin_search_generation."""
    try:
        from backend.utils.session_state import begin_search_generation
        gen = begin_search_generation(session_id=session_id, user_id=user_id)
        out_q.put({"status": "ok", "generation": gen})
    except Exception as exc:
        out_q.put({"status": "error", "type": type(exc).__name__, "message": str(exc)})


def _mp_worker_append_turn(session_id: str, user_id: str, turn_num: int, out_q: mp.Queue):
    """Worker process that appends a conversation turn."""
    try:
        import asyncio
        from backend.utils.conversation_history import append_turn_atomic_async

        async def _do():
            return await append_turn_atomic_async(
                user_id=user_id,
                session_id=session_id,
                turn_id=f"turn_{turn_num}",
                user_message=f"Worker msg {turn_num}",
                assistant_message=f"Worker reply {turn_num}",
            )

        res = asyncio.run(_do())
        out_q.put({"status": "ok", "success": res.success, "turn_seq": res.turn_seq})
    except Exception as exc:
        out_q.put({"status": "error", "type": type(exc).__name__, "message": str(exc)})


def _mp_worker_restart_sim(session_id: str, user_id: str, out_q: mp.Queue):
    """Worker process that performs an operation, closes its pool, and exits."""
    try:
        import asyncio
        from backend.utils.session_state import touch_session_state
        from backend.utils.redis_client import close_redis_pool

        touched = touch_session_state(session_id=session_id, user_id=user_id)
        asyncio.run(close_redis_pool())
        out_q.put({"status": "ok", "touched": touched})
    except Exception as exc:
        out_q.put({"status": "error", "type": type(exc).__name__, "message": str(exc)})


# ---------------------------------------------------------------------------
# Section 1: Lifespan Shutdown & Redis Connection Pool Cleanup Tests
# ---------------------------------------------------------------------------

def test_lifespan_closes_redis_pool_normal():
    """Verify that lifespan shutdown cleanly invokes close_redis_pool()."""
    async def _run():
        with patch("backend.utils.redis_client.close_redis_pool", new_callable=AsyncMock) as mock_close:
            async with lifespan(app):
                pass  # Simulates application running
            mock_close.assert_awaited_once()

    asyncio.run(_run())


def test_lifespan_closes_redis_pool_on_shutdown_error():
    """Verify that lifespan shutdown invokes close_redis_pool() even when earlier cleanup raises."""
    async def _run():
        with patch("backend.services.job_match_client.close_job_match_client", side_effect=RuntimeError("JobMatch pool failed")):
            with patch("backend.utils.redis_client.close_redis_pool", new_callable=AsyncMock) as mock_close:
                async with lifespan(app):
                    pass
                # Redis pool close MUST be awaited via finally block despite preceding error
                mock_close.assert_awaited_once()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Section 2: CAS Concurrency & Remerge Tests (In-Process Fakeredis)
# ---------------------------------------------------------------------------

@pytest.fixture
def fakeredis_setup():
    """Sets up a shared in-memory fakeredis instance for testing."""
    server = fakeredis.FakeServer()
    fake_sync = fakeredis.FakeRedis(server=server, decode_responses=True)
    fake_async = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    set_test_sync_redis_client(fake_sync)
    set_test_redis_client(fake_async)
    yield server, fake_sync, fake_async
    set_test_sync_redis_client(None)
    set_test_redis_client(None)


def test_begin_search_generation_remerge_on_cas_conflict(fakeredis_setup):
    """
    Verify that begin_search_generation handles a deterministic WatchError collision,
    triggers _remerge_gen against the latest competing Redis update (search_gen=5, shift="night"),
    advances the generation to 6, preserves the concurrent filter, and commits successfully without duplicate increments.
    """
    server, fake_sync, _ = fakeredis_setup
    sid = "test_cas_remerge_session"
    uid = "test_user_100"

    # Step 1: Initial state with search_gen = 2, role = nurse
    save_session_state_cas(
        session_id=sid,
        state_dict={"search_gen": 2, "filters": {"role": "nurse"}},
        user_id=uid,
    )

    rkey = to_redis_key(make_composite_key(sid, user_id=uid), user_id=uid, session_id=sid)
    original_pipeline = fake_sync.pipeline
    attempt_count = [0]

    def _competing_pipeline(*args, **kwargs):
        pipe = original_pipeline(*args, **kwargs)
        real_execute = pipe.execute

        def _execute_hook():
            attempt_count[0] += 1
            if attempt_count[0] == 1:
                # Competing worker commits search_gen = 5 and shift = "night" directly to Redis
                competing_state = {
                    "search_gen": 5,
                    "filters": {"role": "nurse", "shift": "night"},
                    "version_seq": 2,
                    "user_id": str(uid),
                    "session_id": sid,
                }
                import json
                fake_sync.set(rkey, json.dumps(competing_state))
                raise WatchError("Simulated concurrent commit by competing worker")
            return real_execute()

        pipe.execute = _execute_hook
        return pipe

    fake_sync.pipeline = _competing_pipeline

    # Step 2: Call begin_search_generation
    gen = begin_search_generation(session_id=sid, user_id=uid)

    assert attempt_count[0] >= 2, "Retry loop was not triggered"
    assert gen == 6, f"Expected generation 6 after remerge, got {gen}"

    # Step 3: Verify state in Redis
    final_state = get_session_state(session_id=sid, user_id=uid)
    assert final_state.get("search_gen") == 6
    assert final_state.get("filters") == {"role": "nurse", "shift": "night"}


def test_claim_next_pagination_page_remerge_on_cas_conflict(fakeredis_setup):
    """
    Verify that claim_next_pagination_page handles a deterministic WatchError collision,
    remerges against the latest pagination state from another worker (e.g. current_page=2, shift="night"),
    advances the page to 3, preserves concurrent fields, and confirms successful commit.
    """
    server, fake_sync, _ = fakeredis_setup
    sid = "test_pag_cas_remerge_session"
    uid = "test_user_pag"

    # Step 1: Initial state with current_page = 1, total_pages = 5, search_gen = 1
    save_session_state_cas(
        session_id=sid,
        state_dict={
            "search_gen": 1,
            "filters": {"role": "doctor"},
            "pagination": {
                "current_page": 1,
                "total_pages": 5,
                "search_gen": 1,
                "has_next": True,
                "has_prev": False,
            }
        },
        user_id=uid,
    )

    rkey = to_redis_key(make_composite_key(sid, user_id=uid), user_id=uid, session_id=sid)
    original_pipeline = fake_sync.pipeline
    attempt_count = [0]

    def _competing_pipeline(*args, **kwargs):
        pipe = original_pipeline(*args, **kwargs)
        real_execute = pipe.execute

        def _execute_hook():
            attempt_count[0] += 1
            if attempt_count[0] == 1:
                # Competing worker advanced to page 2 and added an unrelated filter
                competing_state = {
                    "search_gen": 1,
                    "filters": {"role": "doctor", "department": "surgery"},
                    "pagination": {
                        "current_page": 2,
                        "total_pages": 5,
                        "search_gen": 1,
                        "has_next": True,
                        "has_prev": True,
                    },
                    "version_seq": 2,
                    "user_id": str(uid),
                    "session_id": sid,
                }
                import json
                fake_sync.set(rkey, json.dumps(competing_state))
                raise WatchError("Simulated concurrent pagination commit")
            return real_execute()

        pipe.execute = _execute_hook
        return pipe

    fake_sync.pipeline = _competing_pipeline

    # Step 2: Call claim_next_pagination_page with "next"
    claim_res = claim_next_pagination_page(
        session_id=sid,
        command="next",
        user_id=uid,
    )

    assert attempt_count[0] >= 2, "Retry loop was not triggered"
    assert claim_res.get("success") is True
    # The page should advance against the latest state (2 + 1 = 3)
    assert claim_res.get("claimed_page") == 3
    assert claim_res.get("saved_p_state", {}).get("current_page") == 3

    # Step 3: Verify persisted state in Redis
    final_state = get_session_state(session_id=sid, user_id=uid)
    assert final_state.get("pagination", {}).get("current_page") == 3
    # Verify unrelated concurrent filter was preserved
    assert final_state.get("filters") == {"role": "doctor", "department": "surgery"}


def test_claim_next_pagination_page_exhaustion_fails_gracefully(fakeredis_setup):
    """
    Verify that when CAS retries are completely exhausted, claim_next_pagination_page
    returns success=False and does NOT falsely report success.
    """
    server, fake_sync, _ = fakeredis_setup
    sid = "test_pag_exhaust_session"
    uid = "test_user_exhaust"

    save_session_state_cas(
        session_id=sid,
        state_dict={
            "search_gen": 1,
            "pagination": {"current_page": 1, "total_pages": 5, "search_gen": 1}
        },
        user_id=uid,
    )

    original_pipeline = fake_sync.pipeline

    def _always_failing_pipeline(*args, **kwargs):
        pipe = original_pipeline(*args, **kwargs)
        def _execute_hook():
            raise WatchError("Continuous collision")
        pipe.execute = _execute_hook
        return pipe

    fake_sync.pipeline = _always_failing_pipeline

    res = claim_next_pagination_page(session_id=sid, command="next", user_id=uid)
    assert res.get("success") is False
    assert "error" in res and res["error"] is not None


def test_set_candidate_search_criteria_remerge_on_cas_conflict(fakeredis_setup):
    """
    Verify that set_candidate_search_criteria handles a deterministic WatchError collision,
    remerges against concurrent Redis state updates, preserves unrelated fields,
    and successfully persists the candidate criteria.
    """
    server, fake_sync, _ = fakeredis_setup
    sid = "test_criteria_cas_session"
    uid = "test_user_criteria"

    # Step 1: Initial session state with an unrelated field
    save_session_state_cas(
        session_id=sid,
        state_dict={"language_preference": "english", "candidate_search_context": {}},
        user_id=uid,
    )

    rkey = to_redis_key(make_composite_key(sid, user_id=uid), user_id=uid, session_id=sid)
    original_pipeline = fake_sync.pipeline
    attempt_count = [0]

    def _competing_pipeline(*args, **kwargs):
        pipe = original_pipeline(*args, **kwargs)
        real_execute = pipe.execute

        def _execute_hook():
            attempt_count[0] += 1
            if attempt_count[0] == 1:
                # Competing worker adds a search filter directly to Redis
                competing_state = {
                    "language_preference": "hindi",
                    "concurrent_marker": "worker_2",
                    "version_seq": 2,
                    "user_id": str(uid),
                    "session_id": sid,
                }
                import json
                fake_sync.set(rkey, json.dumps(competing_state))
                raise WatchError("Simulated concurrent criteria commit")
            return real_execute()

        pipe.execute = _execute_hook
        return pipe

    fake_sync.pipeline = _competing_pipeline

    # Step 2: Call set_candidate_search_criteria with new criteria
    crit = SearchCriteria(title="Cardiologist", location="Mumbai")
    set_candidate_search_criteria(session_id=sid, criteria=crit, user_id=uid)

    assert attempt_count[0] >= 2, "Retry loop was not triggered"

    # Step 3: Verify persisted state in Redis
    final_state = get_session_state(session_id=sid, user_id=uid)
    saved_crit = final_state.get("candidate_search_context", {})
    assert saved_crit.get("title") == "Cardiologist"
    assert saved_crit.get("location") == "Mumbai"
    # Unrelated concurrent fields must survive
    assert final_state.get("language_preference") == "hindi"
    assert final_state.get("concurrent_marker") == "worker_2"


def test_begin_search_generation_concurrent_threads(fakeredis_setup):
    """
    Verify that multiple concurrent calls to begin_search_generation all succeed
    and produce monotonically increasing generations without lost updates.
    """
    server, fake_sync, _ = fakeredis_setup
    sid = "test_cas_thread_session"
    uid = "test_user_threads"

    save_session_state_cas(
        session_id=sid,
        state_dict={"search_gen": 0},
        user_id=uid,
    )

    results = []
    errors = []

    def _worker():
        try:
            g = begin_search_generation(session_id=sid, user_id=uid)
            results.append(g)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=_worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Encountered errors: {errors}"
    assert len(results) == 5
    # All returned generations should be distinct positive integers up to 5
    assert sorted(results) == [1, 2, 3, 4, 5]
    final_state = get_session_state(session_id=sid, user_id=uid)
    assert final_state.get("search_gen") == 5


# ---------------------------------------------------------------------------
# Section 3: Dead Legacy State & Docker Config Verification
# ---------------------------------------------------------------------------

def test_dead_legacy_state_deprecation():
    """Verify that EMPLOYER_SESSIONS in ranking_service is annotated as deprecated."""
    from backend.services.ranking_service import EMPLOYER_SESSIONS, get_employer_session
    assert isinstance(EMPLOYER_SESSIONS, dict)
    assert "DEPRECATED" in (get_employer_session.__doc__ or "")


def test_docker_compose_syntax():
    """Verify docker-compose.yml does not contain typo on line 1 and is valid YAML."""
    import yaml
    compose_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "docker-compose.yml")
    assert os.path.exists(compose_path)
    with open(compose_path, "r", encoding="utf-8") as f:
        content = f.read()
    assert not content.startswith("1version")
    parsed = yaml.safe_load(content)
    assert "version" in parsed
    assert "services" in parsed


# ---------------------------------------------------------------------------
# Section 4: Genuine Multi-Process Tests (Live Redis 7 or Skip)
# ---------------------------------------------------------------------------

LIVE_REDIS = is_live_redis_available()
SKIP_REASON = "Live Redis 7 is offline; skipping multi-process cross-worker test (Deployment Gate)"


@pytest.mark.skipif(not LIVE_REDIS, reason=SKIP_REASON)
def test_cross_process_session_state_isolation():
    """
    Worker A writes session S1 with user_id U1.
    Worker B (different process) attempts to write to S1 with user_id U2.
    Worker B must be rejected with RedisSessionOwnershipError.
    """
    sid = f"mp_iso_{int(time.time())}"
    uid_a = "user_alpha"
    uid_b = "user_beta"

    q_a = mp.Queue()
    q_b = mp.Queue()

    # Worker A creates the session
    p_a = mp.Process(target=_mp_worker_ownership, args=(sid, uid_a, q_a))
    p_a.start()
    p_a.join(timeout=5)
    res_a = q_a.get(timeout=2)
    assert res_a.get("status") == "ok"

    # Worker B tries to overwrite with different user_id
    p_b = mp.Process(target=_mp_worker_ownership, args=(sid, uid_b, q_b))
    p_b.start()
    p_b.join(timeout=5)
    res_b = q_b.get(timeout=2)

    assert res_b.get("status") == "error"
    assert "Ownership" in res_b.get("type", "") or "Ownership" in res_b.get("message", "")


@pytest.mark.skipif(not LIVE_REDIS, reason=SKIP_REASON)
def test_cross_process_concurrent_cas_search_gen():
    """
    Worker A and Worker B simultaneously invoke begin_search_generation on the same session.
    Both should succeed with distinct generation numbers.
    """
    sid = f"mp_cas_{int(time.time())}"
    uid = "user_mp_cas"

    # Seed session
    from backend.utils.session_state import save_session_state_cas
    save_session_state_cas(session_id=sid, state_dict={"search_gen": 0}, user_id=uid)

    q_a = mp.Queue()
    q_b = mp.Queue()

    p_a = mp.Process(target=_mp_worker_begin_search_gen, args=(sid, uid, q_a))
    p_b = mp.Process(target=_mp_worker_begin_search_gen, args=(sid, uid, q_b))

    p_a.start()
    p_b.start()
    p_a.join(timeout=5)
    p_b.join(timeout=5)

    res_a = q_a.get(timeout=2)
    res_b = q_b.get(timeout=2)

    assert res_a.get("status") == "ok"
    assert res_b.get("status") == "ok"
    gens = sorted([res_a["generation"], res_b["generation"]])
    assert gens == [1, 2]


@pytest.mark.skipif(not LIVE_REDIS, reason=SKIP_REASON)
def test_cross_process_conversation_history_monotonicity():
    """
    Worker A and Worker B append turns alternately.
    Sequence numbers must remain monotonically increasing and turns preserved.
    """
    sid = f"mp_hist_{int(time.time())}"
    uid = "user_mp_hist"

    q1 = mp.Queue()
    q2 = mp.Queue()

    p1 = mp.Process(target=_mp_worker_append_turn, args=(sid, uid, 1, q1))
    p1.start()
    p1.join(timeout=5)
    res1 = q1.get(timeout=2)
    assert res1.get("status") == "ok"

    p2 = mp.Process(target=_mp_worker_append_turn, args=(sid, uid, 2, q2))
    p2.start()
    p2.join(timeout=5)
    res2 = q2.get(timeout=2)
    assert res2.get("status") == "ok"

    # Main process inspects snapshot
    async def _check():
        return await get_conversation_history_snapshot_async(user_id=uid, session_id=sid)

    snap = asyncio.run(_check())
    assert len(snap.history) == 4  # 2 turns * 2 messages
    assert snap.turn_seq == 2
    assert snap.history[0]["content"] == "Worker msg 1"
    assert snap.history[2]["content"] == "Worker msg 2"


@pytest.mark.skipif(not LIVE_REDIS, reason=SKIP_REASON)
def test_cross_process_worker_restart_pool_cleanup():
    """
    Worker A performs a session touch, shuts down by closing its Redis pool, and exits.
    Worker B performs an operation immediately afterward, verifying that Worker A's
    pool closure does not impact Worker B.
    """
    sid = f"mp_restart_{int(time.time())}"
    uid = "user_mp_restart"

    q_a = mp.Queue()
    q_b = mp.Queue()

    p_a = mp.Process(target=_mp_worker_restart_sim, args=(sid, uid, q_a))
    p_a.start()
    p_a.join(timeout=5)
    res_a = q_a.get(timeout=2)
    assert res_a.get("status") == "ok"

    # Worker B runs after Worker A has closed its pool and terminated
    p_b = mp.Process(target=_mp_worker_ownership, args=(sid, uid, q_b))
    p_b.start()
    p_b.join(timeout=5)
    res_b = q_b.get(timeout=2)
    assert res_b.get("status") == "ok"
