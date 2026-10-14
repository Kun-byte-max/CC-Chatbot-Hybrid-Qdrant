"""
test_phase2b_integration_hardening.py — Targeted Unit Tests for Phase 2B Integration Hardening & Robustness.
"""

import pytest
from backend.services.user_data_intent_parser import UserIntent, parse_user_data_intents
from backend.services.platform_api import _parse_positive_int_id
from backend.services.user_data_service import _parse_positive_int_id as service_parse_id


def test_colloquial_employment_intent_routing():
    # Work history phrasing
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("show my work history")
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("show my work experience")
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("what is my work history")
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("tell me my work history")
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("what is my employment history")
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("show my employment history")

    # Previous / old employment phrasing
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("show my old jobs")
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("show my previous jobs")
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("where did i work before")
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("where have i worked")
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("where was i working")
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("where did I work")
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("what companies have I worked for")
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("what companies did I work for")

    # Current employment phrasing
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("where do I work")
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("where am I working")
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("what company do I work for")
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("what is my current job")
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("show my current employment")

    # Informal variants
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("my jobs")
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("my previous work")
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("my past work")
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("my employment details")
    assert UserIntent.EMPLOYMENT_HISTORY in parse_user_data_intents("my work details")


def test_non_employment_queries_not_intercepted():
    # Job search queries must NOT be intercepted
    assert parse_user_data_intents("find python jobs") == []
    assert parse_user_data_intents("find backend jobs") == []
    assert parse_user_data_intents("jobs near me") == []
    assert parse_user_data_intents("find companies hiring") == []
    assert parse_user_data_intents("what companies are hiring") == []

    # Candidate / People search queries must NOT be intercepted
    assert parse_user_data_intents("find backend developers") == []
    assert parse_user_data_intents("find candidates with React") == []
    assert parse_user_data_intents("find candidates with 5 years experience") == []


def test_user_id_coercion_matrix():
    # Valid positive integer representations
    assert _parse_positive_int_id(19) == 19
    assert _parse_positive_int_id("19") == 19
    assert _parse_positive_int_id(" 19 ") == 19

    assert service_parse_id(19) == 19
    assert service_parse_id("19") == 19
    assert service_parse_id(" 19 ") == 19

    # Invalid values must fail safely to None
    for invalid in [None, "", "abc", "19.5", 0, -1, True, False]:
        assert _parse_positive_int_id(invalid) is None
        assert service_parse_id(invalid) is None


def test_explicit_employment_result_type_contract(monkeypatch):
    import asyncio
    from backend.services.user_data_service import handle_query, ServiceResponse

    mock_employment_payload = {
        "status": True,
        "data": [
            {
                "id": "100",
                "company": "Alpha Corp",
                "company_id": "50",
                "company_logo": "https://example.com/logo.png",
                "is_verified": True,
                "lists": [
                    {
                        "id": "1001",
                        "designation": "Staff Engineer",
                        "employment_type": "Full-time",
                        "joining_date": "2022-01-01",
                        "worked_till_date": "Present",
                        "still_working": "1",
                        "approved": "1",
                        "status": "1",
                        "verificationProcess": {"level1": True, "level2": True, "level3": True, "level4": True}
                    }
                ]
            }
        ]
    }

    def mock_fetch(endpoint_key, token=None, user_slug=None, user_id=None):
        if endpoint_key == "allEmployementNew":
            return mock_employment_payload
        return {"status": False}

    monkeypatch.setattr("backend.services.user_data_service.fetch_endpoint_data", mock_fetch)

    response = asyncio.run(handle_query("show my employment history", token="test_tok", user_id=19))
    assert response.handled is True
    assert response.result_type_payload == "employment"
    assert len(response.results_payload) == 1
    assert response.results_payload[0]["company"] == "Alpha Corp"
