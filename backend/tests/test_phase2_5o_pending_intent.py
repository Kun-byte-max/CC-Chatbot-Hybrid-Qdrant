"""
test_phase2_5o_pending_intent.py — Phase 2.5O Pending Intent Retention & Deduplication Tests
"""

import pytest
import asyncio
from unittest.mock import patch
from fastapi.testclient import TestClient

from backend.utils.session_state import (
    set_pending_requested_level,
    get_pending_requested_level,
    clear_pending_requested_level
)
from backend.services.user_data_service import handle_query, process_query_resolution
from backend.services.user_data_formatter import build_selection_metadata
from backend.main import app

CLIENT = TestClient(app)

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
                    "verificationProcess": {"level1": True, "level2": True, "level3": True, "level4": True}
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


from backend.api.auth import verify_token, get_current_user


def test_1_session_state_helpers():
    """Verify pending_requested_level helper functions in session_state."""
    sess_id = "sess_test_helpers_01"
    set_pending_requested_level(sess_id, 4)
    assert get_pending_requested_level(sess_id) == 4

    clear_pending_requested_level(sess_id)
    assert get_pending_requested_level(sess_id) is None


def test_2_deduplicated_selection_reply_prompt():
    """Verify selection reply contains prompt but NO duplicate textual option list."""
    history = MOCK_MULTI_ROLE_EMP_PAYLOAD["data"]
    meta = build_selection_metadata(history, prompt_text="Which employment record would you like to check?")
    assert meta["type"] == "employment_selection"
    assert len(meta["options"]) == 2  # 1 InnoTech + 1 SP HUMANS company selection option
    assert meta["options"][0]["selection_key"] == "emp_rec_1"


def test_3_multi_turn_level_4_flow():
    """Verify multi-turn Level 4 flow preserves requested_level=4 across Turns 1, 2, and 3."""
    async def _test():
        sess_id = "sess_test_l4_flow"
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=MOCK_MULTI_ROLE_EMP_PAYLOAD):
            # Turn 1: "How do I complete Level 4?"
            res1 = await handle_query("How do I complete Level 4?", user_id="123", session_id=sess_id)
            assert res1.requested_level == 4
            assert res1.presentation_mode == "FOCUSED_EMPLOYMENT_VERIFICATION"
            assert res1.selection_metadata is not None
            assert res1.results_payload == []
            # Verify reply is concise prompt without duplicate textual bullet option list
            assert "• **" not in res1.reply

            # Turn 2: "SP HUMANS PRIVATE LIMITED"
            res2 = await handle_query("SP HUMANS PRIVATE LIMITED", user_id="123", session_id=sess_id)
            assert res2.requested_level == 4
            assert res2.presentation_mode == "FOCUSED_EMPLOYMENT_VERIFICATION"
            assert res2.selection_metadata is not None
            assert res2.results_payload == []

            # Turn 3: Select target_key for Record 5217 from Turn 2 selection_metadata options
            opts2 = res2.selection_metadata["options"]
            target_key = next(opt["selection_key"] for opt in opts2 if opt.get("record_id") == "5217")

            res3 = await handle_query(target_key, user_id="123", session_id=sess_id)
            assert res3.requested_level == 4
            assert res3.presentation_mode == "FOCUSED_EMPLOYMENT_VERIFICATION"
            assert res3.selection_metadata is None
            assert len(res3.results_payload) == 1
            assert res3.results_payload[0]["id"] == "5217"
            assert "Salary verified." in res3.reply or "Provide verified salary evidence" in res3.reply

            # Turn 4: Generic query post-resolution must start fresh
            res4 = await handle_query("Show my employment verification", user_id="123", session_id=sess_id)
            assert res4.requested_level is None
            assert res4.presentation_mode == "GENERIC_EMPLOYMENT_VERIFICATION"

    asyncio.run(_test())


def test_4_multi_turn_level_1_flow():
    """Verify multi-turn Level 1 flow preserves requested_level=1 across disambiguation turns."""
    async def _test():
        sess_id = "sess_test_l1_flow"
        ambig_sp_payload = {
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
                        }
                    ]
                }
            ]
        }
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=ambig_sp_payload):
            # Turn 1: "How do I complete Level 1?"
            res1 = await handle_query("How do I complete Level 1?", user_id="123", session_id=sess_id)
            assert res1.requested_level == 1
            assert res1.presentation_mode == "FOCUSED_EMPLOYMENT_VERIFICATION"
            assert res1.results_payload == []

            # Turn 2: Select emp_rec_1 (technician and operator - Record 5217)
            opts = res1.selection_metadata["options"]
            target_key = next(opt["selection_key"] for opt in opts if opt.get("record_id") == "5217")

            res2 = await handle_query(target_key, user_id="123", session_id=sess_id)
            assert res2.requested_level == 1
            assert res2.presentation_mode == "FOCUSED_EMPLOYMENT_VERIFICATION"
            assert len(res2.results_payload) == 1
            assert res2.results_payload[0]["id"] == "5217"
    asyncio.run(_test())


def test_5_generic_query_remains_generic():
    """Verify generic query does not accidentally trigger focused presentation mode."""
    async def _test():
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=MOCK_MULTI_ROLE_EMP_PAYLOAD):
            res = await handle_query("Show my employment verification status", user_id="123", session_id="sess_generic_chk")
            assert res.requested_level is None
            assert res.presentation_mode == "GENERIC_EMPLOYMENT_VERIFICATION"
    asyncio.run(_test())


def test_6_http_chat_boundary_multi_turn_level4():
    """Full FastAPI TestClient /chat boundary test for multi-turn Level 4 flow."""
    mock_payload = {"uid": 123, "id": 123, "email": "test@example.com"}
    app.dependency_overrides[verify_token] = lambda: mock_payload
    app.dependency_overrides[get_current_user] = lambda: mock_payload

    try:
        headers = {
            "Authorization": "Bearer mock_valid_test_token_1234567890",
            "X-User-Id": "123"
        }
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=MOCK_MULTI_ROLE_EMP_PAYLOAD):
            sess_id = "sess_http_l4_flow"

            # Turn 1: Level 4 request
            r1 = CLIENT.post("/chat", headers=headers, json={"session_id": sess_id, "messages": [{"role": "user", "content": "How do I complete Level 4?"}]})
            assert r1.status_code == 200
            j1 = r1.json()
            assert j1["requested_level"] == 4
            assert j1["presentation_mode"] == "FOCUSED_EMPLOYMENT_VERIFICATION"
            assert j1["selection_metadata"] is not None
            assert j1["results"] == []

            # Turn 2: Select company
            r2 = CLIENT.post("/chat", headers=headers, json={"session_id": sess_id, "messages": [{"role": "user", "content": "SP HUMANS PRIVATE LIMITED"}]})
            assert r2.status_code == 200
            j2 = r2.json()
            assert j2["requested_level"] == 4
            assert j2["presentation_mode"] == "FOCUSED_EMPLOYMENT_VERIFICATION"
            assert j2["results"] == []

            # Turn 3: Select role designation target_key
            opts2 = j2["selection_metadata"]["options"]
            target_key = next(opt["selection_key"] for opt in opts2 if opt.get("record_id") == "5217")

            r3 = CLIENT.post("/chat", headers=headers, json={"session_id": sess_id, "messages": [{"role": "user", "content": target_key}]})
            assert r3.status_code == 200
            j3 = r3.json()
            assert j3["requested_level"] == 4
            assert j3["presentation_mode"] == "FOCUSED_EMPLOYMENT_VERIFICATION"
            assert len(j3["results"]) == 1
            assert j3["results"][0]["id"] == "5217"
    finally:
        app.dependency_overrides.pop(verify_token, None)
        app.dependency_overrides.pop(get_current_user, None)


def test_7_first_turn_l4_single_role_requires_selection():
    """Verify first-turn L4 query on single-role dataset returns selection prompt and results=[] without auto-selecting."""
    single_role_payload = {
        "status": True,
        "message": "Single Role",
        "data": [
            {
                "company": "InnoTech Solution",
                "company_id": "101",
                "is_verified": False,
                "lists": [
                    {
                        "id": "5193",
                        "designation": "Software analyst",
                        "joining_date": "2022-01-01",
                        "worked_till_date": "Present",
                        "still_working": "1",
                        "approved": "0",
                        "status": "1",
                        "verificationProcess": {"level1": False, "level2": False, "level3": False, "level4": False}
                    }
                ]
            }
        ]
    }
    async def _test():
        sess_id = "sess_single_role_t1"
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=single_role_payload):
            res1 = await handle_query("How do I complete Level 4?", user_id="123", session_id=sess_id)
            assert res1.requested_level == 4
            assert res1.presentation_mode == "FOCUSED_EMPLOYMENT_VERIFICATION"
            assert res1.selection_metadata is not None
            assert res1.results_payload == []
            assert "Which employment record would you like to check?" in res1.reply

            # Turn 2: Exact selection resolves single role
            target_key = res1.selection_metadata["options"][0]["selection_key"]
            res2 = await handle_query(target_key, user_id="123", session_id=sess_id)
            assert res2.requested_level == 4
            assert res2.selection_metadata is None
            assert len(res2.results_payload) == 1
            assert res2.results_payload[0]["id"] == "5193"

    asyncio.run(_test())


def test_8_duplicate_intent_alias_deduplication():
    """Verify overlapping verification intent aliases produce exactly ONE canonical guidance section."""
    async def _test():
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=MOCK_MULTI_ROLE_EMP_PAYLOAD):
            res = await handle_query("How do I complete Level 4?", user_id="123", session_id="sess_dedup_chk")
            # Reply should be a concise selection question without repeated verification roadmap blocks
            assert res.reply.count("Which employment record would you like to check?") == 1
            assert res.reply.count("Level 4 — Salary Verification") == 0  # Selection required first

            # After selection, resolved role guidance must contain Level 4 guidance exactly once
            res2 = await handle_query("emp_rec_1", user_id="123", session_id="sess_dedup_chk")
            assert res2.reply.count("Salary Verification") <= 2  # Concise section header + line item only once
    asyncio.run(_test())


def test_9_independent_verification_state_preservation():
    """Verify out-of-order verification states retain independent level completion statuses."""
    out_of_order_payload = {
        "status": True,
        "message": "Out of Order",
        "data": [
            {
                "company": "SP HUMANS PRIVATE LIMITED",
                "company_id": "102",
                "is_verified": False,
                "lists": [
                    {
                        "id": "5217",
                        "designation": "technician and operator",
                        "joining_date": "2024-01-01",
                        "worked_till_date": "2024-06-01",
                        "still_working": "0",
                        "approved": "0",
                        "status": "0",
                        "verificationProcess": {"level1": False, "level2": False, "level3": True, "level4": True}
                    }
                ]
            }
        ]
    }
    async def _test():
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=out_of_order_payload):
            res = await handle_query("How do I complete Level 4?", user_id="123", session_id="sess_out_of_order")
            # Must require selection
            assert res.selection_metadata is not None
            assert res.results_payload == []

            # Turn 2: Select record
            res2 = await handle_query("emp_rec_1", user_id="123", session_id="sess_out_of_order")
            assert res2.results_payload[0]["id"] == "5217"
            vp = res2.results_payload[0]["verificationProcess"]
            assert vp["level1"] is False
            assert vp["level2"] is False
            assert vp["level3"] is True
            assert vp["level4"] is True
    asyncio.run(_test())

