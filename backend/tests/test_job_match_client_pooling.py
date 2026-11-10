"""
test_job_match_client_pooling.py — Targeted test suite for JobMatch HTTP Connection Pooling & Event-Loop Safety.
Phase 3 Stage 3.1.1 verification suite.
"""

import asyncio
import threading
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import httpx

import backend.services.job_match_client as jm_mod
from backend.services.job_match_client import (
    init_job_match_client,
    close_job_match_client,
    set_job_match_client,
    get_job_match_client,
    get_job_match_client_loop,
    job_match_client,
    JobMatchClient,
    JobMatchUnavailableException,
    CandidateNotIndexedException,
    JobMatchAuthException,
    JobMatchContractException,
)


@pytest.fixture(autouse=True)
def cleanup_jm_client():
    """Ensure every test starts and ends with a clean job_match_client state."""
    asyncio.run(close_job_match_client())
    yield
    asyncio.run(close_job_match_client())


def test_1_find_jobs_and_match_preview_reuse_pooled_client():
    """1. JobMatch find_jobs() and match_preview() reuse the pooled client across requests."""
    async def _test():
        with patch("backend.services.job_match_client.JOB_MATCH_ENABLED", True):
            client = init_job_match_client()
            assert client is not None
            assert get_job_match_client() is client

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "status": "success",
                "data": [{"job_id": 101, "score": 0.95}],
            }

            with patch.object(client, "get", new=AsyncMock(return_value=mock_resp)) as mock_get:
                res1 = await job_match_client.find_jobs(candidate_id=123)
                res2 = await job_match_client.match_preview(candidate_id=123, job_id=101)

                assert res1["status"] == "success"
                assert res2["status"] == "success"
                assert mock_get.call_count == 2
                # Identical client handled both calls
                assert get_job_match_client() is client

            await close_job_match_client()

    asyncio.run(_test())


def test_2_application_managed_client_closed_during_lifespan_shutdown():
    """2. The application-managed JobMatch client is closed during lifespan shutdown."""
    async def _test():
        from backend.main import lifespan, app

        async with lifespan(app):
            client = get_job_match_client()
            assert client is not None
            assert not client.is_closed

        # After lifespan exit, client must be closed and global state reset to None
        assert client.is_closed
        assert get_job_match_client() is None

    asyncio.run(_test())


def test_3_calls_on_different_event_loop_use_transient_client_safely():
    """3. Calls outside application lifespan and tests using separate event loops do not reuse an invalid client."""
    app_client_ref = None

    async def _init_on_loop1():
        nonlocal app_client_ref
        app_client_ref = init_job_match_client()
        return app_client_ref

    loop1_client = asyncio.run(_init_on_loop1())
    assert loop1_client is not None

    loop2_used_client = None

    def _run_in_thread():
        async def _test_on_loop2():
            nonlocal loop2_used_client
            with patch("backend.services.job_match_client.JOB_MATCH_ENABLED", True):
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {"status": "success", "results": []}

                created_instances = []
                original_init = httpx.AsyncClient.__init__

                def tracked_init(self, *args, **kwargs):
                    created_instances.append(self)
                    original_init(self, *args, **kwargs)

                with patch.object(httpx.AsyncClient, "__init__", tracked_init), \
                     patch.object(httpx.AsyncClient, "get", new=AsyncMock(return_value=mock_resp)):
                    res = await job_match_client.find_jobs(candidate_id=456)
                    assert res["status"] == "success"
                    if created_instances:
                        loop2_used_client = created_instances[-1]

        asyncio.run(_test_on_loop2())

    t = threading.Thread(target=_run_in_thread)
    t.start()
    t.join()

    # Loop 2 must NOT have used loop 1's client
    assert loop2_used_client is not None
    assert loop2_used_client is not loop1_client
    # Loop 1 client remains open
    assert not loop1_client.is_closed

    async def _cleanup():
        await close_job_match_client()
    asyncio.run(_cleanup())


def test_4_transient_clients_are_closed_after_completion_when_no_pool_exists():
    """4. Transient clients are closed after completion when no pool is active."""
    async def _test():
        set_job_match_client(None)
        assert get_job_match_client() is None

        with patch("backend.services.job_match_client.JOB_MATCH_ENABLED", True):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"status": "success", "data": []}

            transient_client_instance = None
            original_aclose = httpx.AsyncClient.aclose
            aclose_called = False

            async def tracked_aclose(self):
                nonlocal aclose_called, transient_client_instance
                aclose_called = True
                transient_client_instance = self
                return await original_aclose(self)

            with patch.object(httpx.AsyncClient, "aclose", tracked_aclose), \
                 patch.object(httpx.AsyncClient, "get", new=AsyncMock(return_value=mock_resp)):
                res = await job_match_client.find_jobs(candidate_id=789)
                assert res["status"] == "success"

            assert aclose_called is True
            assert transient_client_instance is not None
            assert transient_client_instance.is_closed

    asyncio.run(_test())


def test_5_existing_exception_classification_preserved_with_pooled_client():
    """5. Existing exception classifications (409, 401, timeout) remain completely unchanged."""
    async def _test():
        with patch("backend.services.job_match_client.JOB_MATCH_ENABLED", True):
            client = init_job_match_client()

            # HTTP 409 -> CandidateNotIndexedException
            mock_409 = MagicMock()
            mock_409.status_code = 409
            with patch.object(client, "get", new=AsyncMock(return_value=mock_409)), \
                 patch.object(client, "post", new=AsyncMock(return_value=mock_409)):
                with pytest.raises(CandidateNotIndexedException):
                    await job_match_client.find_jobs(candidate_id=123)

            # HTTP 401 -> JobMatchAuthException
            mock_401 = MagicMock()
            mock_401.status_code = 401
            with patch.object(client, "get", new=AsyncMock(return_value=mock_401)), \
                 patch.object(client, "post", new=AsyncMock(return_value=mock_401)):
                with pytest.raises(JobMatchAuthException):
                    await job_match_client.find_jobs(candidate_id=123)

            # TimeoutException -> JobMatchUnavailableException
            with patch.object(client, "get", new=AsyncMock(side_effect=httpx.TimeoutException("Timeout"))):
                with pytest.raises(JobMatchUnavailableException):
                    await job_match_client.find_jobs(candidate_id=123)

            await close_job_match_client()

    asyncio.run(_test())


def test_6_injected_test_client_not_closed_by_close_job_match_client():
    """6. Injected test clients are not closed by application lifecycle code unless ownership was transferred."""
    async def _test():
        mock_injected = MagicMock(spec=httpx.AsyncClient)
        mock_injected.is_closed = False
        mock_injected.aclose = AsyncMock()

        set_job_match_client(mock_injected, is_injected=True)
        assert get_job_match_client() is mock_injected

        # Calling close_job_match_client unbinds but does not call aclose() on injected client
        await close_job_match_client()
        assert get_job_match_client() is None
        mock_injected.aclose.assert_not_called()

    asyncio.run(_test())
