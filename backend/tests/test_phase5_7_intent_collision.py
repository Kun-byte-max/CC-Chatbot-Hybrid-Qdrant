"""
test_phase5_7_intent_collision.py — Regression tests for Job Search vs. User Data Intent Collision.
Tests that profile-oriented job search requests (e.g., 'jobs as per my skills', 'jobs relevant to my skills')
are correctly routed to job search rather than intercepted by user data handling as PROFILE_SKILLS.
"""

import pytest
import asyncio
from unittest.mock import patch

from backend.services.user_data_intent_parser import (
    parse_user_data_intents,
    parse_user_data_intents_deterministic,
    UserIntent,
)
from backend.services.search_parser import (
    parse_search_query,
    normalize_search_target,
    PROFILE_TRIGGERS,
)


class TestUserDataJobSearchInterceptionRegression:
    """
    Sub-phase A: Ensure job-search queries with skill/profile connectives
    return an empty intent set and are NOT intercepted as PROFILE_SKILLS.
    """

    @pytest.mark.parametrize("query", [
        "i said jobs as per my skills",
        "show me jobs as per my skills",
        "show jobs relevant to my skills",
        "jobs suited for my profile",
        "jobs suitable for my experience",
        "find jobs based on my skills",
        "recommend jobs according to my profile",
        "jobs matching my experience",
        "find software analyst jobs according to my skills",
    ])
    def test_job_search_queries_not_intercepted_by_user_data(self, query: str):
        intents = parse_user_data_intents_deterministic(query)
        assert UserIntent.PROFILE_SKILLS not in intents, (
            f"Query '{query}' incorrectly matched PROFILE_SKILLS"
        )
        assert len(intents) == 0, (
            f"Query '{query}' returned unexpected user data intents: {intents}"
        )

        sync_intents = parse_user_data_intents(query)
        assert UserIntent.PROFILE_SKILLS not in sync_intents
        assert len(sync_intents) == 0


class TestSearchParserProfileJobTriggersRegression:
    """
    Sub-phase B: Ensure search_parser recognizes profile-based job queries and
    activates use_profile_skills / PROFILE skill_source with JOBS search_target.
    """

    @pytest.mark.parametrize("query", [
        "i said jobs as per my skills",
        "show me jobs as per my skills",
        "show jobs relevant to my skills",
        "jobs suited for my profile",
        "jobs as per my profile",
    ])
    def test_search_parser_activates_profile_skills(self, query: str):
        target = normalize_search_target(None, query, role="employee")
        assert target == "JOBS", f"Target should be JOBS for query: {query}"

        # Test deterministic trigger match
        import re
        has_trigger = any(re.search(p, query.lower()) for p in PROFILE_TRIGGERS)
        assert has_trigger is True, f"PROFILE_TRIGGERS should match query: {query}"


class TestPreserveExistingUserDataBehavior:
    """
    Sub-phase C: Ensure authentic user data, profile, and employment history queries
    continue to be properly recognized and routed to user data services.
    """

    def test_profile_skills_intent_preserved(self):
        intents_1 = parse_user_data_intents_deterministic("show me my skills")
        assert UserIntent.PROFILE_SKILLS in intents_1

        intents_2 = parse_user_data_intents_deterministic("what are my skills")
        assert UserIntent.PROFILE_SKILLS in intents_2

    def test_employment_skills_intent_preserved(self):
        intents = parse_user_data_intents_deterministic("show me my employment skills")
        assert (
            UserIntent.EMPLOYMENT_SKILLS in intents
            or UserIntent.PROFILE_SKILLS in intents
            or UserIntent.EMPLOYMENT_HISTORY in intents
        )

    def test_employment_history_intent_preserved(self):
        intents_1 = parse_user_data_intents_deterministic("what were my previous jobs")
        assert UserIntent.EMPLOYMENT_HISTORY in intents_1

        intents_2 = parse_user_data_intents_deterministic("show me my employment history")
        assert UserIntent.EMPLOYMENT_HISTORY in intents_2


class TestPreserveAdjacentRoutingBehavior:
    """
    Sub-phase D: Verify verification guidance and candidate-search queries
    remain unaffected by job-search connective updates.
    """

    def test_verification_guidance_unaffected(self):
        # Verification requests should not be classified as open job search
        intents = parse_user_data_intents_deterministic("how do I complete L1 verification for software analyst")
        # Should not falsely match job search exclusion
        assert UserIntent.PROFILE_SKILLS not in intents

    def test_candidate_search_query_unaffected(self):
        cand_query = "find candidates with nextjs as a skill"
        target = normalize_search_target(None, cand_query, role="employer")
        assert target in ("CANDIDATE_SEARCH", "PEOPLE_SEARCH")

        # Candidate queries shouldn't return user profile skills
        intents = parse_user_data_intents_deterministic(cand_query)
        assert len(intents) == 0

    def test_profile_job_search_with_specific_title(self):
        query = "find software analyst jobs according to my skills"
        target = normalize_search_target(None, query, role="employee")
        assert target == "JOBS"

        intents = parse_user_data_intents_deterministic(query)
        assert len(intents) == 0


class TestChatEndpointPipelineRouting:
    """
    Exercise the actual /chat request pipeline with mocked dependencies
    to confirm correct end-to-end routing without external network calls.
    """

    @pytest.fixture(autouse=True)
    def setup_client(self):
        from fastapi.testclient import TestClient
        from unittest.mock import AsyncMock, MagicMock
        from backend.main import app, verify_token

        def _override(request=None, credentials=None):
            if request is not None and hasattr(request, "state"):
                request.state.user_id = 1001
                request.state.db_user_id = 1001
                request.state.role = "employee"
                request.state.email = "test@example.com"
            return {"uid": 1001, "email": "test@example.com", "role": "employee"}

        app.dependency_overrides[verify_token] = _override
        self.client = TestClient(app)
        yield
        app.dependency_overrides.pop(verify_token, None)

    @pytest.mark.parametrize("query,expected_result_type", [
        ("show me my skills", "user_skills"),
        ("how do I complete L1 verification for software analyst", "verification_guidance"),
    ])
    def test_user_data_and_verification_queries_reach_user_data(self, query, expected_result_type):
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_uds_res = MagicMock()
        mock_uds_res.handled = True
        mock_uds_res.reply = f"Handled {query}"
        mock_uds_res.results_payload = [{"info": query}]
        mock_uds_res.result_type_payload = expected_result_type
        mock_uds_res.presentation_mode = "cards"
        mock_uds_res.action_metadata = None
        mock_uds_res.selection_metadata = None
        mock_uds_res.requested_level = None

        with patch("backend.services.user_data_service.handle_query", new=AsyncMock(return_value=mock_uds_res)) as mock_uds, \
             patch("backend.services.search_router.dispatch_search_target") as mock_dispatch:
            resp = self.client.post("/chat", json={
                "messages": [{"role": "user", "content": query}],
                "session_id": "test_sess_user_data",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert data["result_type"] == expected_result_type
            assert mock_uds.called is True
            assert mock_dispatch.called is False

    @pytest.mark.parametrize("query", [
        "i said jobs as per my skills",
        "show jobs relevant to my skills",
        "find software analyst jobs according to my skills",
    ])
    def test_job_search_queries_reach_job_search(self, query):
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_dispatch_res = {
            "db_context": "Found 1 matching job",
            "results": [{
                "job_id": 101,
                "title": "Software Analyst",
                "company": "Tech Corp",
                "url": "https://collarcheck.com/jobs/101",
            }],
            "result_type": "jobs",
            "allowed_ids": {101},
            "raw_hits": [],
            "pagination": None,
        }

        mock_parsed_query = {
            "keyword": "software analyst" if "analyst" in query else None,
            "search_target": "JOBS",
            "skill_source": "PROFILE",
            "use_profile_skills": True,
            "is_search_intent": True,
        }

        with patch("backend.services.user_data_service.handle_query") as mock_uds, \
             patch("backend.services.search_router.dispatch_search_target", new=AsyncMock(return_value=mock_dispatch_res)) as mock_dispatch, \
             patch("backend.services.search_parser.parse_search_query", new=AsyncMock(return_value=mock_parsed_query)), \
             patch("backend.services.profile_parser.parse_profile_update", new=AsyncMock(return_value={"is_profile_update_intent": False})), \
             patch("backend.services.llm_service.LLMService.get_chat_completion", new=AsyncMock(return_value="Top jobs matching your skills:")):
            resp = self.client.post("/chat", json={
                "messages": [{"role": "user", "content": query}],
                "session_id": "test_sess_job_search",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert data["result_type"] == "jobs"
            assert len(data.get("jobs", [])) == 1
            assert mock_uds.called is False
            assert mock_dispatch.called is True


class TestFollowUpSessionRouting:
    """
    Sub-phase 3: Exercise sequential multi-turn requests in the SAME session:
    Turn A: 'show me my skills' -> reaches user data
    Turn B: 'i said jobs as per my skills' -> reaches job search
    """

    def test_sequential_follow_up_in_same_session(self):
        from fastapi.testclient import TestClient
        from unittest.mock import AsyncMock, MagicMock, patch
        from backend.main import app, verify_token

        def _override(request=None, credentials=None):
            if request is not None and hasattr(request, "state"):
                request.state.user_id = 1001
                request.state.db_user_id = 1001
                request.state.role = "employee"
                request.state.email = "test@example.com"
            return {"uid": 1001, "email": "test@example.com", "role": "employee"}

        app.dependency_overrides[verify_token] = _override
        client = TestClient(app)
        session_id = "sess_followup_multi_turn_123"

        mock_uds_res = MagicMock()
        mock_uds_res.handled = True
        mock_uds_res.reply = "Your profile skills: Python, SQL"
        mock_uds_res.results_payload = [{"skill": "Python"}, {"skill": "SQL"}]
        mock_uds_res.result_type_payload = "user_skills"
        mock_uds_res.presentation_mode = "cards"
        mock_uds_res.action_metadata = None
        mock_uds_res.selection_metadata = None
        mock_uds_res.requested_level = None

        mock_dispatch_res = {
            "db_context": "Found 1 job as per your profile skills",
            "results": [{
                "job_id": 501,
                "title": "Python Backend Engineer",
                "company": "DataTech",
                "url": "https://collarcheck.com/jobs/501",
            }],
            "result_type": "jobs",
            "allowed_ids": {501},
            "raw_hits": [],
            "pagination": None,
        }

        mock_parsed_query = {
            "keyword": None,
            "search_target": "JOBS",
            "skill_source": "PROFILE",
            "use_profile_skills": True,
            "is_search_intent": True,
        }

        try:
            with patch("backend.services.user_data_service.handle_query", new=AsyncMock(return_value=mock_uds_res)) as mock_uds, \
                 patch("backend.services.search_router.dispatch_search_target", new=AsyncMock(return_value=mock_dispatch_res)) as mock_dispatch, \
                 patch("backend.services.search_parser.parse_search_query", new=AsyncMock(return_value=mock_parsed_query)) as mock_sp, \
                 patch("backend.services.profile_parser.parse_profile_update", new=AsyncMock(return_value={"is_profile_update_intent": False})), \
                 patch("backend.services.llm_service.LLMService.get_chat_completion", new=AsyncMock(return_value="Here are jobs as per your skills:")):

                # Turn A: "show me my skills"
                res_a = client.post("/chat", json={
                    "messages": [{"role": "user", "content": "show me my skills"}],
                    "session_id": session_id,
                })
                assert res_a.status_code == 200
                data_a = res_a.json()
                assert data_a["result_type"] == "user_skills"
                assert mock_uds.called is True
                assert mock_dispatch.called is False

                # Reset call trackers for Turn B
                mock_uds.reset_mock()
                mock_dispatch.reset_mock()

                # Turn B: "i said jobs as per my skills" (in the same session)
                res_b = client.post("/chat", json={
                    "messages": [
                        {"role": "user", "content": "show me my skills"},
                        {"role": "assistant", "content": data_a["reply"]},
                        {"role": "user", "content": "i said jobs as per my skills"},
                    ],
                    "session_id": session_id,
                })
                assert res_b.status_code == 200
                data_b = res_b.json()
                assert data_b["result_type"] == "jobs"
                assert len(data_b.get("jobs", [])) == 1
                assert data_b["jobs"][0]["job_id"] == 501
                # Crucial assertion: User-data was bypassed, search was dispatched!
                assert mock_uds.called is False
                assert mock_dispatch.called is True
                assert mock_sp.called is True
        finally:
            app.dependency_overrides.pop(verify_token, None)


class TestProfileAdviceRegression:
    """
    Phase 3: Targeted Regression Tests for Profile-Rating Advice Queries.
    Asserts:
    1. 'How can I improve my CollarCheck profile rating?' does not classify as PEOPLE_SEARCH.
    2. 'How do I improve my profile score?' does not classify as PEOPLE_SEARCH.
    3. 'How do I complete my profile?' does not trigger candidate retrieval.
    4. 'Find jobs matching my profile' retains job-search behavior.
    5. 'Show me jobs based on my profile' retains job-search behavior.
    6. 'Find candidates with Python skills' retains candidate-search behavior.
    7. 'Search profiles of candidates with React experience' retains candidate-search behavior.
    8. The reported informational query does not call PeopleSearchService.search_people().
    9. The informational response contains no unrelated candidate results or candidate playcard payload.
    10. Existing profile-update and employment-verification routing remains unchanged.
    """

    @pytest.mark.parametrize("query", [
        "How can I improve my CollarCheck profile rating?",
        "How do I improve my profile score?",
        "How can I increase my profile rating?",
        "How do I complete my profile?",
    ])
    def test_profile_advice_not_classified_as_people_search(self, query: str):
        target = normalize_search_target(None, query, role="employee")
        assert target != "PEOPLE_SEARCH", (
            f"Query '{query}' was incorrectly classified as PEOPLE_SEARCH in normalize_search_target"
        )
        assert target == "UNKNOWN"

    @pytest.mark.parametrize("query,expected_target", [
        ("Find jobs matching my profile", "JOBS"),
        ("Show me jobs based on my profile", "JOBS"),
        ("Recommend jobs according to my skills", "JOBS"),
        ("Find candidates with Python skills", "PEOPLE_SEARCH"),
        ("Search profiles of candidates with React experience", "PEOPLE_SEARCH"),
    ])
    def test_legitimate_searches_retain_target_behavior(self, query: str, expected_target: str):
        target = normalize_search_target(None, query, role="employee")
        assert target == expected_target, (
            f"Query '{query}' produced target '{target}', expected '{expected_target}'"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", [
        "How can I improve my CollarCheck profile rating?",
        "How do I improve my profile score?",
        "How do I complete my profile?",
    ])
    async def test_informational_queries_do_not_call_people_search_service(self, query: str):
        from unittest.mock import patch, MagicMock
        from backend.api.employee import handle_employee_context

        parsed = {
            "keyword": None,
            "search_target": normalize_search_target(None, query, role="employee"),
            "use_profile_skills": False,
            "skill_source": "NONE",
            "is_search_intent": False,
            "is_knowledge_intent": False,
        }

        with patch("backend.services.people_search_service.PeopleSearchService.search_people") as mock_search_people:
            res = await handle_employee_context(
                last_user_msg=query,
                messages=[{"role": "user", "content": query}],
                session_id="test_sess_advice_guard",
                parsed_query=parsed,
            )
            # PeopleSearchService.search_people must NOT be called
            assert mock_search_people.called is False, (
                f"PeopleSearchService.search_people was called for profile-advice query: {query}"
            )
            # Response must not contain candidate playcards
            if res:
                assert res.get("result_type") != "people"
                assert not res.get("candidates")

    def test_end_to_end_informational_response_has_no_candidate_results_or_playcard(self):
        from fastapi.testclient import TestClient
        from unittest.mock import AsyncMock, patch
        from backend.main import app, verify_token

        def _override(request=None, credentials=None):
            if request is not None and hasattr(request, "state"):
                request.state.user_id = 1001
                request.state.db_user_id = 1001
                request.state.role = "employee"
                request.state.email = "test@example.com"
            return {"uid": 1001, "email": "test@example.com", "role": "employee"}

        app.dependency_overrides[verify_token] = _override
        client = TestClient(app)

        query = "How can I improve my CollarCheck profile rating?"
        mock_parsed = {
            "keyword": None,
            "search_target": "UNKNOWN",
            "skill_source": "NONE",
            "use_profile_skills": False,
            "is_search_intent": False,
        }

        try:
            with patch("backend.services.search_parser.parse_search_query", new=AsyncMock(return_value=mock_parsed)), \
                 patch("backend.services.people_search_service.PeopleSearchService.search_people") as mock_search_people, \
                 patch("backend.services.profile_parser.parse_profile_update", new=AsyncMock(return_value={"is_profile_update_intent": False})), \
                 patch("backend.services.llm_service.LLMService.get_chat_completion", new=AsyncMock(return_value="To improve your CollarCheck profile rating, complete missing profile fields and add verified employment details.")):

                resp = client.post("/chat", json={
                    "messages": [{"role": "user", "content": query}],
                    "session_id": "test_sess_no_playcard",
                })

                assert resp.status_code == 200
                data = resp.json()
                assert data["success"] is True
                assert mock_search_people.called is False
                # Must contain NO candidate playcard results
                assert data.get("candidates") is None
                assert data.get("results") is None
                assert data.get("result_type") != "people"
                assert "improve your CollarCheck profile rating" in data["reply"]
        finally:
            app.dependency_overrides.pop(verify_token, None)

    def test_existing_profile_update_and_verification_routing_preserved(self):
        # 1. Profile update intent parser check
        from backend.services.user_data_intent_parser import parse_user_data_intents_deterministic, UserIntent
        intents_verif = parse_user_data_intents_deterministic("how do I complete L1 verification")
        assert (
            UserIntent.EMPLOYMENT_VERIFICATION in intents_verif
            or UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS in intents_verif
            or UserIntent.VERIFICATION_LEVEL_DEFINITION in intents_verif
        )

        # 2. Gates check
        from backend.utils.gates import should_run_profile_parse
        ran_update, _ = should_run_profile_parse("employee", "update my phone number to 9876543210")
        assert ran_update is True


