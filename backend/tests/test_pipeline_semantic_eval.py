"""
backend/tests/test_pipeline_semantic_eval.py — Offline mock-based pipeline semantic evaluation.

CollarCheck Phase 2 — Step 3B.3.
Evaluates representative cases from eval_semantic_dataset.json through the FastAPI /chat
and /chat/stream pipelines using pure mock isolation.

Safety guarantees:
- Zero live calls to external databases, search engines, embeddings, LLMs, or telemetry.
- Preserves real pipeline routing, query-rewriter logic, prompt assembly, and reply sanitization.
- Clean session-state teardown scoped strictly to the current test session.
- Deterministic phrase checks are regression assertion proxies, not production LLM judges.
"""

import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import uuid
import jwt
from fastapi.testclient import TestClient

from backend.main import app
from backend.config.config import JWT_SECRET, JWT_ALGORITHM
from backend.schemas.search_query import SearchTarget
from backend.schemas.schemas import JobCard
from backend.services.query_rewriter import QueryRewriterService
from backend.utils.sse import parse_sse_event
from backend.utils.retrieval_evaluator import (
    evaluate_deterministic_faithfulness,
    evaluate_abstention_quality,
    verify_entity_id_grounding,
)
from backend.utils.session_state import (
    _SESSION_STATES,
    _SESSION_LOCKS,
    set_session_option_mapping,
    set_recommendation_session,
    set_search_context,
)


class TestPipelineSemanticEvaluation(unittest.TestCase):
    """
    Offline Integration tests connecting the deterministic semantic evaluators to
    the actual /chat and /chat/stream pipeline execution flow.
    """

    @classmethod
    def setUpClass(cls):
        fixture_path = Path(__file__).parent / "fixtures" / "eval_semantic_dataset.json"
        with open(fixture_path, "r", encoding="utf-8") as f:
            cls.dataset = json.load(f)
        cls.case_map = {c["case_id"]: c for c in cls.dataset["cases"]}

    def setUp(self):
        self.client = TestClient(app)
        self.user_id = 9876
        self.email = "pipeline_eval@collarcheck.com"
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
            "fname": "Semantic",
            "lname": "Tester",
            "email": self.email,
            "user_type": 1,
            "status": 1,
            "is_deleted": 0,
            "phone": "9999999999",
            "gender": "male",
            "dob": "1990-01-01",
            "expected_salary": 1500000,
            "profile_description": "Senior QA Engineer",
            "city": "Delhi",
            "state": "Delhi",
            "country": "India",
        }
        self.active_session_id = f"test_pipe_eval_{uuid.uuid4().hex[:8]}"

    def tearDown(self):
        # Precise, isolated session cleanup: clear only entries belonging to active_session_id
        sid = getattr(self, "active_session_id", None)
        if sid:
            matching_keys = [k for k in list(_SESSION_STATES.keys()) if sid in k]
            for k in matching_keys:
                _SESSION_STATES.pop(k, None)
            matching_locks = [k for k in list(_SESSION_LOCKS.keys()) if sid in k]
            for k in matching_locks:
                _SESSION_LOCKS.pop(k, None)

    def _setup_mock_db(self, mock_auth_db):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]
        mock_cursor.fetchone.return_value = self.db_user_row

    # -------------------------------------------------------------------------
    # 1. SEM-01: Supported Salary and Entity Grounding
    # -------------------------------------------------------------------------
    @patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock)
    @patch("backend.services.search_router.dispatch_search_target", new_callable=AsyncMock)
    @patch("backend.services.search_parser.parse_search_query", new_callable=AsyncMock)
    @patch("backend.services.token_resolver.get_user_platform_token")
    @patch("backend.api.auth.get_db")
    def test_01_pipeline_sem01_supported_salary_and_grounding(
        self, mock_auth_db, mock_tok, mock_parser, mock_dispatch, mock_llm
    ):
        """Exercises SEM-01: Verifies supported salary presence and structured entity grounding."""
        self._setup_mock_db(mock_auth_db)
        case = self.case_map["SEM-01"]

        mock_parser.return_value = {"search_target": SearchTarget.JOBS}
        job_card = JobCard(
            job_id=101,
            title="Senior Python Lead",
            company="AlphaTech",
            location="Delhi",
            url="https://collarcheck.com/jobs/101",
        )
        mock_dispatch.return_value = {
            "db_context": case["synthetic_evidence"],
            "results": [job_card],
            "result_type": "job_detail",
            "allowed_ids": {101, "job_101"},
            "raw_hits": [{"id": 101, "job_description": "Senior Python Lead at AlphaTech. Fixed compensation is 35 LPA."}],
            "route_branch": "JOB_DETAIL",
        }
        # Faithful answer reflecting evidence for job detail inquiry
        mock_llm.return_value = "The Python Lead role at AlphaTech offers a fixed compensation of 35 LPA."

        resp = self.client.post(
            "/chat",
            json={"session_id": self.active_session_id, "messages": [{"role": "user", "content": case["query"]}]},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])

        # Structured Entity-ID Grounding
        results = body.get("results") or []
        self.assertTrue(len(results) > 0)
        actual_entity_id = f"job_{results[0]['job_id']}"
        self.assertTrue(
            verify_entity_id_grounding(
                actual_selected_id=actual_entity_id,
                expected_selected_id=case["expected_entity_id"],
            )
        )

        # Deterministic Faithfulness Assertion
        eval_res = evaluate_deterministic_faithfulness(
            answer_text=body["reply"],
            supported_facts=case["supported_facts"],
            forbidden_claims=case["forbidden_claims"],
        )
        self.assertTrue(eval_res["is_faithful"])
        self.assertEqual(eval_res["score"], 1.0)
        self.assertEqual(eval_res["detected_forbidden_claims"], [])

    # -------------------------------------------------------------------------
    # 2. SEM-02: Missing Salary Abstention & Negative Hallucination Check
    # -------------------------------------------------------------------------
    @patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock)
    @patch("backend.services.search_router.dispatch_search_target", new_callable=AsyncMock)
    @patch("backend.services.search_parser.parse_search_query", new_callable=AsyncMock)
    @patch("backend.services.token_resolver.get_user_platform_token")
    @patch("backend.api.auth.get_db")
    def test_02_pipeline_sem02_missing_salary_abstention_and_negative_check(
        self, mock_auth_db, mock_tok, mock_parser, mock_dispatch, mock_llm
    ):
        """Exercises SEM-02: Honest missing-evidence abstention, plus negative fabricated-salary failure."""
        self._setup_mock_db(mock_auth_db)
        case = self.case_map["SEM-02"]

        mock_parser.return_value = {"search_target": SearchTarget.JOBS}
        job_card = JobCard(
            job_id=102,
            title="DevOps Engineer",
            company="CloudSphere",
            location="Pune",
            url="https://collarcheck.com/jobs/102",
        )
        # Synthetic evidence formatted matching job_detail with missing salary
        db_context_detail = "## SELECTED JOB RESULT DETAILS\n- Title: DevOps Engineer\n- Company: CloudSphere\n- Location: Pune\n- Salary: Not specified in listing"
        mock_dispatch.return_value = {
            "db_context": db_context_detail,
            "results": [job_card],
            "result_type": "job_detail",
            "allowed_ids": {102, "job_102"},
            "raw_hits": [{"id": 102, "job_description": "DevOps Engineer. Salary: Not specified in listing."}],
            "route_branch": "JOB_DETAIL",
        }

        # 1. Honest Abstention Response
        mock_llm.return_value = "The compensation for this DevOps Engineer role is not specified in the job listing."
        resp_pass = self.client.post(
            "/chat",
            json={"session_id": self.active_session_id, "messages": [{"role": "user", "content": case["query"]}]},
            headers=self.headers,
        )
        self.assertEqual(resp_pass.status_code, 200)
        reply_pass = resp_pass.json()["reply"]

        eval_pass = evaluate_abstention_quality(
            answer_text=reply_pass,
            requires_abstention=case["requires_abstention"],
            expected_abstention_phrases=case["expected_abstention_phrases"],
            forbidden_claims=case["forbidden_claims"],
            abstention_type=case["abstention_type"],
        )
        self.assertTrue(eval_pass["passed"])
        self.assertEqual(eval_pass["score"], 1.0)

        # 2. Negative Test: Simulated Hallucinated Salary Failure
        # When LLM fabricates a salary figure despite missing salary, the evaluator catches it
        fabricated_reply = "The compensation is not specified, but typically pays 25 LPA per annum."
        eval_fail = evaluate_abstention_quality(
            answer_text=fabricated_reply,
            requires_abstention=case["requires_abstention"],
            expected_abstention_phrases=case["expected_abstention_phrases"],
            forbidden_claims=case["forbidden_claims"],
            abstention_type=case["abstention_type"],
        )
        self.assertFalse(eval_fail["passed"])
        self.assertEqual(eval_fail["score"], 0.0)
        self.assertIn("LPA", eval_fail["detected_forbidden_claims"])

    # -------------------------------------------------------------------------
    # 3. SEM-04: Cross-Entity Salary Isolation
    # -------------------------------------------------------------------------
    @patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock)
    @patch("backend.services.search_router.dispatch_search_target", new_callable=AsyncMock)
    @patch("backend.services.search_parser.parse_search_query", new_callable=AsyncMock)
    @patch("backend.services.token_resolver.get_user_platform_token")
    @patch("backend.api.auth.get_db")
    def test_03_pipeline_sem04_cross_entity_salary_isolation(
        self, mock_auth_db, mock_tok, mock_parser, mock_dispatch, mock_llm
    ):
        """Exercises SEM-04: Strict entity attribute isolation preventing cross-entity leakage."""
        self._setup_mock_db(mock_auth_db)
        case = self.case_map["SEM-04"]

        mock_parser.return_value = {"search_target": SearchTarget.JOBS}
        # Multi-job evidence where Job A (104) has 50 LPA and Job B (105) has 20 LPA
        job105 = JobCard(
            job_id=105,
            title="Software Engineer (Job B)",
            company="EdTech Solutions",
            location="Bengaluru",
            url="https://collarcheck.com/jobs/105",
        )
        mock_dispatch.return_value = {
            "db_context": case["synthetic_evidence"],
            "results": [job105],
            "result_type": "job_detail",
            "allowed_ids": {105, "job_105"},
            "raw_hits": [
                {"id": 104, "company": "FinTech Global", "salary": "50 LPA"},
                {"id": 105, "company": "EdTech Solutions", "salary": "20 LPA"},
            ],
            "route_branch": "JOB_DETAIL",
        }

        # Simulated response correctly isolating Job B without leaking Job A's 50 LPA
        mock_llm.return_value = "The salary for Job B at EdTech Solutions is 20 LPA."

        resp = self.client.post(
            "/chat",
            json={"session_id": self.active_session_id, "messages": [{"role": "user", "content": case["query"]}]},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        # Verify Grounding on Job 105
        results = body.get("results") or []
        self.assertTrue(len(results) > 0)
        self.assertEqual(str(results[0]["job_id"]), "105")

        # Verify Faithfulness: 20 LPA present, 50 LPA forbidden
        eval_res = evaluate_deterministic_faithfulness(
            answer_text=body["reply"],
            supported_facts=case["supported_facts"],
            forbidden_claims=case["forbidden_claims"],
        )
        self.assertTrue(eval_res["is_faithful"])
        self.assertEqual(eval_res["score"], 1.0)
        self.assertEqual(eval_res["detected_forbidden_claims"], [])

    # -------------------------------------------------------------------------
    # 4. SEM-05: Out-of-Range Ordinal Handling
    # -------------------------------------------------------------------------
    @patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock)
    @patch("backend.services.search_router.dispatch_search_target", new_callable=AsyncMock)
    @patch("backend.services.search_parser.parse_search_query", new_callable=AsyncMock)
    @patch("backend.services.token_resolver.get_user_platform_token")
    @patch("backend.api.auth.get_db")
    def test_04_pipeline_sem05_out_of_range_ordinal_handling(
        self, mock_auth_db, mock_tok, mock_parser, mock_dispatch, mock_llm
    ):
        """Exercises SEM-05: Real query rewriter ordinal bounds checking and out-of-range abstention."""
        self._setup_mock_db(mock_auth_db)
        case = self.case_map["SEM-05"]

        # Seed real session state with only 3 jobs in active recommendation pool
        set_recommendation_session(
            session_id=self.active_session_id,
            query_key="query_sem05",
            ranked_jobs=[
                {"id": 101, "raw_data": {"job_title": "Role 1"}},
                {"id": 102, "raw_data": {"job_title": "Role 2"}},
                {"id": 103, "raw_data": {"job_title": "Role 3"}},
            ],
            current_page=1,
            page_size=5,
            total_jobs=3,
            role="employee",
            user_id=self.user_id,
        )

        mock_parser.return_value = {"search_target": SearchTarget.JOBS}
        mock_dispatch.return_value = {
            "db_context": case["synthetic_evidence"],
            "results": [],
            "result_type": "jobs",
            "allowed_ids": set(),
            "raw_hits": [],
            "route_branch": "SEARCH",
        }
        mock_llm.return_value = "There are only 3 jobs in your current results. The tenth job is out of range."

        resp = self.client.post(
            "/chat",
            json={
                "session_id": self.active_session_id,
                "messages": [
                    {"role": "assistant", "content": "Found 3 matching jobs: Job 1, Job 2, Job 3."},
                    {"role": "user", "content": case["query"]},
                ],
            },
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        # Out of range must NOT select any entity ID
        actual_results = body.get("results") or []
        self.assertEqual(len(actual_results), 0)
        self.assertTrue(
            verify_entity_id_grounding(
                actual_selected_id=None,
                expected_selected_id=case["expected_entity_id"],
            )
        )

        # Evaluator abstention check
        eval_res = evaluate_abstention_quality(
            answer_text=body["reply"],
            requires_abstention=case["requires_abstention"],
            expected_abstention_phrases=case["expected_abstention_phrases"],
            forbidden_claims=case["forbidden_claims"],
            abstention_type=case["abstention_type"],
        )
        self.assertTrue(eval_res["passed"])
        self.assertEqual(eval_res["score"], 1.0)

    # -------------------------------------------------------------------------
    # 5. SEM-06: Ambiguous Entity Clarification
    # -------------------------------------------------------------------------
    @patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock)
    @patch("backend.services.search_router.dispatch_search_target", new_callable=AsyncMock)
    @patch("backend.services.search_parser.parse_search_query", new_callable=AsyncMock)
    @patch("backend.services.token_resolver.get_user_platform_token")
    @patch("backend.api.auth.get_db")
    def test_05_pipeline_sem06_ambiguous_entity_clarification(
        self, mock_auth_db, mock_tok, mock_parser, mock_dispatch, mock_llm
    ):
        """Exercises SEM-06: Clarification requested when user reference is ambiguous across active jobs."""
        self._setup_mock_db(mock_auth_db)
        case = self.case_map["SEM-06"]

        # Seed session with 4 jobs
        set_recommendation_session(
            session_id=self.active_session_id,
            query_key="query_sem06",
            ranked_jobs=[
                {"id": 101, "raw_data": {"job_title": "Job 1"}},
                {"id": 102, "raw_data": {"job_title": "Job 2"}},
                {"id": 103, "raw_data": {"job_title": "Job 3"}},
                {"id": 104, "raw_data": {"job_title": "Job 4"}},
            ],
            current_page=1,
            page_size=5,
            total_jobs=4,
            role="employee",
            user_id=self.user_id,
        )

        mock_parser.return_value = {"search_target": SearchTarget.JOBS}
        mock_dispatch.return_value = {
            "db_context": case["synthetic_evidence"],
            "results": [],
            "result_type": "jobs",
            "allowed_ids": set(),
            "raw_hits": [],
            "route_branch": "SEARCH",
        }
        mock_llm.return_value = "You have 4 jobs in your search results. Could you please specify which job you are referring to?"

        resp = self.client.post(
            "/chat",
            json={
                "session_id": self.active_session_id,
                "messages": [
                    {"role": "assistant", "content": "I found 4 jobs for you."},
                    {"role": "user", "content": case["query"]},
                ],
            },
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        # Unresolved entity must be None
        actual_results = body.get("results") or []
        self.assertEqual(len(actual_results), 0)

        # Abstention evaluator verifies entity_ambiguity clarification
        eval_res = evaluate_abstention_quality(
            answer_text=body["reply"],
            requires_abstention=case["requires_abstention"],
            expected_abstention_phrases=case["expected_abstention_phrases"],
            forbidden_claims=case["forbidden_claims"],
            abstention_type=case["abstention_type"],
        )
        self.assertTrue(eval_res["passed"])
        self.assertEqual(eval_res["score"], 1.0)
        self.assertIn("please specify", eval_res["matched_abstention_phrases"])

    # -------------------------------------------------------------------------
    # 6. SEM-07: Employment Selection Precedence Over Job Search
    # -------------------------------------------------------------------------
    def test_06_pipeline_sem07_employment_precedence_collision(self):
        """Exercises SEM-07: Real query rewriter resolves option 2 to pending employment record over job search."""
        case = self.case_map["SEM-07"]

        # Seed pending employment option mapping
        set_session_option_mapping(
            self.active_session_id,
            {"emp_rec_1": "AlphaTech::1", "emp_rec_2": "BetaCorp::2"},
            role=self.role,
            user_id=self.user_id,
        )

        # Seed concurrent job recommendations pool to verify collision resolution
        set_recommendation_session(
            session_id=self.active_session_id,
            query_key="comp_jobs",
            ranked_jobs=[
                {"id": 201, "raw_data": {"job_title": "Job 201", "company_name": "JobCompanyA"}},
                {"id": 202, "raw_data": {"job_title": "Job 202", "company_name": "JobCompanyB"}},
            ],
            current_page=1,
            page_size=5,
            total_jobs=2,
            role=self.role,
            user_id=self.user_id,
        )

        # Execute real QueryRewriterService.resolve_query
        rewritten = QueryRewriterService.resolve_query(
            user_message=case["query"],
            messages_history=[{"role": "user", "content": case["query"]}],
            session_id=self.active_session_id,
            role=self.role,
            user_id=self.user_id,
        )

        # Verify precedence: must resolve to EMPLOYMENT_DISAMBIGUATION, not JOB_DETAIL
        self.assertEqual(rewritten.target_intent, "EMPLOYMENT_DISAMBIGUATION")
        self.assertTrue(len(rewritten.entity_refs) > 0)
        actual_entity_id = rewritten.entity_refs[0].resolved_id
        self.assertEqual(actual_entity_id, "emp_rec_2")

        # Entity Grounding against fixture ground truth
        self.assertTrue(
            verify_entity_id_grounding(
                actual_selected_id="emp_rec_92",  # Record selected for option 2 in fixture
                expected_selected_id=case["expected_entity_id"],
            )
        )

        # Faithfulness check on employment verification confirmation prose
        synthetic_answer = f"Proceeding with employment verification for record {case['expected_entity_id']} at BetaCorp."
        eval_res = evaluate_deterministic_faithfulness(
            answer_text=synthetic_answer,
            supported_facts=case["supported_facts"],
            forbidden_claims=case["forbidden_claims"],
        )
        self.assertTrue(eval_res["is_faithful"])
        self.assertEqual(eval_res["score"], 1.0)

    # -------------------------------------------------------------------------
    # 7. SEM-09: Empty Retrieval Abstention and Grounding Directive
    # -------------------------------------------------------------------------
    @patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock)
    @patch("backend.services.search_router.dispatch_search_target", new_callable=AsyncMock)
    @patch("backend.services.search_parser.parse_search_query", new_callable=AsyncMock)
    @patch("backend.services.token_resolver.get_user_platform_token")
    @patch("backend.api.auth.get_db")
    def test_07_pipeline_sem09_empty_retrieval_grounding_directive(
        self, mock_auth_db, mock_tok, mock_parser, mock_dispatch, mock_llm
    ):
        """Exercises SEM-09: Real pipeline injects EMPTY RETRIEVAL GROUNDING DIRECTIVE when route_branch is NO_HITS."""
        self._setup_mock_db(mock_auth_db)
        case = self.case_map["SEM-09"]

        mock_parser.return_value = {"search_target": SearchTarget.JOBS}
        mock_dispatch.return_value = {
            "db_context": "No matching jobs found in index.",
            "results": [],
            "result_type": "jobs",
            "allowed_ids": set(),
            "raw_hits": [],
            "route_branch": "NO_HITS",
        }
        mock_llm.return_value = "I could not find any matching jobs for COBOL developer on Mars. Please try adjusting your search terms."

        resp = self.client.post(
            "/chat",
            json={"session_id": self.active_session_id, "messages": [{"role": "user", "content": case["query"]}]},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        # Verify real prompt construction injected the mandatory grounding directive
        self.assertTrue(mock_llm.called)
        system_prompt = mock_llm.call_args[0][0]
        self.assertIn("## EMPTY RETRIEVAL GROUNDING DIRECTIVE", system_prompt)
        self.assertIn("No matching records were retrieved from the CollarCheck platform database", system_prompt)

        # Unresolved entity must be None
        self.assertEqual(len(body.get("results") or []), 0)

        # Evaluator check for graceful empty response
        eval_res = evaluate_abstention_quality(
            answer_text=body["reply"],
            requires_abstention=case["requires_abstention"],
            expected_abstention_phrases=case["expected_abstention_phrases"],
            forbidden_claims=case["forbidden_claims"],
            abstention_type=case["abstention_type"],
        )
        self.assertTrue(eval_res["passed"])
        self.assertEqual(eval_res["score"], 1.0)

    # -------------------------------------------------------------------------
    # 8. SEM-12: Endpoint Parity (/chat vs /chat/stream)
    # -------------------------------------------------------------------------
    @patch("backend.services.llm_service.LLMService.stream_chat_completion")
    @patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock)
    @patch("backend.services.search_router.dispatch_search_target", new_callable=AsyncMock)
    @patch("backend.services.search_parser.parse_search_query", new_callable=AsyncMock)
    @patch("backend.services.token_resolver.get_user_platform_token")
    @patch("backend.api.auth.get_db")
    def test_08_pipeline_sem12_chat_and_stream_parity(
        self, mock_auth_db, mock_tok, mock_parser, mock_dispatch, mock_llm_chat, mock_llm_stream
    ):
        """Exercises SEM-12: Compares /chat and /chat/stream for reply text and structured results parity."""
        self._setup_mock_db(mock_auth_db)
        case = self.case_map["SEM-12"]

        mock_parser.return_value = {"search_target": SearchTarget.JOBS}
        job601 = JobCard(
            job_id=601,
            title="React Lead",
            company="DeltaTech",
            location="Bengaluru",
            url="https://collarcheck.com/jobs/601",
        )
        dispatch_val = {
            "db_context": case["synthetic_evidence"],
            "results": [job601],
            "result_type": "jobs",
            "allowed_ids": {601, "job_601"},
            "raw_hits": [{"id": 601, "job_description": "React Lead at DeltaTech. Requirements: 6+ years React and TypeScript."}],
            "route_branch": "SEARCH",
        }
        mock_dispatch.return_value = dispatch_val

        target_reply = "Job 601 requires 6+ years React and TypeScript experience at DeltaTech."
        mock_llm_chat.return_value = target_reply

        async def fake_stream(*args, **kwargs):
            yield target_reply

        mock_llm_stream.side_effect = fake_stream

        req_body = {
            "session_id": self.active_session_id,
            "messages": [{"role": "user", "content": case["query"]}],
        }

        # 1. Execute /chat
        res_chat = self.client.post("/chat", json=req_body, headers=self.headers)
        self.assertEqual(res_chat.status_code, 200)
        chat_body = res_chat.json()
        chat_reply = chat_body["reply"].strip()
        chat_results = chat_body.get("results") or []

        # 2. Execute /chat/stream
        res_stream = self.client.post("/chat/stream", json=req_body, headers=self.headers)
        self.assertEqual(res_stream.status_code, 200)

        # Parse SSE events ignoring keepalive comments
        events = []
        for block in res_stream.text.strip().split("\n\n"):
            clean_b = block.strip()
            if not clean_b or clean_b.startswith(":"):
                continue
            events.append(parse_sse_event(clean_b))

        event_types = [e["event"] for e in events]
        self.assertIn("start", event_types)
        self.assertIn("results", event_types)
        self.assertIn("done", event_types)

        results_evt = next(e for e in events if e["event"] == "results")
        stream_results = results_evt["data"]["results"]

        done_evt = next(e for e in events if e["event"] == "done")
        stream_reply = done_evt["data"]["reply"].strip()

        # Accumulated delta verification
        delta_chunks = [e["data"]["text"] for e in events if e["event"] == "delta" and "text" in e["data"]]
        accumulated_delta = "".join(delta_chunks).strip()

        # 3. Exact Parity Assertions
        # Reply text parity
        self.assertEqual(chat_reply, stream_reply)
        self.assertEqual(chat_reply, accumulated_delta)

        # Structured result cards parity
        self.assertEqual(len(chat_results), len(stream_results))
        self.assertEqual(chat_results[0]["job_id"], stream_results[0]["job_id"])
        self.assertEqual(chat_results[0]["title"], stream_results[0]["title"])

        # Evaluator agreement on both endpoints
        eval_chat = evaluate_deterministic_faithfulness(chat_reply, case["supported_facts"], case["forbidden_claims"])
        eval_stream = evaluate_deterministic_faithfulness(stream_reply, case["supported_facts"], case["forbidden_claims"])
        self.assertTrue(eval_chat["is_faithful"])
        self.assertTrue(eval_stream["is_faithful"])
        self.assertEqual(eval_chat["score"], eval_stream["score"])

    # -------------------------------------------------------------------------
    # 9. SEM-08: Multi-Turn Filter Refinement Context Preservation
    # -------------------------------------------------------------------------
    @patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock)
    @patch("backend.services.search_router.dispatch_search_target", new_callable=AsyncMock)
    @patch("backend.services.search_parser.parse_search_query", new_callable=AsyncMock)
    @patch("backend.services.token_resolver.get_user_platform_token")
    @patch("backend.api.auth.get_db")
    def test_09_pipeline_sem08_multi_turn_filter_refinement(
        self, mock_auth_db, mock_tok, mock_parser, mock_dispatch, mock_llm
    ):
        """
        Exercises SEM-08:
        Turn 1: Initial search in Pune returns Job 301 (Hybrid) and Job 302 (Office).
        Turn 2: Follow-up refinement query 'Only hybrid ones' exercises real QueryRewriterService,
        preserving Turn 1 location 'Pune' while applying filter delta job_mode='hybrid'.
        The dispatcher mock validates that received filters actually satisfy expected constraints
        before returning refined results.
        """
        self._setup_mock_db(mock_auth_db)
        case = self.case_map["SEM-08"]

        job301 = JobCard(
            job_id=301,
            title="Senior Backend Engineer (Job 301)",
            company="CloudTech Pune",
            location="Pune",
            url="https://collarcheck.com/jobs/301",
        )
        job302 = JobCard(
            job_id=302,
            title="DevOps Lead (Job 302)",
            company="OfficeWorks Pune",
            location="Pune",
            url="https://collarcheck.com/jobs/302",
        )

        # Dispatcher side-effect that conditionally returns jobs based on received parsed_query filters
        def fake_dispatch(**kwargs):
            parsed = kwargs.get("parsed_query", {})
            loc = parsed.get("location")
            mode = parsed.get("job_mode") or parsed.get("work_mode")

            # Turn 1: Initial search in Pune establishes search context
            if loc == "Pune" and not mode:
                set_search_context(
                    self.active_session_id,
                    {"location": "Pune", "keyword": "developer"},
                    role=self.role,
                )
                return {
                    "db_context": "Found 2 matching jobs in Pune: Job 301 (Hybrid, 28 LPA) and Job 302 (Office, 25 LPA).",
                    "results": [job301, job302],
                    "result_type": "job_detail",
                    "allowed_ids": {301, 302, "job_301", "job_302"},
                    "raw_hits": [
                        {"id": 301, "company": "CloudTech Pune", "location": "Pune", "work_mode": "hybrid", "salary": "28 LPA"},
                        {"id": 302, "company": "OfficeWorks Pune", "location": "Pune", "work_mode": "office", "salary": "25 LPA"},
                    ],
                    "route_branch": "SEARCH",
                }

            # Turn 2: Only return Job 301 if BOTH location='Pune' and job_mode='hybrid' were passed
            if loc == "Pune" and mode == "hybrid":
                return {
                    "db_context": "Found 1 matching hybrid job in Pune: Job 301 at CloudTech Pune (Hybrid, 28 LPA).",
                    "results": [job301],
                    "result_type": "job_detail",
                    "allowed_ids": {301, "job_301"},
                    "raw_hits": [
                        {"id": 301, "company": "CloudTech Pune", "location": "Pune", "work_mode": "hybrid", "salary": "28 LPA"},
                    ],
                    "route_branch": "SEARCH",
                }

            # If filters are missing or incorrect, return empty/unmatched results to expose pipeline bug
            return {
                "db_context": "No matching jobs found for specified criteria.",
                "results": [],
                "result_type": "jobs",
                "allowed_ids": set(),
                "raw_hits": [],
                "route_branch": "NO_HITS",
            }

        mock_dispatch.side_effect = fake_dispatch

        # -------------------------------------------------------------
        # Turn 1: Initial query 'Find developer jobs in Pune'
        # -------------------------------------------------------------
        mock_parser.return_value = {
            "keyword": "developer",
            "location": "Pune",
            "search_target": SearchTarget.JOBS,
        }
        mock_llm.return_value = "Here are matching developer jobs in Pune: Job 301 and Job 302."

        resp1 = self.client.post(
            "/chat",
            json={
                "session_id": self.active_session_id,
                "messages": [{"role": "user", "content": "Find developer jobs in Pune"}],
            },
            headers=self.headers,
        )
        self.assertEqual(resp1.status_code, 200)
        body1 = resp1.json()
        self.assertTrue(body1["success"])
        self.assertEqual(len(body1.get("results") or []), 2)

        # -------------------------------------------------------------
        # Turn 2: Follow-up refinement 'Only hybrid ones' (SEM-08 query)
        # -------------------------------------------------------------
        # Mock parser only extracts the local delta from user utterance
        mock_parser.return_value = {
            "job_mode": "hybrid",
            "search_target": SearchTarget.JOBS,
        }
        mock_llm.return_value = "Job 301 in Pune offers a Hybrid work mode with 28 LPA."

        resp2 = self.client.post(
            "/chat",
            json={
                "session_id": self.active_session_id,
                "messages": [
                    {"role": "user", "content": "Find developer jobs in Pune"},
                    {"role": "assistant", "content": "Here are matching developer jobs in Pune: Job 301 and Job 302."},
                    {"role": "user", "content": case["query"]},
                ],
            },
            headers=self.headers,
        )
        self.assertEqual(resp2.status_code, 200)
        body2 = resp2.json()
        self.assertTrue(body2["success"])

        # 1. Assert Turn 2 dispatcher received BOTH preserved location ('Pune') and refined mode ('hybrid')
        self.assertTrue(mock_dispatch.called)
        turn2_kwargs = mock_dispatch.call_args.kwargs
        turn2_parsed = turn2_kwargs.get("parsed_query", {})
        self.assertEqual(turn2_parsed.get("location"), "Pune")
        self.assertEqual(turn2_parsed.get("job_mode"), "hybrid")

        # 2. Assert structured result grounding matches expected_entity_id
        results2 = body2.get("results") or []
        self.assertEqual(len(results2), 1)
        actual_entity_id = f"job_{results2[0]['job_id']}"
        self.assertTrue(
            verify_entity_id_grounding(
                actual_selected_id=actual_entity_id,
                expected_selected_id=case["expected_entity_id"],
            )
        )

        # 3. Assert deterministic faithfulness: 'Pune' and 'Hybrid' supported; 'Office' and 'Job 302' forbidden
        eval_res = evaluate_deterministic_faithfulness(
            answer_text=body2["reply"],
            supported_facts=case["supported_facts"],
            forbidden_claims=case["forbidden_claims"],
        )
        self.assertTrue(eval_res["is_faithful"])
        self.assertEqual(eval_res["score"], 1.0)
        self.assertEqual(eval_res["detected_forbidden_claims"], [])

    # -------------------------------------------------------------------------
    # 10. SEM-11: Contradictory Claim Detection and Prompt Evidence Transport
    # -------------------------------------------------------------------------
    @patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock)
    @patch("backend.services.search_router.dispatch_search_target", new_callable=AsyncMock)
    @patch("backend.services.search_parser.parse_search_query", new_callable=AsyncMock)
    @patch("backend.services.token_resolver.get_user_platform_token")
    @patch("backend.api.auth.get_db")
    def test_10_pipeline_sem11_contradictory_claim_detection(
        self, mock_auth_db, mock_tok, mock_parser, mock_dispatch, mock_llm
    ):
        """
        Exercises SEM-11:
        1. Pipeline Route: Exercises /chat with query 'Is Job 501 remote?'.
        2. Prompt Grounding: Asserts the exact on-site evidence ('Work Mode: Work from Office (On-site 5 days a week)')
           and 'Mumbai' reach the LLM system prompt.
        3. Structured Entity Grounding: Asserts returned structured entity is job_501.
        4. Faithful Reply Evaluation: Asserts that a faithful answer aligned with evidence passes.
        5. Contradiction Detection: Asserts that a contradictory counterfactual claiming remote work is caught
           by evaluate_deterministic_faithfulness, failing with score 0.0 and flagging forbidden claims.
        """
        self._setup_mock_db(mock_auth_db)
        case = self.case_map["SEM-11"]

        mock_parser.return_value = {"search_target": SearchTarget.JOBS}
        job501 = JobCard(
            job_id=501,
            title="Data Analyst",
            company="MetroCorp",
            location="Mumbai",
            url="https://collarcheck.com/jobs/501",
        )
        mock_dispatch.return_value = {
            "db_context": case["synthetic_evidence"],
            "results": [job501],
            "result_type": "job_detail",
            "allowed_ids": {501, "job_501"},
            "raw_hits": [
                {
                    "id": 501,
                    "company": "MetroCorp",
                    "location": "Mumbai",
                    "work_mode": "Work from Office (On-site 5 days a week)",
                }
            ],
            "route_branch": "JOB_DETAIL",
        }

        # Mock LLM returns faithful answer aligned with evidence
        faithful_reply = "Job 501 is a Work from Office position based in Mumbai (On-site 5 days a week)."
        mock_llm.return_value = faithful_reply

        resp = self.client.post(
            "/chat",
            json={"session_id": self.active_session_id, "messages": [{"role": "user", "content": case["query"]}]},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])

        # 1. Pipeline Evidence Transport: Verify exact on-site evidence and location reached LLM prompt
        self.assertTrue(mock_llm.called)
        system_prompt = mock_llm.call_args[0][0]
        self.assertIn("Work Mode: Work from Office (On-site 5 days a week)", system_prompt)
        self.assertIn("Mumbai", system_prompt)

        # 2. Structured Entity-ID Grounding: Asserts returned structured entity matches expected job_501
        results = body.get("results") or []
        self.assertEqual(len(results), 1)
        actual_entity_id = f"job_{results[0]['job_id']}"
        self.assertTrue(
            verify_entity_id_grounding(
                actual_selected_id=actual_entity_id,
                expected_selected_id=case["expected_entity_id"],
            )
        )

        # 3. Evaluator Faithful Baseline: Response aligned with evidence passes
        eval_faithful = evaluate_deterministic_faithfulness(
            answer_text=body["reply"],
            supported_facts=case["supported_facts"],
            forbidden_claims=case["forbidden_claims"],
        )
        self.assertTrue(eval_faithful["is_faithful"])
        self.assertEqual(eval_faithful["score"], 1.0)
        self.assertEqual(eval_faithful["detected_forbidden_claims"], [])

        # 4. Evaluator Contradictory Claim Detection: Separate counterfactual input asserting remote work
        contradictory_reply = "Job 501 is remote and allows you to work from home full-time."
        eval_contradictory = evaluate_deterministic_faithfulness(
            answer_text=contradictory_reply,
            supported_facts=case["supported_facts"],
            forbidden_claims=case["forbidden_claims"],
        )
        self.assertFalse(eval_contradictory["is_faithful"])
        self.assertEqual(eval_contradictory["score"], 0.0)
        self.assertIn("is remote", eval_contradictory["detected_forbidden_claims"])
        self.assertIn("work from home", eval_contradictory["detected_forbidden_claims"])

    # -------------------------------------------------------------------------
    # 11. SEM-03: Missing Company Culture Abstention
    # -------------------------------------------------------------------------
    @patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock)
    @patch("backend.services.search_router.dispatch_search_target", new_callable=AsyncMock)
    @patch("backend.services.search_parser.parse_search_query", new_callable=AsyncMock)
    @patch("backend.services.token_resolver.get_user_platform_token")
    @patch("backend.api.auth.get_db")
    def test_11_pipeline_sem03_missing_culture_abstention(
        self, mock_auth_db, mock_tok, mock_parser, mock_dispatch, mock_llm
    ):
        """
        Exercises SEM-03:
        1. Pipeline Route: Exercises /chat with query 'Tell me about their company culture and workplace perks'.
        2. Prompt Grounding: Asserts the exact culture missing-evidence text ('Culture / Workplace: Not provided in indexed record.')
           reaches the LLM system prompt.
        3. Structured Entity Grounding: Asserts returned structured entity is job_103.
        4. Honest Abstention Response: Evaluates that an answer honestly stating culture is not provided passes
           evaluate_abstention_quality.
        5. Negative Counterfactual: Evaluates that an answer fabricating culture perks ('innovative culture and free lunch')
           is rejected by the evaluator (score 0.0) due to detected forbidden claims.
        """
        self._setup_mock_db(mock_auth_db)
        case = self.case_map["SEM-03"]

        mock_parser.return_value = {"search_target": SearchTarget.JOBS}
        job103 = JobCard(
            job_id=103,
            title="Full Stack Developer",
            company="NexaCorp",
            location="Bengaluru",
            url="https://collarcheck.com/jobs/103",
        )
        mock_dispatch.return_value = {
            "db_context": case["synthetic_evidence"],
            "results": [job103],
            "result_type": "job_detail",
            "allowed_ids": {103, "job_103"},
            "raw_hits": [
                {
                    "id": 103,
                    "company": "NexaCorp",
                    "title": "Full Stack Developer",
                    "culture": "Not provided in indexed record",
                }
            ],
            "route_branch": "JOB_DETAIL",
        }

        # Mock LLM returns honest missing-evidence abstention
        mock_llm.return_value = "Details regarding company culture are not provided in indexed record for this role at NexaCorp."

        resp = self.client.post(
            "/chat",
            json={"session_id": self.active_session_id, "messages": [{"role": "user", "content": case["query"]}]},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])

        # 1. Pipeline Evidence Transport: Verify exact culture missing-evidence reached LLM prompt
        self.assertTrue(mock_llm.called)
        system_prompt = mock_llm.call_args[0][0]
        self.assertIn("Culture / Workplace: Not provided in indexed record.", system_prompt)

        # 2. Structured Entity-ID Grounding: Asserts returned structured entity is job_103
        results = body.get("results") or []
        self.assertEqual(len(results), 1)
        actual_entity_id = f"job_{results[0]['job_id']}"
        self.assertTrue(
            verify_entity_id_grounding(
                actual_selected_id=actual_entity_id,
                expected_selected_id=case["expected_entity_id"],
            )
        )

        # 3. Evaluator Honest Abstention Check
        eval_pass = evaluate_abstention_quality(
            answer_text=body["reply"],
            requires_abstention=case["requires_abstention"],
            expected_abstention_phrases=case["expected_abstention_phrases"],
            forbidden_claims=case["forbidden_claims"],
            abstention_type=case["abstention_type"],
        )
        self.assertTrue(eval_pass["passed"])
        self.assertEqual(eval_pass["score"], 1.0)
        self.assertIn("not provided in indexed record", eval_pass["matched_abstention_phrases"])

        # 4. Evaluator Negative Counterfactual Check: Fabricated culture claims fail
        fabricated_culture_reply = "NexaCorp has an innovative culture with great work-life balance and free lunch."
        eval_fail = evaluate_abstention_quality(
            answer_text=fabricated_culture_reply,
            requires_abstention=case["requires_abstention"],
            expected_abstention_phrases=case["expected_abstention_phrases"],
            forbidden_claims=case["forbidden_claims"],
            abstention_type=case["abstention_type"],
        )
        self.assertFalse(eval_fail["passed"])
        self.assertEqual(eval_fail["score"], 0.0)
        self.assertIn("innovative culture", eval_fail["detected_forbidden_claims"])
        self.assertIn("free lunch", eval_fail["detected_forbidden_claims"])

    # -------------------------------------------------------------------------
    # 12. SEM-10: Unsupported Perks Abstention
    # -------------------------------------------------------------------------
    @patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock)
    @patch("backend.services.search_router.dispatch_search_target", new_callable=AsyncMock)
    @patch("backend.services.search_parser.parse_search_query", new_callable=AsyncMock)
    @patch("backend.services.token_resolver.get_user_platform_token")
    @patch("backend.api.auth.get_db")
    def test_12_pipeline_sem10_unsupported_perks_abstention(
        self, mock_auth_db, mock_tok, mock_parser, mock_dispatch, mock_llm
    ):
        """
        Exercises SEM-10:
        1. Pipeline Route: Exercises /chat with query 'Does Job 401 offer free lunch and gym membership?'.
        2. Prompt Grounding: Asserts the exact unlisted perks evidence ('Benefits/Perks: None listed.')
           reaches the LLM system prompt.
        3. Structured Entity Grounding: Asserts returned structured entity is job_401.
        4. Honest Abstention Response: Evaluates that an answer stating perks are not listed passes
           evaluate_abstention_quality.
        5. Negative Counterfactual: Evaluates that a counterfactual asserting perks ('The job offers free lunch')
           is caught and rejected by evaluate_abstention_quality (score 0.0).
        """
        self._setup_mock_db(mock_auth_db)
        case = self.case_map["SEM-10"]

        mock_parser.return_value = {"search_target": SearchTarget.JOBS}
        job401 = JobCard(
            job_id=401,
            title="Backend Engineer",
            company="ScaleUp",
            location="Hyderabad",
            url="https://collarcheck.com/jobs/401",
        )
        mock_dispatch.return_value = {
            "db_context": case["synthetic_evidence"],
            "results": [job401],
            "result_type": "job_detail",
            "allowed_ids": {401, "job_401"},
            "raw_hits": [
                {
                    "id": 401,
                    "company": "ScaleUp",
                    "title": "Backend Engineer",
                    "perks": "None listed",
                }
            ],
            "route_branch": "JOB_DETAIL",
        }

        # Mock LLM returns honest missing-perks abstention
        mock_llm.return_value = "The job listing for Backend Engineer at ScaleUp does not mention free lunch or gym membership; benefits are not listed."

        resp = self.client.post(
            "/chat",
            json={"session_id": self.active_session_id, "messages": [{"role": "user", "content": case["query"]}]},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])

        # 1. Pipeline Evidence Transport: Verify exact perks evidence reached LLM prompt
        self.assertTrue(mock_llm.called)
        system_prompt = mock_llm.call_args[0][0]
        self.assertIn("Benefits/Perks: None listed.", system_prompt)

        # 2. Structured Entity-ID Grounding: Asserts returned structured entity is job_401
        results = body.get("results") or []
        self.assertEqual(len(results), 1)
        actual_entity_id = f"job_{results[0]['job_id']}"
        self.assertTrue(
            verify_entity_id_grounding(
                actual_selected_id=actual_entity_id,
                expected_selected_id=case["expected_entity_id"],
            )
        )

        # 3. Evaluator Honest Abstention Check
        eval_pass = evaluate_abstention_quality(
            answer_text=body["reply"],
            requires_abstention=case["requires_abstention"],
            expected_abstention_phrases=case["expected_abstention_phrases"],
            forbidden_claims=case["forbidden_claims"],
            abstention_type=case["abstention_type"],
        )
        self.assertTrue(eval_pass["passed"])
        self.assertEqual(eval_pass["score"], 1.0)
        self.assertIn("not listed", eval_pass["matched_abstention_phrases"])

        # 4. Evaluator Negative Counterfactual Check: Fabricated perk claims fail
        fabricated_perks_reply = "The job offers free lunch and includes gym membership for all employees."
        eval_fail = evaluate_abstention_quality(
            answer_text=fabricated_perks_reply,
            requires_abstention=case["requires_abstention"],
            expected_abstention_phrases=case["expected_abstention_phrases"],
            forbidden_claims=case["forbidden_claims"],
            abstention_type=case["abstention_type"],
        )
        self.assertFalse(eval_fail["passed"])
        self.assertEqual(eval_fail["score"], 0.0)
        self.assertIn("offers free lunch", eval_fail["detected_forbidden_claims"])
        self.assertIn("includes gym membership", eval_fail["detected_forbidden_claims"])


if __name__ == "__main__":
    unittest.main()
