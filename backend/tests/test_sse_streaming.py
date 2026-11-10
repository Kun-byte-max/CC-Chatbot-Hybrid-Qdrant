"""
test_sse_streaming.py — Targeted test suite for Phase 1A SSE Streaming Foundation.

Covers:
1. SSE event framing and JSON serialization (format_sse_event & parse_sse_event).
2. Upstream chunk parsing, [DONE] handling, and heartbeats in LLMService.stream_chat_completion.
3. Usage-only final chunk parsing and token recording.
4. Upstream error and timeout handling (502, 504, malformed data).
5. Resource cleanup and generator cancellation.
6. POST /chat/stream authentication, request validation, headers, and event protocol.
7. Existing POST /chat JSON compatibility.
"""

import json
import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import pytest
import jwt
from fastapi import HTTPException
from fastapi.testclient import TestClient

from dataclasses import dataclass, is_dataclass
from typing import List, Dict, Any
from backend.main import app, _serialize_cards
from backend.config.config import JWT_SECRET, JWT_ALGORITHM
from backend.services.llm_service import LLMService
from backend.utils.sse import format_sse_event, parse_sse_event


class TestSSEEventFraming(unittest.TestCase):
    """Tests for SSE framing and JSON serialization."""

    def test_format_sse_event_dict(self):
        event = format_sse_event("start", {"version": "1.0", "request_id": "req_123"})
        self.assertEqual(event, 'event: start\ndata: {"version": "1.0", "request_id": "req_123"}\n\n')

    def test_format_sse_event_string(self):
        event = format_sse_event("delta", "Hello world")
        self.assertEqual(event, 'event: delta\ndata: "Hello world"\n\n')

    def test_format_sse_event_multiline_string(self):
        # Crucial test: newlines in payload must be JSON-escaped so data: line has no raw linebreaks
        event = format_sse_event("delta", {"text": "Line 1\nLine 2\nLine 3"})
        lines = event.strip().split("\n")
        self.assertEqual(lines[0], "event: delta")
        self.assertTrue(lines[1].startswith("data: "))
        self.assertIn("\\n", lines[1])
        # Only 2 non-empty lines: event line and data line
        self.assertEqual(len(lines), 2)

    def test_format_sse_event_null(self):
        event = format_sse_event("done", None)
        self.assertEqual(event, "event: done\ndata: null\n\n")

    def test_parse_sse_event_roundtrip(self):
        raw = format_sse_event("custom", {"foo": "bar", "count": 42})
        parsed = parse_sse_event(raw)
        self.assertEqual(parsed["event"], "custom")
        self.assertEqual(parsed["data"], {"foo": "bar", "count": 42})


class TestLLMServiceStreaming(unittest.IsolatedAsyncioTestCase):
    """Targeted tests for LLMService.stream_chat_completion."""

    class MockAsyncResponse:
        def __init__(self, lines, status_code=200, error_bytes=b""):
            self.lines = lines
            self.status_code = status_code
            self.error_bytes = error_bytes

        async def aiter_lines(self):
            for line in self.lines:
                yield line

        async def aread(self):
            return self.error_bytes

        async def aclose(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            await self.aclose()

    @patch("httpx.AsyncClient.stream")
    async def test_stream_chunks_success(self, mock_stream):
        lines = [
            "",  # empty heartbeat
            "data: " + json.dumps({"choices": [{"delta": {"content": "Hello"}}]}),
            "data: " + json.dumps({"choices": [{"delta": {"content": " world"}}]}),
            "data: " + json.dumps({"choices": [{"delta": {"content": "!"}}]}),
            "data: [DONE]",
        ]
        mock_stream.return_value = self.MockAsyncResponse(lines)

        tokens = []
        async for token in LLMService.stream_chat_completion("System prompt", [{"role": "user", "content": "hi"}]):
            tokens.append(token)

        self.assertEqual(tokens, ["Hello", " world", "!"])

    @patch("httpx.AsyncClient.stream")
    @patch("backend.services.llm_service.set_compose_tokens")
    async def test_stream_usage_only_final_chunk(self, mock_set_tokens, mock_stream):
        lines = [
            "data: " + json.dumps({"choices": [{"delta": {"content": "Greeting"}}]}),
            # Usage-only chunk where choices is empty:
            "data: " + json.dumps({
                "choices": [],
                "usage": {"prompt_tokens": 105, "completion_tokens": 12, "total_tokens": 117}
            }),
            "data: [DONE]",
        ]
        mock_stream.return_value = self.MockAsyncResponse(lines)

        tokens = []
        async for token in LLMService.stream_chat_completion("System", [{"role": "user", "content": "hi"}], record_tokens_as="compose"):
            tokens.append(token)

        self.assertEqual(tokens, ["Greeting"])
        mock_set_tokens.assert_called_once_with(105, 12)

    @patch("httpx.AsyncClient.stream")
    async def test_stream_skips_malformed_json_chunk(self, mock_stream):
        lines = [
            "data: " + json.dumps({"choices": [{"delta": {"content": "Valid 1"}}]}),
            "data: {broken json not valid",
            "data: " + json.dumps({"choices": [{"delta": {"content": " Valid 2"}}]}),
            "data: [DONE]",
        ]
        mock_stream.return_value = self.MockAsyncResponse(lines)

        tokens = []
        async for token in LLMService.stream_chat_completion("System", [{"role": "user", "content": "hi"}]):
            tokens.append(token)

        self.assertEqual(tokens, ["Valid 1", " Valid 2"])

    @patch("httpx.AsyncClient.stream")
    async def test_stream_upstream_502_error(self, mock_stream):
        mock_stream.return_value = self.MockAsyncResponse(
            [], status_code=502, error_bytes=b"OpenAI server unavailable"
        )

        with self.assertRaises(HTTPException) as ctx:
            async for _ in LLMService.stream_chat_completion("System", [{"role": "user", "content": "hi"}]):
                pass

        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("temporarily unavailable", ctx.exception.detail)
        self.assertNotIn("OpenAI server unavailable", ctx.exception.detail)

    @patch("httpx.AsyncClient.stream")
    async def test_stream_upstream_timeout_504(self, mock_stream):
        import httpx
        mock_stream.side_effect = httpx.TimeoutException("Read timed out")

        with self.assertRaises(HTTPException) as ctx:
            async for _ in LLMService.stream_chat_completion("System", [{"role": "user", "content": "hi"}]):
                pass

        self.assertEqual(ctx.exception.status_code, 504)
        self.assertIn("timed out", ctx.exception.detail.lower())

    @patch("httpx.AsyncClient.aclose")
    @patch("httpx.AsyncClient.stream")
    async def test_stream_resource_cleanup_on_cancellation(self, mock_stream, mock_aclose):
        lines = [
            "data: " + json.dumps({"choices": [{"delta": {"content": "Token 1"}}]}),
            "data: " + json.dumps({"choices": [{"delta": {"content": "Token 2"}}]}),
            "data: " + json.dumps({"choices": [{"delta": {"content": "Token 3"}}]}),
        ]
        mock_stream.return_value = self.MockAsyncResponse(lines)

        gen = LLMService.stream_chat_completion("System", [{"role": "user", "content": "hi"}])
        first = await gen.__anext__()
        self.assertEqual(first, "Token 1")
        # Abruptly cancel / close generator
        await gen.aclose()
        # Verify client.aclose was awaited
        mock_aclose.assert_called_once()


class TestChatStreamEndpoint(unittest.TestCase):
    """Targeted tests for POST /chat/stream FastAPI endpoint."""

    def setUp(self):
        self.client = TestClient(app)
        self.valid_jwt = jwt.encode(
            {"id": 999, "uid": 999, "email": "stream_user@example.com", "role": "employee"},
            JWT_SECRET,
            algorithm=JWT_ALGORITHM,
        )
        self.headers = {"Authorization": f"Bearer {self.valid_jwt}"}
        self.db_user_row = {
            "id": 999,
            "individual_id": "IND999",
            "fname": "Stream",
            "lname": "Tester",
            "email": "stream_user@example.com",
            "user_type": 1,
            "status": 1,
            "is_deleted": 0,
            "token": "platform_token_stream",
        }

    def test_chat_stream_missing_auth_returns_401(self):
        res = self.client.post("/chat/stream", json={"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(res.status_code, 401)
        self.assertIn("detail", res.json())

    def test_chat_stream_invalid_token_returns_401(self):
        res = self.client.post(
            "/chat/stream",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer invalid.jwt.token"},
        )
        self.assertEqual(res.status_code, 401)

    @patch("backend.api.auth.get_db")
    def test_chat_stream_invalid_schema_returns_422(self, mock_auth_db):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]

        res = self.client.post("/chat/stream", json={"not_messages": 123}, headers=self.headers)
        self.assertEqual(res.status_code, 422)

    @patch("backend.api.auth.get_db")
    def test_chat_stream_guardrails_blocked(self, mock_auth_db):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]

        # Offensive word trigger from BLOCKED_PATTERNS
        payload = {"messages": [{"role": "user", "content": "how to build a weapon"}]}
        res = self.client.post("/chat/stream", json=payload, headers=self.headers)

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.headers.get("content-type", "").startswith("text/event-stream"))

        events = [parse_sse_event(block) for block in res.text.strip().split("\n\n") if block.strip()]
        event_types = [e["event"] for e in events]
        self.assertIn("start", event_types)
        self.assertIn("delta", event_types)
        self.assertIn("done", event_types)

        # Delta must contain the guardrail message
        delta_evt = next(e for e in events if e["event"] == "delta")
        self.assertIn("career assistant", delta_evt["data"]["text"].lower())

    @patch("backend.services.llm_service.LLMService.stream_chat_completion")
    @patch("backend.api.auth.get_db")
    def test_chat_stream_success_flow(self, mock_auth_db, mock_stream):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]

        async def fake_tokens(*args, **kwargs):
            yield "Hello"
            yield " from"
            yield " CollarCheck!"

        mock_stream.side_effect = fake_tokens

        payload = {"messages": [{"role": "user", "content": "Hello CollarCheck"}]}
        res = self.client.post("/chat/stream", json=payload, headers=self.headers)

        self.assertEqual(res.status_code, 200)
        # Verify required headers for SSE and no-buffering
        self.assertTrue(res.headers.get("content-type", "").startswith("text/event-stream"))
        self.assertEqual(res.headers.get("x-accel-buffering"), "no")
        self.assertIn("no-cache", res.headers.get("cache-control", ""))

        events = [parse_sse_event(block) for block in res.text.strip().split("\n\n") if block.strip()]
        event_types = [e["event"] for e in events]

        self.assertEqual(event_types, ["start", "status", "delta", "delta", "delta", "done"])
        deltas = [e["data"]["text"] for e in events if e["event"] == "delta"]
        self.assertEqual("".join(deltas), "Hello from CollarCheck!")

        done_evt = next(e for e in events if e["event"] == "done")
        self.assertEqual(done_evt["data"]["reply"], "Hello from CollarCheck!")
        self.assertTrue(done_evt["data"]["success"])

    @patch("backend.services.llm_service.LLMService.stream_chat_completion")
    @patch("backend.api.auth.get_db")
    def test_chat_stream_mid_stream_error_event(self, mock_auth_db, mock_stream):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]

        async def failing_stream(*args, **kwargs):
            raise HTTPException(status_code=504, detail="Upstream request timed out.")
            yield "Never reached"  # generator syntax

        mock_stream.side_effect = failing_stream

        payload = {"messages": [{"role": "user", "content": "Trigger failure"}]}
        res = self.client.post("/chat/stream", json=payload, headers=self.headers)

        self.assertEqual(res.status_code, 200)
        events = [parse_sse_event(block) for block in res.text.strip().split("\n\n") if block.strip()]
        event_types = [e["event"] for e in events]

        self.assertIn("start", event_types)
        self.assertIn("error", event_types)

        error_evt = next(e for e in events if e["event"] == "error")
        self.assertEqual(error_evt["data"]["error_code"], "HTTP_ERROR")
        self.assertIn("timed out", error_evt["data"]["detail"].lower())

    @patch("backend.services.llm_service.LLMService.stream_chat_completion")
    @patch("backend.api.auth.get_db")
    def test_sse_error_mapping_redacts_synthetic_secrets_and_maps_502(self, mock_auth_db, mock_stream):
        """Verify that 502 HTTPException details containing synthetic bearer tokens or secret keys are mapped to safe 502 message."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]

        fake_secret_token = "Bearer secret_jwt_token_value_abc123"
        fake_api_key = "sk-1234567890abcdef12345"

        async def failing_stream_with_secret(*args, **kwargs):
            raise HTTPException(
                status_code=502,
                detail=f"Failed connecting to upstream with {fake_secret_token} and {fake_api_key}"
            )
            yield "Never reached"

        mock_stream.side_effect = failing_stream_with_secret

        payload = {"messages": [{"role": "user", "content": "Trigger failure with secret"}]}
        res = self.client.post("/chat/stream", json=payload, headers=self.headers)

        self.assertEqual(res.status_code, 200)
        events = [parse_sse_event(block) for block in res.text.strip().split("\n\n") if block.strip()]
        error_evt = next(e for e in events if e["event"] == "error")

        detail_text = error_evt["data"]["detail"]
        self.assertNotIn("secret_jwt_token_value_abc123", detail_text)
        self.assertNotIn("sk-1234567890abcdef12345", detail_text)
        self.assertEqual(detail_text, "The AI service is temporarily unavailable. Please try again shortly.")

    @patch("backend.main.execute_chat_pipeline")
    @patch("backend.api.auth.get_db")
    def test_sse_error_mapping_internal_details_never_leak(self, mock_auth_db, mock_pipeline):
        """
        Verify that internal filesystem paths, SQL schema/column names, raw queries/salaries,
        and arbitrary unrecognized exception strings never reach client-visible SSE payload.
        """
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]

        test_cases = [
            # 1. Arbitrary filesystem path
            (500, "/var/www/collarcheck/backend/services/internal_pool.py: line 42 crashed", "path"),
            # 2. Fake SQL schema / database column
            (500, "SELECT salary_amount, secret_ssn FROM candidate_evaluations_v2 WHERE id=1 failed", "sql"),
            # 3. Synthetic raw user query with candidate profile / salary info
            (400, "Query failed for candidate 'John Doe' with current salary $150,000", "salary"),
            # 4. Fake token / credential not fitting generic regexes
            (403, "Invalid signature for raw_internal_system_token_xyz_98765", "token"),
            # 5. Unknown internal error string containing none of the sanitizer's recognized patterns
            (500, "Corrupt internal memory buffer segment at 0xdeadbeef", "unknown"),
        ]

        for status_code, leaky_detail, label in test_cases:
            with self.subTest(label=label):
                mock_pipeline.side_effect = HTTPException(status_code=status_code, detail=leaky_detail)
                payload = {"messages": [{"role": "user", "content": f"Test {label}"}]}
                res = self.client.post("/chat/stream", json=payload, headers=self.headers)

                self.assertEqual(res.status_code, 200)
                # Sensitive string must be completely absent from entire raw SSE response text
                self.assertNotIn(leaky_detail, res.text)
                events = [parse_sse_event(block) for block in res.text.strip().split("\n\n") if block.strip()]
                error_evt = next((e for e in events if e["event"] == "error"), None)
                self.assertIsNotNone(error_evt, f"Missing error event for {label}")
                self.assertEqual(error_evt["data"]["error_code"], "HTTP_ERROR")
                self.assertFalse(error_evt["data"]["recoverable"])

    @patch("backend.main.execute_chat_pipeline")
    @patch("backend.api.auth.get_db")
    def test_sse_known_safe_validation_error_remains_visible(self, mock_auth_db, mock_pipeline):
        """Verify that known-safe validation error in allowlist remains appropriately visible."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]

        mock_pipeline.side_effect = HTTPException(status_code=400, detail="Invalid request content")

        payload = {"messages": [{"role": "user", "content": "Trigger validation"}]}
        res = self.client.post("/chat/stream", json=payload, headers=self.headers)

        self.assertEqual(res.status_code, 200)
        events = [parse_sse_event(block) for block in res.text.strip().split("\n\n") if block.strip()]
        error_evt = next(e for e in events if e["event"] == "error")
        self.assertEqual(error_evt["data"]["error_code"], "HTTP_ERROR")
        self.assertEqual(error_evt["data"]["detail"], "Invalid request content")
        self.assertFalse(error_evt["data"]["recoverable"])

    @patch("backend.main.execute_chat_pipeline")
    @patch("backend.api.auth.get_db")
    def test_sse_cancellation_does_not_emit_internal_error(self, mock_auth_db, mock_pipeline):
        """Verify asyncio.CancelledError terminates the stream without emitting an error event."""
        import asyncio
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]

        mock_pipeline.side_effect = asyncio.CancelledError()

        with patch("backend.main.log") as mock_log:
            payload = {"messages": [{"role": "user", "content": "Cancel test"}]}
            res = self.client.post("/chat/stream", json=payload, headers=self.headers)

            self.assertEqual(res.status_code, 200)
            events = [parse_sse_event(block) for block in res.text.strip().split("\n\n") if block.strip()]
            event_types = [e["event"] for e in events]
            self.assertNotIn("error", event_types)
            self.assertNotIn("INTERNAL_ERROR", res.text)
            # Log should record outcome=cancelled
            log_messages = [call_args[0][0] for call_args in mock_log.info.call_args_list if call_args[0]]
            turn_logs = [m for m in log_messages if "chat_stream_turn" in str(m)]
            self.assertTrue(any("outcome=cancelled" in m for m in turn_logs))



class TestExistingChatCompatibility(unittest.TestCase):
    """Confirms existing POST /chat JSON contract remains 100% compatible."""

    def setUp(self):
        self.client = TestClient(app)
        self.valid_jwt = jwt.encode(
            {"id": 999, "uid": 999, "email": "compat_user@example.com", "role": "employee"},
            JWT_SECRET,
            algorithm=JWT_ALGORITHM,
        )
        self.headers = {"Authorization": f"Bearer {self.valid_jwt}"}
        self.db_user_row = {
            "id": 999,
            "individual_id": "IND999",
            "fname": "Compat",
            "lname": "Tester",
            "email": "compat_user@example.com",
            "user_type": 1,
            "status": 1,
            "is_deleted": 0,
            "token": "platform_token_compat",
        }

    @patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock)
    @patch("backend.api.auth.get_db")
    def test_post_chat_returns_json_response(self, mock_auth_db, mock_llm):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]

        mock_llm.return_value = "CollarCheck can assist with finding relevant job openings."

        payload = {"messages": [{"role": "user", "content": "Tell me about your services"}]}
        res = self.client.post("/chat", json=payload, headers=self.headers)

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.headers.get("content-type", "").startswith("application/json"))

        data = res.json()
        self.assertIn("request_id", data)

    @patch("backend.main.execute_chat_pipeline", new_callable=AsyncMock)
    @patch("backend.api.auth.get_db")
    def test_post_chat_unhandled_exception_sanitizes_500_detail(self, mock_auth_db, mock_pipeline):
        """Verify that unexpected internal exceptions do not leak raw exception messages, paths, or secrets."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]

        mock_pipeline.side_effect = RuntimeError("Fatal DB connection lost: mysql://root:secretpass@127.0.0.1/prod_db")

        payload = {"messages": [{"role": "user", "content": "Trigger 500 error"}]}
        res = self.client.post("/chat", json=payload, headers=self.headers)

        self.assertEqual(res.status_code, 500)
        err_data = res.json()
        detail = err_data.get("detail", "")
        self.assertNotIn("secretpass", detail)
        self.assertNotIn("mysql://", detail)
        self.assertNotIn("prod_db", detail)
        self.assertEqual(detail, "An internal error occurred while processing the chat request.")


class TestPhase1BStructuredResultsDecoupling(unittest.TestCase):
    """Targeted test suite for Phase 1B: Structured Results Decoupling for SSE."""

    def setUp(self):
        self.client = TestClient(app)
        self.valid_jwt = jwt.encode(
            {"id": 888, "uid": 888, "email": "phase1b_user@example.com", "role": "employee"},
            JWT_SECRET,
            algorithm=JWT_ALGORITHM,
        )
        self.headers = {"Authorization": f"Bearer {self.valid_jwt}"}
        self.db_user_row = {
            "id": 888,
            "individual_id": "IND888",
            "fname": "Phase1B",
            "lname": "Tester",
            "email": "phase1b_user@example.com",
            "user_type": 1,
            "status": 1,
            "is_deleted": 0,
            "token": "platform_token_phase1b",
        }

    @patch("backend.services.llm_service.LLMService.stream_chat_completion")
    @patch("backend.services.search_router.dispatch_search_target")
    @patch("backend.services.search_parser.parse_search_query")
    @patch("backend.api.auth.get_db")
    def test_job_search_results_emitted_before_deltas(
        self, mock_auth_db, mock_parser, mock_dispatch, mock_stream
    ):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]
        mock_cursor.fetchone.return_value = self.db_user_row

        from backend.schemas.search_query import SearchTarget
        mock_parser.return_value = {"search_target": SearchTarget.JOBS}

        from backend.schemas.schemas import JobCard
        job1 = JobCard(
            job_id=101,
            title="Senior Backend Engineer",
            company="Tech Corp",
            location="Bengaluru",
            url="https://collarcheck.com/jobs/101",
            match_score=0.92,
        )
        mock_dispatch.return_value = {
            "db_context": "Jobs found in Bengaluru",
            "results": [job1],
            "result_type": "jobs",
            "allowed_ids": {101},
            "raw_hits": [{"job_description": "Great job"}],
            "pagination": {"total": 1, "page": 1, "pages": 1, "limit": 10},
            "suggestion_chips": ["Apply Now", "More Jobs in Bengaluru"],
        }

        async def fake_stream(*args, **kwargs):
            yield "Here are matching job openings."

        mock_stream.side_effect = fake_stream

        payload = {"messages": [{"role": "user", "content": "Find backend engineer jobs in Bengaluru"}]}
        res = self.client.post("/chat/stream", json=payload, headers=self.headers)

        self.assertEqual(res.status_code, 200)
        events = [parse_sse_event(block) for block in res.text.strip().split("\n\n") if block.strip()]
        event_types = [e["event"] for e in events]

        # Must follow sequence: start -> results -> status -> delta -> done
        self.assertEqual(event_types, ["start", "results", "status", "delta", "done"])

        # Check results event payload shape and content
        results_evt = next(e for e in events if e["event"] == "results")
        self.assertEqual(results_evt["data"]["result_type"], "jobs")
        self.assertEqual(len(results_evt["data"]["results"]), 1)
        self.assertEqual(results_evt["data"]["results"][0]["job_id"], 101)
        self.assertEqual(results_evt["data"]["results"][0]["title"], "Senior Backend Engineer")
        self.assertEqual(results_evt["data"]["pagination"]["total"], 1)
        self.assertEqual(results_evt["data"]["suggestion_chips"], ["Apply Now", "More Jobs in Bengaluru"])

        # Delta arrives after results
        delta_evt = next(e for e in events if e["event"] == "delta")
        self.assertIn("Here are matching job openings", delta_evt["data"]["text"])

        # Done arrives at the end
        done_evt = next(e for e in events if e["event"] == "done")
        self.assertTrue(done_evt["data"]["success"])

    @patch("backend.services.llm_service.LLMService.stream_chat_completion")
    @patch("backend.services.search_router.dispatch_search_target")
    @patch("backend.services.search_parser.parse_search_query")
    @patch("backend.api.auth.get_db")
    def test_candidate_search_results_decoupling(
        self, mock_auth_db, mock_parser, mock_dispatch, mock_stream
    ):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]
        mock_cursor.fetchone.return_value = self.db_user_row

        from backend.schemas.search_query import SearchTarget
        mock_parser.return_value = {"search_target": SearchTarget.PEOPLE_SEARCH}

        from backend.schemas.schemas import CandidateCard
        c1 = CandidateCard(
            id=505,
            candidate_id=505,
            cc_id="CC505",
            name="Alice Smith",
            position="Python Architect",
            experience_years=8,
            skills=["Python", "FastAPI"],
            city="Mumbai",
            url="https://collarcheck.com/candidates/505",
        )
        mock_dispatch.return_value = {
            "db_context": "Found 1 candidate",
            "results": [c1],
            "result_type": "candidates",
            "allowed_ids": {505},
            "raw_hits": [],
            "pagination": {"total": 1, "page": 1, "pages": 1, "limit": 10},
            "suggestion_chips": ["View Profile"],
        }

        async def fake_stream(*args, **kwargs):
            yield "Here are candidates matching your criteria."

        mock_stream.side_effect = fake_stream

        payload = {"messages": [{"role": "user", "content": "Search Python developers in Mumbai"}]}
        res = self.client.post("/chat/stream", json=payload, headers=self.headers)

        self.assertEqual(res.status_code, 200)
        events = [parse_sse_event(block) for block in res.text.strip().split("\n\n") if block.strip()]
        event_types = [e["event"] for e in events]

        self.assertEqual(event_types, ["start", "results", "status", "delta", "done"])
        results_evt = next(e for e in events if e["event"] == "results")
        self.assertEqual(results_evt["data"]["result_type"], "candidates")
        self.assertEqual(results_evt["data"]["results"][0]["id"], 505)
        self.assertEqual(results_evt["data"]["results"][0]["name"], "Alice Smith")

    @patch("backend.services.llm_service.LLMService.stream_chat_completion")
    @patch("backend.services.user_data_service.handle_query")
    @patch("backend.services.user_data_service.parse_user_data_intents")
    @patch("backend.api.auth.get_db")
    def test_deterministic_employment_verification_bypasses_llm(
        self, mock_auth_db, mock_parser, mock_handle_query, mock_stream
    ):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]
        mock_cursor.fetchone.return_value = self.db_user_row

        from backend.services.user_data_intent_parser import UserIntent
        from backend.services.user_data_service import ServiceResponse
        mock_parser.return_value = [UserIntent.EMPLOYMENT_VERIFICATION]

        verification_reply = (
            "🏢 **Acme Corp**\n"
            "Software Engineer\n\n"
            "**Verification roadmap**\n\n"
            "Level 1 — Work Identity (VERIFIED)\n"
            "Level 2 — Company Verification (PENDING)\n\n"
            "Verification Level: 1 / 4"
        )
        mock_handle_query.return_value = ServiceResponse(
            handled=True,
            reply=verification_reply,
            results_payload=[{"company": "Acme Corp", "level": 1, "status": "VERIFIED"}],
            result_type_payload="employment_verification",
            presentation_mode="timeline",
            requested_level=1,
        )

        payload = {"messages": [{"role": "user", "content": "Check my employment verification status"}]}
        res = self.client.post("/chat/stream", json=payload, headers=self.headers)

        self.assertEqual(res.status_code, 200)
        events = [parse_sse_event(block) for block in res.text.strip().split("\n\n") if block.strip()]
        event_types = [e["event"] for e in events]

        # Event ordering for deterministic branch: start -> results -> delta -> done
        self.assertEqual(event_types, ["start", "results", "delta", "done"])

        # LLM stream MUST NOT be invoked
        mock_stream.assert_not_called()

        # Results event carries the structured verification payload
        results_evt = next(e for e in events if e["event"] == "results")
        self.assertEqual(results_evt["data"]["result_type"], "employment_verification")
        self.assertEqual(results_evt["data"]["presentation_mode"], "timeline")
        self.assertEqual(results_evt["data"]["results"][0]["company"], "Acme Corp")

        # Delta carries the exact formatted text with building emoji preserved
        delta_evt = next(e for e in events if e["event"] == "delta")
        self.assertEqual(delta_evt["data"]["text"], verification_reply)
        self.assertIn("🏢 **Acme Corp**", delta_evt["data"]["text"])

        done_evt = next(e for e in events if e["event"] == "done")
        self.assertEqual(done_evt["data"]["reply"], verification_reply)

    @patch("backend.services.llm_service.LLMService.stream_chat_completion")
    @patch("backend.services.search_router.dispatch_search_target")
    @patch("backend.services.search_parser.parse_search_query")
    @patch("backend.api.auth.get_db")
    def test_branch_with_no_structured_results_omits_results_event(
        self, mock_auth_db, mock_parser, mock_dispatch, mock_stream
    ):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]
        mock_cursor.fetchone.return_value = self.db_user_row

        from backend.schemas.search_query import SearchTarget
        mock_parser.return_value = {"search_target": SearchTarget.UNKNOWN}
        mock_dispatch.return_value = {
            "db_context": None,
            "results": None,
            "result_type": None,
            "allowed_ids": set(),
        }

        async def fake_stream(*args, **kwargs):
            yield "Hello! How can I help you today?"

        mock_stream.side_effect = fake_stream

        payload = {"messages": [{"role": "user", "content": "Hello there"}]}
        res = self.client.post("/chat/stream", json=payload, headers=self.headers)

        self.assertEqual(res.status_code, 200)
        events = [parse_sse_event(block) for block in res.text.strip().split("\n\n") if block.strip()]
        event_types = [e["event"] for e in events]

        # Must NOT include results event
        self.assertNotIn("results", event_types)
        self.assertEqual(event_types, ["start", "status", "delta", "done"])

    @patch("backend.services.llm_service.LLMService.stream_chat_completion")
    @patch("backend.services.search_router.dispatch_search_target")
    @patch("backend.services.search_parser.parse_search_query")
    @patch("backend.api.auth.get_db")
    def test_sanitization_removes_duplicate_job_titles_in_stream(
        self, mock_auth_db, mock_parser, mock_dispatch, mock_stream
    ):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]
        mock_cursor.fetchone.return_value = self.db_user_row

        from backend.schemas.search_query import SearchTarget
        mock_parser.return_value = {"search_target": SearchTarget.JOBS}

        from backend.schemas.schemas import JobCard
        job1 = JobCard(
            job_id=202,
            title="Senior React Developer",
            company="WebTech",
            location="Pune",
            url="https://collarcheck.com/jobs/202",
        )
        mock_dispatch.return_value = {
            "db_context": "Jobs found",
            "results": [job1],
            "result_type": "jobs",
            "allowed_ids": {202},
            "raw_hits": [],
        }

        # Simulate LLM repeating the job title in list format
        async def fake_stream(*args, **kwargs):
            yield "Here are matching jobs:\n1. Senior React Developer at WebTech in Pune"

        mock_stream.side_effect = fake_stream

        payload = {"messages": [{"role": "user", "content": "React jobs in Pune"}]}
        res = self.client.post("/chat/stream", json=payload, headers=self.headers)

        self.assertEqual(res.status_code, 200)
        events = [parse_sse_event(block) for block in res.text.strip().split("\n\n") if block.strip()]

        delta_evt = next(e for e in events if e["event"] == "delta")
        emitted_text = delta_evt["data"]["text"]

        # Duplicate title and list formatting must be sanitized out
        self.assertNotIn("1. Senior React Developer at WebTech in Pune", emitted_text)
        self.assertIn("matching jobs for your search", emitted_text)


class TestSerializeCardsAndSSECompatibility(unittest.TestCase):
    """
    Targeted regression tests for _serialize_cards() and SSE format_sse_event() compatibility:
    - Standard and nested Python dataclass serialization
    - RecommendationResult and pagination_meta['all_recs'] serialization
    - Preservation of Pydantic models, dicts, lists, sets, and primitives
    - SSE results event formatting with complex recommendation payloads
    """

    def test_nested_dataclass_serialization(self):
        @dataclass
        class SubItem:
            name: str
            value: int

        @dataclass
        class ParentItem:
            title: str
            sub: SubItem
            sub_list: List[SubItem]

        obj = ParentItem(
            title="Root",
            sub=SubItem(name="Child", value=42),
            sub_list=[SubItem(name="ListChild1", value=1), SubItem(name="ListChild2", value=2)],
        )

        res = _serialize_cards(obj)
        self.assertIsInstance(res, dict)
        self.assertFalse(is_dataclass(res))
        self.assertEqual(res["title"], "Root")
        self.assertEqual(res["sub"], {"name": "Child", "value": 42})
        self.assertEqual(
            res["sub_list"],
            [{"name": "ListChild1", "value": 1}, {"name": "ListChild2", "value": 2}],
        )

    def test_pydantic_and_primitives_preservation(self):
        from backend.schemas.schemas import JobCard
        card = JobCard(
            job_id=123,
            title="Software Engineer",
            company="Tech Corp",
            location="Remote",
            url="https://collarcheck.com/jobs/123",
        )
        data = {
            "card": card,
            "primitives": {"str": "hello", "int": 10, "none": None, "bool": True},
            "collections": {"set": {"a", "b"}, "tuple": (1, 2)},
        }
        serialized = _serialize_cards(data)
        self.assertEqual(serialized["card"]["job_id"], 123)
        self.assertEqual(serialized["primitives"]["str"], "hello")
        self.assertIsNone(serialized["primitives"]["none"])
        self.assertIsInstance(serialized["collections"]["set"], list)
        self.assertEqual(sorted(serialized["collections"]["set"]), ["a", "b"])
        self.assertEqual(serialized["collections"]["tuple"], [1, 2])

    def test_recommendation_result_payload_serialization(self):
        from backend.recommendation.models import (
            RecommendationResult,
            SkillMatchResult,
            ExperienceMatchResult,
            LocationMatchResult,
            SalaryMatchResult,
            TitleMatchResult,
            ScoreResult,
            ExplanationResult,
            RecommendationReason,
        )

        score_res = ScoreResult(
            overall_score=85.0,
            recommendation_level="Good Match",
            skill_score=80.0,
            experience_score=90.0,
            location_score=100.0,
            salary_score=75.0,
            title_score=80.0,
            weight_skill=0.3,
            weight_experience=0.25,
            weight_location=0.2,
            weight_salary=0.15,
            weight_title=0.1,
        )
        skill_res = SkillMatchResult(
            score=80.0,
            matched_skills=["Python"],
            missing_skills=["FastAPI"],
            extra_skills=[],
            total_required=2,
            total_matched=1,
        )
        exp_res = ExperienceMatchResult(
            score=90.0,
            candidate_experience=3.0,
            required_experience=2.0,
            experience_gap=0.0,
            meets_requirement=True,
        )
        loc_res = LocationMatchResult(
            score=100.0,
            candidate_city="Delhi",
            candidate_state="Delhi",
            candidate_country="India",
            job_city="Delhi",
            job_state="Delhi",
            job_country="India",
            work_mode="Office",
            match_level="Exact",
        )
        sal_res = SalaryMatchResult(
            score=75.0,
            candidate_expected_salary=600000,
            job_offered_salary=700000,
            normalized_candidate_salary=600000.0,
            normalized_job_salary=700000.0,
            salary_difference_percent=16.6,
            meets_expectation=True,
        )
        title_res = TitleMatchResult(
            score=80.0,
            candidate_title="Python Dev",
            job_title="Software Developer",
            normalized_candidate_title="python dev",
            normalized_job_title="software developer",
            matched_keywords=["developer"],
            match_level="Partial",
        )
        rec_reason = RecommendationReason(
            type="skill",
            label="Skills",
            message="Matched Python",
            evidence={"matched_skills": ["Python"]},
        )
        explanation_res = ExplanationResult(
            summary="Matched profile",
            strengths=["Skill match"],
            weaknesses=[],
            recommendation_level="Good Match",
            overall_score=85.0,
            search_reasons=["Profile match"],
            recommendation_reasons=[rec_reason],
        )

        rec_result = RecommendationResult(
            overall_score=85.0,
            recommendation_level="Good Match",
            skill_result=skill_res,
            experience_result=exp_res,
            location_result=loc_res,
            salary_result=sal_res,
            title_result=title_res,
            score_result=score_res,
            explanation_result=explanation_res,
            candidate_id=101,
            job_id=585,
            recommendation_reasons=[rec_reason],
        )

        pagination_meta = {
            "current_page": 1,
            "per_page": 10,
            "total_results": 1,
            "total_pages": 1,
            "all_recs": [rec_result],
        }

        serialized = _serialize_cards(pagination_meta)

        # 1. No dataclasses remain
        def assert_no_dataclass(obj):
            self.assertFalse(is_dataclass(obj), f"Found un-serialized dataclass: {type(obj)}")
            if isinstance(obj, dict):
                for v in obj.values():
                    assert_no_dataclass(v)
            elif isinstance(obj, list):
                for item in obj:
                    assert_no_dataclass(item)

        assert_no_dataclass(serialized)

        # 2. Key values preserved
        self.assertEqual(len(serialized["all_recs"]), 1)
        rec_dict = serialized["all_recs"][0]
        self.assertEqual(rec_dict["job_id"], 585)
        self.assertEqual(rec_dict["overall_score"], 85.0)
        self.assertEqual(rec_dict["skill_result"]["score"], 80.0)
        self.assertEqual(rec_dict["recommendation_reasons"][0]["message"], "Matched Python")

        # 3. Payload is directly JSON serializable
        json_str = json.dumps(serialized, ensure_ascii=False)
        self.assertIn('"job_id": 585', json_str)

    def test_sse_results_event_with_serialized_recommendation_payload(self):
        from backend.schemas.schemas import JobCard
        from backend.recommendation.models import (
            RecommendationResult,
            SkillMatchResult,
            ExperienceMatchResult,
            LocationMatchResult,
            SalaryMatchResult,
            TitleMatchResult,
            ScoreResult,
            ExplanationResult,
        )

        rec_result = RecommendationResult(
            overall_score=92.0,
            recommendation_level="Top Match",
            skill_result=SkillMatchResult(score=90.0, matched_skills=["SQL"], missing_skills=[], extra_skills=[], total_required=1, total_matched=1),
            experience_result=ExperienceMatchResult(score=100.0, candidate_experience=2.0, required_experience=1.0, experience_gap=0.0, meets_requirement=True),
            location_result=LocationMatchResult(score=100.0, candidate_city="Chennai", candidate_state="TN", candidate_country="India", job_city="Chennai", job_state="TN", job_country="India", work_mode="Office", match_level="Exact"),
            salary_result=SalaryMatchResult(score=90.0, candidate_expected_salary=None, job_offered_salary=None, normalized_candidate_salary=None, normalized_job_salary=None, salary_difference_percent=0.0, meets_expectation=True),
            title_result=TitleMatchResult(score=90.0, candidate_title="Analyst", job_title="Data Analyst", normalized_candidate_title="analyst", normalized_job_title="data analyst", matched_keywords=["analyst"], match_level="Strong"),
            score_result=ScoreResult(overall_score=92.0, recommendation_level="Top Match", skill_score=90.0, experience_score=100.0, location_score=100.0, salary_score=90.0, title_score=90.0, weight_skill=0.3, weight_experience=0.25, weight_location=0.2, weight_salary=0.15, weight_title=0.1),
            explanation_result=ExplanationResult(summary="Top Match", strengths=[], weaknesses=[], recommendation_level="Top Match", overall_score=92.0),
            candidate_id=1,
            job_id=181,
        )

        job_card = JobCard(
            job_id=181,
            title="Data Analyst",
            company="CollarCheck Partner",
            location="Chennai",
            url="https://collarcheck.com/jobs/181",
        )

        pipeline_pagination = {
            "current_page": 1,
            "per_page": 10,
            "total_results": 1,
            "total_pages": 1,
            "all_recs": [rec_result],
        }

        res_payload = {
            "results": _serialize_cards([job_card]),
            "result_type": "jobs",
            "pagination": _serialize_cards(pipeline_pagination),
        }

        # format_sse_event must not raise TypeError
        sse_frame = format_sse_event("results", res_payload)
        parsed = parse_sse_event(sse_frame)

        self.assertEqual(parsed["event"], "results")
        self.assertEqual(parsed["data"]["result_type"], "jobs")
        self.assertEqual(len(parsed["data"]["results"]), 1)
        self.assertEqual(parsed["data"]["results"][0]["job_id"], 181)
        self.assertEqual(parsed["data"]["pagination"]["all_recs"][0]["job_id"], 181)


