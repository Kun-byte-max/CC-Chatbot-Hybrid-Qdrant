"""
test_search_client_pooling.py — Targeted test suite for Search HTTP Connection Pooling & Event-Loop Safety.
Phase 7 verification suite.
"""

import asyncio
import threading
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import httpx

import backend.services.search_client as sc
from backend.services.search_client import (
    init_search_client,
    close_search_client,
    set_search_client,
    get_search_client,
    get_search_client_loop,
    _make_request,
    search_jobs,
    search_users,
)


@pytest.fixture(autouse=True)
def cleanup_client():
    """Ensure every test starts and ends with a clean search_client state."""
    asyncio.run(close_search_client())
    yield
    asyncio.run(close_search_client())


def test_1_repeated_requests_on_application_loop_reuse_same_client():
    """1. Repeated requests on the application loop reuse the same client."""
    async def _test():
        client = init_search_client()
        assert client is not None
        assert get_search_client() is client

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": True, "data": [{"id": 1}], "total": 1}

        with patch.object(client, "get", new=AsyncMock(return_value=mock_resp)) as mock_get:
            res1 = await sc.search_jobs(keyword="python")
            res2 = await sc.search_jobs(keyword="fastapi")

            assert res1["success"] is True
            assert res2["success"] is True
            assert mock_get.call_count == 2
            # Verify the exact same client instance handled both calls
            assert get_search_client() is client
        await close_search_client()

    asyncio.run(_test())


def test_2_application_managed_client_closed_during_lifespan_shutdown():
    """2. The application-managed client is closed during lifespan shutdown."""
    async def _test():
        from backend.main import lifespan, app

        # Run lifespan as async context manager
        async with lifespan(app):
            client = get_search_client()
            assert client is not None
            assert not client.is_closed

        # After lifespan exit, client must be closed and state reset to None
        assert client.is_closed
        assert get_search_client() is None

    asyncio.run(_test())


def test_3_call_from_different_event_loop_does_not_reuse_application_loop_client():
    """3. A call from a different event loop does not reuse the application-loop client."""
    app_client_ref = None

    async def _init_on_loop1():
        nonlocal app_client_ref
        app_client_ref = init_search_client()
        return app_client_ref

    # Initialize on loop 1
    loop1_client = asyncio.run(_init_on_loop1())
    assert loop1_client is not None

    loop2_used_client = None

    # Call search_jobs on loop 2 in a separate thread
    def _run_in_thread():
        async def _test_on_loop2():
            nonlocal loop2_used_client
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"success": True, "data": []}

            # Intercept client used
            original_init = httpx.AsyncClient.__init__
            created_instances = []

            def tracked_init(self, *args, **kwargs):
                created_instances.append(self)
                original_init(self, *args, **kwargs)

            with patch.object(httpx.AsyncClient, "__init__", tracked_init), \
                 patch.object(httpx.AsyncClient, "get", new=AsyncMock(return_value=mock_resp)):
                await sc.search_jobs(keyword="loop2_call")
                if created_instances:
                    loop2_used_client = created_instances[-1]

        asyncio.run(_test_on_loop2())

    t = threading.Thread(target=_run_in_thread)
    t.start()
    t.join()

    # Crucial assertion: Loop 2 must NOT have used loop 1's client
    assert loop2_used_client is not None
    assert loop2_used_client is not loop1_client
    # And loop1_client must remain open
    assert not loop1_client.is_closed

    # Cleanup loop 1 client
    async def _cleanup():
        await close_search_client()
    asyncio.run(_cleanup())


def test_4_transient_clients_are_closed_after_completion():
    """4. Transient clients are closed after completion."""
    async def _test():
        # Ensure no app client is set
        set_search_client(None)
        assert get_search_client() is None

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": True, "data": []}

        transient_instance = None
        original_aexit = httpx.AsyncClient.__aexit__
        aexit_called = False

        async def tracked_aexit(self, *args, **kwargs):
            nonlocal aexit_called, transient_instance
            aexit_called = True
            transient_instance = self
            return await original_aexit(self, *args, **kwargs)

        with patch.object(httpx.AsyncClient, "__aexit__", tracked_aexit), \
             patch.object(httpx.AsyncClient, "get", new=AsyncMock(return_value=mock_resp)):
            res = await sc.search_jobs(keyword="transient_test")
            assert res["success"] is True

        assert aexit_called is True
        assert transient_instance is not None
        assert transient_instance.is_closed

    asyncio.run(_test())


def test_5_existing_timeout_and_error_handling_remain_unchanged():
    """5. Existing timeout and error handling remain unchanged."""
    async def _test():
        client = init_search_client()

        # 5A: HTTP 500 error response returns success=False
        mock_500 = MagicMock()
        mock_500.status_code = 500
        mock_500.text = "Internal Server Error"

        with patch.object(client, "get", new=AsyncMock(return_value=mock_500)):
            res = await sc.search_jobs(keyword="err500")
            assert res == {"success": False, "data": [], "total": 0}

        # 5B: TimeoutException returns safe fallback structure
        with patch.object(client, "get", new=AsyncMock(side_effect=httpx.TimeoutException("Timeout"))):
            res = await sc.search_jobs(keyword="timeout_test")
            assert res == {"success": False, "data": [], "total": 0}

        # 5C: ConnectError returns safe fallback structure
        with patch.object(client, "get", new=AsyncMock(side_effect=httpx.ConnectError("Connection refused"))):
            res = await sc.search_jobs(keyword="conn_err")
            assert res == {"success": False, "data": [], "total": 0}

        await close_search_client()

    asyncio.run(_test())


def test_6_existing_query_parameters_and_headers_remain_unchanged():
    """6. Existing query parameters and headers remain unchanged."""
    async def _test():
        client = init_search_client()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": True, "data": [], "total": 0, "has_more": False}

        with patch.object(client, "get", new=AsyncMock(return_value=mock_resp)) as mock_get:
            # Pass both valid and None params
            await sc.search_jobs(keyword="python", location="Delhi", empty_param=None)

            assert mock_get.called is True
            call_kwargs = mock_get.call_args.kwargs
            # None params must be stripped
            assert "empty_param" not in call_kwargs["params"]
            assert call_kwargs["params"]["keyword"] == "python"
            assert call_kwargs["params"]["location"] == "Delhi"

            # Headers check
            headers = call_kwargs["headers"]
            assert "X-API-Key" in headers
            assert "Authorization" in headers
            assert headers["Authorization"].startswith("Bearer")

        await close_search_client()

    asyncio.run(_test())


def test_7_connection_limits_are_configured_as_intended():
    """7. Connection limits are configured as intended."""
    async def _test():
        client = init_search_client(
            timeout=10.0,
            max_connections=30,
            max_keepalive_connections=20,
            keepalive_expiry=30.0,
        )
        assert client.timeout.read == 10.0
        # Inspect pool configuration directly on transport pool
        pool = client._transport._pool
        assert pool._max_connections == 30
        assert pool._max_keepalive_connections == 20
        assert pool._keepalive_expiry == 30.0
        await close_search_client()

    asyncio.run(_test())


def test_8_tests_can_inject_and_reset_client_without_contamination():
    """8. Tests can inject/reset the client without cross-test contamination."""
    async def _test():
        custom_mock_client = MagicMock(spec=httpx.AsyncClient)
        custom_mock_client.is_closed = False

        set_search_client(custom_mock_client)
        assert get_search_client() is custom_mock_client

        set_search_client(None)
        assert get_search_client() is None

    asyncio.run(_test())


def test_9_existing_search_client_mocks_continue_to_work():
    """9. Existing search-client mocks continue to work."""
    async def _test():
        # 9A: Patching search_jobs at module namespace
        with patch("backend.services.search_client.search_jobs", new=AsyncMock(return_value={"success": True, "data": [{"id": 999}], "total": 1})):
            res = await sc.search_jobs(keyword="test")
            assert res["success"] is True
            assert res["data"][0]["id"] == 999

        # 9B: Patching httpx.AsyncClient.get (as in test_job_match_integration.py)
        set_search_client(None)  # uses transient client
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": True, "data": [{"id": 777}], "total": 1}

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            res = await sc.search_jobs(keyword="test_mock_get")
            assert res["success"] is True
            assert res["data"][0]["id"] == 777

    asyncio.run(_test())
