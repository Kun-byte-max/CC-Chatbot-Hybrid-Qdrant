"""
test_llm_client_pooling.py — Targeted test suite for LLM HTTP Connection Pooling & Event-Loop Safety.
Phase 3 Stage 3.1.1 verification suite.
"""

import asyncio
import threading
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import httpx

import backend.services.llm_service as llm_mod
from backend.services.llm_service import (
    init_llm_client,
    close_llm_client,
    set_llm_client,
    get_llm_client,
    get_llm_client_loop,
    LLMService,
)


@pytest.fixture(autouse=True)
def cleanup_llm_client():
    """Ensure every test starts and ends with a clean llm_client state."""
    asyncio.run(close_llm_client())
    yield
    asyncio.run(close_llm_client())


def test_1_repeated_buffered_requests_reuse_same_client():
    """1. Repeated buffered LLM requests reuse the same valid pooled client."""
    async def _test():
        client = init_llm_client()
        assert client is not None
        assert get_llm_client() is client

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Hello world"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

        with patch.object(client, "post", new=AsyncMock(return_value=mock_resp)) as mock_post:
            res1 = await LLMService.get_chat_completion("System prompt", [{"role": "user", "content": "Hi 1"}])
            res2 = await LLMService.get_chat_completion("System prompt", [{"role": "user", "content": "Hi 2"}])

            assert res1 == "Hello world"
            assert res2 == "Hello world"
            assert mock_post.call_count == 2
            # Confirm identical client was used across both requests
            assert get_llm_client() is client
        await close_llm_client()

    asyncio.run(_test())


def test_2_streaming_requests_use_pooled_client_and_preserve_chunk_parsing():
    """2. Streaming requests use the pooled client and preserve chunk parsing and response behavior."""
    async def _test():
        client = init_llm_client()
        assert client is not None

        stream_lines = [
            'data: {"choices": [{"delta": {"content": "Hello "}}]}\n',
            'data: {"choices": [{"delta": {"content": "there!"}}]}\n',
            'data: [DONE]\n'
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        async def _aiter_lines():
            for line in stream_lines:
                yield line

        mock_resp.aiter_lines = _aiter_lines

        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def _mock_stream(*args, **kwargs):
            yield mock_resp

        with patch.object(client, "stream", side_effect=_mock_stream) as mock_stream_fn:
            tokens = []
            async for chunk in LLMService.stream_chat_completion("System prompt", [{"role": "user", "content": "Stream me"}]):
                tokens.append(chunk)

            assert tokens == ["Hello ", "there!"]
            assert mock_stream_fn.called is True
            # Shared client remains open
            assert not client.is_closed

        await close_llm_client()

    asyncio.run(_test())


def test_3_closing_stream_generator_early_releases_upstream_without_closing_shared_client():
    """3. Closing or cancelling a streaming generator releases its upstream response without closing the shared client."""
    async def _test():
        client = init_llm_client()
        assert client is not None

        response_closed = False

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        async def _aiter_lines():
            try:
                yield 'data: {"choices": [{"delta": {"content": "Token1"}}]}\n'
                yield 'data: {"choices": [{"delta": {"content": "Token2"}}]}\n'
            finally:
                pass

        mock_resp.aiter_lines = _aiter_lines

        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def _mock_stream(*args, **kwargs):
            nonlocal response_closed
            try:
                yield mock_resp
            finally:
                response_closed = True

        with patch.object(client, "stream", side_effect=_mock_stream):
            stream_gen = LLMService.stream_chat_completion("System", [{"role": "user", "content": "Test"}])
            # Consume first token only
            first_tok = await stream_gen.__anext__()
            assert first_tok == "Token1"
            # Early close the generator
            await stream_gen.aclose()

        # The stream response context exited cleanly
        assert response_closed is True
        # Crucial check: Shared client remains alive and open for future requests
        assert client.is_closed is False
        assert get_llm_client() is client

        await close_llm_client()

    asyncio.run(_test())


def test_4_application_managed_client_closed_during_lifespan_shutdown():
    """4. The application-managed client is closed during lifespan shutdown."""
    async def _test():
        from backend.main import lifespan, app

        async with lifespan(app):
            client = get_llm_client()
            assert client is not None
            assert not client.is_closed

        # After lifespan exit, client must be closed and global state reset to None
        assert client.is_closed
        assert get_llm_client() is None

    asyncio.run(_test())


def test_5_different_event_loop_uses_transient_fallback_and_does_not_break():
    """5. Calls outside application lifespan and tests using separate event loops do not reuse an invalid client."""
    app_client_ref = None

    async def _init_on_loop1():
        nonlocal app_client_ref
        app_client_ref = init_llm_client()
        return app_client_ref

    loop1_client = asyncio.run(_init_on_loop1())
    assert loop1_client is not None

    loop2_used_client = None

    def _run_in_thread():
        async def _test_on_loop2():
            nonlocal loop2_used_client
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "choices": [{"message": {"content": "Loop 2 response"}, "finish_reason": "stop"}]
            }

            created_instances = []
            original_init = httpx.AsyncClient.__init__

            def tracked_init(self, *args, **kwargs):
                created_instances.append(self)
                original_init(self, *args, **kwargs)

            with patch.object(httpx.AsyncClient, "__init__", tracked_init), \
                 patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=mock_resp)):
                res = await LLMService.get_chat_completion("Sys", [{"role": "user", "content": "Loop 2"}])
                assert res == "Loop 2 response"
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
        await close_llm_client()
    asyncio.run(_cleanup())


def test_6_transient_client_is_closed_after_completion_when_no_pool_exists():
    """6. Transient clients are closed after completion when no pool is active."""
    async def _test():
        set_llm_client(None)
        assert get_llm_client() is None

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Transient test"}, "finish_reason": "stop"}]
        }

        transient_client_instance = None
        original_aclose = httpx.AsyncClient.aclose
        aclose_called = False

        async def tracked_aclose(self):
            nonlocal aclose_called, transient_client_instance
            aclose_called = True
            transient_client_instance = self
            return await original_aclose(self)

        with patch.object(httpx.AsyncClient, "aclose", tracked_aclose), \
             patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=mock_resp)):
            res = await LLMService.get_chat_completion("Sys", [{"role": "user", "content": "Transient"}])
            assert res == "Transient test"

        assert aclose_called is True
        assert transient_client_instance is not None
        assert transient_client_instance.is_closed

    asyncio.run(_test())


def test_7_existing_timeout_and_error_handling_remain_unchanged():
    """7. Existing timeout, HTTP 502, and unexpected errors remain correctly propagated."""
    async def _test():
        client = init_llm_client()

        # TimeoutException -> HTTPException 504
        with patch.object(client, "post", side_effect=httpx.TimeoutException("Timeout")):
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as exc_info:
                await LLMService.get_chat_completion("Sys", [{"role": "user", "content": "Timeout"}])
            assert exc_info.value.status_code == 504

        # Non-200 -> HTTPException 502
        mock_500 = MagicMock()
        mock_500.status_code = 500
        mock_500.text = "Internal Server Error"
        with patch.object(client, "post", new=AsyncMock(return_value=mock_500)):
            with pytest.raises(HTTPException) as exc_info:
                await LLMService.get_chat_completion("Sys", [{"role": "user", "content": "Err 500"}])
            assert exc_info.value.status_code == 502

        await close_llm_client()

    asyncio.run(_test())


def test_8_injected_test_client_not_closed_by_close_llm_client():
    """8. Injected test clients are not closed by application lifecycle code unless ownership was transferred."""
    async def _test():
        mock_injected = MagicMock(spec=httpx.AsyncClient)
        mock_injected.is_closed = False
        mock_injected.aclose = AsyncMock()

        set_llm_client(mock_injected, is_injected=True)
        assert get_llm_client() is mock_injected

        # Calling close_llm_client should unbind but not invoke aclose() on injected client
        await close_llm_client()
        assert get_llm_client() is None
        mock_injected.aclose.assert_not_called()

    asyncio.run(_test())
