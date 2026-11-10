"""
test_async_intent_parser.py — Targeted test suite verifying native async user-data intent parsing.
Ensures parse_user_data_intents_async executes inside an active asyncio event loop
without nest_asyncio and without raising RuntimeError: This event loop is already running.
"""

import pytest
from unittest.mock import patch, AsyncMock
from backend.services.user_data_intent_parser import (
    UserIntent,
    parse_user_data_intents_async,
    parse_user_data_intents_llm,
    parse_user_data_intents,
)


@pytest.mark.anyio
async def test_async_intent_parser_with_running_loop():
    """Verify parse_user_data_intents_llm runs inside active event loop cleanly."""
    with patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = '["PROFILE_SUMMARY"]'
        
        intents = await parse_user_data_intents_llm("Please provide a comprehensive summary overview of my account profile")
        
        assert UserIntent.PROFILE_SUMMARY in intents
        mock_llm.assert_awaited_once()


@pytest.mark.anyio
async def test_async_intent_parser_llm_failure_fallback():
    """Verify deterministic fallback works when LLM throws an exception inside active event loop."""
    with patch("backend.services.llm_service.LLMService.get_chat_completion", side_effect=Exception("LLM Timeout")):
        intents = await parse_user_data_intents_async("Show my employment history")
        
        # Deterministic parser should catch this query instantly
        assert UserIntent.EMPLOYMENT_HISTORY in intents


def test_sync_intent_parser_deterministic_fast_path():
    """Verify synchronous interface continues working for deterministic rules."""
    intents = parse_user_data_intents("What is my salary?")
    assert UserIntent.EMPLOYMENT_SALARY in intents


# ==============================================================================
# Phase 1 Step 2 Regression Tests
# ==============================================================================

@pytest.mark.anyio
async def test_async_conversational_intent_reaches_llm():
    """Requirement A: Verify that non-deterministic query invokes LLM classifier and parses intent."""
    with patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = '["EMPLOYMENT_SALARY"]'
        intents = await parse_user_data_intents_async("How much salary did I report?")
        assert UserIntent.EMPLOYMENT_SALARY in intents
        mock_llm.assert_awaited_once()


@pytest.mark.anyio
async def test_deterministic_fast_path_skips_llm():
    """Requirement B: Verify deterministic query is resolved without invoking LLM."""
    with patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock) as mock_llm:
        intents = await parse_user_data_intents_async("Show me my employment history.")
        assert UserIntent.EMPLOYMENT_HISTORY in intents
        mock_llm.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("query", [
    "Python developer jobs in Delhi",
    "jobs according to my skills",
    "Show me Python developer jobs in Delhi.",
])
async def test_job_search_queries_do_not_produce_user_data_intents(query: str):
    """Requirement C: Job-search queries are not captured as user-data intents."""
    with patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock) as mock_llm:
        # LLM system prompt instructions say return [] for job searches
        mock_llm.return_value = '[]'
        intents = await parse_user_data_intents_async(query)
        assert len(intents) == 0


@pytest.mark.anyio
async def test_llm_failure_graceful_fallback():
    """Requirement D: When LLM raises exception, parser safely returns empty list without crashing."""
    with patch("backend.services.llm_service.LLMService.get_chat_completion", side_effect=Exception("API Unreachable")):
        intents = await parse_user_data_intents_async("A completely unstructured profile question")
        assert intents == []


@pytest.mark.anyio
async def test_section_5_candidate_queries():
    """Section 5: Verify exact behavior of candidate queries under async parser."""
    # 1. Deterministic profile skills query
    skills_profile = await parse_user_data_intents_async("What skills are listed on my profile?")
    assert UserIntent.PROFILE_SKILLS in skills_profile

    # 2. Query needing LLM classification: "How much salary did I report?"
    with patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = '["EMPLOYMENT_SALARY"]'
        salary_intents = await parse_user_data_intents_async("How much salary did I report?")
        assert UserIntent.EMPLOYMENT_SALARY in salary_intents
        mock_llm.assert_awaited_once()

    # 3. Query: "What skills are listed under my current job?"
    # Confirmed fix: Accurately classifies as EMPLOYMENT_SKILLS
    job_skills_deterministic = parse_user_data_intents("What skills are listed under my current job?")
    assert UserIntent.EMPLOYMENT_SKILLS in job_skills_deterministic
    assert UserIntent.PROFILE_SKILLS not in job_skills_deterministic


# ==============================================================================
# Phase 1 Step 3 Regression Tests: Employment Skills Discrimination
# ==============================================================================

@pytest.mark.parametrize("query,expected_intent,disallowed_intent", [
    # 1. "What skills are listed under my current job?" -> EMPLOYMENT_SKILLS
    ("What skills are listed under my current job?", UserIntent.EMPLOYMENT_SKILLS, UserIntent.PROFILE_SKILLS),
    # 2. "What skills were used in my previous job?" -> EMPLOYMENT_SKILLS
    ("What skills were used in my previous job?", UserIntent.EMPLOYMENT_SKILLS, UserIntent.PROFILE_SKILLS),
    # 3. "What skills are listed on my profile?" -> PROFILE_SKILLS
    ("What skills are listed on my profile?", UserIntent.PROFILE_SKILLS, UserIntent.EMPLOYMENT_SKILLS),
    # 4. "Show me my profile skills" -> PROFILE_SKILLS
    ("Show me my profile skills", UserIntent.PROFILE_SKILLS, UserIntent.EMPLOYMENT_SKILLS),
])
def test_employment_and_profile_skills_intent_discrimination(query: str, expected_intent: UserIntent, disallowed_intent: UserIntent):
    """Step 3: Verify accurate intent discrimination between employment skills and profile skills."""
    intents = parse_user_data_intents(query)
    assert expected_intent in intents, f"Query '{query}' expected {expected_intent}, got {intents}"
    assert disallowed_intent not in intents, f"Query '{query}' unexpectedly matched {disallowed_intent}"


@pytest.mark.parametrize("job_query", [
    # 5. "Python developer jobs in Delhi" -> no user-data intent
    "Python developer jobs in Delhi",
    # 6. "Jobs according to my skills" -> no user-data intent
    "Jobs according to my skills",
    "jobs as per my skills",
])
def test_skills_job_search_queries_not_captured_as_user_data(job_query: str):
    """Step 3: Verify job-search queries mentioning skills preserve job-search routing."""
    intents = parse_user_data_intents(job_query)
    assert len(intents) == 0, f"Job search query '{job_query}' was captured by user-data intents: {intents}"
    assert UserIntent.PROFILE_SKILLS not in intents
    assert UserIntent.EMPLOYMENT_SKILLS not in intents


@pytest.mark.anyio
async def test_execute_chat_pipeline_awaits_async_user_data_parser():
    """Requirement E: Verify execute_chat_pipeline invokes parse_user_data_intents_async."""
    from unittest.mock import MagicMock
    from backend.main import execute_chat_pipeline
    from backend.schemas.schemas import ChatRequest, ChatMessage

    chat_req = ChatRequest(
        messages=[ChatMessage(role="user", content="How much salary did I report?")],
        session_id="test_async_repair_sess",
    )

    mock_req = MagicMock()
    mock_req.state = MagicMock()
    mock_req.state.db_user_id = 999
    mock_req.state.user_id = "999"
    mock_req.state.role = "employee"
    mock_req.state.email = "test@collarcheck.com"
    mock_req.headers = {}

    timing_mock = MagicMock()
    timing_mock.request_id = "req_test_123"

    mock_uds_response = MagicMock()
    mock_uds_response.handled = True
    mock_uds_response.reply = "Your reported salary is 12 LPA."
    mock_uds_response.results_payload = []
    mock_uds_response.result_type_payload = "salary"
    mock_uds_response.presentation_mode = None
    mock_uds_response.action_metadata = None
    mock_uds_response.selection_metadata = None
    mock_uds_response.requested_level = None

    with patch("backend.services.user_data_service.parse_user_data_intents_async", new_callable=AsyncMock) as mock_async_parser, \
         patch("backend.services.user_data_service.handle_query", new_callable=AsyncMock) as mock_handle_query:

        mock_async_parser.return_value = [UserIntent.EMPLOYMENT_SALARY]
        mock_handle_query.return_value = mock_uds_response

        res = await execute_chat_pipeline(
            request=chat_req,
            req=mock_req,
            _token_payload={"uid": 999, "role": "employee", "email": "test@collarcheck.com"},
            timing=timing_mock,
        )

        mock_async_parser.assert_awaited_once_with("How much salary did I report?")
        mock_handle_query.assert_awaited_once()
        assert res.is_terminal is True
        assert res.reply == "Your reported salary is 12 LPA."
        assert res.intent_hint == "user_data"
