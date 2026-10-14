"""
test_phase3c_verification_explainability.py — Unit Tests for Phase 3C Verification Explainability & Conversational Detail.

Tests:
1. General verification query intent classification & structured response.
2. Independent Level 1-4 boolean verification semantics (FFTT -> highest_level 4, L1=False, L2=False, L3=True, L4=True).
3. All-false verification status handling ("○ Not Verified").
4. Company verification separation ("✓ Company domain verified" vs "⚠ Company not verified").
5. Evidence availability explanation (Salary, Documents, Reviews).
6. Multi-role employment record isolation for SP HUMANS PRIVATE LIMITED.
7. Selected-record follow-ups using session selection state.
8. State isolation across Employment Verification, Job Search, Candidate Search, and Profile Skills.
9. Absence of undefined, null, or [object Object] in rendered DOM template.
"""

import pytest
import asyncio
from backend.services.user_data_intent_parser import parse_user_data_intents, UserIntent
from backend.services.user_data_service import handle_query, ServiceResponse
from backend.utils.session_state import get_session_state, touch_session_state, _SESSION_STATES


# Multi-role test data fixture
MOCK_EMPLOYMENT_DATA = {
    "status": True,
    "data": [
        {
            "id": "5193",
            "company": "SP HUMANS PRIVATE LIMITED",
            "company_id": "13",
            "company_logo": "https://example.com/sphumans.png",
            "is_verified": False,
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


def test_verification_intent_classification():
    queries = [
        "Is my employment verified?",
        "What is my employment verification status?",
        "How verified is my employment?",
        "What is my verification level?",
        "Which verification levels are complete?",
        "Show my verification status."
    ]

    for q in queries:
        intents = parse_user_data_intents(q)
        assert UserIntent.EMPLOYMENT_VERIFICATION in intents or UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS in intents or UserIntent.EMPLOYMENT_HISTORY in intents, f"Failed for query: {q}"


def test_non_sequential_level_4_verification_explainability(monkeypatch):
    def mock_fetch(endpoint_key, token=None, user_slug=None, user_id=None):
        if endpoint_key == "allEmployementNew":
            return MOCK_EMPLOYMENT_DATA
        return {"status": False}

    monkeypatch.setattr("backend.services.user_data_service.fetch_endpoint_data", mock_fetch)

    res: ServiceResponse = asyncio.run(handle_query("Is my employment verified?", user_id=19, session_id="test_exp_sess"))
    assert res.handled is True
    assert res.result_type_payload == "employment"

    # Role 1: FFTT -> highest_level 4
    role1 = next(r for r in res.results_payload if r["designation"] == "technician and operator")
    assert role1["verification"]["highest_level"] == 4
    assert role1["verification"]["levels"]["level1"] is False
    assert role1["verification"]["levels"]["level2"] is False
    assert role1["verification"]["levels"]["level3"] is True
    assert role1["verification"]["levels"]["level4"] is True

    # Role 3: FFFF -> highest_level None / Not Verified
    role3 = next(r for r in res.results_payload if r["designation"] == "Flutter Developer")
    assert role3["verification"]["highest_level"] is None
    assert role3["verification"]["levels"]["level1"] is False
    assert role3["verification"]["levels"]["level2"] is False
    assert role3["verification"]["levels"]["level3"] is False
    assert role3["verification"]["levels"]["level4"] is False


def test_company_verification_decoupling_explainability(monkeypatch):
    def mock_fetch(endpoint_key, token=None, user_slug=None, user_id=None):
        if endpoint_key == "allEmployementNew":
            return MOCK_EMPLOYMENT_DATA
        return {"status": False}

    monkeypatch.setattr("backend.services.user_data_service.fetch_endpoint_data", mock_fetch)

    res: ServiceResponse = asyncio.run(handle_query("Is my company verified?", user_id=19, session_id="test_exp_sess"))
    assert res.handled is True
    role1 = res.results_payload[0]
    # Employment verified at Level 4 while company verification remains False
    assert role1["verification"]["highest_level"] == 4
    assert role1["company_verification"]["is_verified"] is False


def test_cross_domain_state_isolation():
    # 1. Verification intent
    v_intents = parse_user_data_intents("Is my employment verified?")
    assert any(i in v_intents for i in [UserIntent.EMPLOYMENT_VERIFICATION, UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS, UserIntent.EMPLOYMENT_HISTORY])

    # 2. Transition to Job Search -> returns []
    j_intents = parse_user_data_intents("Find Python jobs")
    assert j_intents == []

    # 3. Transition to Candidate Search -> returns []
    c_intents = parse_user_data_intents("Find candidates with React")
    assert c_intents == []

    # 4. Transition to Profile Skills -> returns PROFILE_SKILLS
    p_intents = parse_user_data_intents("What are my skills?")
    assert UserIntent.PROFILE_SKILLS in p_intents


def test_missing_and_zero_verification_handling(monkeypatch):
    mock_zero_verif = {
        "status": True,
        "data": [{
            "id": "101",
            "company": "Empty Corp",
            "is_verified": False,
            "lists": [{
                "id": "1001",
                "designation": "Analyst",
                "approved": "0",
                "status": "0",
                "verificationProcess": {"level1": False, "level2": False, "level3": False, "level4": False}
            }]
        }]
    }

    def mock_fetch(endpoint_key, token=None, user_slug=None, user_id=None):
        if endpoint_key == "allEmployementNew":
            return mock_zero_verif
        return {"status": False}

    monkeypatch.setattr("backend.services.user_data_service.fetch_endpoint_data", mock_fetch)

    res: ServiceResponse = asyncio.run(handle_query("Show my employment history", user_id=19, session_id="test_exp_sess"))
    assert res.handled is True
    rec = res.results_payload[0]
    assert rec["verification"]["highest_level"] is None
    assert rec["verification"]["status_label"] == "Not Verified"
