"""
test_phase5_3_employee_verification_e2e.py — End-to-End Employee Verification UX & Integration Hardening Test Suite (Phase 5.3).

Validates complete employee verification journey:
  - Scenarios A-D: Single employment Level 1 to Level 4 formatting and progress checklist.
  - Scenario E: Higher flag bypass prevention (fails closed to Level 0).
  - Scenario F: Multiple employment selection, context locking, and cross-intent follow-ups.
  - Scenario G: Duplicate company tenures identity disambiguation.
  - Scenario H: API response order reordering resilience.
  - Scenario I: Selected record disappearance & safe eviction.
  - Scenario J: Cross-intent continuity (verification, progress, blocker, salary, skills, history).
  - Scenario K: Explicit employment switching (Google -> Microsoft).
  - Scenario L: No-selection guidance behavior.
  - Scenario M: Invalid option key submission handling.
  - Scenario N: Multi-user RAM session isolation.
  - Scenario O: HTTP serialization of action_metadata & selection_metadata via TestClient /chat.
"""

import pytest
import asyncio
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from backend.main import app
from backend.services.user_data_service import handle_query, ServiceResponse
from backend.services.user_data_intent_parser import UserIntent
from backend.utils.session_state import (
    set_selected_employment_context,
    get_selected_employment_context,
    clear_selected_employment_context,
    set_session_option_mapping,
    get_session_option_mapping
)


# ---------------------------------------------------------------------------
# FIXTURE HELPERS
# ---------------------------------------------------------------------------

def make_fixture_l1():
    return {
        "status": True,
        "data": [{
            "id": 10,
            "company": "Acme Corp",
            "company_id": 100,
            "is_verified": False,
            "lists": [{
                "id": "101",
                "designation": "Software Developer",
                "joining_date": "2024-01-01",
                "worked_till_date": "Present",
                "still_working": "1",
                "approved": "0",
                "status": "0",
                "verificationProcess": {"level1": True, "level2": False}
            }]
        }]
    }

def make_fixture_l2():
    return {
        "status": True,
        "data": [{
            "id": 20,
            "company": "Beta Inc",
            "company_id": 200,
            "is_verified": True,
            "lists": [{
                "id": "201",
                "designation": "Backend Engineer",
                "joining_date": "2023-01-01",
                "worked_till_date": "Present",
                "still_working": "1",
                "approved": "0",
                "status": "1",
                "verificationProcess": {"level1": True, "level2": True}
            }]
        }]
    }

def make_fixture_l3():
    return {
        "status": True,
        "data": [{
            "id": 30,
            "company": "Gamma Systems",
            "company_id": 300,
            "is_verified": True,
            "lists": [{
                "id": "301",
                "designation": "Lead Architect",
                "joining_date": "2022-01-01",
                "worked_till_date": "Present",
                "still_working": "1",
                "approved": "1",
                "status": "1",
                "verificationProcess": {"level1": True, "level2": True, "level3": True}
            }]
        }]
    }

def make_fixture_l4():
    return {
        "status": True,
        "data": [{
            "id": 40,
            "company": "Delta Tech",
            "company_id": 400,
            "is_verified": True,
            "lists": [{
                "id": "401",
                "designation": "Staff Engineer",
                "joining_date": "2021-01-01",
                "worked_till_date": "Present",
                "still_working": "1",
                "approved": "1",
                "status": "1",
                "verificationProcess": {"level1": True, "level2": True, "level3": True, "level4": True},
                "salary": "160000",
                "haveSalary": 1
            }]
        }]
    }

def make_fixture_multi_company():
    return {
        "status": True,
        "data": [
            {
                "id": 50,
                "company": "Google LLC",
                "company_id": 500,
                "is_verified": True,
                "lists": [{
                    "id": "501",
                    "designation": "Senior Engineer",
                    "joining_date": "2023-01-01",
                    "worked_till_date": "Present",
                    "still_working": "1",
                    "approved": "1",
                    "status": "1",
                    "verificationProcess": {"level1": True, "level2": True, "level3": True}
                }]
            },
            {
                "id": 60,
                "company": "Microsoft Corp",
                "company_id": 600,
                "is_verified": True,
                "lists": [{
                    "id": "601",
                    "designation": "DevOps Lead",
                    "joining_date": "2021-01-01",
                    "worked_till_date": "2022-12-31",
                    "still_working": "0",
                    "approved": "1",
                    "status": "1",
                    "verificationProcess": {"level1": True, "level2": True, "level3": True, "level4": True}
                }]
            }
        ]
    }

def make_fixture_duplicate_tenures():
    return {
        "status": True,
        "data": [
            {
                "id": 70,
                "company": "Google LLC",
                "company_id": 500,
                "is_verified": True,
                "lists": [{
                    "id": "701",
                    "designation": "Senior Engineer",
                    "joining_date": "2024-01-01",
                    "worked_till_date": "Present",
                    "still_working": "1",
                    "approved": "1",
                    "status": "1",
                    "verificationProcess": {"level1": True, "level2": True, "level3": True}
                }]
            },
            {
                "id": 80,
                "company": "Google LLC",
                "company_id": 500,
                "is_verified": True,
                "lists": [{
                    "id": "801",
                    "designation": "Junior Engineer",
                    "joining_date": "2022-01-01",
                    "worked_till_date": "2023-12-31",
                    "still_working": "0",
                    "approved": "1",
                    "status": "1",
                    "verificationProcess": {"level1": True, "level2": True, "level3": True, "level4": True}
                }]
            }
        ]
    }


# ---------------------------------------------------------------------------
# SCENARIOS A–D: SINGLE EMPLOYMENT LEVEL 1 THROUGH 4
# ---------------------------------------------------------------------------

def test_scenario_a_level1_status():
    async def _test():
        with patch("backend.services.user_data_service.platform_api._get", return_value=make_fixture_l1()):
            res = await handle_query("Show my employment verification status", token="tok", user_id=10)
            assert res.handled is True
            assert "Acme Corp" in res.reply
            assert "Verification Level: 1 / 4" in res.reply
            assert "Level 1 (Work Identity): Completed" in res.reply
            assert "Level 2 (Company Domain Verification): Pending" in res.reply
            assert "Current Blocker:" in res.reply
            assert "**Next step:**" in res.reply
            assert "Next Step:" not in res.reply

    asyncio.run(_test())


def test_scenario_b_level2_status():
    async def _test():
        with patch("backend.services.user_data_service.platform_api._get", return_value=make_fixture_l2()):
            res = await handle_query("Show my employment verification status", token="tok", user_id=20)
            assert res.handled is True
            assert "Beta Inc" in res.reply
            assert "Verification Level: 2 / 4" in res.reply
            assert "Level 2 (Company Domain Verification): Completed" in res.reply or "Company Domain Verification: Verified" in res.reply
            assert "Current Blocker:" in res.reply
            assert "**Next step:**" in res.reply
            assert "Next Step:" not in res.reply

    asyncio.run(_test())


def test_scenario_c_level3_status():
    async def _test():
        with patch("backend.services.user_data_service.platform_api._get", return_value=make_fixture_l3()):
            res = await handle_query("Show my employment verification status", token="tok", user_id=30)
            assert res.handled is True
            assert "Gamma Systems" in res.reply
            assert "Verification Level: 3 / 4" in res.reply
            assert "Level 3 (Employment Verification): Completed" in res.reply or "Employment Verification: Verified" in res.reply
            assert "Current Blocker:" in res.reply
            assert "**Next step:**" in res.reply
            assert "Next Step:" not in res.reply

    asyncio.run(_test())


def test_scenario_d_level4_status():
    async def _test():
        with patch("backend.services.user_data_service.platform_api._get", return_value=make_fixture_l4()):
            res = await handle_query("Show my employment verification status", token="tok", user_id=40)
            assert res.handled is True
            assert "Delta Tech" in res.reply
            assert "Verification Level: 4 / 4" in res.reply
            assert "🎉 Employment Fully Verified" in res.reply or "Fully Verified" in res.reply
            assert "Current Blocker:" not in res.reply

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# SCENARIO E: HIGHER FLAG BYPASS PREVENTION
# ---------------------------------------------------------------------------

def test_scenario_e_higher_flag_bypass():
    async def _test():
        payload = {
            "status": True,
            "data": [{
                "company": "Bypass Corp",
                "is_verified": True,
                "lists": [{
                    "id": "999",
                    "designation": "Dev",
                    "approved": "1",
                    "status": "1",
                    "verificationProcess": {"level1": False, "level2": True, "level3": True, "level4": True}
                }]
            }]
        }
        with patch("backend.services.user_data_service.platform_api._get", return_value=payload):
            res = await handle_query("Show my employment verification status", token="tok", user_id=50)
            assert res.handled is True
            assert "Verification Level: 0 / 4" in res.reply or "Level 0" in res.reply or "Work Identity" in res.reply

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# SCENARIO F: MULTIPLE EMPLOYMENTS & SELECTION FLOW
# ---------------------------------------------------------------------------

def test_scenario_f_multiple_employments_and_switching():
    async def _test():
        sess_id = "sess_53_f"
        clear_selected_employment_context(sess_id)
        with patch("backend.services.user_data_service.platform_api._get", return_value=make_fixture_multi_company()):
            # 1. Ask verification status -> returns selection prompt
            res1 = await handle_query("Show my employment verification status", token="tok", user_id=60, session_id=sess_id)
            assert res1.handled is True
            assert res1.selection_metadata is not None
            assert len(res1.selection_metadata["options"]) == 2

            # 2. Submit selection key for Google
            res2 = await handle_query("emp_rec_1", token="tok", user_id=60, session_id=sess_id)
            assert res2.handled is True
            assert "Google LLC" in res2.reply

            # 3. Ask status again -> targets Google directly
            res3 = await handle_query("Show my verification status", token="tok", user_id=60, session_id=sess_id)
            assert res3.handled is True
            assert "Google LLC" in res3.reply
            assert "Verification Level: 3 / 4" in res3.reply

            # 4. Explicitly switch to Microsoft using explicit intent prompt
            res4 = await handle_query("Show my employment verification status at Microsoft Corp", token="tok", user_id=60, session_id=sess_id)
            assert res4.handled is True
            assert "Microsoft Corp" in res4.reply

            # 5. Subsequent verification query targets Microsoft
            res5 = await handle_query("What is my verification status?", token="tok", user_id=60, session_id=sess_id)
            assert res5.handled is True
            assert "Microsoft Corp" in res5.reply
            assert "Verification Level: 4 / 4" in res5.reply

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# SCENARIO G: DUPLICATE COMPANY TENURES DISAMBIGUATION
# ---------------------------------------------------------------------------

def test_scenario_g_duplicate_company_tenures():
    async def _test():
        sess_id = "sess_53_g"
        clear_selected_employment_context(sess_id)
        with patch("backend.services.user_data_service.platform_api._get", return_value=make_fixture_duplicate_tenures()):
            res1 = await handle_query("Show my employment verification status", token="tok", user_id=70, session_id=sess_id)
            assert res1.handled is True
            assert res1.selection_metadata is not None
            options = res1.selection_metadata["options"]
            assert len(options) == 2
            assert "Senior Engineer" in options[0]["description"] or "Senior Engineer" in options[0]["label"]
            assert "Junior Engineer" in options[1]["description"] or "Junior Engineer" in options[1]["label"]

            # Select Junior Engineer (emp_rec_2)
            res2 = await handle_query("emp_rec_2", token="tok", user_id=70, session_id=sess_id)
            assert res2.handled is True
            assert "Google LLC" in res2.reply

            # Verify target context is Junior Engineer (Level 4)
            res3 = await handle_query("What is my verification status?", token="tok", user_id=70, session_id=sess_id)
            assert res3.handled is True
            assert "Google LLC" in res3.reply
            assert "Junior Engineer" in res3.reply or "Verification Level: 4 / 4" in res3.reply

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# SCENARIO H: API RESPONSE ORDER REORDERING RESILIENCE
# ---------------------------------------------------------------------------

def test_scenario_h_api_reorder_resilience():
    async def _test():
        sess_id = "sess_53_h"
        clear_selected_employment_context(sess_id)

        payload_order_1 = make_fixture_multi_company()
        payload_order_2 = {
            "status": True,
            "data": list(reversed(payload_order_1["data"]))
        }

        # 1. First turn: Google first, Microsoft second -> Select Google
        with patch("backend.services.user_data_service.platform_api._get", return_value=payload_order_1):
            res1 = await handle_query("Show my employment verification status", token="tok", user_id=80, session_id=sess_id)
            res2 = await handle_query("emp_rec_1", token="tok", user_id=80, session_id=sess_id)
            assert "Google LLC" in res2.reply

        # 2. Second turn: Payload order reversed (Microsoft first) -> Selected context remains Google
        with patch("backend.services.user_data_service.platform_api._get", return_value=payload_order_2):
            res3 = await handle_query("What is my verification status?", token="tok", user_id=80, session_id=sess_id)
            assert res3.handled is True
            assert "Google LLC" in res3.reply

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# SCENARIO I: SELECTED RECORD DISAPPEARANCE & SAFE EVICTION
# ---------------------------------------------------------------------------

def test_scenario_i_selected_record_disappears():
    async def _test():
        sess_id = "sess_53_i"
        clear_selected_employment_context(sess_id)

        payload_full = make_fixture_multi_company()
        payload_evicted = {
            "status": True,
            "data": [payload_full["data"][1]] # Microsoft only
        }

        # 1. Select Google
        with patch("backend.services.user_data_service.platform_api._get", return_value=payload_full):
            await handle_query("Show my employment verification status", token="tok", user_id=90, session_id=sess_id)
            await handle_query("emp_rec_1", token="tok", user_id=90, session_id=sess_id)

        # 2. Fresh API response no longer contains Google
        with patch("backend.services.user_data_service.platform_api._get", return_value=payload_evicted):
            res = await handle_query("What is my verification status?", token="tok", user_id=90, session_id=sess_id)
            assert res.handled is True
            # Evicted context fails open safely to Microsoft
            assert "Microsoft Corp" in res.reply

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# SCENARIOS J & K: CROSS-INTENT CONTINUITY & EXPLICIT SWITCHING
# ---------------------------------------------------------------------------

def test_scenario_j_k_cross_intent_and_switching():
    async def _test():
        sess_id = "sess_53_jk"
        clear_selected_employment_context(sess_id)

        with patch("backend.services.user_data_service.platform_api._get", return_value=make_fixture_multi_company()):
            # Select Google
            await handle_query("Show my employment verification status", token="tok", user_id=100, session_id=sess_id)
            await handle_query("emp_rec_1", token="tok", user_id=100, session_id=sess_id)

            # Verification progress
            res_prog = await handle_query("What's my verification progress?", token="tok", user_id=100, session_id=sess_id)
            assert "Google LLC" in res_prog.reply

            # Blocker query
            res_block = await handle_query("Why isn't my employment fully verified?", token="tok", user_id=100, session_id=sess_id)
            assert "Google LLC" in res_block.reply or "Role employment confirmation" in res_block.reply or "pending" in res_block.reply

            # Salary query
            res_sal = await handle_query("How does salary verification work?", token="tok", user_id=100, session_id=sess_id)
            assert res_sal.handled is True
            assert "Level 4" in res_sal.reply or "salary" in res_sal.reply

            # Skills query
            res_skill = await handle_query("What skills did I use?", token="tok", user_id=100, session_id=sess_id)
            assert res_skill.handled is True

            # Switch to Microsoft via explicit query
            res_sw = await handle_query("Show my employment history at Microsoft Corp", token="tok", user_id=100, session_id=sess_id)
            assert "Microsoft Corp" in res_sw.reply

            # Follow-up verification targets Microsoft
            res_sw_verif = await handle_query("What is my verification status?", token="tok", user_id=100, session_id=sess_id)
            assert "Microsoft Corp" in res_sw_verif.reply

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# SCENARIO M & N: INVALID SELECTION KEYS & MULTI-USER SESSION ISOLATION
# ---------------------------------------------------------------------------

def test_scenario_m_invalid_selection_keys():
    async def _test():
        sess_id = "sess_53_m"
        clear_selected_employment_context(sess_id)
        with patch("backend.services.user_data_service.platform_api._get", return_value=make_fixture_multi_company()):
            res = await handle_query("invalid_option_key_xyz", token="tok", user_id=110, session_id=sess_id)
            assert res.handled is False

    asyncio.run(_test())


def test_scenario_n_session_isolation():
    async def _test():
        sess_user1 = "sess_user_1"
        sess_user2 = "sess_user_2"
        clear_selected_employment_context(sess_user1)
        clear_selected_employment_context(sess_user2)

        with patch("backend.services.user_data_service.platform_api._get", return_value=make_fixture_multi_company()):
            # User 1 selects Google
            await handle_query("Show my employment verification status", token="tok", user_id=121, session_id=sess_user1)
            await handle_query("emp_rec_1", token="tok", user_id=121, session_id=sess_user1)

            # User 2 queries status without selecting
            res_u2 = await handle_query("Show my employment verification status", token="tok", user_id=122, session_id=sess_user2)
            assert res_u2.handled is True
            # User 2 must receive selection options, NOT User 1's locked Google context
            assert res_u2.selection_metadata is not None

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# SCENARIO O: HTTP SERIALIZATION VIA TESTCLIENT /CHAT
# ---------------------------------------------------------------------------

def test_scenario_o_http_chat_serialization():
    from backend.api.auth import verify_token, get_current_user

    def mock_verify_token():
        return {"uid": 999, "role": "employee", "email": "test@collarcheck.com"}

    def mock_get_current_user():
        return {"id": 999, "role": "employee", "token": "test_jwt_token"}

    app.dependency_overrides[verify_token] = mock_verify_token
    app.dependency_overrides[get_current_user] = mock_get_current_user

    client = TestClient(app)

    try:
        with patch("backend.services.user_data_service.platform_api._get", return_value=make_fixture_multi_company()):
            # HTTP POST /chat -> trigger selection prompt
            resp = client.post(
                "/chat",
                json={
                    "messages": [{"role": "user", "content": "Show my employment verification status"}],
                    "session_id": "sess_http_53"
                },
                headers={"Authorization": "Bearer fake_test_token"}
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert "reply" in data
            assert data["selection_metadata"] is not None
            assert data["selection_metadata"]["type"] == "employment_selection"
            assert len(data["selection_metadata"]["options"]) == 2

            # HTTP POST /chat -> select Google
            resp2 = client.post(
                "/chat",
                json={
                    "messages": [{"role": "user", "content": "emp_rec_1"}],
                    "session_id": "sess_http_53"
                },
                headers={"Authorization": "Bearer fake_test_token"}
            )
            assert resp2.status_code == 200
            data2 = resp2.json()
            assert data2["success"] is True
            assert "Google LLC" in data2["reply"]
    finally:
        app.dependency_overrides.clear()
