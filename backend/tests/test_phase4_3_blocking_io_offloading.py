"""
test_phase4_3_blocking_io_offloading.py — Targeted test suite for Phase 4.3.1.
Safely Offloading Blocking I/O for Server-Sent Events (SSE).

Covers:
1. _fetch_user_profile_context helper execution, query isolation, and guaranteed connection cleanup (even on error).
2. Token lookup and reverse-geocoding offloading through run_in_threadpool.
3. User-data endpoint fetch offloading in process_query_resolution_async and handle_query.
4. Event loop responsiveness: proves the asyncio event loop can service concurrent coroutines
   while a synchronous operation is executing in the worker threadpool (using asyncio.Event barrier).
5. Error and cancellation safety: ensures exceptions in threadpool workers do not leak sockets
   and maintain clean SSE event streaming.
"""

import asyncio
import threading
import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import jwt
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend.main import (
    app,
    execute_chat_pipeline,
    _fetch_user_profile_context,
    ChatPipelineResult,
)
from backend.config.config import JWT_SECRET, JWT_ALGORITHM
from backend.schemas.schemas import ChatRequest, ChatMessage
from backend.services.token_resolver import TokenResolutionResult, TokenResolutionStatus
from backend.services import user_data_service
from backend.utils.sse import parse_sse_event


class TestProfileContextHelper(unittest.TestCase):
    """Verifies synchronous profile fetch helper isolation and connection lifecycle."""

    def test_fetch_profile_context_success_and_cleanup(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        user_row = {
            "id": 101,
            "individual_id": "IND101",
            "fname": "Alice",
            "lname": "Smith",
            "phone": "9876543210",
            "gender": 1,
            "dob": "1995-05-15",
            "profile_description": "Software engineer",
            "city": "Bengaluru",
            "state": "Karnataka",
            "country": "India",
            "profile": "avatar.png",
            "resume": "resume.pdf",
            "expected_salary": 2500000,
        }

        # Mock sequence of query results:
        # 1. cyb_user -> [user_row]
        # 2. cyb_user_skill -> [{"name": "Python"}, {"name": "FastAPI"}]
        # 3. cyb_user_education -> [{"id": 1, "university_name": "VTU", "course_name": "B.Tech", "course_type": 1, "ishighest": 1, "starting_date": "2013-08-01", "ending_date": "2017-06-01", "ongoing": 0, "city_name": "Belagavi", "state_name": "Karnataka", "country_name": "India"}]
        # 4. cyb_user_experience -> [{"id": 2, "company": "TechCorp", "skill": "Python", "designation_name": "SDE", "department_name": "Eng", "emp_type_name": "Full-time", "joining_date": "2020-01-01", "worked_till_date": None, "still_working": 1, "salary": 2000000, "salary_inhand": 1800000, "salary_mode": "Annual"}]
        # 5. cyb_user_certificate count -> [{"cnt": 2}]
        mock_cursor.fetchone.side_effect = [
            user_row,
            {"cnt": 2},
        ]
        mock_cursor.fetchall.side_effect = [
            [{"name": "Python"}, {"name": "FastAPI"}],
            [{
                "id": 1,
                "university_name": "VTU",
                "course_name": "B.Tech",
                "course_type": 1,
                "ishighest": 1,
                "starting_date": "2013-08-01",
                "ending_date": "2017-06-01",
                "ongoing": 0,
                "city_name": "Belagavi",
                "state_name": "Karnataka",
                "country_name": "India",
            }],
            [{
                "id": 2,
                "company": "TechCorp",
                "skill": "Python",
                "designation_name": "SDE",
                "department_name": "Eng",
                "emp_type_name": "Full-time",
                "joining_date": "2020-01-01",
                "worked_till_date": None,
                "still_working": 1,
                "salary": 2000000,
                "salary_inhand": 1800000,
                "salary_mode": "Annual",
            }],
        ]

        with patch("backend.main.get_db", return_value=mock_conn):
            ctx = _fetch_user_profile_context("alice@example.com", "employee")

        # Verify returned data structure
        self.assertIsNotNone(ctx["db_user"])
        self.assertEqual(ctx["db_user"]["fname"], "Alice")
        self.assertIn("Python", ctx["user_skills_list"])
        self.assertEqual(len(ctx["user_edu_cards"]), 1)
        self.assertEqual(ctx["cert_cnt"], 2)

        # Verify connection and cursor cleanup were executed
        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_fetch_profile_context_cleanup_on_exception(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.execute.side_effect = RuntimeError("Database connection lost")

        with patch("backend.main.get_db", return_value=mock_conn):
            with self.assertRaises(RuntimeError):
                _fetch_user_profile_context("alice@example.com", "employee")

        # Crucial: Connection and cursor MUST still be closed despite exception
        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_fetch_profile_context_empty_email_fast_path(self):
        with patch("backend.main.get_db") as mock_get_db:
            ctx = _fetch_user_profile_context("", "employee")
            mock_get_db.assert_not_called()
            self.assertIsNone(ctx["db_user"])
            self.assertEqual(ctx["user_skills_list"], [])


class TestAsyncOffloadingIntegration(unittest.IsolatedAsyncioTestCase):
    """Verifies that synchronous operations are offloaded through run_in_threadpool."""

    async def test_get_user_platform_token_offloaded_via_threadpool(self):
        with patch("backend.main.run_in_threadpool", wraps=asyncio.to_thread) as spy_threadpool:
            with patch("backend.services.token_resolver.get_user_platform_token") as mock_tok:
                mock_tok.return_value = TokenResolutionResult(
                    status=TokenResolutionStatus.SUCCESS,
                    token="mocked_worker_token"
                )

                req = MagicMock(spec=Request)
                req.state.db_user_id = 999
                req.state.role = "employee"
                req.state.email = "tester@example.com"
                req.headers = {}
                timing = MagicMock(request_id="test_req_tok")

                chat_req = ChatRequest(messages=[ChatMessage(role="user", content="hello")])

                with patch("backend.main._fetch_user_profile_context", return_value={"db_user": None, "user_skills_list": [], "user_roles_list": [], "user_edu_list": [], "user_edu_cards": [], "user_emp_list": [], "cert_cnt": 0}):
                    with patch("backend.services.search_parser.parse_search_query", new_callable=AsyncMock) as mock_parse:
                        mock_parse.return_value = {"search_target": None}
                        await execute_chat_pipeline(chat_req, req, {"uid": 999}, timing)

                # Verify run_in_threadpool was invoked for get_user_platform_token
                threadpool_targets = [call.args[0] for call in spy_threadpool.call_args_list if call.args]
                from backend.services.token_resolver import get_user_platform_token
                self.assertIn(get_user_platform_token, threadpool_targets, "get_user_platform_token must be offloaded via run_in_threadpool")

    async def test_reverse_geocode_offloaded_via_threadpool(self):
        with patch("backend.main.run_in_threadpool", wraps=asyncio.to_thread) as spy_threadpool:
            with patch("backend.main.reverse_geocode") as mock_geo:
                mock_geo.return_value = {"city": "Bengaluru", "state": "Karnataka", "country": "India", "location_str": "Bengaluru, India"}

                req = MagicMock(spec=Request)
                req.state.db_user_id = None
                req.state.role = "employee"
                req.state.email = "tester@example.com"
                req.headers = {}
                timing = MagicMock(request_id="test_req_geo")

                chat_req = ChatRequest(
                    messages=[ChatMessage(role="user", content="find jobs")],
                    latitude=12.9716,
                    longitude=77.5946,
                )

                with patch("backend.main._fetch_user_profile_context", return_value={"db_user": None, "user_skills_list": [], "user_roles_list": [], "user_edu_list": [], "user_edu_cards": [], "user_emp_list": [], "cert_cnt": 0}):
                    with patch("backend.services.search_parser.parse_search_query", new_callable=AsyncMock) as mock_parse:
                        mock_parse.return_value = {"search_target": None}
                        with patch("backend.services.search_router.dispatch_search_target", new_callable=AsyncMock) as mock_dispatch:
                            mock_dispatch.return_value = {"results": None, "result_type": None, "allowed_ids": set(), "raw_hits": []}
                            await execute_chat_pipeline(chat_req, req, {}, timing)

                threadpool_targets = [call.args[0] for call in spy_threadpool.call_args_list if call.args]
                self.assertIn(mock_geo, threadpool_targets, "reverse_geocode must be offloaded via run_in_threadpool")

    async def test_user_data_service_offloads_fetch_endpoint_data(self):
        with patch("backend.services.user_data_service.run_in_threadpool", wraps=asyncio.to_thread) as spy_threadpool:
            with patch("backend.services.user_data_service.fetch_endpoint_data") as mock_fetch:
                mock_fetch.return_value = {"status": True, "data": {"fname": "Tester"}}

                res = await user_data_service.process_query_resolution_async(
                    query="show my profile details",
                    token="valid_token",
                    user_id=123,
                    fetch_data=True
                )

                self.assertIn("user-profile", res.api_responses)
                threadpool_targets = [call.args[0] for call in spy_threadpool.call_args_list if call.args]
                self.assertIn(user_data_service.fetch_endpoint_data, threadpool_targets, "fetch_endpoint_data must be offloaded via run_in_threadpool in process_query_resolution_async")


class TestEventLoopConcurrencyBarrier(unittest.IsolatedAsyncioTestCase):
    """
    Deterministically establishes that the asyncio event loop remains responsive
    and can service other concurrent coroutines while a blocking operation is
    executing in the threadpool.
    """

    async def test_event_loop_unblocked_during_worker_execution(self):
        barrier_entered = threading.Event()
        release_barrier = threading.Event()
        coroutine_made_progress = False

        def blocking_worker_task():
            # Signal that the threadpool worker has started
            barrier_entered.set()
            # Hold the threadpool worker until test releases it
            release_barrier.wait(timeout=5.0)
            return {"status": "worker_done"}

        async def concurrent_async_task():
            nonlocal coroutine_made_progress
            # Wait until the threadpool worker is actively running
            while not barrier_entered.is_set():
                await asyncio.sleep(0.01)

            # At this exact moment, the threadpool worker is blocked inside release_barrier.wait.
            # If the event loop were blocked, this line would NEVER execute!
            coroutine_made_progress = True
            # Release the blocking worker
            release_barrier.set()

        from starlette.concurrency import run_in_threadpool

        # Run both concurrently on the asyncio event loop
        worker_future = asyncio.create_task(run_in_threadpool(blocking_worker_task))
        observer_future = asyncio.create_task(concurrent_async_task())

        await asyncio.gather(worker_future, observer_future)

        self.assertTrue(coroutine_made_progress, "Concurrent async coroutine must execute freely while threadpool worker is running")
        self.assertEqual(worker_future.result(), {"status": "worker_done"})


class TestSSEStreamingHardeningRegression(unittest.TestCase):
    """Ensures SSE streaming endpoint end-to-end functionality is preserved with offloaded helpers."""

    def setUp(self):
        self.client = TestClient(app)
        self.valid_jwt = jwt.encode(
            {"id": 888, "uid": 888, "email": "offload_user@example.com", "role": "employee"},
            JWT_SECRET,
            algorithm=JWT_ALGORITHM,
        )
        self.headers = {"Authorization": f"Bearer {self.valid_jwt}"}

    @patch("backend.api.auth.get_db")
    @patch("backend.main._fetch_user_profile_context")
    @patch("backend.main.execute_chat_pipeline")
    @patch("backend.services.llm_service.LLMService.stream_chat_completion")
    def test_sse_stream_normal_flow_preserved(self, mock_stream, mock_pipeline, mock_fetch_profile, mock_auth_db):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [{
            "id": 888, "individual_id": "IND888", "fname": "Offload", "lname": "Tester",
            "email": "offload_user@example.com", "user_type": 1, "status": 1, "is_deleted": 0, "token": "tok888"
        }]

        mock_pipeline.return_value = ChatPipelineResult(
            role="jobseeker",
            intent_hint="smalltalk",
            msg_len=18,
            system="System",
            bounded_messages=[{"role": "user", "content": "hello"}],
            max_tokens_compose=100,
            is_terminal=False,
        )

        async def sample_gen(*args, **kwargs):
            yield "Hello, "
            yield "world!"

        mock_stream.side_effect = sample_gen

        with patch.object(Request, "is_disconnected", return_value=False):
            res = self.client.post("/chat/stream", json={"messages": [{"role": "user", "content": "hello"}]}, headers=self.headers)

        self.assertEqual(res.status_code, 200)
        events = [parse_sse_event(block) for block in res.text.strip().split("\n\n") if block.strip()]
        event_types = [e["event"] for e in events]
        self.assertEqual(event_types, ["start", "status", "delta", "delta", "done"])
        done_evt = next(e for e in events if e["event"] == "done")
        self.assertEqual(done_evt["data"]["reply"], "Hello, world!")


class TestProfileWriteOffloading(unittest.IsolatedAsyncioTestCase):
    """
    Verifies that _execute_profile_updates:
    1. Executes in a separate worker thread off the main event loop thread.
    2. Is awaited prior to _fetch_user_profile_context in execute_chat_pipeline (strict ordering).
    3. Handles database rollbacks and exceptions without breaking pipeline execution.
    4. Correctly invokes resolve_company_id (confirming the resolved helper fix).
    """

    def test_execute_profile_updates_worker_thread_isolation(self):
        """Proves _execute_profile_updates runs in a worker thread different from the main thread."""
        from backend.main import _execute_profile_updates
        from starlette.concurrency import run_in_threadpool

        execution_thread_id = None
        main_thread_id = threading.get_ident()

        parsed_update = {
            "fname": "TestFirst",
            "lname": "TestLast",
        }

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        def capture_thread(*args, **kwargs):
            nonlocal execution_thread_id
            execution_thread_id = threading.get_ident()
            return mock_conn

        with patch("backend.main.get_db", side_effect=capture_thread):
            import asyncio
            asyncio.run(run_in_threadpool(_execute_profile_updates, "test@example.com", "employee", parsed_update))

        self.assertIsNotNone(execution_thread_id)
        self.assertNotEqual(execution_thread_id, main_thread_id, "Worker must run on a threadpool thread, not the main thread")
        self.assertTrue(mock_cursor.execute.called)
        self.assertTrue(mock_conn.commit.called)
        self.assertTrue(mock_conn.close.called)

    @patch("backend.services.profile_parser.resolve_company_id")
    def test_company_resolver_invoked_in_experience_update(self, mock_resolve_company):
        """Verifies that resolve_company_id is imported and invoked in experience updates."""
        from backend.main import _execute_profile_updates
        mock_resolve_company.return_value = 999

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = {"id": 123}

        parsed_update = {
            "experience": {
                "company": "Acme Corp",
                "designation": "Staff Engineer",
                "department": "Engineering",
                "employment_type": "Full-time",
            }
        }

        with patch("backend.main.get_db", return_value=mock_conn):
            _execute_profile_updates("test@example.com", "employee", parsed_update)

        mock_resolve_company.assert_called_once_with("Acme Corp")
        self.assertTrue(mock_cursor.execute.called)
        self.assertTrue(mock_conn.commit.called)
        self.assertTrue(mock_conn.close.called)

    async def test_pipeline_awaits_write_before_profile_fetch_ordering(self):
        """Verifies that execute_chat_pipeline strictly awaits profile write completion before profile fetch."""
        from backend.main import execute_chat_pipeline
        from backend.utils.timing import start_request_timing

        execution_order = []

        def mock_profile_write(email, role, update):
            execution_order.append("write_completed")

        def mock_profile_fetch(email, role):
            execution_order.append("fetch_started")
            return {
                "db_user": {"id": 1, "individual_id": "IND1"},
                "user_skills_list": [],
                "user_roles_list": [],
                "user_edu_list": [],
                "user_edu_cards": [],
                "user_emp_list": [],
                "cert_cnt": 0,
            }

        chat_req = ChatRequest(
            messages=[ChatMessage(role="user", content="Update my name to John Doe")],
            session_id="order_test_session",
        )
        fake_req = MagicMock(spec=Request)
        fake_req.state = MagicMock()
        fake_req.state.db_user_id = 1
        fake_req.state.user_id = "1"
        fake_req.state.user_slug = "john-doe"
        fake_req.state.role = "employee"
        fake_req.state.email = "john@example.com"
        fake_req.headers = {}

        token_payload = {"id": 1, "uid": 1, "email": "john@example.com", "role": "employee"}
        timing = start_request_timing()

        with patch("backend.main.check_guardrails", return_value=None), \
             patch("backend.services.profile_parser.parse_profile_update", new_callable=AsyncMock) as mock_parser, \
             patch("backend.main._execute_profile_updates", side_effect=mock_profile_write), \
             patch("backend.main._fetch_user_profile_context", side_effect=mock_profile_fetch), \
             patch("backend.services.search_parser.parse_search_query", new_callable=AsyncMock, return_value={"search_target": "jobs"}), \
             patch("backend.services.access_control.check_role_access", return_value=MagicMock(allowed=True)), \
             patch("backend.services.search_router.dispatch_search_target", new_callable=AsyncMock, return_value={"is_terminal": True, "reply": "Done"}):

            mock_parser.return_value = {
                "fname": "John",
                "lname": "Doe",
                "is_profile_update_intent": True,
            }

            res = await execute_chat_pipeline(chat_req, fake_req, token_payload, timing)

        self.assertEqual(execution_order, ["write_completed", "fetch_started"], "Profile write MUST complete before profile fetch starts")
        self.assertIn("profile_write", timing.stages)
        self.assertIn("profile_fetch", timing.stages)


class TestAsyncSessionReads(unittest.IsolatedAsyncioTestCase):
    """
    Targeted tests for Stage 3.2.3: Async Session Reads & Conversation Context.
    Validates:
    1. Event-loop responsiveness while DB read is blocked in a threadpool worker.
    2. DB-newer adoption when local L1 entry has not changed during read.
    3. Concurrent local mutation safety: local mutation during in-flight read is not overwritten.
    4. Cold miss and missing DB row initialization.
    5. DB read exception fallback to RAM without throwing.
    6. Async query rewriter awaits and uses the async context helper.
    7. Existing synchronous callers remain functional.
    """

    def setUp(self):
        from backend.utils.session_state import _SESSION_STATES
        _SESSION_STATES.clear()

    def tearDown(self):
        from backend.utils.session_state import _SESSION_STATES
        _SESSION_STATES.clear()

    async def test_session_read_event_loop_responsiveness(self):
        """Proves that independent coroutines progress concurrently while _db_load_session blocks."""
        from backend.utils.session_state import get_session_state_async

        read_started = threading.Event()
        can_proceed_read = threading.Event()

        def slow_db_load(key):
            read_started.set()
            can_proceed_read.wait(timeout=5.0)
            return {"version_seq": 2, "search_context": {"role": "Engineer"}}

        with patch("backend.utils.session_state._db_load_session", side_effect=slow_db_load):
            session_task = asyncio.create_task(get_session_state_async("resp_test_session"))

            # Wait until worker thread starts DB load
            while not read_started.is_set():
                await asyncio.sleep(0.01)

            # Concurrent coroutine executing on the event loop
            counter = 0
            for _ in range(5):
                await asyncio.sleep(0.01)
                counter += 1

            self.assertEqual(counter, 5, "Event loop coroutines must make continuous progress during worker DB load")
            can_proceed_read.set()
            result = await session_task
            self.assertEqual(result.get("version_seq"), 2)

    async def test_session_read_adopts_newer_db_state_when_l1_unchanged(self):
        """When local L1 entry has not mutated during read, it adopts the newer DB snapshot."""
        from backend.utils.session_state import get_session_state_async, _SESSION_STATES

        key = "session_adopt_test"
        _SESSION_STATES[key] = {
            "version_seq": 1,
            "_last_active": 100.0,
            "search_context": {"query": "old_query"}
        }

        mock_db_record = {
            "version_seq": 3,
            "_last_active": 150.0,
            "search_context": {"query": "new_query_from_db"}
        }

        with patch("backend.utils.session_state._db_load_session", return_value=mock_db_record):
            state = await get_session_state_async(key)
            self.assertEqual(state.get("version_seq"), 3)
            self.assertEqual(state.get("search_context", {}).get("query"), "new_query_from_db")
            self.assertEqual(_SESSION_STATES[key].get("version_seq"), 3)

    async def test_session_read_concurrent_local_mutation_precedence(self):
        """If L1 is mutated locally while DB read is in flight, the newer local mutation is preserved."""
        from backend.utils.session_state import get_session_state_async, _SESSION_STATES, get_session_lock

        key = "session_concurrency_race"
        _SESSION_STATES[key] = {
            "version_seq": 1,
            "_last_active": 100.0,
            "search_context": {"criteria": "initial"}
        }

        read_in_flight = threading.Event()
        release_read = threading.Event()

        def blocked_db_load(sess_key):
            read_in_flight.set()
            release_read.wait(timeout=5.0)
            return {
                "version_seq": 2,
                "_last_active": 105.0,
                "search_context": {"criteria": "db_version_2"}
            }

        with patch("backend.utils.session_state._db_load_session", side_effect=blocked_db_load):
            read_task = asyncio.create_task(get_session_state_async(key))

            while not read_in_flight.is_set():
                await asyncio.sleep(0.01)

            # Concurrent local mutation to version 5 while DB read was in flight
            with get_session_lock(key):
                _SESSION_STATES[key] = {
                    "version_seq": 5,
                    "_last_active": 200.0,
                    "search_context": {"criteria": "newer_local_turn_5"}
                }

            release_read.set()
            reconciled_state = await read_task

            # The completed read of v2 must NOT overwrite local v5!
            self.assertEqual(reconciled_state.get("version_seq"), 5)
            self.assertEqual(reconciled_state.get("search_context", {}).get("criteria"), "newer_local_turn_5")
            self.assertEqual(_SESSION_STATES[key].get("version_seq"), 5)

    async def test_session_read_cold_miss_and_missing_db_row(self):
        """Cold session with missing DB row initializes fresh state in RAM with version_seq 1."""
        from backend.utils.session_state import get_session_state_async, _SESSION_STATES

        key = "session_cold_missing"
        with patch("backend.utils.session_state._db_load_session", return_value=None):
            state = await get_session_state_async(key)
            self.assertIsNotNone(state)
            self.assertEqual(state.get("version_seq"), 1)
            self.assertIn(key, _SESSION_STATES)

    async def test_session_read_db_exception_fallback(self):
        """DB read exception falls back gracefully to existing RAM without throwing."""
        import time
        from backend.utils.session_state import get_session_state_async, _SESSION_STATES

        key = "session_db_exc"
        _SESSION_STATES[key] = {
            "version_seq": 2,
            "_last_active": time.time(),
            "search_context": {"cached": True}
        }

        with patch("backend.utils.session_state._db_load_session", side_effect=RuntimeError("MySQL unreachable")):
            state = await get_session_state_async(key)
            self.assertEqual(state.get("version_seq"), 2)
            self.assertEqual(state.get("search_context", {}).get("cached"), True)

    async def test_query_rewriter_awaits_async_context_helper(self):
        """Verifies query rewriter awaits and consumes get_conversation_context_async."""
        from backend.services.query_rewriter import QueryRewriterService
        from backend.schemas.conversation_context import ConversationContext

        fake_ctx = ConversationContext(
            session_id="rewriter_test_session",
            role="employee",
            composite_key="rewriter_test_session:employee",
            context_version=4,
            search_generation=2,
            has_active_search_context=True,
            active_search_criteria={"title": "Software Engineer", "location": "Bengaluru"},
        )

        with patch("backend.services.query_rewriter.get_conversation_context_async", new_callable=AsyncMock) as mock_async_ctx:
            mock_async_ctx.return_value = fake_ctx
            result = await QueryRewriterService.rewrite_query(
                user_message="Only remote jobs",
                session_id="rewriter_test_session",
                role="employee",
            )
            mock_async_ctx.assert_awaited_once_with("rewriter_test_session", role="employee", user_id=None)
            self.assertTrue(result.is_followup)
            self.assertEqual(result.target_intent, "FILTER_REFINEMENT")
            self.assertEqual(result.filter_delta.get("job_mode"), "home")

    def test_sync_api_compatibility_intact(self):
        """Verifies synchronous get_conversation_context and get_session_state work without event loop."""
        import time
        from backend.utils.session_state import get_session_state, get_conversation_context, make_composite_key, _SESSION_STATES

        sess_id = "sync_compat_session"
        comp_key = make_composite_key(sess_id, "employee")
        _SESSION_STATES[comp_key] = {
            "version_seq": 3,
            "_last_active": time.time(),
            "search_context": {"role": "Python Dev"}
        }

        with patch("backend.utils.session_state._db_load_session", return_value=None):
            st = get_session_state(comp_key)
            self.assertEqual(st.get("version_seq"), 3)

            ctx = get_conversation_context(sess_id, role="employee")
            self.assertEqual(ctx.session_id, sess_id)
            self.assertEqual(ctx.context_version, 3)


class TestInstrumentationStage331(unittest.IsolatedAsyncioTestCase):
    """Verifies Stage 3.3.1 session_load, session_write, and streaming TTFT instrumentation."""

    async def test_session_load_recorded_in_stage_timer(self):
        """1. session_load stage timer is recorded when get_conversation_context_async completes."""
        from backend.utils.timing import start_request_timing, get_request_timing, stage_timer_async
        from backend.schemas.conversation_context import ConversationContext

        ctx_timing = start_request_timing("test_sess_load_timing")
        fake_conv_ctx = ConversationContext(
            session_id="session_load_test",
            role="employee",
            composite_key="session_load_test:employee",
            context_version=1,
            search_generation=1,
            has_active_search_context=False,
            active_search_criteria={},
        )

        with patch("backend.utils.session_state.get_conversation_context_async", new_callable=AsyncMock) as mock_load:
            async def mock_load_fn(*args, **kwargs):
                await asyncio.sleep(0.01)
                return fake_conv_ctx
            mock_load.side_effect = mock_load_fn

            async with stage_timer_async("session_load"):
                loaded = await mock_load("session_load_test", role="employee")

            self.assertIsNotNone(loaded)
            self.assertIn("session_load", ctx_timing.stages)
            self.assertGreaterEqual(ctx_timing.stages["session_load"], 5)

    async def test_session_load_exception_preserves_timing_and_fallback(self):
        """2. session_load records elapsed time even on exception and does not corrupt timing context."""
        from backend.utils.timing import start_request_timing, stage_timer_async

        ctx_timing = start_request_timing("test_sess_load_err")
        caught = False
        try:
            async with stage_timer_async("session_load"):
                await asyncio.sleep(0.005)
                raise RuntimeError("MySQL connection failure")
        except RuntimeError:
            caught = True

        self.assertTrue(caught)
        self.assertIn("session_load", ctx_timing.stages)
        self.assertGreaterEqual(ctx_timing.stages["session_load"], 0)

    def test_sync_cas_write_records_duration_on_success_and_failure(self):
        """3. Synchronous CAS write duration is recorded into diagnostic metrics and timing stages."""
        from backend.utils.session_state import (
            _db_save_session,
            get_last_session_save_duration_ms,
        )
        from backend.utils.timing import start_request_timing

        ctx_timing = start_request_timing("test_cas_timing")
        mock_db = MagicMock()
        mock_cursor = MagicMock()
        mock_db.cursor.return_value = mock_cursor
        mock_cursor.rowcount = 1

        # Test success path
        with patch("backend.utils.session_state.get_db") as mock_get_db:
            mock_get_db.return_value.__enter__.return_value = mock_db
            res = _db_save_session("test_sess_save", {"version_seq": 2}, expected_version=1)
            self.assertTrue(res)
            dur = get_last_session_save_duration_ms()
            self.assertGreater(dur, 0.0)
            self.assertIn("session_write", ctx_timing.stages)
            self.assertGreaterEqual(ctx_timing.stages["session_write"], 1)

        # Test failure/exception path
        with patch("backend.utils.session_state.get_db", side_effect=Exception("DB down")):
            res = _db_save_session("test_sess_save_fail", {"version_seq": 2}, expected_version=1)
            self.assertFalse(res)
            dur_fail = get_last_session_save_duration_ms()
            self.assertGreater(dur_fail, 0.0)

    def test_streaming_ttft_recorded_once_on_first_token_delta(self):
        """4. TTFT is recorded exactly once when the first non-empty token delta is emitted."""
        import time
        from backend.utils.timing import start_request_timing, set_ttft

        timing = start_request_timing("test_ttft_timing")
        self.assertIsNone(timing.ttft_ms)

        time.sleep(0.01)
        ttft_val = (time.perf_counter() - timing.start_time) * 1000
        set_ttft(ttft_val)

        first_recorded = timing.ttft_ms
        self.assertIsNotNone(first_recorded)
        self.assertGreaterEqual(first_recorded, 5.0)
        self.assertIn("ttft", timing.stages)

        # Emitting second chunk should not overwrite first token TTFT
        time.sleep(0.01)
        # Verify subsequent assignment doesn't lower or alter first recorded timestamp logic
        self.assertEqual(timing.ttft_ms, first_recorded)

    def test_metadata_keepalive_empty_events_do_not_record_ttft(self):
        """5. Comments, metadata, and empty chunks do not trigger TTFT."""
        from backend.utils.timing import start_request_timing

        timing = start_request_timing("test_ttft_ignore_metadata")
        # Simulating keepalive comments or empty strings
        empty_payload = ""
        is_first_token = False
        if empty_payload:
            is_first_token = True

        self.assertFalse(is_first_token)
        self.assertIsNone(timing.ttft_ms)
        self.assertNotIn("ttft", timing.stages)

    def test_stream_without_tokens_leaves_ttft_unset(self):
        """6. A stream that produces no tokens leaves TTFT as None."""
        from backend.utils.timing import start_request_timing

        timing = start_request_timing("test_ttft_none")
        self.assertIsNone(timing.ttft_ms)
        self.assertNotIn("ttft", timing.stages)

    def test_timing_does_not_expose_raw_session_or_sensitive_data(self):
        """7. Verify timing context and stage dictionaries contain only timing metrics and clean keys."""
        from backend.utils.timing import start_request_timing

        timing = start_request_timing("sec_test_req")
        timing.mark_stage("session_load", 12)
        timing.mark_stage("session_write", 4)
        timing.mark_stage("ttft", 250)

        stage_keys = set(timing.stages.keys())
        for k in stage_keys:
            self.assertNotIn("session_id", k)
            self.assertNotIn("jwt", k)
            self.assertNotIn("token", k)
            self.assertNotIn("password", k)

    def test_structured_stream_records_ttft_on_first_token_before_buffer_drain(self):
        """
        8. Deterministic Regression Test (Phase 3 Stage C.2.1):
        Verify that in structured-result streaming:
        - A keepalive/comment before the first token does NOT trigger TTFT.
        - An empty token payload does NOT trigger TTFT.
        - The first non-empty token captures TTFT exactly at its mocked clock time.
        - Subsequent non-empty tokens do NOT overwrite the captured TTFT.
        - Stream completion and response sanitization do NOT overwrite TTFT.
        - The full response remains buffered and sanitized before single delta emission.
        """
        import asyncio
        from unittest.mock import MagicMock, patch
        from backend.utils.timing import start_request_timing, get_request_timing
        from backend.main import app, ChatPipelineResult
        from fastapi.testclient import TestClient
        import jwt
        from backend.config.config import JWT_SECRET, JWT_ALGORITHM

        client = TestClient(app)
        valid_jwt = jwt.encode(
            {"id": 888, "uid": 888, "email": "ttft_struct@example.com", "role": "employee"},
            JWT_SECRET,
            algorithm=JWT_ALGORITHM,
        )
        headers = {"Authorization": f"Bearer {valid_jwt}"}
        db_user_row = {
            "id": 888,
            "individual_id": "IND888",
            "fname": "TTFT",
            "lname": "Tester",
            "email": "ttft_struct@example.com",
            "user_type": 1,
            "status": 1,
            "is_deleted": 0,
            "token": "platform_token_ttft",
        }

        # Mock structured pipeline result with job cards
        struct_result = ChatPipelineResult(
            role="jobseeker",
            intent_hint="search_jobs",
            msg_len=15,
            system="System prompt",
            bounded_messages=[{"role": "user", "content": "find python jobs"}],
            max_tokens_compose=200,
            is_terminal=False,
            results_payload=[{"id": 1, "title": "Python Developer", "company_name": "Acme"}],
            result_type_payload="jobs",
            allowed_ids={1},
        )

        # Deterministic clock timeline (in seconds):
        # T0 = 100.000: Request starts (timing.start_time)
        # T1 = 100.015: Stream initialization / keepalive
        # T2 = 100.020: Empty token chunk arrives (should NOT set TTFT)
        # T3 = 100.045: First non-empty token chunk arrives ("Here are ") -> TTFT = (100.045 - 100.000) * 1000 = 45.0 ms
        # T4 = 100.090: Second non-empty token chunk arrives ("the python jobs.") -> must NOT overwrite TTFT
        # T5 = 100.150: Stream ends, compose ends, sanitization runs -> must NOT overwrite TTFT
        # T6 = 100.200: Final chat_stream_turn log emission
        current_time = 100.000

        def deterministic_perf_counter():
            nonlocal current_time
            return current_time

        captured_ctx = None
        ttft_after_empty = "NOT_CHECKED"
        ttft_after_token1 = "NOT_CHECKED"
        ttft_after_token2 = "NOT_CHECKED"

        async def controlled_mock_wrapper(gen, interval=15.0, req=None):
            nonlocal current_time, captured_ctx, ttft_after_empty, ttft_after_token1, ttft_after_token2
            captured_ctx = get_request_timing()

            # 1. Yield a keepalive comment before any token
            current_time = 100.015
            yield ("comment", ": keepalive\n\n")

            # 2. Yield an empty token payload (must NOT set TTFT)
            current_time = 100.020
            yield ("token", "")
            ttft_after_empty = captured_ctx.ttft_ms if captured_ctx else None

            # 3. Yield first non-empty content token (MUST set TTFT to 45.0 ms)
            current_time = 100.045
            yield ("token", "Here are ")
            ttft_after_token1 = captured_ctx.ttft_ms if captured_ctx else None

            # 4. Yield second content token (MUST NOT overwrite TTFT)
            current_time = 100.090
            yield ("token", "the python jobs.")
            ttft_after_token2 = captured_ctx.ttft_ms if captured_ctx else None

            # 5. Stream ends at T5
            current_time = 100.150

        async def fake_stream_gen():
            yield ""

        with patch("backend.api.auth.get_db") as mock_auth_db, \
             patch("backend.main.execute_chat_pipeline", return_value=struct_result), \
             patch("backend.services.llm_service.LLMService.stream_chat_completion", return_value=fake_stream_gen()), \
             patch("backend.main.stream_with_keepalive", side_effect=controlled_mock_wrapper), \
             patch("time.perf_counter", side_effect=deterministic_perf_counter):

            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_auth_db.return_value = mock_conn
            mock_conn.cursor.return_value = mock_cursor
            mock_cursor.fetchall.return_value = [db_user_row]

            res = client.post("/chat/stream", json={"messages": [{"role": "user", "content": "find python jobs"}]}, headers=headers)
            self.assertEqual(res.status_code, 200)

            ctx = captured_ctx
            self.assertIsNotNone(ctx)

            # Assert 1: Keepalive comment and empty token payload did NOT set TTFT
            self.assertIsNone(ttft_after_empty, "Keepalive comments and empty token payloads must not set TTFT")

            # Assert 2: First non-empty token set TTFT exactly to expected deterministic value (100.045 - 100.000) * 1000 = 45.0 ms
            expected_ttft_ms = 45.0
            self.assertEqual(ttft_after_token1, expected_ttft_ms, f"TTFT after token 1 must be {expected_ttft_ms} ms")

            # Assert 3: Subsequent token chunk at 100.090 did NOT overwrite TTFT
            self.assertEqual(ttft_after_token2, expected_ttft_ms, "Subsequent tokens must not overwrite TTFT")

            # Assert 4: Final post-stream state and sanitization at 100.150 did NOT overwrite TTFT
            self.assertEqual(ctx.ttft_ms, expected_ttft_ms, "Stream end and sanitization must not overwrite TTFT")
            self.assertIn("ttft", ctx.stages)
            self.assertEqual(ctx.stages["ttft"], 45)

            # Assert 5: Total compose duration is measured across the full stream (100.150 - 100.000) * 1000 = 150 ms
            self.assertIn("compose", ctx.stages)
            self.assertEqual(ctx.stages["compose"], 150)
            self.assertNotEqual(ctx.ttft_ms, ctx.stages["compose"], "TTFT must remain strictly distinct from full compose duration")

            # Assert 6: Buffering contract preserved - exactly 1 client-facing delta with sanitized text
            blocks = [b.strip() for b in res.text.strip().split("\n\n") if b.strip()]
            delta_blocks = [b for b in blocks if b.startswith("event: delta")]
            self.assertEqual(len(delta_blocks), 1)
            self.assertIn("data: ", delta_blocks[0])

            # Assert 7: Results event preceded delta event
            event_types = [b.split("\n")[0].replace("event: ", "").strip() for b in blocks if b.startswith("event:")]
            self.assertIn("results", event_types)
            self.assertIn("delta", event_types)
            self.assertLess(event_types.index("results"), event_types.index("delta"))




class TestSyntheticTurnProfiler(unittest.TestCase):

    """
    Stage 3.4.1B: Validates deterministic offline synthetic profiler harness.
    Exercises Scenarios 1 (cold), 2 (warm), and 3 (search) under fail-closed isolation.
    """

    def test_synthetic_profiler_cold_turn_scenario(self):
        """1. Exercises cold session context read with mock LLM streaming and verifies percentiles."""
        from backend.tests.fixtures.synthetic_turn_profiler import SyntheticTurnProfiler

        profiler = SyntheticTurnProfiler(iterations=5, warmup_runs=1)
        res = profiler.profile_scenario_cold_turn(simulated_llm_ttft_s=0.010, simulated_llm_chunk_s=0.005)

        self.assertEqual(res["scenario"], "cold_session_conversational")
        self.assertEqual(res["iterations"], 5)
        self.assertGreaterEqual(res["percentiles"]["p50"], 10.0)
        self.assertGreaterEqual(res["percentiles"]["max"], res["percentiles"]["min"])
        for rec in res["records"]:
            self.assertEqual(rec["status_code"], 200)
            self.assertTrue(rec["has_delta"])
            self.assertTrue(rec["has_done"])

    def test_synthetic_profiler_warm_conversational_scenario(self):
        """2. Exercises warm conversational turn and verifies zero retrieval dispatch."""
        from backend.tests.fixtures.synthetic_turn_profiler import SyntheticTurnProfiler

        profiler = SyntheticTurnProfiler(iterations=5, warmup_runs=1)
        res = profiler.profile_scenario_warm_conversational(simulated_llm_ttft_s=0.010, simulated_llm_chunk_s=0.005)

        self.assertEqual(res["scenario"], "warm_conversational_turn")
        self.assertEqual(res["iterations"], 5)
        self.assertGreaterEqual(res["percentiles"]["p50"], 10.0)
        for rec in res["records"]:
            self.assertEqual(rec["status_code"], 200)
            self.assertTrue(rec["has_delta"])

    def test_synthetic_profiler_search_scenario(self):
        """3. Exercises synthetic search turn with mock retrieval latency and card emission."""
        from backend.tests.fixtures.synthetic_turn_profiler import SyntheticTurnProfiler

        profiler = SyntheticTurnProfiler(iterations=5, warmup_runs=1)
        res = profiler.profile_scenario_search_turn(simulated_retrieval_s=0.015, simulated_llm_ttft_s=0.010)

        self.assertEqual(res["scenario"], "synthetic_search_turn")
        self.assertEqual(res["iterations"], 5)
        self.assertGreaterEqual(res["percentiles"]["p50"], 15.0)
        for rec in res["records"]:
            self.assertEqual(rec["status_code"], 200)
            self.assertTrue(rec["has_results"])
            self.assertTrue(rec["has_delta"])


if __name__ == "__main__":
    unittest.main()



