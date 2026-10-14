"""
test_phase2_5k_structured_context.py — Phase 2.5K & 2.5M Identity & Guidance Tests
"""

import pytest
import asyncio
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from backend.services.user_data_intent_parser import detect_requested_level, parse_user_data_intents, UserIntent
from backend.services.user_data_service import process_query_resolution, process_query_resolution_async, handle_query, ResolutionResult, ServiceResponse
from backend.services.user_data_formatter import format_multi_intent_response, resolve_employment_selection_context
from backend.api.auth import verify_token, get_current_user
from backend.main import app

CLIENT = TestClient(app)

# Standard mock employment payload with multi-role history (InnoTech + 3 SP HUMANS roles: 5217, 5219, 5218)
MOCK_MULTI_ROLE_EMP_PAYLOAD = {
    "status": True,
    "message": "All Employment",
    "data": [
        {
            "company": "InnoTech Solutions",
            "company_id": "101",
            "is_verified": True,
            "company_claim_status": "Claimed",
            "lists": [
                {
                    "id": "5101",
                    "designation": "Software Engineer",
                    "joining_date": "2021-01-01",
                    "worked_till_date": "2023-12-31",
                    "still_working": "0",
                    "approved": "1",
                    "status": "1",
                    "is_verified": True,
                    "verificationProcess": {
                        "level1": True,
                        "level2": True,
                        "level3": True,
                        "level4": True
                    }
                }
            ]
        },
        {
            "company": "SP HUMANS PRIVATE LIMITED",
            "company_id": "102",
            "is_verified": False,
            "company_claim_status": "Not Claimed",
            "lists": [
                {
                    "id": "5217",
                    "designation": "technician and operator",
                    "joining_date": "2024-01-01",
                    "worked_till_date": "2024-06-01",
                    "still_working": "0",
                    "approved": "0",
                    "status": "0",
                    "is_verified": False,
                    "verificationProcess": {
                        "level1": False,
                        "level2": False,
                        "level3": True,
                        "level4": True
                    }
                },
                {
                    "id": "5219",
                    "designation": "Teacher assistant",
                    "joining_date": "2024-06-02",
                    "worked_till_date": "2024-09-01",
                    "still_working": "0",
                    "approved": "0",
                    "status": "0",
                    "is_verified": False,
                    "verificationProcess": {
                        "level1": False,
                        "level2": False,
                        "level3": True,
                        "level4": True
                    }
                },
                {
                    "id": "5218",
                    "designation": "Flutter Developer",
                    "joining_date": "2024-09-02",
                    "worked_till_date": "Present",
                    "still_working": "1",
                    "approved": "0",
                    "status": "0",
                    "is_verified": False,
                    "verificationProcess": {
                        "level1": False,
                        "level2": False,
                        "level3": True,
                        "level4": True
                    }
                }
            ]
        }
    ]
}


def test_detect_requested_level_helper():
    """Verify requested_level deterministic detection in parser."""
    assert detect_requested_level("How do I complete Level 1?") == 1
    assert detect_requested_level("How do I complete Level 2?") == 2
    assert detect_requested_level("How do I complete Level 3?") == 3
    assert detect_requested_level("How do I complete Level 4?") == 4
    assert detect_requested_level("Show my employment verification") is None


def test_structured_requested_level_in_resolution_result():
    """Verify requested_level is attached to ResolutionResult during process_query_resolution."""
    res1 = process_query_resolution("How do I complete Level 3?", fetch_data=False)
    assert res1.requested_level == 3

    res_gen = process_query_resolution("Show my employment verification", fetch_data=False)
    assert res_gen.requested_level is None


def test_zero_results_leakage_when_selection_unresolved():
    """CONTRACT INVARIANT: Whenever selection_metadata is present (selection unresolved), results[] MUST be empty []."""
    async def _test():
        # Using multiple previous companies so context is unresolved
        ambiguous_payload = {
            "status": True,
            "message": "All Employment",
            "data": [
                {
                    "company": "Company A",
                    "lists": [{"id": "1", "designation": "Role A", "worked_till_date": "2022-01-01", "still_working": "0"}]
                },
                {
                    "company": "Company B",
                    "lists": [{"id": "2", "designation": "Role B", "worked_till_date": "2023-01-01", "still_working": "0"}]
                }
            ]
        }
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=ambiguous_payload):
            res1 = await handle_query("How do I complete Level 1?", user_id="123")
            assert res1.selection_metadata is not None
            assert res1.results_payload == []

            res4 = await handle_query("How do I complete Level 4?", user_id="123")
            assert res4.selection_metadata is not None
            assert res4.results_payload == []
    asyncio.run(_test())


def test_exact_role_record_id_selection():
    """Verify that selecting emp_rec_1 isolates ONLY Record 5217 (technician and operator), NOT all 3 SP HUMANS roles."""
    async def _test():
        ambig_payload = {
            "status": True,
            "message": "All Employment",
            "data": [
                {
                    "company": "SP HUMANS PRIVATE LIMITED",
                    "company_id": "102",
                    "is_verified": False,
                    "company_claim_status": "Not Claimed",
                    "lists": [
                        {
                            "id": "5217",
                            "designation": "technician and operator",
                            "joining_date": "2024-01-01",
                            "worked_till_date": "2024-06-01",
                            "still_working": "0",
                            "approved": "0",
                            "status": "0",
                            "is_verified": False,
                            "verificationProcess": {"level1": False, "level2": False, "level3": True, "level4": True}
                        },
                        {
                            "id": "5219",
                            "designation": "Teacher assistant",
                            "joining_date": "2024-06-02",
                            "worked_till_date": "2024-09-01",
                            "still_working": "0",
                            "approved": "0",
                            "status": "0",
                            "is_verified": False,
                            "verificationProcess": {"level1": False, "level2": False, "level3": True, "level4": True}
                        },
                        {
                            "id": "5218",
                            "designation": "Flutter Developer",
                            "joining_date": "2024-09-02",
                            "worked_till_date": "2024-12-01",
                            "still_working": "0",
                            "approved": "0",
                            "status": "0",
                            "is_verified": False,
                            "verificationProcess": {"level1": False, "level2": False, "level3": True, "level4": True}
                        }
                    ]
                }
            ]
        }
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=ambig_payload):
            sess_id = "sess_test_role_id"
            # Turn 1: trigger selection metadata
            res1 = await handle_query("How do I complete Level 3?", user_id="123", session_id=sess_id)
            assert res1.selection_metadata is not None

            # Find option corresponding to Record 5217
            opts = res1.selection_metadata.get("options", [])
            target_key = None
            for opt in opts:
                if opt.get("record_id") == "5217" or "technician" in opt.get("label", "").lower():
                    target_key = opt["selection_key"]
                    break

            assert target_key is not None

            # Turn 2: select target key
            res2 = await handle_query(target_key, user_id="123", session_id=sess_id)
            assert res2.handled is True
            results = res2.results_payload

            # Invariant: results must contain EXACTLY 1 card for Record 5217
            assert len(results) == 1
            assert results[0]["id"] == "5217"
            assert "technician" in results[0]["designation"].lower()
    asyncio.run(_test())


def test_state_aware_semantic_guidance():
    """Verify that L3 and L4 guidance strings reflect actual completion state."""
    async def _test():
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=MOCK_MULTI_ROLE_EMP_PAYLOAD):
            res = await handle_query("Show my employment verification status at InnoTech Solutions", user_id="123")
            assert res.handled is True
            results = res.results_payload
            assert len(results) == 1
            contract = results[0]["verification"]["semantic_contract"]

            # InnoTech has L3=True, L4=True
            assert contract["level3"]["state"] == "COMPLETED"
            assert contract["level3"]["guidance"] == "Employment verified by employer."

            assert contract["level4"]["state"] == "COMPLETED"
            assert contract["level4"]["guidance"] == "Salary verified."
    asyncio.run(_test())


def test_progression_metadata_repair_for_out_of_order_state():
    """Verify progression metadata for SP HUMANS (L1=false, L2=false, L3=true, L4=true)."""
    async def _test():
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=MOCK_MULTI_ROLE_EMP_PAYLOAD):
            res = await handle_query("emp_rec_2", user_id="123") # Target SP HUMANS Flutter Developer (active)
            results = res.results_payload
            assert len(results) == 1
            role = results[0]
            verif = role["verification"]

            # Out of order state
            assert verif["levels"]["level1"] is False
            assert verif["levels"]["level2"] is False
            assert verif["levels"]["level3"] is True
            assert verif["levels"]["level4"] is True

            # Progression metadata
            assert verif["highest_level"] == 4
            assert verif["next_level"] == 1
            assert verif["next_action"] == "Work identity verification details are incomplete."
            assert "All verification levels" not in verif["next_action"]
    asyncio.run(_test())


def test_actual_http_chat_boundary_verification():
    """Phase 6: Actual HTTP POST /chat boundary verification using mocked endpoint data."""
    mock_payload = {"uid": 123, "id": 123, "email": "test@example.com"}
    app.dependency_overrides[verify_token] = lambda: mock_payload
    app.dependency_overrides[get_current_user] = lambda: mock_payload

    try:
        headers = {
            "Authorization": "Bearer mock_valid_test_token_1234567890",
            "X-User-Id": "123"
        }
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=MOCK_MULTI_ROLE_EMP_PAYLOAD):
            # Test L1 unresolved ambiguous: selection_metadata present, results MUST be empty []
            resp1 = CLIENT.post("/chat", headers=headers, json={"messages": [{"role": "user", "content": "How do I complete Level 1?"}]})
            assert resp1.status_code == 200
            j1 = resp1.json()
            assert j1["success"] is True
            assert j1["requested_level"] == 1
            assert j1["presentation_mode"] == "FOCUSED_EMPLOYMENT_VERIFICATION"

            # Test L4 unresolved ambiguous
            resp4 = CLIENT.post("/chat", headers=headers, json={"messages": [{"role": "user", "content": "How do I complete Level 4?"}]})
            assert resp4.status_code == 200
            j4 = resp4.json()
            assert j4["requested_level"] == 4

            # Test Generic in fresh session
            resp_gen = CLIENT.post("/chat", headers=headers, json={"session_id": "sess_gen_boundary", "messages": [{"role": "user", "content": "Show my employment verification"}]})
            assert resp_gen.status_code == 200
            j_gen = resp_gen.json()
            assert j_gen["requested_level"] is None
    finally:
        app.dependency_overrides.pop(verify_token, None)
        app.dependency_overrides.pop(get_current_user, None)
