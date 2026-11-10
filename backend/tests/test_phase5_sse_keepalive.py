"""
backend/tests/test_phase5_sse_keepalive.py — Targeted test suite for Phase 5.0 SSE Keepalive Comments.

Covers:
1. Delayed pipeline emits at least one keepalive before its subsequent real events.
2. Delayed upstream stream emits keepalives between real events.
3. Real SSE event ordering and payloads remain unchanged.
4. No keepalive is emitted after done or error.
5. Client disconnection or generator cancellation cleans up pending tasks and closes the upstream generator.
6. Pipeline exceptions do not leave pending tasks or cause duplicate error events.
7. A normal short response does not emit unnecessary keepalives.
8. The legacy JSON /chat route remains 100% unchanged.
"""

import asyncio
import json
import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend.main import app, ChatPipelineResult
from backend.config.config import JWT_SECRET, JWT_ALGORITHM
from backend.services.llm_service import LLMService
from backend.utils.sse import (
    format_sse_event,
    format_sse_comment,
    parse_sse_event,
    is_sse_comment,
    await_with_keepalive,
    stream_with_keepalive,
    validate_keepalive_interval,
)
from backend.schemas.schemas import JobCard


class TestPhase5SSEKeepalive(unittest.TestCase):
    """Targeted integration and contract tests for Phase 5 SSE Keepalive Comments."""

    def setUp(self):
        self.client = TestClient(app)
        self.valid_jwt = jwt.encode(
            {"id": 999, "uid": 999, "email": "keepalive_user@example.com", "role": "employee"},
            JWT_SECRET,
            algorithm=JWT_ALGORITHM,
        )
        self.headers = {"Authorization": f"Bearer {self.valid_jwt}"}
        self.db_user_row = {
            "id": 999,
            "individual_id": "IND999",
            "fname": "Keepalive",
            "lname": "Tester",
            "email": "keepalive_user@example.com",
            "user_type": 1,
            "status": 1,
            "is_deleted": 0,
            "token": "platform_token_keepalive",
        }

    # ------------------------------------------------------------------------
    # 1. Delayed pipeline emits at least one keepalive before subsequent real events
    # ------------------------------------------------------------------------
    def test_delayed_pipeline_emits_keepalive_in_chat_stream(self):
        with patch("backend.api.auth.get_db") as mock_auth_db:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_auth_db.return_value = mock_conn
            mock_conn.cursor.return_value = mock_cursor
            mock_cursor.fetchall.return_value = [self.db_user_row]

            async def slow_pipeline(*args, **kwargs):
                # Delay to trigger keepalive (interval configured to 0.03s)
                await asyncio.sleep(0.08)
                return ChatPipelineResult(
                    role="jobseeker",
                    intent_hint="smalltalk",
                    msg_len=10,
                    system="System",
                    bounded_messages=[{"role": "user", "content": "hello"}],
                    max_tokens_compose=100,
                    is_terminal=False,
                    results_payload=None,
                    result_type_payload=None,
                )

            async def fake_stream(*args, **kwargs):
                yield "Hello after delayed pipeline."

            with patch("backend.main.execute_chat_pipeline", side_effect=slow_pipeline):
                with patch("backend.services.llm_service.LLMService.stream_chat_completion", side_effect=fake_stream):
                    with patch("backend.main.SSE_KEEPALIVE_INTERVAL_SECONDS", 0.03):
                        payload = {"messages": [{"role": "user", "content": "hello"}]}
                        res = self.client.post("/chat/stream", json=payload, headers=self.headers)

            self.assertEqual(res.status_code, 200)
            raw_blocks = [b.strip() for b in res.text.strip().split("\n\n") if b.strip()]

            # Must contain at least one keepalive comment block
            comments = [b for b in raw_blocks if is_sse_comment(b)]
            self.assertGreaterEqual(len(comments), 1, "Must emit at least one keepalive during delayed pipeline")
            self.assertEqual(comments[0], ": keepalive")

            # Verify that the keepalive appeared AFTER start and BEFORE status/delta/done
            first_comment_idx = raw_blocks.index(comments[0])
            start_idx = next(i for i, b in enumerate(raw_blocks) if b.startswith("event: start"))
            status_idx = next(i for i, b in enumerate(raw_blocks) if b.startswith("event: status"))
            self.assertLess(start_idx, first_comment_idx, "Start event must precede keepalive")
            self.assertLess(first_comment_idx, status_idx, "Keepalive must precede pipeline results/status")

    # ------------------------------------------------------------------------
    # 2. Delayed upstream stream emits keepalives between real events
    # ------------------------------------------------------------------------
    def test_delayed_upstream_stream_emits_keepalive_between_events(self):
        with patch("backend.api.auth.get_db") as mock_auth_db:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_auth_db.return_value = mock_conn
            mock_conn.cursor.return_value = mock_cursor
            mock_cursor.fetchall.return_value = [self.db_user_row]

            mock_pipeline = ChatPipelineResult(
                role="jobseeker",
                intent_hint="smalltalk",
                msg_len=10,
                system="System",
                bounded_messages=[{"role": "user", "content": "hi"}],
                max_tokens_compose=100,
                is_terminal=False,
                results_payload=None,
                result_type_payload=None,
            )

            async def slow_stream(*args, **kwargs):
                yield "Part 1"
                # Delay between tokens to trigger keepalive (interval 0.03s)
                await asyncio.sleep(0.08)
                yield "Part 2"

            with patch("backend.main.execute_chat_pipeline", return_value=mock_pipeline):
                with patch("backend.services.llm_service.LLMService.stream_chat_completion", side_effect=slow_stream):
                    with patch("backend.main.SSE_KEEPALIVE_INTERVAL_SECONDS", 0.03):
                        payload = {"messages": [{"role": "user", "content": "hi"}]}
                        res = self.client.post("/chat/stream", json=payload, headers=self.headers)

            self.assertEqual(res.status_code, 200)
            raw_blocks = [b.strip() for b in res.text.strip().split("\n\n") if b.strip()]

            # Must contain keepalive comment between Part 1 and Part 2 deltas
            comments = [b for b in raw_blocks if is_sse_comment(b)]
            self.assertGreaterEqual(len(comments), 1, "Must emit keepalive during upstream streaming delay")

            delta_blocks = [b for b in raw_blocks if b.startswith("event: delta")]
            self.assertEqual(len(delta_blocks), 2, "Must receive both delta tokens")
            self.assertIn("Part 1", delta_blocks[0])
            self.assertIn("Part 2", delta_blocks[1])

            idx_delta1 = raw_blocks.index(delta_blocks[0])
            idx_delta2 = raw_blocks.index(delta_blocks[1])
            idx_comment = raw_blocks.index(comments[0])
            self.assertLess(idx_delta1, idx_comment, "First delta must arrive before keepalive")
            self.assertLess(idx_comment, idx_delta2, "Keepalive must arrive before second delta")

    # ------------------------------------------------------------------------
    # 3. Real SSE event ordering and payloads remain unchanged
    # ------------------------------------------------------------------------
    def test_real_sse_event_ordering_and_payloads_unchanged(self):
        with patch("backend.api.auth.get_db") as mock_auth_db:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_auth_db.return_value = mock_conn
            mock_conn.cursor.return_value = mock_cursor
            mock_cursor.fetchall.return_value = [self.db_user_row]

            card = JobCard(
                job_id=901,
                title="Staff Engineer",
                company="Acme Corp",
                location="Bengaluru",
                url="https://collarcheck.com/jobs/901",
            )
            mock_pipeline = ChatPipelineResult(
                role="jobseeker",
                intent_hint="job_search",
                msg_len=15,
                system="System",
                bounded_messages=[{"role": "user", "content": "jobs"}],
                max_tokens_compose=150,
                is_terminal=False,
                results_payload=[card],
                result_type_payload="jobs",
                allowed_ids={"901"},
                pagination={"total": 1, "page": 1, "pages": 1, "limit": 10},
                suggestion_chips=["Apply Now"],
            )

            async def fake_stream(*args, **kwargs):
                yield "Here are matching roles."

            with patch("backend.main.execute_chat_pipeline", return_value=mock_pipeline):
                with patch("backend.services.llm_service.LLMService.stream_chat_completion", side_effect=fake_stream):
                    payload = {"messages": [{"role": "user", "content": "jobs"}]}
                    res = self.client.post("/chat/stream", json=payload, headers=self.headers)

            self.assertEqual(res.status_code, 200)
            raw_blocks = [b.strip() for b in res.text.strip().split("\n\n") if b.strip()]
            parsed_events = [parse_sse_event(b) for b in raw_blocks]
            real_events = [e for e in parsed_events if e["event"] != "comment"]

            # Exact event ordering preserved
            event_types = [e["event"] for e in real_events]
            self.assertEqual(event_types, ["start", "results", "status", "delta", "done"])

            # Verify results payload
            results_evt = next(e for e in real_events if e["event"] == "results")
            self.assertEqual(results_evt["data"]["result_type"], "jobs")
            self.assertEqual(results_evt["data"]["results"][0]["job_id"], 901)
            self.assertEqual(results_evt["data"]["pagination"]["total"], 1)

            # Verify done payload
            done_evt = next(e for e in real_events if e["event"] == "done")
            self.assertTrue(done_evt["data"]["success"])

    def test_structured_results_framing_with_keepalive(self):
        with patch("backend.api.auth.get_db") as mock_auth_db:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_auth_db.return_value = mock_conn
            mock_conn.cursor.return_value = mock_cursor
            mock_cursor.fetchall.return_value = [self.db_user_row]

            card = JobCard(
                job_id=777,
                title="Principal Cloud Architect",
                company="SkyTech",
                location="Bengaluru",
                url="https://collarcheck.com/jobs/777",
            )
            mock_pipeline = ChatPipelineResult(
                role="jobseeker",
                intent_hint="job_search",
                msg_len=15,
                system="System",
                bounded_messages=[{"role": "user", "content": "cloud jobs"}],
                max_tokens_compose=150,
                is_terminal=False,
                results_payload=[card],
                result_type_payload="jobs",
                allowed_ids={"777"},
            )

            async def slow_stream(*args, **kwargs):
                yield "Here are matching cloud "
                # Delay to trigger keepalive during structured framing buffer
                await asyncio.sleep(0.06)
                yield "engineering roles."

            with patch("backend.main.execute_chat_pipeline", return_value=mock_pipeline):
                with patch("backend.services.llm_service.LLMService.stream_chat_completion", side_effect=slow_stream):
                    with patch("backend.main.SSE_KEEPALIVE_INTERVAL_SECONDS", 0.02):
                        payload = {"messages": [{"role": "user", "content": "cloud jobs"}]}
                        res = self.client.post("/chat/stream", json=payload, headers=self.headers)

            self.assertEqual(res.status_code, 200)
            raw_blocks = [b.strip() for b in res.text.strip().split("\n\n") if b.strip()]

            # Standalone keepalive comments emitted
            comments = [b for b in raw_blocks if is_sse_comment(b)]
            self.assertGreaterEqual(len(comments), 1, "Must emit keepalive during structured framing delay")
            self.assertEqual(comments[0], ": keepalive")

            # Parse all real events
            parsed_events = [parse_sse_event(b) for b in raw_blocks]
            real_events = [e for e in parsed_events if e["event"] != "comment"]
            event_types = [e["event"] for e in real_events]

            # Sequence: start -> results -> status -> delta -> done
            self.assertEqual(event_types, ["start", "results", "status", "delta", "done"])

            # Verify delta text does NOT contain keepalive comment strings
            delta_evt = next(e for e in real_events if e["event"] == "delta")
            delta_text = delta_evt["data"]["text"]
            self.assertIn("Here are matching cloud engineering roles", delta_text)
            self.assertNotIn(": keepalive", delta_text)
            self.assertNotIn("keepalive", delta_text)

            # Results payload preserved
            results_evt = next(e for e in real_events if e["event"] == "results")
            self.assertEqual(results_evt["data"]["result_type"], "jobs")
            self.assertEqual(results_evt["data"]["results"][0]["job_id"], 777)

            # Exactly one done event
            done_evts = [e for e in real_events if e["event"] == "done"]
            self.assertEqual(len(done_evts), 1, "Must emit exactly one terminal done event")
            self.assertTrue(done_evts[0]["data"]["success"])

    # ------------------------------------------------------------------------
    # 4. No keepalive emitted after done or error
    # ------------------------------------------------------------------------
    def test_no_keepalive_after_done(self):
        with patch("backend.api.auth.get_db") as mock_auth_db:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_auth_db.return_value = mock_conn
            mock_conn.cursor.return_value = mock_cursor
            mock_cursor.fetchall.return_value = [self.db_user_row]

            mock_pipeline = ChatPipelineResult(
                role="jobseeker",
                intent_hint="smalltalk",
                msg_len=5,
                system="System",
                bounded_messages=[{"role": "user", "content": "hi"}],
                max_tokens_compose=50,
                is_terminal=True,
                reply="Terminal response reply",
            )

            with patch("backend.main.execute_chat_pipeline", return_value=mock_pipeline):
                payload = {"messages": [{"role": "user", "content": "hi"}]}
                res = self.client.post("/chat/stream", json=payload, headers=self.headers)

            self.assertEqual(res.status_code, 200)
            raw_blocks = [b.strip() for b in res.text.strip().split("\n\n") if b.strip()]

            # Last block MUST be the done event, no trailing keepalives
            self.assertTrue(raw_blocks[-1].startswith("event: done"), "Stream must terminate with done event")
            parsed = parse_sse_event(raw_blocks[-1])
            self.assertEqual(parsed["event"], "done")
            self.assertEqual(parsed["data"]["reply"], "Terminal response reply")

    def test_no_keepalive_after_error(self):
        with patch("backend.api.auth.get_db") as mock_auth_db:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_auth_db.return_value = mock_conn
            mock_conn.cursor.return_value = mock_cursor
            mock_cursor.fetchall.return_value = [self.db_user_row]

            with patch("backend.main.execute_chat_pipeline", side_effect=HTTPException(status_code=400, detail="Invalid request content")):
                payload = {"messages": [{"role": "user", "content": "bad"}]}
                res = self.client.post("/chat/stream", json=payload, headers=self.headers)

            self.assertEqual(res.status_code, 200)
            raw_blocks = [b.strip() for b in res.text.strip().split("\n\n") if b.strip()]

            # Last block MUST be the error event, no trailing keepalives
            self.assertTrue(raw_blocks[-1].startswith("event: error"), "Stream must terminate with error event")
            parsed = parse_sse_event(raw_blocks[-1])
            self.assertEqual(parsed["event"], "error")
            self.assertEqual(parsed["data"]["error_code"], "HTTP_ERROR")

    # ------------------------------------------------------------------------
    # 5. Client disconnect or cancellation cleans up tasks and closes generator
    # ------------------------------------------------------------------------
    def test_disconnect_cleans_up_generator(self):
        with patch("backend.api.auth.get_db") as mock_auth_db:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_auth_db.return_value = mock_conn
            mock_conn.cursor.return_value = mock_cursor
            mock_cursor.fetchall.return_value = [self.db_user_row]

            mock_pipeline = ChatPipelineResult(
                role="jobseeker",
                intent_hint="smalltalk",
                msg_len=5,
                system="System",
                bounded_messages=[{"role": "user", "content": "hi"}],
                max_tokens_compose=50,
                is_terminal=False,
            )

            generator_closed = False
            token_emitted = False

            async def mock_stream(*args, **kwargs):
                nonlocal generator_closed, token_emitted
                try:
                    token_emitted = True
                    yield "Token 1"
                    await asyncio.sleep(0.05)
                    yield "Token 2"
                finally:
                    generator_closed = True

            async def fake_is_disconnected():
                return token_emitted

            with patch("backend.main.execute_chat_pipeline", return_value=mock_pipeline):
                with patch("backend.services.llm_service.LLMService.stream_chat_completion", side_effect=mock_stream):
                    with patch.object(Request, "is_disconnected", side_effect=fake_is_disconnected):
                        with patch("backend.main.SSE_KEEPALIVE_INTERVAL_SECONDS", 0.02):
                            payload = {"messages": [{"role": "user", "content": "hi"}]}
                            res = self.client.post("/chat/stream", json=payload, headers=self.headers)

            self.assertEqual(res.status_code, 200)
            self.assertTrue(generator_closed, "stream_gen must be cleanly closed via aclose() upon disconnect")

    def test_disconnect_during_retrieval(self):
        with patch("backend.api.auth.get_db") as mock_auth_db:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_auth_db.return_value = mock_conn
            mock_conn.cursor.return_value = mock_cursor
            mock_cursor.fetchall.return_value = [self.db_user_row]

            pipeline_started = asyncio.Event()
            pipeline_cancelled = asyncio.Event()

            async def slow_pipeline(*args, **kwargs):
                pipeline_started.set()
                try:
                    await asyncio.sleep(10.0)
                except asyncio.CancelledError:
                    pipeline_cancelled.set()
                    raise
                return ChatPipelineResult(
                    role="jobseeker",
                    intent_hint="smalltalk",
                    msg_len=5,
                    system="System",
                    bounded_messages=[{"role": "user", "content": "hi"}],
                    max_tokens_compose=50,
                    is_terminal=False,
                )

            keepalive_checked = False

            async def fake_is_disconnected():
                nonlocal keepalive_checked
                if pipeline_started.is_set():
                    # Return False on first check to let keepalive emit, then True on next check
                    if not keepalive_checked:
                        keepalive_checked = True
                        return False
                    return True
                return False

            with patch("backend.main.execute_chat_pipeline", side_effect=slow_pipeline):
                with patch("backend.services.llm_service.LLMService.stream_chat_completion") as mock_stream:
                    with patch.object(Request, "is_disconnected", side_effect=fake_is_disconnected):
                        with patch("backend.main.SSE_KEEPALIVE_INTERVAL_SECONDS", 0.02):
                            payload = {"messages": [{"role": "user", "content": "hi"}]}
                            res = self.client.post("/chat/stream", json=payload, headers=self.headers)

            self.assertEqual(res.status_code, 200)
            raw_blocks = [b.strip() for b in res.text.strip().split("\n\n") if b.strip()]

            # Must contain start event and at least one keepalive comment
            self.assertTrue(any(b.startswith("event: start") for b in raw_blocks))
            self.assertTrue(any(is_sse_comment(b) for b in raw_blocks), "Must emit keepalive while retrieval is waiting")

            # Must NOT start LLM generator after disconnect
            mock_stream.assert_not_called()

            # Pending pipeline task must have been cancelled
            self.assertTrue(pipeline_cancelled.is_set(), "Pipeline task must be cancelled on disconnect")

            # No done or error event emitted after disconnect
            self.assertFalse(any(b.startswith("event: done") for b in raw_blocks), "Must not emit done after disconnect")
            self.assertFalse(any(b.startswith("event: error") for b in raw_blocks), "Must not emit error after disconnect")

    # ------------------------------------------------------------------------
    # 6. Pipeline exceptions do not leave pending tasks or duplicate error events
    # ------------------------------------------------------------------------
    def test_pipeline_exception_single_error_event(self):
        with patch("backend.api.auth.get_db") as mock_auth_db:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_auth_db.return_value = mock_conn
            mock_conn.cursor.return_value = mock_cursor
            mock_cursor.fetchall.return_value = [self.db_user_row]

            with patch("backend.main.execute_chat_pipeline", side_effect=RuntimeError("Database failure")):
                payload = {"messages": [{"role": "user", "content": "crash"}]}
                res = self.client.post("/chat/stream", json=payload, headers=self.headers)

            self.assertEqual(res.status_code, 200)
            raw_blocks = [b.strip() for b in res.text.strip().split("\n\n") if b.strip()]
            error_blocks = [b for b in raw_blocks if b.startswith("event: error")]

            # Exactly one error event emitted
            self.assertEqual(len(error_blocks), 1, "Must emit exactly one error event on unhandled exception")
            parsed = parse_sse_event(error_blocks[0])
            self.assertEqual(parsed["data"]["error_code"], "INTERNAL_ERROR")

    # ------------------------------------------------------------------------
    # 7. Normal short response does not emit unnecessary keepalives
    # ------------------------------------------------------------------------
    def test_normal_short_response_emits_no_keepalives(self):
        with patch("backend.api.auth.get_db") as mock_auth_db:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_auth_db.return_value = mock_conn
            mock_conn.cursor.return_value = mock_cursor
            mock_cursor.fetchall.return_value = [self.db_user_row]

            mock_pipeline = ChatPipelineResult(
                role="jobseeker",
                intent_hint="smalltalk",
                msg_len=5,
                system="System",
                bounded_messages=[{"role": "user", "content": "hi"}],
                max_tokens_compose=50,
                is_terminal=False,
            )

            async def fast_stream(*args, **kwargs):
                yield "Hello there!"

            with patch("backend.main.execute_chat_pipeline", return_value=mock_pipeline):
                with patch("backend.services.llm_service.LLMService.stream_chat_completion", side_effect=fast_stream):
                    # Default conservative interval: 15.0s
                    with patch("backend.main.SSE_KEEPALIVE_INTERVAL_SECONDS", 15.0):
                        payload = {"messages": [{"role": "user", "content": "hi"}]}
                        res = self.client.post("/chat/stream", json=payload, headers=self.headers)

            self.assertEqual(res.status_code, 200)
            raw_blocks = [b.strip() for b in res.text.strip().split("\n\n") if b.strip()]

            # Zero keepalive comments
            comments = [b for b in raw_blocks if is_sse_comment(b)]
            self.assertEqual(len(comments), 0, "Fast response must emit 0 keepalive comments")

            event_types = [parse_sse_event(b)["event"] for b in raw_blocks]
            self.assertEqual(event_types, ["start", "status", "delta", "done"])

    # ------------------------------------------------------------------------
    # 8. Legacy JSON /chat route remains unchanged
    # ------------------------------------------------------------------------
    def test_legacy_json_chat_route_unchanged(self):
        with patch("backend.api.auth.get_db") as mock_auth_db:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_auth_db.return_value = mock_conn
            mock_conn.cursor.return_value = mock_cursor
            mock_cursor.fetchall.return_value = [self.db_user_row]

            mock_pipeline = ChatPipelineResult(
                role="jobseeker",
                intent_hint="smalltalk",
                msg_len=5,
                system="System",
                bounded_messages=[{"role": "user", "content": "hi"}],
                max_tokens_compose=50,
                is_terminal=True,
                reply="Hello from JSON /chat",
            )

            with patch("backend.main.execute_chat_pipeline", return_value=mock_pipeline):
                payload = {"messages": [{"role": "user", "content": "hi"}]}
                res = self.client.post("/chat", json=payload, headers=self.headers)

            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.headers["content-type"].startswith("application/json"))
            body = res.json()
            self.assertEqual(body["reply"], "Hello from JSON /chat")


class TestSSEKeepaliveUtilities(unittest.IsolatedAsyncioTestCase):
    """Direct isolated unit tests for await_with_keepalive and stream_with_keepalive."""

    async def test_await_with_keepalive_emits_comment_on_delay(self):
        async def slow_fn():
            await asyncio.sleep(0.06)
            return "finished"

        items = []
        async for kind, payload in await_with_keepalive(slow_fn(), interval=0.02):
            items.append((kind, payload))

        # Must have at least one comment and the final result
        comments = [p for k, p in items if k == "comment"]
        results = [p for k, p in items if k == "result"]
        self.assertGreaterEqual(len(comments), 1)
        self.assertEqual(comments[0], ": keepalive\n\n")
        self.assertEqual(results, ["finished"])

    async def test_await_with_keepalive_cancels_task_on_error(self):
        async def failing_fn():
            await asyncio.sleep(0.01)
            raise ValueError("Failure in coro")

        with self.assertRaises(ValueError):
            async for _ in await_with_keepalive(failing_fn(), interval=0.05):
                pass

    async def test_stream_with_keepalive_closes_generator_on_cancellation(self):
        generator_closed = False

        async def infinite_stream():
            nonlocal generator_closed
            try:
                while True:
                    yield "item"
                    await asyncio.sleep(0.01)
            finally:
                generator_closed = True

        stream = infinite_stream()
        gen = stream_with_keepalive(stream, interval=0.05)
        item = await gen.__anext__()
        self.assertEqual(item, ("token", "item"))
        await gen.aclose()

        self.assertTrue(generator_closed, "Upstream generator must be closed when stream_with_keepalive is closed via aclose()")


class TestKeepaliveConfiguration(unittest.TestCase):
    """Direct tests for keepalive interval validation policy."""

    def test_valid_positive_float(self):
        self.assertEqual(validate_keepalive_interval(10.5), 10.5)

    def test_valid_positive_int(self):
        self.assertEqual(validate_keepalive_interval(5), 5.0)

    def test_valid_positive_str(self):
        self.assertEqual(validate_keepalive_interval("12.0"), 12.0)

    def test_none_defaults(self):
        self.assertEqual(validate_keepalive_interval(None), 15.0)

    def test_invalid_string_defaults(self):
        self.assertEqual(validate_keepalive_interval("invalid_str"), 15.0)
        self.assertEqual(validate_keepalive_interval("15s"), 15.0)
        self.assertEqual(validate_keepalive_interval("disabled"), 15.0)

    def test_zero_defaults(self):
        self.assertEqual(validate_keepalive_interval(0), 15.0)
        self.assertEqual(validate_keepalive_interval("0.0"), 15.0)

    def test_negative_defaults(self):
        self.assertEqual(validate_keepalive_interval(-5.0), 15.0)
        self.assertEqual(validate_keepalive_interval("-1"), 15.0)

    def test_nan_defaults(self):
        self.assertEqual(validate_keepalive_interval(float("nan")), 15.0)

    def test_positive_infinity_defaults(self):
        self.assertEqual(validate_keepalive_interval(float("inf")), 15.0)

    def test_negative_infinity_defaults(self):
        self.assertEqual(validate_keepalive_interval(float("-inf")), 15.0)

    def test_invalid_type_defaults(self):
        self.assertEqual(validate_keepalive_interval([15]), 15.0)
        self.assertEqual(validate_keepalive_interval({"interval": 15}), 15.0)
