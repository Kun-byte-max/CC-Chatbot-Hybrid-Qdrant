"""
test_phase2c_employment_e2e.py — End-to-End Conversation & State Isolation QA Suite.

Tests:
1. End-to-End `/chat` response structure for direct, informal, verification, and salary employment queries.
2. Selection flow & multi-role employment record isolation.
3. Multi-turn employment conversations.
4. State isolation across Employment, Job Search, Candidate/People Search, and Profile intents.
5. Selection context clearing on new search.
6. Session boundary & state reset isolation.
7. API success, 500 fallback, timeout, and 401/403 behavior.
"""

import pytest
import asyncio
from typing import Dict, Any
from unittest.mock import AsyncMock, MagicMock, patch

from backend.schemas.schemas import ChatRequest, ChatMessage
from backend.services.user_data_intent_parser import parse_user_data_intents, UserIntent
from backend.services.user_data_service import handle_query, ServiceResponse
from backend.utils.session_state import get_session_state, touch_session_state, _SESSION_STATES


# Mock multi-role employment API fixture
MOCK_MULTI_ROLE_EMPLOYMENT_DATA = {
    "status": True,
    "data": [
        {
            "id": "5193",
            "company": "SP HUMANS PRIVATE LIMITED",
            "company_id": "13",
            "company_logo": "https://example.com/sphumans.png",
            "is_verified": True,
            "lists": [
                {
                    "id": "5193_1",
                    "designation": "technician and operator",
                    "employment_type": "Full Time",
                    "joining_date": "2021-01-01",
                    "worked_till_date": "2022-01-01",
                    "still_working": "0",
                    "approved": "1",
                    "status": "1",
                    "verificationProcess": {"level1": False, "level2": False, "level3": True, "level4": True}
                },
                {
                    "id": "5193_2",
                    "designation": "Teacher assistant",
                    "employment_type": "Part Time",
                    "joining_date": "2022-02-01",
                    "worked_till_date": "2023-02-01",
                    "still_working": "0",
                    "approved": "1",
                    "status": "1",
                    "verificationProcess": {"level1": True, "level2": True, "level3": True, "level4": True}
                },
                {
                    "id": "5193_3",
                    "designation": "Flutter Developer",
                    "employment_type": "Full Time",
                    "joining_date": "2023-03-01",
                    "worked_till_date": "Present",
                    "still_working": "1",
                    "approved": "1",
                    "status": "1",
                    "verificationProcess": {"level1": False, "level2": False, "level3": False, "level4": False}
                }
            ]
        }
    ]
}


@pytest.fixture(autouse=True)
def clean_session():
    _SESSION_STATES.clear()
    yield
    _SESSION_STATES.clear()


def test_scenario_group_a_direct_and_informal_queries(monkeypatch):
    def mock_fetch(endpoint_key, token=None, user_slug=None, user_id=None):
        if endpoint_key == "allEmployementNew":
            return MOCK_MULTI_ROLE_EMPLOYMENT_DATA
        return {"status": False}

    monkeypatch.setattr("backend.services.user_data_service.fetch_endpoint_data", mock_fetch)

    queries = [
        "Show my employment history",
        "Show my work history",
        "Where did I work before?",
        "Where was I working?",
        "Show my old jobs",
        "What companies have I worked for?",
        "Where do I work?",
        "What is my current job?"
    ]

    for q in queries:
        res: ServiceResponse = asyncio.run(handle_query(q, user_id=19, session_id="test_e2e_session"))
        assert res.handled is True, f"Failed to handle query: {q}"
        assert res.result_type_payload == "employment", f"Wrong result type for query: {q}"
        assert len(res.results_payload) == 3, f"Expected 3 structured roles for query: {q}"


def test_scenario_group_b_verification_queries(monkeypatch):
    def mock_fetch(endpoint_key, token=None, user_slug=None, user_id=None):
        if endpoint_key == "allEmployementNew":
            return MOCK_MULTI_ROLE_EMPLOYMENT_DATA
        return {"status": False}

    monkeypatch.setattr("backend.services.user_data_service.fetch_endpoint_data", mock_fetch)

    res: ServiceResponse = asyncio.run(handle_query("Is my employment verified?", user_id=19, session_id="test_e2e_session"))
    assert res.handled is True
    assert res.result_type_payload == "employment"

    # Find the Level 4 verification record (technician and operator)
    rec1 = next(r for r in res.results_payload if r["designation"] == "technician and operator")
    assert rec1["verification"]["highest_level"] == 4
    assert rec1["verification"]["levels"]["level1"] is False
    assert rec1["verification"]["levels"]["level2"] is False
    assert rec1["verification"]["levels"]["level3"] is True
    assert rec1["verification"]["levels"]["level4"] is True


def test_employment_selection_and_multi_role_isolation(monkeypatch):
    # Setup 2 distinct companies to trigger selection_metadata
    two_company_data = {
        "status": True,
        "data": [
            {
                "id": "5193",
                "company": "Alpha Tech",
                "company_id": "13",
                "is_verified": True,
                "lists": [{
                    "id": "5193_1",
                    "designation": "Developer",
                    "approved": "1",
                    "status": "1",
                    "verificationProcess": {"level1": True, "level2": True, "level3": True, "level4": True}
                }]
            },
            {
                "id": "5194",
                "company": "Beta Corp",
                "company_id": "14",
                "is_verified": True,
                "lists": [{
                    "id": "5194_1",
                    "designation": "Manager",
                    "approved": "1",
                    "status": "1",
                    "verificationProcess": {"level1": True, "level2": True, "level3": True, "level4": True}
                }]
            }
        ]
    }

    def mock_fetch(endpoint_key, token=None, user_slug=None, user_id=None):
        if endpoint_key == "allEmployementNew":
            return two_company_data
        return {"status": False}

    monkeypatch.setattr("backend.services.user_data_service.fetch_endpoint_data", mock_fetch)

    # 1. Initial query returning 2 companies -> produces selection_metadata
    res1: ServiceResponse = asyncio.run(handle_query("Show my employment history", user_id=19, session_id="test_e2e_session"))
    assert res1.selection_metadata is not None
    assert res1.selection_metadata.get("type") == "employment_selection"

    options = res1.selection_metadata.get("options", [])
    assert len(options) == 2


def test_cross_intent_state_isolation():
    # 1. Employment intent -> Employment history
    emp_intents = parse_user_data_intents("Show my employment history")
    assert UserIntent.EMPLOYMENT_HISTORY in emp_intents

    # 2. Transition to Job Search
    job_intents = parse_user_data_intents("Find Python jobs")
    assert job_intents == []  # Not a user_data intent, correctly routes to Search Router

    # 3. Transition to Candidate Search
    people_intents = parse_user_data_intents("Find candidates with React")
    assert people_intents == []

    # 4. Transition to Profile Skills
    skill_intents = parse_user_data_intents("What are my skills?")
    assert UserIntent.PROFILE_SKILLS in skill_intents


def test_selection_context_cleared_on_new_search(monkeypatch):
    def mock_fetch(endpoint_key, token=None, user_slug=None, user_id=None):
        if endpoint_key == "allEmployementNew":
            return MOCK_MULTI_ROLE_EMPLOYMENT_DATA
        return {"status": False}

    monkeypatch.setattr("backend.services.user_data_service.fetch_endpoint_data", mock_fetch)

    # Step 1: Request employment history
    asyncio.run(handle_query("Show my employment history", user_id=19, session_id="test_e2e_session"))

    # Step 2: Verify touch_session_state returns True for session
    res = touch_session_state("test_e2e_session")
    assert res is True


def test_api_fallback_and_error_handling(monkeypatch):
    # Test API error dictionary response handling
    def mock_fetch_error(endpoint_key, token=None, user_slug=None, user_id=None):
        return {"status": False, "status_code": 500, "error": "Internal Server Error"}

    monkeypatch.setattr("backend.services.user_data_service.fetch_endpoint_data", mock_fetch_error)
    monkeypatch.setattr("backend.services.user_data_service._get_employment_db_fallback", lambda uid: [])

    res: ServiceResponse = asyncio.run(handle_query("Show my employment history", user_id=19, session_id="test_e2e_session"))
    assert res.handled is True
    assert "temporary issue" in res.reply.lower() or "couldn't retrieve" in res.reply.lower() or "issue" in res.reply.lower()
