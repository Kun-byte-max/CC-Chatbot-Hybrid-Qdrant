"""
Targeted tests for Phase 2B.1 Hardening Implementation:
- Redis infrastructure exception classification (ConnectionError, TimeoutError, OOM)
- CAS contention vs infrastructure failure distinction
- Domain helper propagation of failed writes
- FastAPI HTTP 503 translation
- SSE structured error emission on Redis unavailability
- Async non-blocking offload verification
"""

import pytest
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi import HTTPException
from fastapi.testclient import TestClient

from redis.exceptions import (
    ConnectionError as RedisConnectionError,
    TimeoutError as RedisTimeoutError,
    ResponseError as RedisResponseError,
    WatchError,
)
from backend.utils.redis_client import (
    RedisUnavailableError,
    RedisSessionOOMError,
    RedisSessionOwnershipError,
)
from backend.utils.session_state import (
    save_session_state_cas,
    save_session_state_cas_async,
    touch_session_state,
    touch_session_state_async,
    set_search_context,
    set_pagination_state,
    get_session_state,
)


# ============================================================================
# 1. CAS EXCEPTION CLASSIFICATION TESTS
# ============================================================================

def test_save_session_state_cas_connection_failure():
    """Sync CAS raises RedisUnavailableError on connection failure, never returns False silently."""
    session_id = "test_cas_conn_fail_01"
    with patch("backend.utils.session_state.get_sync_redis_client") as mock_rc:
        mock_rc.return_value.pipeline.side_effect = RedisConnectionError("Connection refused")
        payload = {"role": "employee", "version_seq": 1}
        with pytest.raises(RedisUnavailableError) as exc_info:
            save_session_state_cas(session_id, payload, user_id="usr_100")
        assert "Redis unavailable during CAS write" in str(exc_info.value)


def test_save_session_state_cas_async_connection_failure():
    """Async CAS raises RedisUnavailableError on connection failure."""
    async def _test():
        session_id = "test_cas_conn_fail_async_01"
        with patch("backend.utils.session_state.get_async_redis_client") as mock_rc:
            mock_rc.return_value.pipeline.side_effect = RedisConnectionError("Async connection dropped")
            payload = {"role": "employee", "version_seq": 1}
            with pytest.raises(RedisUnavailableError) as exc_info:
                await save_session_state_cas_async(session_id, payload, user_id="usr_100")
            assert "Redis unavailable during async CAS write" in str(exc_info.value)
    asyncio.run(_test())


def test_save_session_state_cas_timeout():
    """Sync CAS raises RedisUnavailableError on timeout."""
    session_id = "test_cas_timeout_01"
    with patch("backend.utils.session_state.get_sync_redis_client") as mock_rc:
        mock_rc.return_value.pipeline.side_effect = RedisTimeoutError("Socket timeout reading from socket")
        payload = {"role": "employee", "version_seq": 1}
        with pytest.raises(RedisUnavailableError):
            save_session_state_cas(session_id, payload, user_id="usr_100")


def test_save_session_state_cas_async_timeout():
    """Async CAS raises RedisUnavailableError on timeout."""
    async def _test():
        session_id = "test_cas_timeout_async_01"
        with patch("backend.utils.session_state.get_async_redis_client") as mock_rc:
            mock_rc.return_value.pipeline.side_effect = RedisTimeoutError("Async timeout reading from socket")
            payload = {"role": "employee", "version_seq": 1}
            with pytest.raises(RedisUnavailableError):
                await save_session_state_cas_async(session_id, payload, user_id="usr_100")
    asyncio.run(_test())


def test_save_session_state_cas_oom_response():
    """Sync CAS classifies Redis OOM error as RedisSessionOOMError."""
    session_id = "test_cas_oom_01"
    with patch("backend.utils.session_state.get_sync_redis_client") as mock_rc:
        mock_pipe = mock_rc.return_value.pipeline.return_value
        mock_pipe.execute.side_effect = RedisResponseError("OOM command not allowed when used memory > 'maxmemory'")
        payload = {"role": "employee", "version_seq": 1}
        with pytest.raises(RedisSessionOOMError):
            save_session_state_cas(session_id, payload, user_id="usr_100")


def test_save_session_state_cas_async_oom_response():
    """Async CAS classifies Redis OOM error as RedisSessionOOMError."""
    async def _test():
        session_id = "test_cas_oom_async_01"
        with patch("backend.utils.session_state.get_async_redis_client") as mock_rc:
            mock_pipe = AsyncMock()
            mock_pipe.watch = AsyncMock()
            mock_pipe.get = AsyncMock(return_value=None)
            mock_pipe.multi = MagicMock()
            mock_pipe.set = MagicMock()
            mock_pipe.execute = AsyncMock(side_effect=RedisResponseError("OOM command not allowed when used memory > 'maxmemory'"))
            mock_rc.return_value.pipeline.return_value = mock_pipe
            payload = {"role": "employee", "version_seq": 1}
            with pytest.raises(RedisSessionOOMError):
                await save_session_state_cas_async(session_id, payload, user_id="usr_100")
    asyncio.run(_test())


def test_save_session_state_cas_watch_error_contention_exhaustion():
    """WatchError optimistic contention exhausts retries and returns False (normal CAS failure)."""
    session_id = "test_cas_contention_01"
    with patch("backend.utils.session_state.get_sync_redis_client") as mock_rc:
        mock_pipe = mock_rc.return_value.pipeline.return_value
        mock_pipe.execute.side_effect = WatchError("Contention")
        payload = {"role": "employee", "version_seq": 1}
        success = save_session_state_cas(session_id, payload, user_id="usr_100")
        assert success is False
        assert mock_pipe.execute.call_count == 3  # MAX_CAS_RETRIES attempts


# ============================================================================
# 2. TOUCH EXCEPTION CLASSIFICATION TESTS
# ============================================================================

def test_touch_session_state_connection_error():
    """touch_session_state raises RedisUnavailableError on connection failure."""
    session_id = "test_touch_conn_fail_01"
    with patch("backend.utils.session_state.get_sync_redis_client") as mock_rc:
        mock_rc.return_value.get.side_effect = RedisConnectionError("Connection lost")
        with pytest.raises(RedisUnavailableError):
            touch_session_state(session_id, user_id="usr_100")


def test_touch_session_state_async_connection_error():
    """touch_session_state_async raises RedisUnavailableError on connection failure."""
    async def _test():
        session_id = "test_touch_conn_fail_async_01"
        with patch("backend.utils.session_state.get_async_redis_client") as mock_rc:
            mock_rc.return_value.get.side_effect = RedisConnectionError("Async connection lost")
            with pytest.raises(RedisUnavailableError):
                await touch_session_state_async(session_id, user_id="usr_100")
    asyncio.run(_test())


# ============================================================================
# 3. DOMAIN HELPER PROPAGATION TESTS
# ============================================================================

def test_domain_helpers_propagate_redis_failure():
    """Domain helper set_search_context must NOT swallow Redis connection errors."""
    session_id = "test_domain_propagate_01"
    with patch("backend.utils.session_state.get_sync_redis_client") as mock_rc:
        mock_rc.return_value.pipeline.side_effect = RedisConnectionError("Server unreachable")
        with pytest.raises(RedisUnavailableError):
            set_search_context(session_id, {"title": "Python Dev"}, user_id="usr_100")


# ============================================================================
# 4. HTTP 503 & SSE ERROR RESPONSE TESTS
# ============================================================================

def test_chat_endpoint_returns_503_on_redis_unavailable():
    """POST /chat returns HTTP 503 when Redis is unavailable, NOT generic 500."""
    from backend.main import app
    from backend.api.auth import verify_token
    app.dependency_overrides[verify_token] = lambda: {"sub": "100", "id": 100, "role": "employee", "email": "test@test.com"}
    client = TestClient(app, raise_server_exceptions=False)

    try:
        with patch("backend.utils.session_state.touch_session_state_async", side_effect=RedisUnavailableError("Redis cluster unreachable")):
            resp = client.post(
                "/chat",
                json={"messages": [{"role": "user", "content": "Find jobs"}], "session_id": "sess_test_12345"},
                headers={"Authorization": "Bearer mock_token"},
            )
            assert resp.status_code == 503
            assert "Session storage service unavailable" in resp.json().get("detail", "")
    finally:
        app.dependency_overrides.pop(verify_token, None)


def test_chat_stream_endpoint_emits_redis_unavailable_event():
    """POST /chat/stream emits structured SSE error event with REDIS_UNAVAILABLE when Redis is down."""
    from backend.main import app
    from backend.api.auth import verify_token
    app.dependency_overrides[verify_token] = lambda: {"sub": "100", "id": 100, "role": "employee", "email": "test@test.com"}
    client = TestClient(app, raise_server_exceptions=False)

    try:
        with patch("backend.main.execute_chat_pipeline", side_effect=RedisUnavailableError("Redis down")):
            resp = client.post(
                "/chat/stream",
                json={"messages": [{"role": "user", "content": "Find jobs"}], "session_id": "sess_test_12345"},
                headers={"Authorization": "Bearer mock_token"},
            )
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            body_text = resp.text
            assert 'event: error' in body_text
            assert 'REDIS_UNAVAILABLE' in body_text
            assert '"recoverable": true' in body_text or '"recoverable":true' in body_text
    finally:
        app.dependency_overrides.pop(verify_token, None)


# ============================================================================
# 5. ASYNC REQUEST PATH OFF-LOOP VERIFICATION TESTS
# ============================================================================

def test_async_paths_use_threadpool_offload():
    """Verify that handle_query and employee/employer contexts use run_in_threadpool for Redis I/O."""
    import inspect
    import re
    from backend.services import user_data_service
    from backend.api import employee, employer

    # 1. user_data_service.py source inspect
    uds_src = inspect.getsource(user_data_service.handle_query)
    assert re.search(r"run_in_threadpool\s*\(\s*set_pending_requested_level", uds_src)
    assert re.search(r"run_in_threadpool\s*\(\s*get_pending_requested_level", uds_src)
    assert re.search(r"run_in_threadpool\s*\(\s*clear_pending_requested_level", uds_src)
    assert re.search(r"run_in_threadpool\s*\(\s*format_multi_intent_response", uds_src)

    # 2. employee.py source inspect
    emp_src = inspect.getsource(employee.handle_employee_context)
    assert re.search(r"run_in_threadpool\s*\(\s*get_pagination_state", emp_src)
    assert re.search(r"run_in_threadpool\s*\(\s*set_pagination_state", emp_src)
    assert re.search(r"run_in_threadpool\s*\(\s*get_search_context", emp_src)
    assert re.search(r"run_in_threadpool\s*\(\s*update_employee_session_cas", emp_src)
    assert re.search(r"run_in_threadpool\s*\(\s*set_recommendation_session", emp_src)

    # 3. employer.py source inspect
    empr_src = inspect.getsource(employer.handle_employer_context)
    assert re.search(r"run_in_threadpool\s*\(\s*get_pagination_state", empr_src)
    assert re.search(r"run_in_threadpool\s*\(\s*handle_pagination_command", empr_src)
    assert re.search(r"run_in_threadpool\s*\(\s*get_employer_search_context", empr_src)
    assert re.search(r"run_in_threadpool\s*\(\s*update_employer_session_cas", empr_src)
    assert re.search(r"run_in_threadpool\s*\(\s*recommendation_service\.get_paginated_employer_candidate_recommendations", empr_src)
    # Verify all get_employer_search_context calls are passed as first argument to run_in_threadpool
    threadpool_calls = len(re.findall(r"run_in_threadpool\s*\(\s*get_employer_search_context", empr_src))
    assert threadpool_calls == 2


def test_handle_employer_context_runtime_threadpool_execution():
    """Verify runtime offloading of get_employer_search_context and recommendations during handle_employer_context."""
    import fakeredis
    import fakeredis.aioredis
    from backend.utils.redis_client import set_test_redis_client, set_test_sync_redis_client
    from backend.api.employer import handle_employer_context

    server = fakeredis.FakeServer()
    fake_sync = fakeredis.FakeRedis(server=server, decode_responses=True)
    fake_async = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    set_test_redis_client(fake_async)
    set_test_sync_redis_client(fake_sync)

    calls = []
    original_threadpool = __import__("starlette.concurrency", fromlist=["run_in_threadpool"]).run_in_threadpool

    async def spy_threadpool(func, *args, **kwargs):
        calls.append(getattr(func, "__name__", str(func)))
        return await original_threadpool(func, *args, **kwargs)

    async def _test():
        with patch("backend.api.employer.run_in_threadpool", side_effect=spy_threadpool):
            with patch("backend.services.employer_search_executor.execute_employer_candidate_search", new_callable=AsyncMock, return_value=([], MagicMock(), {"raw_hits": [{"id": 1, "name": "Dev"}]})):
                with patch("backend.api.employer.parse_search_query", new_callable=AsyncMock, return_value={"is_search_intent": True, "skills": ["python"]}):
                    res = await handle_employer_context("Hire a Python developer", session_id="sess_emp_test_12345", user_id="usr_emp_01")
                    assert "get_employer_search_context" in calls
                    assert "get_paginated_employer_candidate_recommendations" in calls
                    assert res is not None

    try:
        asyncio.run(_test())
    finally:
        set_test_redis_client(None)
        set_test_sync_redis_client(None)


