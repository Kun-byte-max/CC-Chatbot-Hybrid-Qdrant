"""
backend/tests/test_langfuse_telemetry.py — Comprehensive targeted tests for Langfuse tracing.

Tests cover:
1. Tracing disabled: existing behavior preserved, client is None, NoOpSpan used.
2. Tracing enabled: one request trace is created and correlated.
3. Final generation and parser calls have distinguishable observation names.
4. Provider token usage is recorded accurately.
5. Missing usage does not become zero.
6. Telemetry initialization/export failure does not break chat.
7. SSE chunks remain incremental and in the original wire format.
8. TTFT and total stream duration are recorded correctly.
9. Client disconnect and cancellation do not corrupt cleanup.
10. Credentials and sensitive records are not emitted in trace metadata.
"""

import asyncio
import hashlib
import hmac
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.config.config import validate_sample_rate
from backend.utils.telemetry import (
    NoOpGeneration,
    NoOpObservation,
    RequestObservationWrapper,
    GenerationObservationWrapper,
    is_telemetry_enabled,
    get_langfuse_client,
    reset_telemetry_client_for_testing,
    sanitize_telemetry_metadata,
    validate_retrieval_metadata,
    compute_session_hmac,
    shutdown_telemetry,
    trace_request,
    trace_generation,
)
from backend.services.llm_service import LLMService
from backend.main import chat, chat_stream, ChatRequest, ChatPipelineResult
from backend.services.hybrid_job_search import execute_hybrid_job_search
from backend.services.job_match_client import (
    CandidateNotIndexedException,
    JobMatchAuthException,
    JobMatchContractException,
    JobMatchUnavailableException,
)


class TestLangfuseTelemetry(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        reset_telemetry_client_for_testing()

    def tearDown(self):
        reset_telemetry_client_for_testing()

    # 1. Tracing disabled: existing behavior preserved
    def test_tracing_disabled_by_default(self):
        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=False):
            self.assertFalse(is_telemetry_enabled())
            client = get_langfuse_client()
            self.assertIsNone(client)

            with trace_request(endpoint="/chat", request_id="req123") as span:
                self.assertIsInstance(span, NoOpObservation)
                span.set_outcome("ok")
                span.set_total_ms(150)

            with trace_generation(name="test_gen") as gen:
                self.assertIsInstance(gen, NoOpGeneration)
                gen.set_usage(10, 20)
                gen.set_ttft(45.5)

    # 2. Tracing enabled: one request trace is created and correlated
    def test_tracing_enabled_request_correlated(self):
        mock_raw_obs = MagicMock()
        mock_client = MagicMock()
        mock_client.start_as_current_observation.return_value.__enter__.return_value = mock_raw_obs

        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=True), \
             patch("backend.utils.telemetry.get_langfuse_client", return_value=mock_client):

            with trace_request(
                endpoint="/chat",
                request_id="test_req_001",
                role="employee",
                session_id="session_xyz",
            ) as req_trace:
                self.assertIsInstance(req_trace, RequestObservationWrapper)
                req_trace.set_role("employee")
                req_trace.set_intent_hint("search")
                req_trace.set_outcome("ok")
                req_trace.set_total_ms(120)

            mock_client.start_as_current_observation.assert_called_once()
            call_kwargs = mock_client.start_as_current_observation.call_args[1]
            self.assertEqual(call_kwargs["name"], "chat.request")
            self.assertEqual(call_kwargs["as_type"], "chain")
            self.assertEqual(call_kwargs["metadata"]["request_id"], "test_req_001")
            self.assertEqual(call_kwargs["metadata"]["endpoint"], "/chat")
            # Raw session_id is omitted by default when no HMAC secret is configured
            self.assertNotIn("session_id", call_kwargs["metadata"])
            self.assertNotIn("session_ref", call_kwargs["metadata"])

            mock_raw_obs.update.assert_called_once()
            update_kwargs = mock_raw_obs.update.call_args[1]
            self.assertEqual(update_kwargs["metadata"]["outcome"], "ok")
            self.assertEqual(update_kwargs["metadata"]["total_ms"], 120)

    # 3. Final generation and parser calls have distinguishable observations
    async def test_distinguishable_observation_names(self):
        recorded_names = []

        class MockObs:
            def update(self, **kwargs): pass

        def mock_start(**kwargs):
            recorded_names.append(kwargs.get("name"))
            cm = MagicMock()
            cm.__enter__.return_value = MockObs()
            return cm

        mock_client = MagicMock()
        mock_client.start_as_current_observation.side_effect = mock_start

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": '{"is_search_intent": true}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 15, "total_tokens": 65},
        }

        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=True), \
             patch("backend.utils.telemetry.get_langfuse_client", return_value=mock_client), \
             patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):

            # Call as search_parser
            await LLMService.get_chat_completion("System prompt", [{"role": "user", "content": "Hi"}], name="search_parser")
            # Call as profile_parser
            await LLMService.get_chat_completion("System prompt", [{"role": "user", "content": "Hi"}], name="profile_parser")
            # Call as chat_compose
            await LLMService.get_chat_completion("System prompt", [{"role": "user", "content": "Hi"}], name="chat_compose")

        self.assertIn("search_parser", recorded_names)
        self.assertIn("profile_parser", recorded_names)
        self.assertIn("chat_compose", recorded_names)

    # 4. Provider token usage is recorded accurately
    async def test_provider_token_usage_recorded_accurately(self):
        mock_raw_gen = MagicMock()
        mock_client = MagicMock()
        mock_client.start_as_current_observation.return_value.__enter__.return_value = mock_raw_gen

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Hello candidate!"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 128, "completion_tokens": 42, "total_tokens": 170},
        }

        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=True), \
             patch("backend.utils.telemetry.get_langfuse_client", return_value=mock_client), \
             patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):

            reply = await LLMService.get_chat_completion("Prompt", [{"role": "user", "content": "Hi"}], name="chat_compose")
            self.assertEqual(reply, "Hello candidate!")

        mock_raw_gen.update.assert_called_once()
        update_kwargs = mock_raw_gen.update.call_args[1]
        self.assertEqual(update_kwargs["usage_details"], {"input": 128, "output": 42, "total": 170})
        self.assertEqual(update_kwargs["metadata"]["finish_reason"], "stop")

    # 5. Missing usage does not become zero
    def test_missing_usage_not_zeroed(self):
        mock_raw_gen = MagicMock()
        wrapper = GenerationObservationWrapper(mock_raw_gen)

        # Missing or negative usage from provider
        wrapper.set_usage(prompt_tokens=-1, completion_tokens=-1)
        wrapper.finalize()

        mock_raw_gen.update.assert_called_once()
        update_kwargs = mock_raw_gen.update.call_args[1]
        self.assertNotIn("usage_details", update_kwargs)

    # 6. Telemetry initialization/export failure does not break chat
    async def test_telemetry_failure_does_not_break_chat(self):
        mock_client = MagicMock()
        mock_client.start_as_current_observation.side_effect = RuntimeError("Langfuse connection failed!")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Normal response works!"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=True), \
             patch("backend.utils.telemetry.get_langfuse_client", return_value=mock_client), \
             patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):

            # Exception in telemetry should be swallowed and chat must return normally
            reply = await LLMService.get_chat_completion("Prompt", [{"role": "user", "content": "Hi"}])
            self.assertEqual(reply, "Normal response works!")

    # 7. SSE chunks remain incremental and in original format
    # 8. TTFT and total stream duration recorded correctly
    async def test_sse_streaming_incremental_and_ttft(self):
        mock_raw_gen = MagicMock()
        mock_client = MagicMock()
        mock_client.start_as_current_observation.return_value.__enter__.return_value = mock_raw_gen

        async def fake_aiter_lines():
            yield "data: " + json.dumps({"choices": [{"delta": {"content": "First "}, "finish_reason": None}]})
            yield "data: " + json.dumps({"choices": [{"delta": {"content": "token!"}, "finish_reason": None}]})
            yield "data: " + json.dumps({
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 8, "total_tokens": 38}
            })
            yield "data: [DONE]"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.aiter_lines = fake_aiter_lines

        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_stream_ctx.__aexit__ = AsyncMock()

        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=True), \
             patch("backend.utils.telemetry.get_langfuse_client", return_value=mock_client), \
             patch("httpx.AsyncClient.stream", return_value=mock_stream_ctx):

            tokens = []
            async for token in LLMService.stream_chat_completion("System prompt", [{"role": "user", "content": "Hi"}], name="chat_stream_compose"):
                tokens.append(token)

            self.assertEqual(tokens, ["First ", "token!"])

        # Check TTFT and usage were set on the observation
        mock_raw_gen.update.assert_called_once()
        update_kwargs = mock_raw_gen.update.call_args[1]
        self.assertIn("ttft_ms", update_kwargs["metadata"])
        self.assertGreaterEqual(update_kwargs["metadata"]["ttft_ms"], 0.0)
        self.assertEqual(update_kwargs["usage_details"], {"input": 30, "output": 8, "total": 38})

    # 9. Client disconnect and cancellation do not corrupt cleanup
    async def test_stream_cancellation_and_cleanup(self):
        mock_raw_gen = MagicMock()
        mock_client = MagicMock()
        mock_client.start_as_current_observation.return_value.__enter__.return_value = mock_raw_gen

        client_closed = False

        async def fake_aiter_lines():
            yield "data: " + json.dumps({"choices": [{"delta": {"content": "Chunk 1"}}]})
            # Simulate cancellation mid-stream
            raise asyncio.CancelledError()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.aiter_lines = fake_aiter_lines

        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_stream_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=True), \
             patch("backend.utils.telemetry.get_langfuse_client", return_value=mock_client), \
             patch("httpx.AsyncClient.stream", return_value=mock_stream_ctx):

            with self.assertRaises(asyncio.CancelledError):
                async for _ in LLMService.stream_chat_completion("System", [{"role": "user", "content": "Hi"}]):
                    pass

        # Verify generation error was recorded and finalize executed
        mock_raw_gen.update.assert_called_once()
        update_kwargs = mock_raw_gen.update.call_args[1]
        self.assertEqual(update_kwargs["level"], "ERROR")
        self.assertEqual(update_kwargs["metadata"]["error_type"], "CancelledError")

    # 10. Credentials, sensitive records, and sensitive values in strings are redacted
    def test_sensitive_metadata_sanitization(self):
        leaky_metadata = {
            "request_id": "req_999",
            "endpoint": "/chat",
            "Authorization": "Bearer super_secret_token",
            "jwt": "eyJhbGciOiJIUzI1NiJ9.secret",
            "password": "db_password_123",
            "token": "platform_token_xyz",
            "db_password": "eq$oadWrNP89NXvf",
            "salary": "1500000",
            "email": "user@example.com",
            "phone": "+919876543210",
            "safe_nested": {
                "role": "employee",
                "api_key": "hidden_key_123",
                "intent": "search",
                "notes": "Contact user at alice@example.com or phone +1-555-555-1234. Token was Bearer xyz123456789.",
                "header_val": "Bearer my_secret_token_123",
                "session_state": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w",
                "custom_key": "sk-proj-1234567890abcdefghij",
            }
        }

        sanitized = sanitize_telemetry_metadata(leaky_metadata)

        self.assertIn("request_id", sanitized)
        self.assertIn("endpoint", sanitized)
        self.assertNotIn("Authorization", sanitized)
        self.assertNotIn("jwt", sanitized)
        self.assertNotIn("password", sanitized)
        self.assertNotIn("token", sanitized)
        self.assertNotIn("db_password", sanitized)
        self.assertNotIn("salary", sanitized)
        self.assertNotIn("email", sanitized)
        self.assertNotIn("phone", sanitized)
        self.assertNotIn("api_key", sanitized["safe_nested"])
        self.assertEqual(sanitized["safe_nested"]["role"], "employee")
        self.assertEqual(sanitized["safe_nested"]["intent"], "search")
        # Assert string values are properly redacted
        self.assertIn("[REDACTED_EMAIL]", sanitized["safe_nested"]["notes"])
        self.assertIn("[REDACTED_PHONE]", sanitized["safe_nested"]["notes"])
        self.assertIn("[REDACTED_BEARER]", sanitized["safe_nested"]["notes"])
        self.assertEqual(sanitized["safe_nested"]["header_val"], "[REDACTED_BEARER]")
        self.assertEqual(sanitized["safe_nested"]["session_state"], "[REDACTED_JWT]")
        self.assertEqual(sanitized["safe_nested"]["custom_key"], "[REDACTED_API_KEY]")

    # 11. F-01: Sample rate validation and non-clamping fallback
    def test_sample_rate_validation_and_clamping(self):
        # Valid finite floats in [0.0, 1.0] are preserved exactly
        self.assertEqual(validate_sample_rate("0"), 0.0)
        self.assertEqual(validate_sample_rate("0.0"), 0.0)
        self.assertEqual(validate_sample_rate("0.5"), 0.5)
        self.assertEqual(validate_sample_rate("1"), 1.0)
        self.assertEqual(validate_sample_rate("1.0"), 1.0)
        self.assertEqual(validate_sample_rate(0.75), 0.75)

        # Malformed, non-numeric, or empty fall back to default (1.0)
        self.assertEqual(validate_sample_rate(""), 1.0)
        self.assertEqual(validate_sample_rate(None), 1.0)
        self.assertEqual(validate_sample_rate("invalid"), 1.0)
        self.assertEqual(validate_sample_rate("abc"), 1.0)

        # Finite out-of-range values fall back to default (not clamped to 0.0 or 1.0)
        self.assertEqual(validate_sample_rate("-0.5"), 1.0)
        self.assertEqual(validate_sample_rate("2.0"), 1.0)
        self.assertEqual(validate_sample_rate(-1.0), 1.0)
        self.assertEqual(validate_sample_rate(1.5), 1.0)

        # Non-finite values fall back to default
        self.assertEqual(validate_sample_rate("nan"), 1.0)
        self.assertEqual(validate_sample_rate("inf"), 1.0)
        self.assertEqual(validate_sample_rate("-inf"), 1.0)

    # 12. F-01: Disabled tracing with malformed sample rate does not crash or init client
    def test_disabled_mode_with_malformed_sample_rate_does_not_crash(self):
        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=False), \
             patch.dict("os.environ", {"LANGFUSE_SAMPLE_RATE": "not_a_number"}):
            self.assertFalse(is_telemetry_enabled())
            client = get_langfuse_client()
            self.assertIsNone(client)

    # 13. F-01: Sample rate is passed to SDK constructor
    def test_sample_rate_applied_to_client(self):
        mock_cls = MagicMock()
        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=True), \
             patch("backend.config.config.LANGFUSE_PUBLIC_KEY", "pk-lf-test"), \
             patch("backend.config.config.LANGFUSE_SECRET_KEY", "sk-lf-test"), \
             patch("backend.config.config.LANGFUSE_SAMPLE_RATE", 0.5), \
             patch("langfuse.Langfuse", mock_cls):
            client = get_langfuse_client()
            mock_cls.assert_called_once()
            call_kwargs = mock_cls.call_args[1]
            self.assertEqual(call_kwargs["sample_rate"], 0.5)

    # 14. F-02: Session ID privacy and HMAC
    def test_session_id_privacy_and_hmac(self):
        mock_raw_obs = MagicMock()
        mock_client = MagicMock()
        mock_client.start_as_current_observation.return_value.__enter__.return_value = mock_raw_obs

        # Case A: Without dedicated HMAC secret, session_id is omitted completely
        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=True), \
             patch("backend.utils.telemetry.get_langfuse_client", return_value=mock_client), \
             patch("backend.config.config.LANGFUSE_SESSION_HMAC_SECRET", ""):

            with trace_request(endpoint="/chat", request_id="req1", session_id="user@example.com"):
                pass

            call_kwargs = mock_client.start_as_current_observation.call_args[1]
            self.assertNotIn("session_id", call_kwargs["metadata"])
            self.assertNotIn("session_ref", call_kwargs["metadata"])
            self.assertNotIn("user@example.com", str(call_kwargs["metadata"]))

        mock_client.reset_mock()

        # Case B: With dedicated HMAC secret, keyed HMAC is computed (never plain SHA256 or raw ID)
        secret = "my_dedicated_telemetry_secret_key"
        expected_ref = hmac.new(secret.encode("utf-8"), b"user@example.com", hashlib.sha256).hexdigest()[:16]

        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=True), \
             patch("backend.utils.telemetry.get_langfuse_client", return_value=mock_client), \
             patch("backend.config.config.LANGFUSE_SESSION_HMAC_SECRET", secret):

            with trace_request(endpoint="/chat", request_id="req2", session_id="user@example.com"):
                pass

            call_kwargs = mock_client.start_as_current_observation.call_args[1]
            self.assertNotIn("session_id", call_kwargs["metadata"])
            self.assertEqual(call_kwargs["metadata"]["session_ref"], expected_ref)
            self.assertNotIn("user@example.com", str(call_kwargs["metadata"]))

    # 15. F-02: Sanitizer fails closed on un-serializable or unexpected object
    def test_sanitizer_fails_closed_on_bad_object(self):
        class BadObject:
            def __str__(self):
                raise RuntimeError("Cannot stringify!")

        bad_metadata = {"bad_item": BadObject()}
        # Must not raise exception
        result = sanitize_telemetry_metadata(bad_metadata)
        self.assertIsInstance(result, dict)

    # 16. F-03: Client initialization missing credentials fails closed
    def test_client_init_missing_credentials(self):
        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=True), \
             patch("backend.config.config.LANGFUSE_PUBLIC_KEY", ""), \
             patch("backend.config.config.LANGFUSE_SECRET_KEY", ""):
            client = get_langfuse_client()
            self.assertIsNone(client)

    # 17. F-03: Client initialization SDK exception fails closed
    def test_client_init_sdk_exception_fails_closed(self):
        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=True), \
             patch("backend.config.config.LANGFUSE_PUBLIC_KEY", "pk-lf-test"), \
             patch("backend.config.config.LANGFUSE_SECRET_KEY", "sk-lf-test"), \
             patch("langfuse.Langfuse", side_effect=RuntimeError("Langfuse init exploded")):
            client = get_langfuse_client()
            self.assertIsNone(client)

    # 18. F-03: Chat endpoint terminal early return records ok_terminal
    async def test_chat_endpoint_terminal_early_return(self):
        mock_raw_obs = MagicMock()
        mock_client = MagicMock()
        mock_client.start_as_current_observation.return_value.__enter__.return_value = mock_raw_obs

        mock_req = MagicMock()
        mock_req.state.role = "user"
        chat_req = ChatRequest(messages=[{"role": "user", "content": "Hello"}], session_id="test_sess_123")

        mock_pipeline_res = MagicMock(
            is_terminal=True,
            reply="Hello, how can I help you?",
            results=None,
            result_type=None,
            presentation_mode=None,
            action_metadata=None,
            selection_metadata=None,
            requested_level=None,
            jobs=None,
            candidates=None,
            pagination=None,
            suggestion_chips=["Search jobs"],
            role="user",
            intent_hint="greeting",
            msg_len=5,
        )

        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=True), \
             patch("backend.utils.telemetry.get_langfuse_client", return_value=mock_client), \
             patch("backend.main.execute_chat_pipeline", new_callable=AsyncMock, return_value=mock_pipeline_res):
            res = await chat(chat_req, mock_req, {"sub": "user1"})
            self.assertEqual(res.reply, "Hello, how can I help you?")
            self.assertTrue(res.success)

        # Verify observation finalized with ok_terminal exactly once
        mock_raw_obs.update.assert_called_once()
        update_kwargs = mock_raw_obs.update.call_args[1]
        self.assertEqual(update_kwargs["metadata"]["outcome"], "ok_terminal")
        self.assertEqual(update_kwargs["status_message"], "ok_terminal")

    # 19. F-03: Shutdown telemetry clean and handles exceptions safely
    def test_shutdown_telemetry_lifecycle(self):
        mock_client = MagicMock()
        mock_client.shutdown.return_value = None

        with patch("backend.utils.telemetry._langfuse_client", mock_client):
            shutdown_telemetry()
            mock_client.shutdown.assert_called_once()

        # Repeated shutdown should be safe no-op
        shutdown_telemetry()

        # Exception during shutdown should not bubble up
        broken_client = MagicMock()
        broken_client.shutdown.side_effect = RuntimeError("Shutdown socket error")
        with patch("backend.utils.telemetry._langfuse_client", broken_client):
            # Must not raise
            shutdown_telemetry()

    # 20. F-03: Missing usage in LLMService is omitted and not converted to zero
    async def test_missing_usage_in_llm_service_omitted(self):
        mock_raw_gen = MagicMock()
        mock_client = MagicMock()
        mock_client.start_as_current_observation.return_value.__enter__.return_value = mock_raw_gen

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # Provider response without usage dictionary
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Answer without usage tokens."}}],
        }

        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=True), \
             patch("backend.utils.telemetry.get_langfuse_client", return_value=mock_client), \
             patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):

            reply = await LLMService.get_chat_completion("Prompt", [{"role": "user", "content": "Hi"}])
            self.assertEqual(reply, "Answer without usage tokens.")

        mock_raw_gen.update.assert_called_once()
        update_kwargs = mock_raw_gen.update.call_args[1]
        self.assertNotIn("usage_details", update_kwargs)

    # 21. Phase 2.1: validate_retrieval_metadata allowlist and bounding enforcement
    def test_validate_retrieval_metadata_allowlist_and_bounding(self):
        valid_input = {
            "search_mode": "EXPLICIT_KEYWORD",
            "search_provider": "hybrid",
            "provider_status": "success",
            "job_mode_requested": "remote",
            "jobmatch_fallback_reason": "none",
            "retrieval_error_type": "TimeoutException",
            "meilisearch_count": 15,
            "jobmatch_count": 8,
            "fused_count": 20,
            "eligible_count": 12,
            "final_count": 5,
            "job_mode_rejected_count": 2,
            "salary_rejected_count": 1,
            "experience_rejected_count": 3,
            "emp_type_rejected_count": 0,
            "rrf_input_count": 23,
            "rrf_output_count": 20,
            "meili_ms": 42.567,
            "jobmatch_ms": 88.12,
            "total_ms": 110.45,
            "retrieval_total_ms": 110.45,
            "candidate_present": True,
            "meilisearch_attempted": True,
            "jobmatch_attempted": True,
            "hybrid_executed": True,
            "filters_applied": ["city", "job_mode", "role_type", "urgent"],
            "provider_latency_ms": {"meilisearch": 42.56, "jobmatch": 88.12},
            "provider_hit_count": {"meilisearch": 15, "jobmatch": 8},
            "filter_drop_counts": {"job_mode": 2, "salary": 1, "experience": 3},
            # Malicious / PII / Unknown keys that MUST be stripped:
            "user_query": "Find Python developer in Delhi paying 20 LPA",
            "raw_prompt": "You are a recruiter...",
            "candidate_name": "Rajesh Kumar",
            "user_id": 12345,
            "candidate_id": 99999,
            "email": "rajesh@example.com",
            "phone": "+919876543210",
            "salary": 2000000,
            "token": "secret_bearer_token",
            "password": "db_password_xyz",
            "documents": [{"title": "Software Engineer", "cv": "Confidential..."}],
        }

        clean = validate_retrieval_metadata(valid_input)

        # Assert valid fields are preserved and properly bounded
        self.assertEqual(clean["search_mode"], "EXPLICIT_KEYWORD")
        self.assertEqual(clean["search_provider"], "hybrid")
        self.assertEqual(clean["provider_status"], "success")
        self.assertEqual(clean["job_mode_requested"], "remote")
        self.assertEqual(clean["meilisearch_count"], 15)
        self.assertEqual(clean["jobmatch_count"], 8)
        self.assertEqual(clean["fused_count"], 20)
        self.assertEqual(clean["eligible_count"], 12)
        self.assertEqual(clean["final_count"], 5)
        self.assertEqual(clean["job_mode_rejected_count"], 2)
        self.assertEqual(clean["salary_rejected_count"], 1)
        self.assertEqual(clean["experience_rejected_count"], 3)
        self.assertEqual(clean["emp_type_rejected_count"], 0)
        self.assertEqual(clean["meili_ms"], 42.6)
        self.assertEqual(clean["jobmatch_ms"], 88.1)
        self.assertEqual(clean["total_ms"], 110.5)
        self.assertEqual(clean["candidate_present"], True)
        self.assertEqual(clean["meilisearch_attempted"], True)
        self.assertEqual(clean["jobmatch_attempted"], True)
        self.assertEqual(clean["filters_applied"], ["city", "job_mode", "role_type", "urgent"])
        self.assertEqual(clean["provider_latency_ms"]["meilisearch"], 42.6)
        self.assertEqual(clean["provider_hit_count"]["meilisearch"], 15)
        self.assertEqual(clean["filter_drop_counts"]["job_mode"], 2)

        # Assert ALL unsafe/unknown fields were rejected
        self.assertNotIn("user_query", clean)
        self.assertNotIn("raw_prompt", clean)
        self.assertNotIn("candidate_name", clean)
        self.assertNotIn("user_id", clean)
        self.assertNotIn("candidate_id", clean)
        self.assertNotIn("email", clean)
        self.assertNotIn("phone", clean)
        self.assertNotIn("salary", clean)
        self.assertNotIn("token", clean)
        self.assertNotIn("password", clean)
        self.assertNotIn("documents", clean)

    # 22. Phase 2.1: Missing metrics are not fabricated as zeros
    def test_validate_retrieval_metadata_missing_metrics_not_fabricated(self):
        partial_input = {
            "search_mode": "EXPLICIT_KEYWORD",
            "meilisearch_count": 5,
        }
        clean = validate_retrieval_metadata(partial_input)
        self.assertEqual(clean["search_mode"], "EXPLICIT_KEYWORD")
        self.assertEqual(clean["meilisearch_count"], 5)
        # Verify unmeasured fields are completely omitted, NOT fabricated
        self.assertNotIn("jobmatch_count", clean)
        self.assertNotIn("jobmatch_ms", clean)
        self.assertNotIn("meili_ms", clean)
        self.assertNotIn("fused_count", clean)
        self.assertNotIn("filters_applied", clean)

    # 23. Phase 2.1: Invalid types, booleans-as-integers, negative numbers rejected
    def test_validate_retrieval_metadata_invalid_types_rejected(self):
        corrupted_input = {
            "search_mode": "INVALID_UNKNOWN_MODE",
            "search_provider": "untrusted_provider_xyz",
            "meilisearch_count": -10,  # Negative count
            "jobmatch_count": True,    # Boolean instead of int
            "meili_ms": -45.0,         # Negative latency
            "total_ms": "not_a_number",
            "candidate_present": "yes",# String instead of bool
            "filters_applied": "city", # String instead of list
        }
        clean = validate_retrieval_metadata(corrupted_input)
        self.assertEqual(clean, {})

        # Non-dict inputs fail closed
        self.assertEqual(validate_retrieval_metadata(None), {})
        self.assertEqual(validate_retrieval_metadata([1, 2, 3]), {})
        self.assertEqual(validate_retrieval_metadata("string"), {})

    # 24. Phase 2.1: Request observation receives retrieval metadata without overwriting trace metadata
    def test_request_observation_set_retrieval_metadata(self):
        mock_raw_obs = MagicMock()
        mock_client = MagicMock()
        mock_client.start_as_current_observation.return_value.__enter__.return_value = mock_raw_obs

        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=True), \
             patch("backend.utils.telemetry.get_langfuse_client", return_value=mock_client):

            with trace_request(
                endpoint="/chat",
                request_id="test_req_retrieval_01",
                role="employee",
                session_id="session_retrieval_xyz",
            ) as req_trace:
                req_trace.set_role("employee")
                req_trace.set_intent_hint("search")
                req_trace.set_outcome("ok")
                req_trace.set_total_ms(180)
                req_trace.set_retrieval_metadata({
                    "search_mode": "EXPLICIT_KEYWORD",
                    "search_provider": "hybrid",
                    "meilisearch_count": 12,
                    "jobmatch_count": 6,
                    "fused_count": 16,
                    "eligible_count": 10,
                    "final_count": 5,
                    "meili_ms": 35.2,
                    "jobmatch_ms": 78.4,
                    "total_ms": 95.0,
                    "filters_applied": ["job_mode", "city"],
                })

        mock_client.start_as_current_observation.assert_called_once()
        call_kwargs = mock_client.start_as_current_observation.call_args[1]
        self.assertEqual(call_kwargs["metadata"]["endpoint"], "/chat")
        self.assertEqual(call_kwargs["metadata"]["request_id"], "test_req_retrieval_01")

        mock_raw_obs.update.assert_called_once()
        update_kwargs = mock_raw_obs.update.call_args[1]
        meta = update_kwargs["metadata"]

        # Verify existing request-level metadata is completely intact
        self.assertEqual(meta["role"], "employee")
        self.assertEqual(meta["intent_hint"], "search")
        self.assertEqual(meta["outcome"], "ok")
        self.assertEqual(meta["total_ms"], 180)

        # Verify retrieval metadata namespace is cleanly attached
        self.assertIn("retrieval", meta)
        retrieval = meta["retrieval"]
        self.assertEqual(retrieval["search_mode"], "EXPLICIT_KEYWORD")
        self.assertEqual(retrieval["search_provider"], "hybrid")
        self.assertEqual(retrieval["meilisearch_count"], 12)
        self.assertEqual(retrieval["jobmatch_count"], 6)
        self.assertEqual(retrieval["fused_count"], 16)
        self.assertEqual(retrieval["filters_applied"], ["job_mode", "city"])

    # 25. Phase 2.1: No-op observation safely ignores retrieval metadata when telemetry is disabled
    def test_noop_observation_ignores_retrieval_metadata(self):
        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=False):
            with trace_request(endpoint="/chat", request_id="req_noop") as span:
                self.assertIsInstance(span, NoOpObservation)
                # Must not raise
                span.set_retrieval_metadata({"search_mode": "EXPLICIT_KEYWORD", "fused_count": 10})
                span.set_outcome("ok")

    # 26. Phase 2.1: Hybrid search telemetry propagation through employee orchestration
    async def test_hybrid_search_telemetry_propagation(self):
        from backend.api.employee import handle_employee_context

        fake_hits = [
            {
                "id": 101,
                "job_title": "Python Developer",
                "company_name": "Tech Corp",
                "city_name": "Delhi",
                "job_mode_name": "Office",
                "job_mode_id": 1,
                "experience": "2 years",
                "salary": "12 LPA",
            },
            {
                "id": 102,
                "job_title": "Senior Python Engineer",
                "company_name": "Innovate Ltd",
                "city_name": "Delhi",
                "job_mode_name": "Office",
                "job_mode_id": 1,
                "experience": "5 years",
                "salary": "18 LPA",
            },
        ]

        # Mock search_jobs returning fake hits
        with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock, return_value={"success": True, "data": fake_hits}), \
             patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock, return_value={"results": []}):

            hits, telemetry = await execute_hybrid_job_search(
                last_user_msg="python developer in delhi",
                keyword="python",
                explicit_loc=True,
                target_city_name="Delhi",
                base_filters={"city": [1]},
            )

            self.assertEqual(len(hits), 2)
            self.assertEqual(telemetry["search_mode"], "EXPLICIT_LOCATION")
            self.assertEqual(telemetry["search_provider"], "meilisearch")  # JobMatch disabled
            self.assertEqual(telemetry["meilisearch_count"], 2)
            self.assertIn("city", telemetry["filters_applied"])
            self.assertIn("keyword", telemetry["filters_applied"])

            # Call handle_employee_context to verify propagation into OrchestrationResult
            with patch("backend.api.employee.execute_hybrid_job_search", new_callable=AsyncMock, return_value=(hits, telemetry)):
                orch_dict = await handle_employee_context(
                    last_user_msg="python developer in delhi",
                    session_id="session_prop_01",
                    candidate_id=None,
                )
                self.assertIsNotNone(orch_dict)
                self.assertIn("search_metadata", orch_dict)
                self.assertEqual(orch_dict["search_metadata"]["search_mode"], "EXPLICIT_LOCATION")
                self.assertEqual(orch_dict["search_metadata"]["meilisearch_count"], 2)

    # 27. Phase 2.1: /chat endpoint forwards retrieval_metadata to trace_request
    async def test_chat_endpoint_attaches_retrieval_metadata(self):
        mock_raw_obs = MagicMock()
        mock_client = MagicMock()
        mock_client.start_as_current_observation.return_value.__enter__.return_value = mock_raw_obs

        mock_req = MagicMock()
        mock_req.state.role = "employee"
        chat_req = ChatRequest(messages=[{"role": "user", "content": "python developer jobs"}], session_id="test_sess_01")

        mock_pipeline_res = ChatPipelineResult(
            is_terminal=False,
            system="System prompt",
            bounded_messages=[{"role": "user", "content": "python developer jobs"}],
            allowed_ids={101},
            results_payload=[{"job_id": 101, "title": "Python Dev", "company": "Acme", "url": "https://collarcheck.com/jobs/101"}],
            result_type_payload="jobs",
            max_tokens_compose=512,
            pagination=None,
            suggestion_chips=["More jobs"],
            role="employee",
            intent_hint="search",
            msg_len=21,
            retrieval_metadata={
                "search_mode": "EXPLICIT_KEYWORD",
                "search_provider": "hybrid",
                "meilisearch_count": 8,
                "jobmatch_count": 4,
                "fused_count": 10,
                "final_count": 1,
                "meili_ms": 30.5,
                "total_ms": 75.0,
                "filters_applied": ["keyword"],
            },
        )

        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=True), \
             patch("backend.utils.telemetry.get_langfuse_client", return_value=mock_client), \
             patch("backend.main.execute_chat_pipeline", new_callable=AsyncMock, return_value=mock_pipeline_res), \
             patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock, return_value="Here are the top matches:"):

            res = await chat(chat_req, mock_req, {"sub": "employee_01"})
            self.assertTrue(res.success)
            # Verify public response does not leak retrieval_metadata
            self.assertFalse(hasattr(res, "retrieval_metadata"))

        # Verify trace observation received retrieval metadata
        mock_raw_obs.update.assert_called_once()
        update_kwargs = mock_raw_obs.update.call_args[1]
        self.assertIn("retrieval", update_kwargs["metadata"])
        retrieval = update_kwargs["metadata"]["retrieval"]
        self.assertEqual(retrieval["search_mode"], "EXPLICIT_KEYWORD")
        self.assertEqual(retrieval["search_provider"], "hybrid")
        self.assertEqual(retrieval["fused_count"], 10)
        self.assertEqual(retrieval["filters_applied"], ["keyword"])

    # 28. Phase 2.1: /chat/stream forwards retrieval_metadata to trace_request
    async def test_chat_stream_attaches_retrieval_metadata(self):
        mock_raw_obs = MagicMock()
        mock_client = MagicMock()
        mock_client.start_as_current_observation.return_value.__enter__.return_value = mock_raw_obs

        mock_req = MagicMock()
        mock_req.state.role = "employee"
        mock_req.is_disconnected = AsyncMock(return_value=False)
        chat_req = ChatRequest(messages=[{"role": "user", "content": "recommend jobs for me"}], session_id="test_sess_02")

        mock_pipeline_res = ChatPipelineResult(
            is_terminal=False,
            system="System prompt",
            bounded_messages=[{"role": "user", "content": "recommend jobs for me"}],
            allowed_ids={201},
            results_payload=[{"job_id": 201, "title": "Data Scientist", "company": "AI Labs", "url": "https://collarcheck.com/jobs/201"}],
            result_type_payload="jobs",
            max_tokens_compose=512,
            pagination=None,
            suggestion_chips=["Refine"],
            role="employee",
            intent_hint="search",
            msg_len=21,
            retrieval_metadata={
                "search_mode": "PROFILE_RECOMMENDATION",
                "search_provider": "hybrid",
                "fused_count": 15,
                "eligible_count": 15,
                "final_count": 1,
                "total_ms": 90.0,
            },
        )

        async def fake_stream_tokens(*args, **kwargs):
            yield "Here are your recommendations:"

        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=True), \
             patch("backend.utils.telemetry.get_langfuse_client", return_value=mock_client), \
             patch("backend.main.execute_chat_pipeline", new_callable=AsyncMock, return_value=mock_pipeline_res), \
             patch("backend.services.llm_service.LLMService.stream_chat_completion", side_effect=fake_stream_tokens):

            response = await chat_stream(chat_req, mock_req, {"sub": "employee_02"})
            # Consume the SSE generator
            events = []
            async for chunk in response.body_iterator:
                events.append(chunk)

            self.assertTrue(len(events) > 0)

        # Verify trace observation received retrieval metadata
        mock_raw_obs.update.assert_called_once()
        update_kwargs = mock_raw_obs.update.call_args[1]
        self.assertIn("retrieval", update_kwargs["metadata"])
        retrieval = update_kwargs["metadata"]["retrieval"]
        self.assertEqual(retrieval["search_mode"], "PROFILE_RECOMMENDATION")
        self.assertEqual(retrieval["fused_count"], 15)

    # 29. Phase 2.2: JobMatch disabled -> no request, no fabricated latency (jobmatch_ms omitted)
    async def test_jobmatch_disabled_omits_latency_and_records_outcome(self):
        with patch("backend.services.hybrid_job_search.JOB_MATCH_ENABLED", False), \
             patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock, return_value={"success": True, "data": []}), \
             patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:

            hits, meta = await execute_hybrid_job_search(
                last_user_msg="python jobs",
                candidate_id=123,
                keyword="python",
                limit=5,
            )
            mock_jm.assert_not_called()
            self.assertEqual(meta["jobmatch_outcome"], "disabled")
            self.assertEqual(meta["jobmatch_fallback_reason"], "disabled")
            self.assertFalse(meta["jobmatch_attempted"])
            self.assertNotIn("jobmatch_ms", meta)

    # 30. Phase 2.2: Missing candidate identifier -> no request, no fabricated latency
    async def test_missing_candidate_omits_latency_and_records_outcome(self):
        with patch("backend.services.hybrid_job_search.JOB_MATCH_ENABLED", True), \
             patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock, return_value={"success": True, "data": []}), \
             patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:

            hits, meta = await execute_hybrid_job_search(
                last_user_msg="python jobs",
                candidate_id=None,
                keyword="python",
                limit=5,
            )
            mock_jm.assert_not_called()
            self.assertEqual(meta["jobmatch_outcome"], "missing_candidate_id")
            self.assertEqual(meta["jobmatch_fallback_reason"], "missing_candidate_id")
            self.assertFalse(meta["jobmatch_attempted"])
            self.assertNotIn("jobmatch_ms", meta)

    # 31. Phase 2.2: Successful JobMatch with results -> success_with_results, measured latency, no fallback reason
    async def test_jobmatch_success_with_results(self):
        jm_mock = {
            "results": [{"job_id": 101, "final_score": 0.95, "payload": {"job_id": 101, "job_title": "Python Dev"}}]
        }
        with patch("backend.services.hybrid_job_search.JOB_MATCH_ENABLED", True), \
             patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock, return_value={"success": True, "data": []}), \
             patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock, return_value=jm_mock):

            hits, meta = await execute_hybrid_job_search(
                last_user_msg="python jobs",
                candidate_id=123,
                keyword="python",
                limit=5,
            )
            self.assertEqual(meta["jobmatch_outcome"], "success_with_results")
            self.assertTrue(meta["jobmatch_attempted"])
            self.assertIn("jobmatch_ms", meta)
            self.assertGreaterEqual(meta["jobmatch_ms"], 0.0)
            # Must NOT claim a fallback occurred when successful!
            self.assertNotIn("jobmatch_fallback_reason", meta)

    # 32. Phase 2.2: Successful empty JobMatch -> empty_results, measured latency
    async def test_jobmatch_empty_results(self):
        with patch("backend.services.hybrid_job_search.JOB_MATCH_ENABLED", True), \
             patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock, return_value={"success": True, "data": []}), \
             patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock, return_value={"results": []}):

            hits, meta = await execute_hybrid_job_search(
                last_user_msg="python jobs",
                candidate_id=123,
                keyword="python",
                limit=5,
            )
            self.assertEqual(meta["jobmatch_outcome"], "empty_results")
            self.assertEqual(meta["jobmatch_fallback_reason"], "empty_results")
            self.assertTrue(meta["jobmatch_attempted"])
            self.assertIn("jobmatch_ms", meta)

    # 33. Phase 2.2: Candidate not indexed (HTTP 409) -> candidate_not_indexed
    async def test_jobmatch_candidate_not_indexed(self):
        with patch("backend.services.hybrid_job_search.JOB_MATCH_ENABLED", True), \
             patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock, return_value={"success": True, "data": []}), \
             patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock, side_effect=CandidateNotIndexedException("Candidate 123 not indexed")):

            hits, meta = await execute_hybrid_job_search(
                last_user_msg="python jobs",
                candidate_id=123,
                keyword="python",
                limit=5,
            )
            self.assertEqual(meta["jobmatch_outcome"], "candidate_not_indexed")
            self.assertEqual(meta["jobmatch_fallback_reason"], "candidate_not_indexed")
            self.assertIn("jobmatch_ms", meta)

    # 34. Phase 2.2: Authentication failure (HTTP 401/403) -> authentication_failure
    async def test_jobmatch_auth_failure(self):
        with patch("backend.services.hybrid_job_search.JOB_MATCH_ENABLED", True), \
             patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock, return_value={"success": True, "data": []}), \
             patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock, side_effect=JobMatchAuthException("Status 401")):

            hits, meta = await execute_hybrid_job_search(
                last_user_msg="python jobs",
                candidate_id=123,
                keyword="python",
                limit=5,
            )
            self.assertEqual(meta["jobmatch_outcome"], "authentication_failure")
            self.assertEqual(meta["jobmatch_fallback_reason"], "authentication_failure")

    # 35. Phase 2.2: Timeout -> timeout
    async def test_jobmatch_timeout(self):
        with patch("backend.services.hybrid_job_search.JOB_MATCH_ENABLED", True), \
             patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock, return_value={"success": True, "data": []}), \
             patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock, side_effect=JobMatchUnavailableException("Network error: timeout contacting API")):

            hits, meta = await execute_hybrid_job_search(
                last_user_msg="python jobs",
                candidate_id=123,
                keyword="python",
                limit=5,
            )
            self.assertEqual(meta["jobmatch_outcome"], "timeout")
            self.assertEqual(meta["jobmatch_fallback_reason"], "timeout")

    # 36. Phase 2.2: Service unavailable (HTTP 503/5xx) -> service_unavailable
    async def test_jobmatch_service_unavailable(self):
        with patch("backend.services.hybrid_job_search.JOB_MATCH_ENABLED", True), \
             patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock, return_value={"success": True, "data": []}), \
             patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock, side_effect=JobMatchUnavailableException("Service returned HTTP 503")):

            hits, meta = await execute_hybrid_job_search(
                last_user_msg="python jobs",
                candidate_id=123,
                keyword="python",
                limit=5,
            )
            self.assertEqual(meta["jobmatch_outcome"], "service_unavailable")
            self.assertEqual(meta["jobmatch_fallback_reason"], "service_unavailable")

    # 37. Phase 2.2: Contract validation error (HTTP 400/422) -> contract_error
    async def test_jobmatch_contract_error(self):
        with patch("backend.services.hybrid_job_search.JOB_MATCH_ENABLED", True), \
             patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock, return_value={"success": True, "data": []}), \
             patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock, side_effect=JobMatchContractException("Invalid candidate_id")):

            hits, meta = await execute_hybrid_job_search(
                last_user_msg="python jobs",
                candidate_id=123,
                keyword="python",
                limit=5,
            )
            self.assertEqual(meta["jobmatch_outcome"], "contract_error")
            self.assertEqual(meta["jobmatch_fallback_reason"], "contract_error")

    # 38. Phase 2.2: Unexpected provider error -> unexpected_error
    async def test_jobmatch_unexpected_error(self):
        with patch("backend.services.hybrid_job_search.JOB_MATCH_ENABLED", True), \
             patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock, return_value={"success": True, "data": []}), \
             patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock, side_effect=RuntimeError("Unexpected connection reset")):

            hits, meta = await execute_hybrid_job_search(
                last_user_msg="python jobs",
                candidate_id=123,
                keyword="python",
                limit=5,
            )
            self.assertEqual(meta["jobmatch_outcome"], "unexpected_error")
            self.assertEqual(meta["jobmatch_fallback_reason"], "unexpected_error")

    # 39. Phase 2.2: Outcome allowlist validation accepts all outcomes and rejects unknowns
    def test_jobmatch_outcome_allowlist_validation(self):
        valid_outcomes = [
            "disabled", "missing_candidate_id", "success_with_results",
            "empty_results", "candidate_not_indexed", "authentication_failure",
            "timeout", "service_unavailable", "contract_error", "unexpected_error", "not_attempted_other"
        ]
        for vo in valid_outcomes:
            res = validate_retrieval_metadata({"jobmatch_outcome": vo})
            self.assertEqual(res.get("jobmatch_outcome"), vo)

        # Unknown categories rejected
        self.assertNotIn("jobmatch_outcome", validate_retrieval_metadata({"jobmatch_outcome": "unknown_failure"}))
        self.assertNotIn("jobmatch_outcome", validate_retrieval_metadata({"jobmatch_outcome": 12345}))
        self.assertNotIn("jobmatch_outcome", validate_retrieval_metadata({"jobmatch_outcome": "SELECT * FROM users"}))

    # 40. Phase 2.2: Direct metadata injection bypassed setter, sanitized at finalize()
    def test_direct_metadata_injection_sanitized_at_finalization(self):
        mock_raw_obs = MagicMock()
        mock_client = MagicMock()
        mock_client.start_as_current_observation.return_value.__enter__.return_value = mock_raw_obs

        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=True), \
             patch("backend.utils.telemetry.get_langfuse_client", return_value=mock_client):

            with trace_request(endpoint="/chat", request_id="req_test_inject_01") as req_trace:
                # Deliberately bypass set_retrieval_metadata() and directly inject unsafe payload
                req_trace.metadata["retrieval"] = {
                    "search_mode": "EXPLICIT_KEYWORD",
                    "final_count": 5,
                    "salary": "1500000",             # UNSAFE
                    "raw_query": "python developer", # UNSAFE
                    "candidate_name": "Alice Smith", # UNSAFE
                    "phone": "+1-555-0199",           # UNSAFE
                    "malicious_sql": "DROP TABLE",    # UNSAFE
                }

        mock_raw_obs.update.assert_called_once()
        update_kwargs = mock_raw_obs.update.call_args[1]
        self.assertIn("retrieval", update_kwargs["metadata"])
        retrieval = update_kwargs["metadata"]["retrieval"]

        # Safe allowlisted fields are retained
        self.assertEqual(retrieval["search_mode"], "EXPLICIT_KEYWORD")
        self.assertEqual(retrieval["final_count"], 5)

        # Unsafe fields were completely stripped by finalize revalidation!
        self.assertNotIn("salary", retrieval)
        self.assertNotIn("raw_query", retrieval)
        self.assertNotIn("candidate_name", retrieval)
        self.assertNotIn("phone", retrieval)
        self.assertNotIn("malicious_sql", retrieval)

    # 41. Phase 2.2: Finalize revalidation exception cannot fail the chat request
    def test_finalize_revalidation_failure_does_not_fail_request(self):
        mock_raw_obs = MagicMock()
        mock_client = MagicMock()
        mock_client.start_as_current_observation.return_value.__enter__.return_value = mock_raw_obs

        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=True), \
             patch("backend.utils.telemetry.get_langfuse_client", return_value=mock_client), \
             patch("backend.utils.telemetry.validate_retrieval_metadata", side_effect=Exception("Catastrophic validation crash")):

            # trace_request must NOT raise even if validation crashes inside finalize
            with trace_request(endpoint="/chat", request_id="req_test_inject_02") as req_trace:
                req_trace.metadata["retrieval"] = {"final_count": 3}

        mock_raw_obs.update.assert_called_once()

    # 42. Privacy regression: application logs do not leak raw message or raw session_id
    async def test_application_logs_exclude_raw_messages_and_session_id(self):
        import logging
        from backend.main import execute_chat_pipeline
        from backend.schemas.schemas import ChatRequest, ChatMessage
        from backend.utils.timing import start_request_timing

        sensitive_msg = "My salary is 25 LPA at SecretCorp and my phone is 9876543210"
        sensitive_session = "sensitive_session_id_xyz999"

        req_obj = ChatRequest(
            messages=[ChatMessage(role="user", content=sensitive_msg)],
            session_id=sensitive_session,
        )
        fake_req = MagicMock()
        fake_req.state.role = "employee"
        fake_req.state.email = "test@example.com"
        fake_req.state.db_user_id = 9999
        mock_token = {"role": "employee", "email": "test@example.com", "id": 9999}
        timing = start_request_timing()

        log_records = []
        class LogInterceptor(logging.Handler):
            def emit(self, record):
                log_records.append(record.getMessage())

        interceptor = LogInterceptor()
        logging.getLogger("backend.main").addHandler(interceptor)

        mock_profile = {
            "db_user": {
                "id": 9999,
                "individual_id": 9999,
                "fname": "Test",
                "lname": "User",
                "phone": "",
                "dob": "",
                "profile_description": "",
                "expected_salary": 0,
            },
            "user_skills_list": [],
            "user_roles_list": [],
            "user_edu_list": [],
            "user_edu_cards": [],
            "user_emp_list": [],
            "cert_cnt": 0,
        }

        try:
            with patch("backend.services.user_data_service.parse_user_data_intents_async", new_callable=AsyncMock, return_value=[]), \
                 patch("backend.services.profile_parser.parse_profile_update", new_callable=AsyncMock, return_value={}), \
                 patch("backend.services.search_parser.parse_search_query", new_callable=AsyncMock, return_value={"search_target": "jobs"}), \
                 patch("backend.main._fetch_user_profile_context", return_value=mock_profile), \
                 patch("backend.services.search_router.dispatch_search_target", new_callable=AsyncMock, return_value={"db_context": "test", "results": []}):
                await execute_chat_pipeline(req_obj, fake_req, mock_token, timing)

            all_logs = " ".join(log_records)
            self.assertNotIn(sensitive_msg, all_logs)
            self.assertNotIn("25 LPA", all_logs)
            self.assertNotIn("9876543210", all_logs)
            self.assertNotIn(sensitive_session, all_logs)
        finally:
            logging.getLogger("backend.main").removeHandler(interceptor)

    # 43. Privacy regression: trace_generation does not capture raw inputs or outputs
    def test_trace_generation_excludes_raw_input_and_output(self):
        mock_raw_obs = MagicMock()
        mock_client = MagicMock()
        mock_client.start_as_current_observation.return_value.__enter__.return_value = mock_raw_obs

        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=True), \
             patch("backend.utils.telemetry.get_langfuse_client", return_value=mock_client):

            with trace_generation(
                name="chat_compose",
                model="gpt-4o-mini",
                metadata={"max_tokens": 512, "prompt": "Secret prompt text", "user_message": "Secret user query"},
            ) as gen:
                gen.set_usage(prompt_tokens=50, completion_tokens=100)

            # 1. start_as_current_observation must not contain input or output
            call_kwargs = mock_client.start_as_current_observation.call_args[1]
            self.assertNotIn("input", call_kwargs)
            self.assertNotIn("output", call_kwargs)
            init_meta = call_kwargs.get("metadata", {})
            self.assertNotIn("prompt", init_meta)
            self.assertNotIn("user_message", init_meta)

            # 2. update() on finalize must also never contain raw prompt or user_message
            mock_raw_obs.update.assert_called_once()
            update_kwargs = mock_raw_obs.update.call_args[1]
            self.assertNotIn("input", update_kwargs)
            self.assertNotIn("output", update_kwargs)
            final_meta = update_kwargs.get("metadata", {})
            self.assertNotIn("prompt", final_meta)
            self.assertNotIn("user_message", final_meta)
            self.assertIn("usage_details", update_kwargs)
            self.assertEqual(update_kwargs["usage_details"]["input"], 50)
            self.assertEqual(update_kwargs["usage_details"]["output"], 100)


if __name__ == "__main__":
    unittest.main()

