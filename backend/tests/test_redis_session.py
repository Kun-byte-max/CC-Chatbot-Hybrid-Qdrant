import time
import asyncio
import pytest
import fakeredis
from unittest.mock import patch, MagicMock

from backend.utils.redis_client import (
    set_test_redis_client,
    set_test_sync_redis_client,
    get_sync_redis_client,
    RedisUnavailableError,
    RedisSessionOwnershipError,
    RedisSessionPayloadLimitError,
    RedisSessionOOMError,
)
from backend.utils.session_state import (
    get_session_state,
    get_session_state_async,
    save_session_state_cas,
    save_session_state_cas_async,
    touch_session_state,
    touch_session_state_async,
    delete_session,
    delete_session_async,
    set_search_context,
    get_search_context,
    set_pagination_state,
    get_pagination_state,
    clear_pagination_state,
    set_recommendation_session,
    get_recommendation_session,
    get_conversation_context,
    make_composite_key,
    to_redis_key,
    resolve_effective_user_id,
    is_valid_session_id,
    _SESSION_STATES,
    SESSION_TTL_SECONDS,
)
from backend.schemas.schemas import ChatRequest


@pytest.fixture(autouse=True)
def setup_fakeredis():
    """Sets up a shared in-memory fakeredis instance for sync and async operations."""
    import fakeredis.aioredis
    server = fakeredis.FakeServer()
    fake_sync = fakeredis.FakeRedis(server=server, decode_responses=True)
    fake_async = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    set_test_redis_client(fake_async)
    set_test_sync_redis_client(fake_sync)
    _SESSION_STATES.clear()
    yield fake_sync
    set_test_redis_client(None)
    set_test_sync_redis_client(None)
    _SESSION_STATES.clear()


# 1. Create a new session
def test_create_new_session():
    sess_id = f"sess_new_{time.time()}"
    state = get_session_state(sess_id, role="employee", user_id=101)
    assert state["session_id"] == sess_id
    assert state["user_id"] == "101"
    assert state["role"] == "employee"
    assert state["version_seq"] == 1
    assert state["search_context"] is None


# 2. Read an existing session
def test_read_existing_session():
    sess_id = f"sess_read_{time.time()}"
    initial_payload = {
        "search_context": {"keyword": "backend developer", "location": "Bangalore"},
        "language_preference": "english",
    }
    saved = save_session_state_cas(sess_id, initial_payload, role="employee", user_id=101)
    assert saved is True

    loaded = get_session_state(sess_id, role="employee", user_id=101)
    assert loaded["search_context"] == initial_payload["search_context"]
    assert loaded["language_preference"] == "english"
    assert loaded["user_id"] == "101"


# 3. Update search context
def test_update_search_context():
    sess_id = f"sess_search_{time.time()}"
    set_search_context(sess_id, {"title": "Python Engineer"}, role="employee", user_id=101)
    ctx = get_search_context(sess_id, role="employee", user_id=101)
    assert ctx == {"title": "Python Engineer"}


# 4. Update pagination
def test_update_pagination():
    sess_id = f"sess_pag_{time.time()}"
    pag_data = {
        "current_page": 2,
        "total_pages": 5,
        "total_results": 25,
        "query_fingerprint": "fingerprint_123",
    }
    set_pagination_state(sess_id, pag_data, role="employee", user_id=101)
    loaded_pag = get_pagination_state(sess_id, role="employee", user_id=101)
    assert loaded_pag["current_page"] == 2
    assert loaded_pag["total_pages"] == 5

    clear_pagination_state(sess_id, role="employee", user_id=101)
    cleared = get_pagination_state(sess_id, role="employee", user_id=101)
    assert cleared is None


# 5. Update recommendation context
def test_update_recommendation_context():
    sess_id = f"sess_rec_{time.time()}"
    jobs = [{"job_id": 1, "title": "Dev 1"}, {"job_id": 2, "title": "Dev 2"}]
    set_recommendation_session(
        session_id=sess_id,
        query_key="python dev",
        ranked_jobs=jobs,
        current_page=1,
        page_size=5,
        total_jobs=2,
        role="employee",
        user_id=101,
    )
    rec = get_recommendation_session(sess_id, role="employee", user_id=101)
    assert rec is not None
    assert rec["total_jobs"] == 2
    assert len(rec["ranked_jobs"]) == 2


# 6. Delete a session
def test_delete_session():
    sess_id = f"sess_del_{time.time()}"
    save_session_state_cas(sess_id, {"search_context": {"data": "test"}}, role="employee", user_id=101)
    deleted = delete_session(sess_id, role="employee", user_id=101)
    assert deleted is True

    # After deletion, state returns empty new session
    state = get_session_state(sess_id, role="employee", user_id=101)
    assert state.get("search_context") is None


# 7. Verify TTL creation and refresh
def test_verify_ttl_creation_and_refresh(setup_fakeredis):
    client = setup_fakeredis
    sess_id = f"sess_ttl_{time.time()}"
    save_session_state_cas(sess_id, {"search_context": {"val": 1}}, role="employee", user_id=101)
    key = make_composite_key(sess_id, role="employee", user_id=101)
    rkey = to_redis_key(key, user_id=101, session_id=sess_id)

    ttl = client.ttl(rkey)
    assert ttl > 0
    assert ttl <= SESSION_TTL_SECONDS

    # Touch session refreshes TTL
    touched = touch_session_state(sess_id, role="employee", user_id=101)
    assert touched is True
    assert client.ttl(rkey) > 0


# 8. Verify authenticated user isolation
def test_authenticated_user_isolation():
    sess_id = "shared_session_uuid"
    save_session_state_cas(sess_id, {"search_context": {"user": "alice"}}, role="employee", user_id=101)
    save_session_state_cas(sess_id, {"search_context": {"user": "bob"}}, role="employee", user_id=202)

    alice_state = get_session_state(sess_id, role="employee", user_id=101)
    bob_state = get_session_state(sess_id, role="employee", user_id=202)

    assert alice_state["search_context"]["user"] == "alice"
    assert bob_state["search_context"]["user"] == "bob"

    # User 202 attempting to access Alice's key directly must be rejected
    alice_key = make_composite_key(sess_id, role="employee", user_id=101)
    with pytest.raises(RedisSessionOwnershipError):
        get_session_state(alice_key, role="employee", user_id=202)


# 9. Verify independent sessions belonging to the same user
def test_independent_sessions_same_user():
    user_id = 999
    sess_a = "conversation_turn_a"
    sess_b = "conversation_turn_b"

    save_session_state_cas(sess_a, {"search_context": {"topic": "ai"}}, role="employee", user_id=user_id)
    save_session_state_cas(sess_b, {"search_context": {"topic": "finance"}}, role="employee", user_id=user_id)

    ctx_a = get_session_state(sess_a, role="employee", user_id=user_id)
    ctx_b = get_session_state(sess_b, role="employee", user_id=user_id)

    assert ctx_a["search_context"]["topic"] == "ai"
    assert ctx_b["search_context"]["topic"] == "finance"


# 10. Verify missing or invalid authenticated identity is rejected
def test_missing_authenticated_identity_rejected():
    anon_sess_1 = "sess_client_anon_1"

    # resolve_effective_user_id must reject None/empty/anon user_id
    with pytest.raises(RedisSessionOwnershipError):
        resolve_effective_user_id(None, anon_sess_1)

    with pytest.raises(RedisSessionOwnershipError):
        resolve_effective_user_id("", anon_sess_1)

    with pytest.raises(RedisSessionOwnershipError):
        resolve_effective_user_id("anon", anon_sess_1)

    # get_session_state must reject missing or anonymous user_id
    with pytest.raises(RedisSessionOwnershipError):
        get_session_state(anon_sess_1, role="employee", user_id=None)

    with pytest.raises(RedisSessionOwnershipError):
        get_session_state(anon_sess_1, role="employee", user_id="anon")

    # save_session_state_cas must reject missing authenticated user_id
    with pytest.raises(RedisSessionOwnershipError):
        save_session_state_cas(anon_sess_1, {"search_context": {"data": 1}}, role="employee", user_id=None)


# 11. Verify concurrent updates (CAS optimistic locking)
def test_concurrent_updates_cas():
    sess_id = f"sess_cas_{time.time()}"
    save_session_state_cas(sess_id, {"search_context": {"turn": 1}}, role="employee", user_id=101)

    state1 = get_session_state(sess_id, role="employee", user_id=101)
    state2 = get_session_state(sess_id, role="employee", user_id=101)

    # Writer 1 commits turn 2
    state1["search_context"] = {"turn": 2, "writer1": True}
    res1 = save_session_state_cas(sess_id, state1, role="employee", user_id=101)
    assert res1 is True

    # Writer 2 attempts commit with stale version, providing remerge_fn
    def remerge_turn(reloaded):
        merged = dict(reloaded)
        ctx = dict(merged.get("search_context") or {})
        ctx["writer2"] = True
        merged["search_context"] = ctx
        return merged

    state2["search_context"] = {"turn": 2, "writer2": True}
    res2 = save_session_state_cas(sess_id, state2, role="employee", remerge_fn=remerge_turn, user_id=101)
    assert res2 is True

    final_state = get_session_state(sess_id, role="employee", user_id=101)
    assert final_state["search_context"].get("writer1") is True
    assert final_state["search_context"].get("writer2") is True
    assert final_state["version_seq"] >= 3


# 12. Verify Redis failure handling (no silent memory/DB fallback)
def test_redis_failure_handling():
    sess_id = f"sess_fail_{time.time()}"
    set_test_redis_client(None)
    set_test_sync_redis_client(None)

    with patch("backend.utils.session_state.get_sync_redis_client") as mock_get_client:
        mock_get_client.side_effect = RedisUnavailableError("Redis is completely down")
        with pytest.raises(RedisUnavailableError):
            get_session_state(sess_id, role="employee", user_id=101)


# 13. Verify that no MySQL session queries execute
def test_zero_mysql_session_queries():
    sess_id = f"sess_no_mysql_{time.time()}"
    # Patch get_db in repositories to verify it is NEVER called
    with patch("backend.repositories.db.get_db") as mock_db:
        mock_db.side_effect = RuntimeError("CRITICAL ERROR: MySQL get_db was called for session operation!")

        # Perform full session lifecycle
        save_session_state_cas(sess_id, {"search_context": {"no_sql": True}}, role="employee", user_id=101)
        state = get_session_state(sess_id, role="employee", user_id=101)
        assert state["search_context"]["no_sql"] is True

        set_pagination_state(sess_id, {"page": 1}, role="employee", user_id=101)
        clear_pagination_state(sess_id, role="employee", user_id=101)
        touch_session_state(sess_id, role="employee", user_id=101)
        delete_session(sess_id, role="employee", user_id=101)

        # Confirm get_db was called exactly 0 times
        assert mock_db.call_count == 0


# 14. Verify no production _SESSION_STATES dependency remains
def test_no_production_session_states_dependency():
    sess_id = f"sess_no_dict_{time.time()}"
    _SESSION_STATES.clear()

    # Save session
    save_session_state_cas(sess_id, {"search_context": {"ram_only": True}}, role="employee", user_id=101)

    # In-memory _SESSION_STATES MUST remain empty because production does not write to it
    assert len(_SESSION_STATES) == 0

    # Redis holds authoritative state
    loaded = get_session_state(sess_id, role="employee", user_id=101)
    assert loaded["search_context"]["ram_only"] is True


# 15. Verify async session read
def test_get_session_state_async():
    sess_id = f"sess_async_read_{time.time()}"
    save_session_state_cas(sess_id, {"search_context": {"title": "Full Stack Dev"}}, role="employee", user_id=101)

    state = asyncio.run(get_session_state_async(sess_id, role="employee", user_id=101))
    assert state["session_id"] == sess_id
    assert state["user_id"] == "101"
    assert state["search_context"]["title"] == "Full Stack Dev"


# 16. Verify async CAS session save
def test_save_session_state_cas_async():
    sess_id = f"sess_async_save_{time.time()}"
    payload = {"search_context": {"keyword": "React"}, "version_seq": 1}

    success = asyncio.run(save_session_state_cas_async(sess_id, payload, role="employee", user_id=101))
    assert success is True

    loaded = asyncio.run(get_session_state_async(sess_id, role="employee", user_id=101))
    assert loaded["search_context"]["keyword"] == "React"
    assert loaded["version_seq"] >= 2


# 17. Verify async session touch
def test_touch_session_state_async(setup_fakeredis):
    client = setup_fakeredis
    sess_id = f"sess_async_touch_{time.time()}"
    save_session_state_cas(sess_id, {"search_context": {"v": 1}}, role="employee", user_id=101)

    touched = asyncio.run(touch_session_state_async(sess_id, role="employee", user_id=101))
    assert touched is True


# 18. Verify async session delete
def test_delete_session_async():
    sess_id = f"sess_async_del_{time.time()}"
    save_session_state_cas(sess_id, {"search_context": {"v": 1}}, role="employee", user_id=101)

    deleted = asyncio.run(delete_session_async(sess_id, role="employee", user_id=101))
    assert deleted is True

    state = asyncio.run(get_session_state_async(sess_id, role="employee", user_id=101))
    assert state.get("search_context") is None


# 19. Verify async Redis failure handling
def test_async_redis_failure_handling():
    sess_id = f"sess_async_fail_{time.time()}"
    set_test_redis_client(None)

    with patch("backend.utils.session_state.get_async_redis_client") as mock_async_client:
        mock_async_client.side_effect = RedisUnavailableError("Async Redis down")
        with pytest.raises(RedisUnavailableError):
            asyncio.run(get_session_state_async(sess_id, role="employee", user_id=101))


# 20. Verify Redis timeout handling
def test_redis_timeout_handling():
    sess_id = f"sess_timeout_{time.time()}"

    with patch("backend.utils.session_state.get_sync_redis_client") as mock_sync_client:
        mock_instance = MagicMock()
        mock_instance.get.side_effect = TimeoutError("Redis connection timed out")
        mock_sync_client.return_value = mock_instance

        with pytest.raises(RedisUnavailableError) as exc_info:
            get_session_state(sess_id, role="employee", user_id=101)
        assert "Redis unavailable" in str(exc_info.value)


# 21. Verify Redis OOM handling
def test_redis_oom_handling():
    sess_id = f"sess_oom_{time.time()}"

    with patch("backend.utils.session_state.get_sync_redis_client") as mock_sync_client:
        mock_instance = MagicMock()
        import redis.exceptions
        mock_instance.get.side_effect = redis.exceptions.ResponseError("OOM command not allowed when used memory > 'maxmemory'")
        mock_sync_client.return_value = mock_instance

        with pytest.raises(RedisUnavailableError) as exc_info:
            get_session_state(sess_id, role="employee", user_id=101)
        assert "Redis unavailable" in str(exc_info.value)


# 22. Verify invalid session ID formats rejected
def test_invalid_session_id_rejected():
    assert is_valid_session_id("default_session") is False
    assert is_valid_session_id("DEFAULT_SESSION") is False
    assert is_valid_session_id("") is False
    assert is_valid_session_id(None) is False
    assert is_valid_session_id("ab") is False  # length < 3
    assert is_valid_session_id("a" * 129) is False  # length > 128
    assert is_valid_session_id("none") is False
    assert is_valid_session_id("null") is False
    assert is_valid_session_id("undefined") is False
    assert is_valid_session_id("anon") is False
    assert is_valid_session_id("invalid spaces in id") is False
    assert is_valid_session_id("invalid;injection") is False

    # Valid session IDs
    assert is_valid_session_id("sess_valid_12345") is True
    assert is_valid_session_id("101:sess_abc:employee") is True
    assert is_valid_session_id("user-session-uuid-1234") is True


# 23. Verify ChatRequest has no static default_session
def test_no_static_default_session_id():
    req = ChatRequest(messages=[])
    assert req.session_id is None
