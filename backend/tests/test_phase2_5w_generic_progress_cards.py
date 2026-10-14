"""
test_phase2_5w_generic_progress_cards.py — Targeted test suite for Phase 2.5W.
Verifies that generic verification progress queries return ALL applicable employment roles and playcards across all companies.
"""

import pytest
import asyncio
from unittest.mock import patch
from fastapi.testclient import TestClient

from backend.main import app
from backend.api.auth import verify_token, get_current_user
from backend.services.user_data_service import handle_query
from backend.utils.session_state import (
    set_selected_employment_context,
    get_selected_employment_context,
    clear_selected_employment_context,
    set_pending_requested_level,
    get_pending_requested_level
)

CLIENT = TestClient(app)

MOCK_MULTI_COMPANY_MULTI_ROLE_PAYLOAD = {
    "status": True,
    "message": "All Employment",
    "data": [
        {
            "company": "InnoTech Solution",
            "company_id": "201",
            "is_verified": False,
            "company_claim_status": "Not Claimed",
            "lists": [
                {
                    "id": "5193",
                    "designation": "Software analyst",
                    "joining_date": "2022-01-01",
                    "worked_till_date": "Present",
                    "still_working": "1",
                    "approved": "0",
                    "status": "1",
                    "is_verified": False,
                    "verificationProcess": {"level1": False, "level2": False, "level3": False, "level4": False}
                }
            ]
        },
        {
            "company": "SP HUMANS PRIVATE LIMITED",
            "company_id": "102",
            "is_verified": True,
            "company_claim_status": "Claimed",
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
                }
            ]
        }
    ]
}


def test_1_multiple_companies_generic_progress_returns_all_roles():
    """Case 1: Generic progress returns all applicable roles across multiple companies."""
    async def _test():
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=MOCK_MULTI_COMPANY_MULTI_ROLE_PAYLOAD):
            res = await handle_query("show my verification progress", user_id="123", session_id="sess_5w_1")
            assert res.handled is True
            assert res.requested_level is None
            assert res.presentation_mode == "GENERIC_EMPLOYMENT_VERIFICATION"
            assert res.selection_metadata is None
            assert len(res.results_payload) == 3
            ids = {r["id"] for r in res.results_payload}
            assert ids == {"5193", "5217", "5219"}
    asyncio.run(_test())


def test_2_multiple_roles_within_one_company_returned_uniquely():
    """Case 2 & 10: Multiple roles within SP HUMANS returned without duplication."""
    async def _test():
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=MOCK_MULTI_COMPANY_MULTI_ROLE_PAYLOAD):
            res = await handle_query("show my verification progress", user_id="123", session_id="sess_5w_2")
            sp_roles = [r for r in res.results_payload if r["company"] == "SP HUMANS PRIVATE LIMITED"]
            assert len(sp_roles) == 2
            sp_ids = [r["id"] for r in sp_roles]
            assert sorted(sp_ids) == ["5217", "5219"]
            assert len(set(sp_ids)) == len(sp_ids)
    asyncio.run(_test())


def test_3_mixed_verification_states_totals_match_results_length():
    """Case 3 & 9: Summary totals in reply text match results_payload length exactly."""
    async def _test():
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=MOCK_MULTI_COMPANY_MULTI_ROLE_PAYLOAD):
            res = await handle_query("show my verification progress", user_id="123", session_id="sess_5w_3")
            assert res.result_type_payload == "employment_verification"
            assert "(3 records:" in res.reply or "3 records:" in res.reply or "3 records" in res.reply
            assert len(res.results_payload) == 3
    asyncio.run(_test())


def test_4_prior_selection_leakage_bypassed_for_generic_query():
    """Case 4: A stored session selection key does not truncate a new generic progress query."""
    async def _test():
        sess_id = "sess_5w_leakage_check"
        set_selected_employment_context(sess_id, "InnoTech Solution", record_key="innotech::5193")
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=MOCK_MULTI_COMPANY_MULTI_ROLE_PAYLOAD):
            res = await handle_query("show my verification progress", user_id="123", session_id=sess_id)
            assert res.selection_metadata is None
            assert len(res.results_payload) == 3
            ids = {r["id"] for r in res.results_payload}
            assert ids == {"5193", "5217", "5219"}
    asyncio.run(_test())


def test_5_explicit_company_targeting_filters_to_requested_company():
    """Case 5: Explicit company query returns only the matching company's records."""
    async def _test():
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=MOCK_MULTI_COMPANY_MULTI_ROLE_PAYLOAD):
            res = await handle_query("Show InnoTech Solution verification status", user_id="123", session_id="sess_5w_comp")
            assert len(res.results_payload) == 1
            assert res.results_payload[0]["company"] == "InnoTech Solution"
            assert res.results_payload[0]["id"] == "5193"
    asyncio.run(_test())


def test_6_explicit_role_key_targeting_resolves_exact_role():
    """Case 6: Explicit role targeting via multi-turn selection returns only the exact selected role."""
    async def _test():
        sess_id = "sess_5w_role_key"
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=MOCK_MULTI_COMPANY_MULTI_ROLE_PAYLOAD):
            # Turn 1: Level 4 query requires selection
            res1 = await handle_query("How do I complete Level 4?", user_id="123", session_id=sess_id)
            # Turn 2: Select company
            res2 = await handle_query("SP HUMANS PRIVATE LIMITED", user_id="123", session_id=sess_id)
            # Turn 3: Select option key for technician and operator (record 5217)
            opts = res2.selection_metadata["options"]
            target_key = next(opt["selection_key"] for opt in opts if opt.get("record_id") == "5217")
            res3 = await handle_query(target_key, user_id="123", session_id=sess_id)

            assert len(res3.results_payload) == 1
            assert res3.results_payload[0]["id"] == "5217"
            assert res3.results_payload[0]["designation"] == "technician and operator"
    asyncio.run(_test())


def test_7_level_specific_selection_flow_preserved():
    """Case 7: Level 4 explicit query on Turn 1 with single role requires selection prompt."""
    async def _test():
        single_payload = {
            "status": True,
            "message": "All Employment",
            "data": [MOCK_MULTI_COMPANY_MULTI_ROLE_PAYLOAD["data"][0]]
        }
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=single_payload):
            res = await handle_query("How do I complete Level 4?", user_id="123", session_id="sess_5w_l4")
            assert res.requested_level == 4
            assert res.presentation_mode == "FOCUSED_EMPLOYMENT_VERIFICATION"
            assert res.selection_metadata is not None
            assert res.results_payload == []
    asyncio.run(_test())


def test_8_out_of_order_verification_states_preserved():
    """Case 8: Out-of-order L3/L4 completion booleans are preserved under multi-role output."""
    async def _test():
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=MOCK_MULTI_COMPANY_MULTI_ROLE_PAYLOAD):
            res = await handle_query("show my verification progress", user_id="123", session_id="sess_5w_ooo")
            sp_5217 = next(r for r in res.results_payload if r["id"] == "5217")
            vp = sp_5217["verificationProcess"]
            assert vp["level1"] is False
            assert vp["level2"] is False
            assert vp["level3"] is True
            assert vp["level4"] is True
    asyncio.run(_test())


def test_9_http_boundary_generic_progress():
    """Full FastAPI TestClient boundary test for generic progress query."""
    mock_user = {"uid": 123, "id": 123, "email": "test@example.com"}
    app.dependency_overrides[verify_token] = lambda: mock_user
    app.dependency_overrides[get_current_user] = lambda: mock_user

    headers = {
        "Authorization": "Bearer test_token_1234567890",
        "X-User-Id": "123"
    }
    with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=MOCK_MULTI_COMPANY_MULTI_ROLE_PAYLOAD):
        response = CLIENT.post(
            "/chat",
            headers=headers,
            json={"session_id": "sess_5w_http", "messages": [{"role": "user", "content": "show my verification progress"}]}
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["requested_level"] is None
        assert payload["presentation_mode"] == "GENERIC_EMPLOYMENT_VERIFICATION"
        assert payload["result_type"] == "employment_verification"
        assert payload["selection_metadata"] is None
        assert len(payload["results"]) == 3
        ids = [r["id"] for r in payload["results"]]
        assert sorted(ids) == ["5193", "5217", "5219"]
