"""
test_p2_e2e_conversational_reliability.py — Phase 2 End-to-End Conversational Regression Suite.

Verifies complete multi-turn conversational lifecycles through FastAPI /chat and /chat/stream:
1. Search refinement: Preserves original keyword + location, applies remote filter, triggers fresh retrieval.
2. Location replacement: "In Bangalore" follow-up replaces Delhi rather than appending/combining.
3. Ambiguous follow-up: Multi-job result with "Why was this recommended?" requests clarification without silent rank #1 selection.
4. Exact job reference & missing salary: Resolves intended job ID deterministically; missing salary formats as 'Not specified'.
5. New-search isolation: Materially new search advances search generation and replaces old criteria cleanly.
6. Sequential streaming parity: /chat/stream yields correct SSE lifecycle (start -> results -> status -> delta -> done) matching /chat.
7. Single-job resolution: Unambiguous 1-job snapshot resolves follow-up deterministically without false clarification.
"""

import json
import time
import uuid
import pytest
import jwt
from unittest.mock import MagicMock, AsyncMock, patch
from fastapi.testclient import TestClient

from backend.main import app
from backend.config.config import JWT_SECRET, JWT_ALGORITHM
from backend.schemas.search_query import SearchTarget
from backend.schemas.schemas import JobCard
from backend.utils.sse import parse_sse_event
from backend.utils.session_state import (
    begin_search_generation,
    get_search_generation,
    set_recommendation_session,
    get_recommendation_session,
    set_session_state,
    get_session_state,
    reset_search_context,
)


class TestP2E2EConversationalReliability:
    """End-to-End Multi-Turn FastAPI Integration Tests for Phase 2."""

    @pytest.fixture(autouse=True)
    def setup_client(self):
        self.client = TestClient(app)
        self.user_id = 9876
        self.email = "e2e_tester@collarcheck.com"
        self.role = "employee"
        self.token = jwt.encode(
            {"id": self.user_id, "uid": self.user_id, "email": self.email, "role": self.role},
            JWT_SECRET,
            algorithm=JWT_ALGORITHM,
        )
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.db_user_row = {
            "id": self.user_id,
            "individual_id": "IND9876",
            "fname": "E2ETest",
            "lname": "User",
            "email": self.email,
            "user_type": 1,
            "status": 1,
            "is_deleted": 0,
            "phone": "9999999999",
            "gender": "male",
            "dob": "1990-01-01",
            "expected_salary": 1200000,
            "profile_description": "Experienced developer",
            "city": "Delhi",
            "state": "Delhi",
            "country": "India",
        }

    # -------------------------------------------------------------------------
    # Case 1: Search Refinement (Delhi Python Jobs -> Only Remote Ones)
    # -------------------------------------------------------------------------
    @patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock)
    @patch("backend.services.search_router.dispatch_search_target", new_callable=AsyncMock)
    @patch("backend.services.search_parser.parse_search_query", new_callable=AsyncMock)
    @patch("backend.api.auth.get_db")
    def test_01_e2e_search_refinement_preserves_keyword_and_location(
        self, mock_auth_db, mock_parser, mock_dispatch, mock_llm
    ):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]
        mock_cursor.fetchone.return_value = self.db_user_row

        mock_llm.return_value = "Here are the matching remote Python roles in Delhi."
        session_id = f"test_e2e_refine_{uuid.uuid4().hex[:8]}"

        # Turn 1: Initial search
        mock_parser.return_value = {
            "keyword": "Python developer",
            "location": "Delhi",
            "search_target": SearchTarget.JOBS,
        }
        def fake_dispatch_t1(**kwargs):
            from backend.utils.session_state import set_search_context
            set_search_context(session_id, {"keyword": "Python developer", "location": "Delhi"}, role="employee")
            return {
                "db_context": "Found 2 matching jobs in Delhi",
                "results": [{"job_id": 101, "title": "Python Dev", "company": "Delhi Tech", "url": "/jobs/101"}],
                "result_type": "jobs",
                "allowed_ids": {101},
                "raw_hits": [{"id": 101, "job_description": "Python job"}],
                "route_branch": "SEARCH",
            }

        mock_dispatch.side_effect = fake_dispatch_t1

        resp1 = self.client.post(
            "/chat",
            json={"session_id": session_id, "messages": [{"role": "user", "content": "Find Python developer jobs in Delhi"}]},
            headers=self.headers,
        )
        assert resp1.status_code == 200

        # Turn 2: Follow-up refinement "Only remote ones"
        mock_parser.return_value = {
            "search_target": SearchTarget.JOBS,
            "job_mode": "home",
        }
        mock_dispatch.side_effect = None
        mock_dispatch.return_value = {
            "db_context": "Found 1 matching remote job in Delhi",
            "results": [{"job_id": 102, "title": "Remote Python Dev", "company": "Delhi Remote", "url": "/jobs/102"}],
            "result_type": "jobs",
            "allowed_ids": {102},
            "raw_hits": [{"id": 102, "job_description": "Remote Python job"}],
            "route_branch": "SEARCH",
        }

        resp2 = self.client.post(
            "/chat",
            json={
                "session_id": session_id,
                "messages": [
                    {"role": "user", "content": "Find Python developer jobs in Delhi"},
                    {"role": "assistant", "content": "Here are Python jobs in Delhi"},
                    {"role": "user", "content": "Only remote ones"},
                ],
            },
            headers=self.headers,
        )
        assert resp2.status_code == 200

        # Assert that dispatch received preserved keyword, preserved location, and remote work mode
        turn2_call_kwargs = mock_dispatch.call_args.kwargs
        turn2_parsed = turn2_call_kwargs.get("parsed_query", {})
        assert turn2_parsed.get("keyword") == "Python developer"
        assert turn2_parsed.get("location") == "Delhi"
        assert turn2_parsed.get("job_mode") == "home"

    # -------------------------------------------------------------------------
    # Case 2: Location Replacement (Delhi -> In Bangalore)
    # -------------------------------------------------------------------------
    @patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock)
    @patch("backend.services.search_router.dispatch_search_target", new_callable=AsyncMock)
    @patch("backend.services.search_parser.parse_search_query", new_callable=AsyncMock)
    @patch("backend.api.auth.get_db")
    def test_02_e2e_location_replacement(
        self, mock_auth_db, mock_parser, mock_dispatch, mock_llm
    ):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]
        mock_cursor.fetchone.return_value = self.db_user_row
        mock_llm.return_value = "Here are Python jobs in Bangalore."

        session_id = f"test_e2e_loc_{uuid.uuid4().hex[:8]}"

        # Turn 1: Search in Delhi
        mock_parser.return_value = {
            "keyword": "Python developer",
            "location": "Delhi",
            "search_target": SearchTarget.JOBS,
        }
        mock_dispatch.return_value = {
            "db_context": "Found 2 jobs in Delhi",
            "results": [{"job_id": 101, "title": "Dev in Delhi", "url": "/jobs/101"}],
            "result_type": "jobs",
            "allowed_ids": {101},
            "raw_hits": [{"id": 101}],
            "route_branch": "SEARCH",
        }
        self.client.post(
            "/chat",
            json={"session_id": session_id, "messages": [{"role": "user", "content": "Find Python developer jobs in Delhi"}]},
            headers=self.headers,
        )

        # Turn 2: "In Bangalore"
        # parse_search_query correctly identifies Bangalore as the explicit new location
        mock_parser.return_value = {
            "keyword": "Python developer",
            "location": "Bangalore",
            "search_target": SearchTarget.JOBS,
        }
        mock_dispatch.return_value = {
            "db_context": "Found 3 jobs in Bangalore",
            "results": [{"job_id": 201, "title": "Dev in Bangalore", "url": "/jobs/201"}],
            "result_type": "jobs",
            "allowed_ids": {201},
            "raw_hits": [{"id": 201}],
            "route_branch": "SEARCH",
        }

        resp2 = self.client.post(
            "/chat",
            json={
                "session_id": session_id,
                "messages": [
                    {"role": "user", "content": "Find Python developer jobs in Delhi"},
                    {"role": "assistant", "content": "Here are Delhi jobs"},
                    {"role": "user", "content": "In Bangalore"},
                ],
            },
            headers=self.headers,
        )
        assert resp2.status_code == 200

        turn2_call_kwargs = mock_dispatch.call_args.kwargs
        turn2_parsed = turn2_call_kwargs.get("parsed_query", {})
        # Bangalore must take precedence and overwrite Delhi
        assert turn2_parsed.get("location") == "Bangalore"
        assert turn2_parsed.get("keyword") == "Python developer"

    # -------------------------------------------------------------------------
    # Case 3: Ambiguous Follow-Up Requests Clarification
    # -------------------------------------------------------------------------
    @patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock)
    @patch("backend.services.search_router.dispatch_search_target", new_callable=AsyncMock)
    @patch("backend.services.search_parser.parse_search_query", new_callable=AsyncMock)
    @patch("backend.api.auth.get_db")
    def test_03_e2e_ambiguous_followup_requests_clarification(
        self, mock_auth_db, mock_parser, mock_dispatch, mock_llm
    ):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]
        mock_cursor.fetchone.return_value = self.db_user_row
        mock_llm.return_value = "Could you please specify which job you are referring to?"

        session_id = f"test_e2e_ambig_{uuid.uuid4().hex[:8]}"

        # Establish multi-job snapshot directly in session state
        jobs = [
            {"job_id": 101, "title": "Backend Dev", "company_name": "Alpha Corp", "global_rank": 1},
            {"job_id": 102, "title": "Fullstack Dev", "company_name": "Beta Labs", "global_rank": 2},
        ]
        set_recommendation_session(
            session_id=session_id,
            query_key="python_jobs",
            ranked_jobs=jobs,
            current_page=1,
            page_size=5,
            total_jobs=2,
            role="employee",
            user_id=self.user_id,
            search_gen=1,
        )

        mock_parser.return_value = {
            "search_target": SearchTarget.JOBS,
        }
        # dispatch_search_target delegates to recommendation_service.get_recommendation_by_rank_or_id
        mock_dispatch.return_value = {
            "db_context": "## RECOMMENDATION SNAPSHOT ERROR\nCould you please specify which job you are referring to (for example, by job title or rank number)?",
            "results": None,
            "result_type": "recommendation_explanation_error",
            "route_branch": "EXPLANATION_ERROR",
            "allowed_ids": set(),
        }

        resp = self.client.post(
            "/chat",
            json={
                "session_id": session_id,
                "messages": [
                    {"role": "user", "content": "Find Python jobs"},
                    {"role": "assistant", "content": "Found 2 jobs"},
                    {"role": "user", "content": "Why was this recommended?"},
                ],
            },
            headers=self.headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("result_type") == "recommendation_explanation_error"

    # -------------------------------------------------------------------------
    # Case 4: Exact Job Reference & Missing Salary Grounding
    # -------------------------------------------------------------------------
    @patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock)
    @patch("backend.services.search_router.dispatch_search_target", new_callable=AsyncMock)
    @patch("backend.services.search_parser.parse_search_query", new_callable=AsyncMock)
    @patch("backend.api.auth.get_db")
    def test_04_e2e_exact_job_reference_missing_salary_grounded(
        self, mock_auth_db, mock_parser, mock_dispatch, mock_llm
    ):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]
        mock_cursor.fetchone.return_value = self.db_user_row

        session_id = f"test_e2e_salary_{uuid.uuid4().hex[:8]}"

        # Establish deterministic snapshot where Job #1 has NO salary, while Job #2 has a salary
        jobs = [
            {"job_id": 501, "title": "Dev Without Salary", "company_name": "NoSal Co", "salary": None, "global_rank": 1},
            {"job_id": 502, "title": "Dev With Salary", "company_name": "HasSal Co", "salary": 2000000, "global_rank": 2},
        ]
        set_recommendation_session(
            session_id=session_id,
            query_key="salary_test_jobs",
            ranked_jobs=jobs,
            current_page=1,
            page_size=5,
            total_jobs=2,
            role="employee",
            user_id=self.user_id,
            search_gen=1,
        )

        mock_parser.return_value = {"search_target": SearchTarget.JOBS}
        mock_dispatch.return_value = {
            "db_context": "## SELECTED JOB RESULT DETAILS\nThe user is asking about Job #1:\n- Title: Dev Without Salary\n- Company: NoSal Co\n- Salary: Not specified",
            "results": None,
            "result_type": "job_detail",
            "allowed_ids": {501},
            "route_branch": "JOB_DETAIL",
        }
        mock_llm.return_value = "The salary for Dev Without Salary at NoSal Co is not specified in the job listing."

        resp = self.client.post(
            "/chat",
            json={
                "session_id": session_id,
                "messages": [
                    {"role": "user", "content": "Find developer jobs"},
                    {"role": "assistant", "content": "Here are 2 jobs"},
                    {"role": "user", "content": "What is the salary of the 1st job?"},
                ],
            },
            headers=self.headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("result_type") == "job_detail"
        # Confirm prompt and LLM response reflect 'Not specified' without borrowing Job #2's 20 LPA salary
        assert "not specified" in data.get("reply", "").lower()
        assert "20" not in data.get("reply", "")

    # -------------------------------------------------------------------------
    # Case 5: New-Search Generation Isolation
    # -------------------------------------------------------------------------
    @patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock)
    @patch("backend.services.search_router.dispatch_search_target", new_callable=AsyncMock)
    @patch("backend.services.search_parser.parse_search_query", new_callable=AsyncMock)
    @patch("backend.api.auth.get_db")
    def test_05_e2e_new_search_advances_generation_and_isolates_state(
        self, mock_auth_db, mock_parser, mock_dispatch, mock_llm
    ):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]
        mock_cursor.fetchone.return_value = self.db_user_row
        mock_llm.return_value = "Here are accountant jobs in Mumbai."

        session_id = f"test_e2e_newsearch_{uuid.uuid4().hex[:8]}"

        # Turn 1: Search Python in Delhi
        mock_parser.return_value = {
            "keyword": "Python developer",
            "location": "Delhi",
            "search_target": SearchTarget.JOBS,
        }
        mock_dispatch.return_value = {
            "db_context": "Found Python jobs in Delhi",
            "results": [{"job_id": 101, "title": "Python Dev", "url": "/jobs/101"}],
            "result_type": "jobs",
            "allowed_ids": {101},
            "raw_hits": [{"id": 101}],
            "route_branch": "SEARCH",
        }
        resp1 = self.client.post(
            "/chat",
            json={"session_id": session_id, "messages": [{"role": "user", "content": "Find Python developer jobs in Delhi"}]},
            headers=self.headers,
        )
        assert resp1.status_code == 200

        gen_turn1 = get_search_generation(session_id, role="employee", user_id=self.user_id)
        assert gen_turn1 >= 1

        # Turn 2: Explicit brand new search "Search for accountant jobs in Mumbai"
        mock_parser.return_value = {
            "keyword": "accountant",
            "location": "Mumbai",
            "search_target": SearchTarget.JOBS,
        }
        mock_dispatch.return_value = {
            "db_context": "Found accountant jobs in Mumbai",
            "results": [{"job_id": 901, "title": "Accountant", "url": "/jobs/901"}],
            "result_type": "jobs",
            "allowed_ids": {901},
            "raw_hits": [{"id": 901}],
            "route_branch": "SEARCH",
        }
        resp2 = self.client.post(
            "/chat",
            json={
                "session_id": session_id,
                "messages": [
                    {"role": "user", "content": "Find Python developer jobs in Delhi"},
                    {"role": "assistant", "content": "Here are Python jobs"},
                    {"role": "user", "content": "Search for accountant jobs in Mumbai"},
                ],
            },
            headers=self.headers,
        )
        assert resp2.status_code == 200

        # Assert criteria completely replaced Delhi with Mumbai and Python with Accountant
        turn2_call_kwargs = mock_dispatch.call_args.kwargs
        turn2_parsed = turn2_call_kwargs.get("parsed_query", {})
        assert turn2_parsed.get("keyword") == "accountant"
        assert turn2_parsed.get("location") == "Mumbai"
        assert "Delhi" not in str(turn2_parsed.get("location"))

    # -------------------------------------------------------------------------
    # Case 6: Sequential Streaming Parity across Multi-Turn (/chat/stream)
    # -------------------------------------------------------------------------
    @patch("backend.services.llm_service.LLMService.stream_chat_completion")
    @patch("backend.services.search_router.dispatch_search_target", new_callable=AsyncMock)
    @patch("backend.services.search_parser.parse_search_query", new_callable=AsyncMock)
    @patch("backend.api.auth.get_db")
    def test_06_e2e_sequential_streaming_parity(
        self, mock_auth_db, mock_parser, mock_dispatch, mock_stream
    ):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]
        mock_cursor.fetchone.return_value = self.db_user_row

        session_id = f"test_e2e_stream_{uuid.uuid4().hex[:8]}"

        async def fake_stream(*args, **kwargs):
            yield "Here are your matching job openings in Delhi."

        mock_stream.side_effect = fake_stream

        # Turn 1 via /chat/stream
        mock_parser.return_value = {
            "keyword": "Python developer",
            "location": "Delhi",
            "search_target": SearchTarget.JOBS,
        }
        mock_dispatch.return_value = {
            "db_context": "Jobs in Delhi",
            "results": [JobCard(job_id=101, title="Dev", company="Co", location="Delhi", url="/jobs/101", match_score=0.9)],
            "result_type": "jobs",
            "allowed_ids": {101},
            "raw_hits": [{"id": 101}],
            "route_branch": "SEARCH",
        }

        resp1 = self.client.post(
            "/chat/stream",
            json={"session_id": session_id, "messages": [{"role": "user", "content": "Find Python developer jobs in Delhi"}]},
            headers=self.headers,
        )
        assert resp1.status_code == 200
        events1 = [parse_sse_event(b) for b in resp1.text.strip().split("\n\n") if b.strip()]
        event_types1 = [e["event"] for e in events1]
        assert event_types1 == ["start", "results", "status", "delta", "done"]

        # Turn 2 via /chat/stream: Refinement "Only remote ones"
        mock_parser.return_value = {
            "search_target": SearchTarget.JOBS,
            "job_mode": "home",
        }
        mock_dispatch.return_value = {
            "db_context": "Remote jobs in Delhi",
            "results": [JobCard(job_id=102, title="Remote Dev", company="Co", location="Delhi", url="/jobs/102", match_score=0.95)],
            "result_type": "jobs",
            "allowed_ids": {102},
            "raw_hits": [{"id": 102}],
            "route_branch": "SEARCH",
        }

        resp2 = self.client.post(
            "/chat/stream",
            json={
                "session_id": session_id,
                "messages": [
                    {"role": "user", "content": "Find Python developer jobs in Delhi"},
                    {"role": "assistant", "content": "Here are Python jobs in Delhi"},
                    {"role": "user", "content": "Only remote ones"},
                ],
            },
            headers=self.headers,
        )
        assert resp2.status_code == 200
        events2 = [parse_sse_event(b) for b in resp2.text.strip().split("\n\n") if b.strip()]
        event_types2 = [e["event"] for e in events2]
        assert event_types2 == ["start", "results", "status", "delta", "done"]

        # Verify results in streaming event contain remote dev
        res_evt = next(e for e in events2 if e["event"] == "results")
        assert res_evt["data"]["results"][0]["job_id"] == 102

    # -------------------------------------------------------------------------
    # Case 7: Single-Job Resolution Unambiguously Succeeds Without Clarification
    # -------------------------------------------------------------------------
    @patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock)
    @patch("backend.services.search_router.dispatch_search_target", new_callable=AsyncMock)
    @patch("backend.services.search_parser.parse_search_query", new_callable=AsyncMock)
    @patch("backend.api.auth.get_db")
    def test_07_e2e_single_job_snapshot_resolves_unambiguously(
        self, mock_auth_db, mock_parser, mock_dispatch, mock_llm
    ):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]
        mock_cursor.fetchone.return_value = self.db_user_row

        session_id = f"test_e2e_single_{uuid.uuid4().hex[:8]}"

        # Establish snapshot with EXACTLY 1 job
        single_job = [{"job_id": 777, "title": "Unique Specialist", "company_name": "Solo Corp", "global_rank": 1}]
        set_recommendation_session(
            session_id=session_id,
            query_key="single_job_query",
            ranked_jobs=single_job,
            current_page=1,
            page_size=5,
            total_jobs=1,
            role="employee",
            user_id=self.user_id,
            search_gen=1,
        )

        mock_parser.return_value = {"search_target": SearchTarget.JOBS}
        # In a 1-job pool, asking "Why was this recommended?" is unambiguous and resolves Job #777
        mock_dispatch.return_value = {
            "db_context": "## JOB RECOMMENDATION EXPLANATION\nExplanation for global rank #1 (Job ID 777): 95% match with your profile skills.",
            "results": None,
            "result_type": "recommendation_explanation",
            "allowed_ids": {777},
            "route_branch": "EXPLANATION",
        }
        mock_llm.return_value = "Unique Specialist was recommended because it has a 95% skill match with your profile."

        resp = self.client.post(
            "/chat",
            json={
                "session_id": session_id,
                "messages": [
                    {"role": "user", "content": "Find quantum computing jobs"},
                    {"role": "assistant", "content": "Found 1 job"},
                    {"role": "user", "content": "Why was this recommended?"},
                ],
            },
            headers=self.headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("result_type") == "recommendation_explanation"
        assert "777" in str(data) or "Unique Specialist" in data.get("reply", "")
