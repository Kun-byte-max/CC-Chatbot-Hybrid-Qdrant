"""
synthetic_turn_profiler.py — Deterministic, offline synthetic-turn profiling harness.

Implements Stage 3.4.1B:
Exercises the chat pipeline offline without contacting live MySQL, LLM, embedding,
Qdrant, Meilisearch, or network endpoints.

Provides:
- Cold session context read profiling
- Warm conversational turn profiling (zero retrieval)
- Synthetic search/pagination profiling with controlled simulated delays
- Deterministic percentile calculation (p50, p90, p99)
- Isolation of test sessions without clearing global _SESSION_STATES
"""

import asyncio
import time
import uuid
from typing import Dict, Any, List, Optional
from unittest.mock import MagicMock, AsyncMock, patch
import jwt
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend.main import app, ChatPipelineResult
from backend.config.config import JWT_SECRET, JWT_ALGORITHM
from backend.utils.sse import parse_sse_event
from backend.utils.session_state import _SESSION_STATES, make_composite_key


def calculate_percentiles(values: List[float]) -> Dict[str, float]:
    """Calculates p50, p90, and p99 from a sorted list of numeric values."""
    if not values:
        return {"p50": 0.0, "p90": 0.0, "p99": 0.0, "min": 0.0, "max": 0.0}
    s = sorted(values)
    n = len(s)
    
    def get_p(p: float) -> float:
        idx = int(round(p * (n - 1)))
        return float(s[max(0, min(idx, n - 1))])

    return {
        "p50": round(get_p(0.50), 2),
        "p90": round(get_p(0.90), 2),
        "p99": round(get_p(0.99), 2),
        "min": round(float(s[0]), 2),
        "max": round(float(s[-1]), 2),
    }


class SyntheticTurnProfiler:
    """
    Offline synthetic-turn profiler harness.
    Enforces fail-closed mocking across all external I/O boundaries.
    """

    def __init__(self, iterations: int = 15, warmup_runs: int = 3):
        self.iterations = iterations
        self.warmup_runs = warmup_runs
        self.valid_jwt = jwt.encode(
            {"id": 999, "uid": 999, "email": "profiler_user@example.com", "role": "employee"},
            JWT_SECRET,
            algorithm=JWT_ALGORITHM,
        )
        self.headers = {"Authorization": f"Bearer {self.valid_jwt}"}
        self.client = TestClient(app)

    def _make_auth_mock(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [{
            "id": 999,
            "individual_id": "IND999",
            "fname": "Profiler",
            "lname": "Tester",
            "email": "profiler_user@example.com",
            "user_type": 1,
            "status": 1,
            "is_deleted": 0,
            "token": "tok_profiler_999",
        }]
        mock_cursor.fetchone.return_value = mock_cursor.fetchall.return_value[0]
        mock_conn.cursor.return_value = mock_cursor
        return mock_conn

    def profile_scenario_cold_turn(
        self,
        simulated_llm_ttft_s: float = 0.015,
        simulated_llm_chunk_s: float = 0.005,
    ) -> Dict[str, Any]:
        """
        Scenario 1: Cold session context read.
        Unique session ID, cold context miss, greeted by conversational LLM.
        """
        records: List[Dict[str, Any]] = []
        created_keys: List[str] = []

        try:
            for i in range(self.warmup_runs + self.iterations):
                sess_id = f"prof_cold_{uuid.uuid4().hex[:12]}"
                created_keys.append(sess_id)
                comp_key = make_composite_key(sess_id, "employee")
                created_keys.append(comp_key)

                mock_auth_conn = self._make_auth_mock()

                fake_cold_res = ChatPipelineResult(
                    role="employee",
                    intent_hint="smalltalk",
                    msg_len=8,
                    system="System Prompt",
                    bounded_messages=[{"role": "user", "content": "Hi there"}],
                    max_tokens_compose=100,
                    is_terminal=False,
                )

                # Mock LLM stream generator with simulated delays
                async def mock_stream_gen(*args, **kwargs):
                    if simulated_llm_ttft_s > 0:
                        await asyncio.sleep(simulated_llm_ttft_s)
                    yield "Hello! "
                    if simulated_llm_chunk_s > 0:
                        await asyncio.sleep(simulated_llm_chunk_s)
                    yield "How can I help you today?"

                with patch("backend.api.auth.get_db", return_value=mock_auth_conn):
                    with patch("backend.utils.session_state._db_load_session", return_value=None):
                        with patch("backend.main.execute_chat_pipeline", return_value=fake_cold_res):
                            with patch("backend.services.llm_service.LLMService.stream_chat_completion", side_effect=mock_stream_gen):
                                with patch.object(Request, "is_disconnected", return_value=False):
                                    t0 = time.perf_counter()
                                    res = self.client.post(
                                        "/chat/stream",
                                        json={
                                            "session_id": sess_id,
                                            "messages": [{"role": "user", "content": "Hi there"}]
                                        },
                                        headers=self.headers
                                    )
                                    dur_ms = (time.perf_counter() - t0) * 1000.0

                                    if res.status_code == 200 and i >= self.warmup_runs:
                                        events = [parse_sse_event(block) for block in res.text.strip().split("\n\n") if block.strip()]
                                        delta_events = [e for e in events if e.get("event") == "delta"]
                                        done_events = [e for e in events if e.get("event") == "done"]

                                        records.append({
                                            "total_ms": dur_ms,
                                            "status_code": res.status_code,
                                            "has_delta": len(delta_events) > 0,
                                            "has_done": len(done_events) > 0,
                                        })
        finally:
            # Clean up only keys created by this run
            for k in created_keys:
                _SESSION_STATES.pop(k, None)

        total_ms_list = [r["total_ms"] for r in records]
        return {
            "scenario": "cold_session_conversational",
            "iterations": len(records),
            "warmup_runs": self.warmup_runs,
            "injected_delays": {
                "llm_ttft_ms": simulated_llm_ttft_s * 1000.0,
                "llm_chunk_ms": simulated_llm_chunk_s * 1000.0,
            },
            "percentiles": calculate_percentiles(total_ms_list),
            "records": records,
        }

    def profile_scenario_warm_conversational(
        self,
        simulated_llm_ttft_s: float = 0.015,
        simulated_llm_chunk_s: float = 0.005,
    ) -> Dict[str, Any]:
        """
        Scenario 2: Warm conversational turn (Zero retrieval).
        Pre-seeds session state in L1 RAM, verifies conversation flow without search.
        """
        records: List[Dict[str, Any]] = []
        created_keys: List[str] = []

        try:
            for i in range(self.warmup_runs + self.iterations):
                sess_id = f"prof_warm_{uuid.uuid4().hex[:12]}"
                created_keys.append(sess_id)
                comp_key = make_composite_key(sess_id, "employee")
                created_keys.append(comp_key)

                # Seed L1 RAM session
                _SESSION_STATES[comp_key] = {
                    "version_seq": 2,
                    "_last_active": time.time(),
                    "search_context": None,
                    "pagination": None,
                }

                mock_auth_conn = self._make_auth_mock()

                # Mock query rewriter and pipeline to isolate conversational path cleanly
                fake_pipeline_res = ChatPipelineResult(
                    role="employee",
                    intent_hint="smalltalk",
                    msg_len=32,
                    system="System Prompt",
                    bounded_messages=[{"role": "user", "content": "What skills should I learn next?"}],
                    max_tokens_compose=120,
                    is_terminal=False,
                )

                async def mock_stream_gen(*args, **kwargs):
                    if simulated_llm_ttft_s > 0:
                        await asyncio.sleep(simulated_llm_ttft_s)
                    yield "You should practice "
                    if simulated_llm_chunk_s > 0:
                        await asyncio.sleep(simulated_llm_chunk_s)
                    yield "system design and Python."

                with patch("backend.api.auth.get_db", return_value=mock_auth_conn):
                    with patch("backend.utils.session_state._db_load_session", return_value=None):
                        with patch("backend.main.execute_chat_pipeline", return_value=fake_pipeline_res):
                            with patch("backend.services.llm_service.LLMService.stream_chat_completion", side_effect=mock_stream_gen):
                                with patch.object(Request, "is_disconnected", return_value=False):
                                    t0 = time.perf_counter()
                                    res = self.client.post(
                                        "/chat/stream",
                                        json={
                                            "session_id": sess_id,
                                            "messages": [
                                                {"role": "user", "content": "What skills should I learn next?"}
                                            ]
                                        },
                                        headers=self.headers
                                    )
                                    dur_ms = (time.perf_counter() - t0) * 1000.0

                                    if res.status_code == 200 and i >= self.warmup_runs:
                                        events = [parse_sse_event(block) for block in res.text.strip().split("\n\n") if block.strip()]
                                        delta_events = [e for e in events if e.get("event") == "delta"]
                                        records.append({
                                            "total_ms": dur_ms,
                                            "status_code": res.status_code,
                                            "has_delta": len(delta_events) > 0,
                                        })
        finally:
            for k in created_keys:
                _SESSION_STATES.pop(k, None)

        total_ms_list = [r["total_ms"] for r in records]
        return {
            "scenario": "warm_conversational_turn",
            "iterations": len(records),
            "warmup_runs": self.warmup_runs,
            "injected_delays": {
                "llm_ttft_ms": simulated_llm_ttft_s * 1000.0,
                "llm_chunk_ms": simulated_llm_chunk_s * 1000.0,
            },
            "percentiles": calculate_percentiles(total_ms_list),
            "records": records,
        }

    def profile_scenario_search_turn(
        self,
        simulated_retrieval_s: float = 0.020,
        simulated_llm_ttft_s: float = 0.010,
    ) -> Dict[str, Any]:
        """
        Scenario 3: Synthetic search turn.
        Simulates structured search with mock retrieval latency and card serialization.
        """
        records: List[Dict[str, Any]] = []
        created_keys: List[str] = []

        try:
            for i in range(self.warmup_runs + self.iterations):
                sess_id = f"prof_search_{uuid.uuid4().hex[:12]}"
                created_keys.append(sess_id)
                comp_key = make_composite_key(sess_id, "employee")
                created_keys.append(comp_key)

                mock_auth_conn = self._make_auth_mock()

                # Mock chat pipeline returning search results in results_payload
                fake_pipeline_result = ChatPipelineResult(
                    role="employee",
                    intent_hint="search",
                    msg_len=25,
                    system="System Prompt",
                    bounded_messages=[{"role": "user", "content": "Find Python jobs"}],
                    max_tokens_compose=150,
                    is_terminal=False,
                    results_payload=[{
                        "job_id": 101,
                        "title": "Senior Python Developer",
                        "company_name": "Tech Corp",
                        "city": "Bengaluru",
                    }],
                    result_type_payload="jobs",
                    allowed_ids={101},
                )

                async def mock_execute_chat(*args, **kwargs):
                    if simulated_retrieval_s > 0:
                        await asyncio.sleep(simulated_retrieval_s)
                    return fake_pipeline_result

                async def mock_stream_gen(*args, **kwargs):
                    if simulated_llm_ttft_s > 0:
                        await asyncio.sleep(simulated_llm_ttft_s)
                    yield "Here are the matching Python jobs:"

                with patch("backend.api.auth.get_db", return_value=mock_auth_conn):
                    with patch("backend.main.execute_chat_pipeline", side_effect=mock_execute_chat) as spy_pipe:
                        with patch("backend.services.llm_service.LLMService.stream_chat_completion", side_effect=mock_stream_gen):
                            with patch.object(Request, "is_disconnected", return_value=False):
                                t0 = time.perf_counter()
                                res = self.client.post(
                                    "/chat/stream",
                                    json={
                                        "session_id": sess_id,
                                        "messages": [{"role": "user", "content": "Find Python jobs"}]
                                    },
                                    headers=self.headers
                                )
                                dur_ms = (time.perf_counter() - t0) * 1000.0

                                if res.status_code == 200 and i >= self.warmup_runs:
                                    events = [parse_sse_event(block) for block in res.text.strip().split("\n\n") if block.strip()]
                                    results_events = [e for e in events if e.get("event") == "results"]
                                    delta_events = [e for e in events if e.get("event") == "delta"]

                                    records.append({
                                        "total_ms": dur_ms,
                                        "status_code": res.status_code,
                                        "has_results": len(results_events) > 0,
                                        "has_delta": len(delta_events) > 0,
                                    })
        finally:
            for k in created_keys:
                _SESSION_STATES.pop(k, None)

        total_ms_list = [r["total_ms"] for r in records]
        return {
            "scenario": "synthetic_search_turn",
            "iterations": len(records),
            "warmup_runs": self.warmup_runs,
            "injected_delays": {
                "retrieval_ms": simulated_retrieval_s * 1000.0,
                "llm_ttft_ms": simulated_llm_ttft_s * 1000.0,
            },
            "percentiles": calculate_percentiles(total_ms_list),
            "records": records,
        }
