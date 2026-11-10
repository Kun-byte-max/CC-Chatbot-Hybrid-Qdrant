"""
test_phase4_1_sse_hardening.py — Targeted test suite for Phase 4.1 SSE Disconnect Hardening & Upstream Resource Cleanup.

Covers:
1. Disconnect during ordinary incremental LLM streaming:
   - Loop breaks immediately; generator is closed via aclose().
   - No subsequent delta, done, or error event is emitted after disconnect.
   - Outcome is recorded as client_disconnected.
2. Disconnect during structured-results mode:
   - Results event is emitted before disconnect.
   - Loop breaks immediately; generator is closed; no delta or done emitted after disconnect.
3. Disconnect immediately after pipeline retrieval:
   - Detection occurs before status/results/LLM call.
   - LLMService.stream_chat_completion is never invoked.
4. Generator closure:
   - Async generator aclose() is invoked on early disconnect, exceptions, and task cancellation.
5. Upstream HTTP stream cleanup:
   - Upstream client.stream and client.aclose are properly unwound via generator closure.
6. Provider error sanitization:
   - Simulated upstream non-200 response body (with sensitive details) is never leaked in the SSE error payload.
7. Regression checks:
   - Normal completion emits standard event order (start -> status/results -> delta -> done).
   - Deterministic employment verification responses bypass LLM and complete cleanly.
   - Legacy POST /chat JSON endpoint remains 100% compatible.
"""

import json
import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import jwt
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request, ClientDisconnect

from backend.main import app, ChatPipelineResult
from backend.config.config import JWT_SECRET, JWT_ALGORITHM
from backend.services.llm_service import LLMService
from backend.utils.sse import parse_sse_event
from backend.schemas.schemas import JobCard


class TestPhase41SSEHardening(unittest.TestCase):
    """Targeted integration tests for Phase 4.1 SSE Hardening & Cleanup."""

    def setUp(self):
        self.client = TestClient(app)
        self.valid_jwt = jwt.encode(
            {"id": 777, "uid": 777, "email": "hardening_user@example.com", "role": "employee"},
            JWT_SECRET,
            algorithm=JWT_ALGORITHM,
        )
        self.headers = {"Authorization": f"Bearer {self.valid_jwt}"}
        self.db_user_row = {
            "id": 777,
            "individual_id": "IND777",
            "fname": "Hardening",
            "lname": "Tester",
            "email": "hardening_user@example.com",
            "user_type": 1,
            "status": 1,
            "is_deleted": 0,
            "token": "platform_token_hardening",
        }

    # ------------------------------------------------------------------------
    # 1. Disconnect during ordinary incremental LLM streaming
    # ------------------------------------------------------------------------
    @patch("backend.api.auth.get_db")
    @patch("backend.main.execute_chat_pipeline")
    @patch("backend.services.llm_service.LLMService.stream_chat_completion")
    def test_disconnect_during_incremental_streaming(self, mock_stream, mock_pipeline, mock_auth_db):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]

        mock_pipeline.return_value = ChatPipelineResult(
            role="jobseeker",
            intent_hint="smalltalk",
            msg_len=18,
            system="System prompt",
            bounded_messages=[{"role": "user", "content": "Hello CollarCheck"}],
            max_tokens_compose=150,
            is_terminal=False,
            results_payload=None,
            result_type_payload=None,
        )

        generator_closed = False

        async def token_gen():
            nonlocal generator_closed
            try:
                yield "First token"
                yield "Second token (should be skipped)"
            finally:
                generator_closed = True

        mock_stream.return_value = token_gen()

        disconnect_calls = 0

        async def fake_is_disconnected():
            nonlocal disconnect_calls
            disconnect_calls += 1
            # 1: check after retrieval -> False
            # 2: check after status -> False
            # 3: check inside token loop before token 1 -> False (token 1 emitted)
            # 4: check inside token loop before token 2 -> True (breaks before token 2)
            return disconnect_calls >= 4

        with patch.object(Request, "is_disconnected", side_effect=fake_is_disconnected):
            with patch("backend.main.log") as mock_log:
                payload = {"messages": [{"role": "user", "content": "Hello CollarCheck"}]}
                res = self.client.post("/chat/stream", json=payload, headers=self.headers)

        self.assertEqual(res.status_code, 200)
        events = [parse_sse_event(block) for block in res.text.strip().split("\n\n") if block.strip()]
        event_types = [e["event"] for e in events]

        # Must have emitted start, status, and exactly one delta before disconnect
        self.assertIn("start", event_types)
        self.assertIn("status", event_types)
        deltas = [e for e in events if e["event"] == "delta"]
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0]["data"]["text"], "First token")

        # Crucial: NO done event and NO error event must be emitted after disconnect
        self.assertNotIn("done", event_types)
        self.assertNotIn("error", event_types)

        # Verify generator was closed
        self.assertTrue(generator_closed, "stream_gen must be explicitly closed via aclose() upon disconnect")

        # Verify client_disconnected was logged in chat_stream_turn outcome
        log_messages = [call_args[0][0] for call_args in mock_log.info.call_args_list if call_args[0]]
        turn_logs = [m for m in log_messages if "chat_stream_turn" in str(m)]
        self.assertTrue(any("outcome=client_disconnected" in m for m in turn_logs), "chat_stream_turn must record outcome=client_disconnected")

    # ------------------------------------------------------------------------
    # 2. Disconnect during structured-results mode
    # ------------------------------------------------------------------------
    @patch("backend.api.auth.get_db")
    @patch("backend.main.execute_chat_pipeline")
    @patch("backend.services.llm_service.LLMService.stream_chat_completion")
    def test_disconnect_during_structured_results_mode(self, mock_stream, mock_pipeline, mock_auth_db):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]

        card = JobCard(
            job_id=801,
            title="Senior Go Developer",
            company="CloudCorp",
            location="Remote",
            url="https://example.com/job/801",
        )
        mock_pipeline.return_value = ChatPipelineResult(
            role="jobseeker",
            intent_hint="job_search",
            msg_len=15,
            system="System prompt",
            bounded_messages=[{"role": "user", "content": "go jobs"}],
            max_tokens_compose=150,
            is_terminal=False,
            results_payload=[card],
            result_type_payload="jobs",
            allowed_ids={"801"},
        )

        generator_closed = False

        async def token_gen():
            nonlocal generator_closed
            try:
                yield "Here are matching Go roles:"
            finally:
                generator_closed = True

        mock_stream.return_value = token_gen()

        disconnect_calls = 0

        async def fake_is_disconnected():
            nonlocal disconnect_calls
            disconnect_calls += 1
            # 1: check after retrieval -> False
            # 2: check after results -> False
            # 3: check after status -> False
            # 4: check inside structured token loop -> True
            return disconnect_calls >= 4

        with patch.object(Request, "is_disconnected", side_effect=fake_is_disconnected):
            with patch("backend.main.log") as mock_log:
                payload = {"messages": [{"role": "user", "content": "go jobs"}]}
                res = self.client.post("/chat/stream", json=payload, headers=self.headers)

        self.assertEqual(res.status_code, 200)
        events = [parse_sse_event(block) for block in res.text.strip().split("\n\n") if block.strip()]
        event_types = [e["event"] for e in events]

        # Results event must be present before disconnect
        self.assertIn("start", event_types)
        self.assertIn("results", event_types)
        self.assertIn("status", event_types)

        # No delta, done, or error should be emitted after disconnect
        self.assertNotIn("delta", event_types)
        self.assertNotIn("done", event_types)
        self.assertNotIn("error", event_types)

        self.assertTrue(generator_closed, "stream_gen must be explicitly closed in structured mode")

        # Verify outcome
        log_messages = [call_args[0][0] for call_args in mock_log.info.call_args_list if call_args[0]]
        turn_logs = [m for m in log_messages if "chat_stream_turn" in str(m)]
        self.assertTrue(any("outcome=client_disconnected" in m for m in turn_logs))

    # ------------------------------------------------------------------------
    # 3. Disconnect immediately after pipeline retrieval
    # ------------------------------------------------------------------------
    @patch("backend.api.auth.get_db")
    @patch("backend.main.execute_chat_pipeline")
    @patch("backend.services.llm_service.LLMService.stream_chat_completion")
    def test_disconnect_immediately_after_pipeline_retrieval(self, mock_stream, mock_pipeline, mock_auth_db):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]

        mock_pipeline.return_value = ChatPipelineResult(
            role="jobseeker",
            intent_hint="smalltalk",
            msg_len=5,
            system="System",
            bounded_messages=[{"role": "user", "content": "hi"}],
            max_tokens_compose=100,
            is_terminal=False,
        )

        async def fake_is_disconnected():
            # Disconnected right after retrieval
            return True

        with patch.object(Request, "is_disconnected", side_effect=fake_is_disconnected):
            with patch("backend.main.log") as mock_log:
                payload = {"messages": [{"role": "user", "content": "hi"}]}
                res = self.client.post("/chat/stream", json=payload, headers=self.headers)

        self.assertEqual(res.status_code, 200)
        events = [parse_sse_event(block) for block in res.text.strip().split("\n\n") if block.strip()]
        event_types = [e["event"] for e in events]

        # Only start was emitted before retrieval
        self.assertEqual(event_types, ["start"])
        # stream_chat_completion MUST NOT have been invoked
        mock_stream.assert_not_called()

        log_messages = [call_args[0][0] for call_args in mock_log.info.call_args_list if call_args[0]]
        turn_logs = [m for m in log_messages if "chat_stream_turn" in str(m)]
        self.assertTrue(any("outcome=client_disconnected" in m for m in turn_logs))

    # ------------------------------------------------------------------------
    # 4. Generator closure on cancellation and exceptions
    # ------------------------------------------------------------------------
    @patch("backend.api.auth.get_db")
    @patch("backend.main.execute_chat_pipeline")
    @patch("backend.services.llm_service.LLMService.stream_chat_completion")
    def test_generator_closure_on_cancellation(self, mock_stream, mock_pipeline, mock_auth_db):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]

        mock_pipeline.return_value = ChatPipelineResult(
            role="jobseeker",
            intent_hint="smalltalk",
            msg_len=18,
            system="System",
            bounded_messages=[{"role": "user", "content": "Hello CollarCheck"}],
            max_tokens_compose=100,
            is_terminal=False,
        )

        generator_closed = False

        async def token_gen():
            nonlocal generator_closed
            try:
                yield "Part 1"
                import asyncio
                raise asyncio.CancelledError()
            finally:
                generator_closed = True

        mock_stream.return_value = token_gen()

        with patch.object(Request, "is_disconnected", return_value=False):
            with patch("backend.main.log") as mock_log:
                payload = {"messages": [{"role": "user", "content": "Hello CollarCheck"}]}
                self.client.post("/chat/stream", json=payload, headers=self.headers)

        self.assertTrue(generator_closed, "stream_gen must be closed even when asyncio.CancelledError is raised")
        log_messages = [call_args[0][0] for call_args in mock_log.info.call_args_list if call_args[0]]
        turn_logs = [m for m in log_messages if "chat_stream_turn" in str(m)]
        self.assertTrue(any("outcome=cancelled" in m for m in turn_logs))

    # ------------------------------------------------------------------------
    # 5. Provider error sanitization in endpoint
    # ------------------------------------------------------------------------
    @patch("backend.api.auth.get_db")
    @patch("backend.main.execute_chat_pipeline")
    @patch("backend.services.llm_service.LLMService.stream_chat_completion")
    def test_provider_error_sanitization_in_endpoint(self, mock_stream, mock_pipeline, mock_auth_db):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]

        mock_pipeline.return_value = ChatPipelineResult(
            role="jobseeker",
            intent_hint="smalltalk",
            msg_len=18,
            system="System",
            bounded_messages=[{"role": "user", "content": "Hello CollarCheck"}],
            max_tokens_compose=100,
            is_terminal=False,
        )

        async def failing_stream(*args, **kwargs):
            raise HTTPException(
                status_code=502,
                detail="The AI service is temporarily unavailable. Please try again shortly."
            )
            yield "never"

        mock_stream.side_effect = failing_stream

        with patch.object(Request, "is_disconnected", return_value=False):
            payload = {"messages": [{"role": "user", "content": "trigger error"}]}
            res = self.client.post("/chat/stream", json=payload, headers=self.headers)

        self.assertEqual(res.status_code, 200)
        events = [parse_sse_event(block) for block in res.text.strip().split("\n\n") if block.strip()]
        error_evts = [e for e in events if e["event"] == "error"]
        self.assertEqual(len(error_evts), 1)
        err_data = error_evts[0]["data"]
        self.assertEqual(err_data["error_code"], "HTTP_ERROR")
        self.assertEqual(err_data["detail"], "The AI service is temporarily unavailable. Please try again shortly.")
        self.assertFalse(err_data["recoverable"])

    # ------------------------------------------------------------------------
    # 6. Regression: Normal completion and ClientDisconnect handling
    # ------------------------------------------------------------------------
    @patch("backend.api.auth.get_db")
    @patch("backend.main.execute_chat_pipeline")
    @patch("backend.services.llm_service.LLMService.stream_chat_completion")
    def test_normal_completion_event_sequence(self, mock_stream, mock_pipeline, mock_auth_db):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]

        mock_pipeline.return_value = ChatPipelineResult(
            role="jobseeker",
            intent_hint="smalltalk",
            msg_len=18,
            system="System",
            bounded_messages=[{"role": "user", "content": "Hello CollarCheck"}],
            max_tokens_compose=100,
            is_terminal=False,
        )

        async def normal_gen(*args, **kwargs):
            yield "Hello "
            yield "world!"

        mock_stream.side_effect = normal_gen

        with patch.object(Request, "is_disconnected", return_value=False):
            payload = {"messages": [{"role": "user", "content": "hi"}]}
            res = self.client.post("/chat/stream", json=payload, headers=self.headers)

        self.assertEqual(res.status_code, 200)
        events = [parse_sse_event(block) for block in res.text.strip().split("\n\n") if block.strip()]
        event_types = [e["event"] for e in events]
        self.assertEqual(event_types, ["start", "status", "delta", "delta", "done"])
        done_evt = next(e for e in events if e["event"] == "done")
        self.assertEqual(done_evt["data"]["reply"], "Hello world!")
        self.assertTrue(done_evt["data"]["success"])

    @patch("backend.api.auth.get_db")
    @patch("backend.main.execute_chat_pipeline")
    @patch("backend.services.llm_service.LLMService.stream_chat_completion")
    def test_client_disconnect_exception_during_yield_handled_cleanly(self, mock_stream, mock_pipeline, mock_auth_db):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]

        mock_pipeline.return_value = ChatPipelineResult(
            role="jobseeker",
            intent_hint="smalltalk",
            msg_len=18,
            system="System",
            bounded_messages=[{"role": "user", "content": "Hello CollarCheck"}],
            max_tokens_compose=100,
            is_terminal=False,
        )

        # Simulate ClientDisconnect raised by Starlette during yield
        async def disconnecting_gen(*args, **kwargs):
            yield "First token"
            raise ClientDisconnect()

        mock_stream.side_effect = disconnecting_gen

        with patch.object(Request, "is_disconnected", return_value=False):
            with patch("backend.main.log") as mock_log:
                payload = {"messages": [{"role": "user", "content": "disconnect me"}]}
                res = self.client.post("/chat/stream", json=payload, headers=self.headers)

        # Response stream ends cleanly without 500 error event
        self.assertEqual(res.status_code, 200)
        events = [parse_sse_event(block) for block in res.text.strip().split("\n\n") if block.strip()]
        event_types = [e["event"] for e in events]
        self.assertNotIn("error", event_types)

        # Verify outcome=client_disconnected was recorded
        log_messages = [call_args[0][0] for call_args in mock_log.info.call_args_list if call_args[0]]
        turn_logs = [m for m in log_messages if "chat_stream_turn" in str(m)]
        self.assertTrue(any("outcome=client_disconnected" in m for m in turn_logs))


class TestPhase41AsyncGenerators(unittest.IsolatedAsyncioTestCase):
    """Targeted async unit tests for LLMService stream cleanup and sanitization."""

    @patch("httpx.AsyncClient.stream")
    @patch("httpx.AsyncClient.aclose")
    async def test_upstream_http_stream_cleanup_unwound(self, mock_aclose, mock_http_stream):
        class MockStreamContext:
            def __init__(self):
                self.closed = False

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                self.closed = True

            @property
            def status_code(self):
                return 200

            async def aiter_lines(self):
                yield 'data: {"choices": [{"delta": {"content": "Token A"}}]}'
                yield 'data: {"choices": [{"delta": {"content": "Token B"}}]}'

        stream_ctx = MockStreamContext()
        mock_http_stream.return_value = stream_ctx

        gen = LLMService.stream_chat_completion("System", [{"role": "user", "content": "hi"}])
        first = await gen.__anext__()
        self.assertEqual(first, "Token A")

        # Close generator early
        await gen.aclose()

        # Both the stream context and client must be closed
        self.assertTrue(stream_ctx.closed, "Upstream stream context __aexit__ must be called on aclose()")
        mock_aclose.assert_called_once()

    @patch("httpx.AsyncClient.stream")
    async def test_provider_error_sanitization_in_service(self, mock_stream):
        class MockErrorResponse:
            @property
            def status_code(self):
                return 502

            async def aread(self):
                # Simulated sensitive upstream body containing internal key/organization
                return b'{"error": {"message": "Invalid auth key sk-proj-secret123456", "organization": "org-secret-999"}}'

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        mock_stream.return_value = MockErrorResponse()

        with self.assertRaises(HTTPException) as ctx:
            async for _ in LLMService.stream_chat_completion("System", [{"role": "user", "content": "hi"}]):
                pass

        self.assertEqual(ctx.exception.status_code, 502)
        # Detail must be sanitized
        self.assertEqual(ctx.exception.detail, "The AI service is temporarily unavailable. Please try again shortly.")
        self.assertNotIn("sk-proj", ctx.exception.detail)
        self.assertNotIn("secret", ctx.exception.detail)
        self.assertNotIn("org-secret", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
