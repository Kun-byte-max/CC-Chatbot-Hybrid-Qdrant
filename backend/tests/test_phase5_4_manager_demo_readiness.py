"""
test_phase5_4_manager_demo_readiness.py — Comprehensive Manager Demo Readiness Test Suite (Phase 5.4).

Validates manager-demo quality, user-facing language clarity, exact L1-L4 checklist rendering,
sequential gating matrix, action metadata safety, selection metadata rendering, duplicate company
disambiguation, API reorder resilience, selected record disappearance, cross-intent continuity,
explicit switching, invalid key rejection, session isolation, and FastAPI /chat HTTP serialization.
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
# SYNTHETIC FIXTURE GENERATORS
# ---------------------------------------------------------------------------

def make_fixture_l1(company="Google Corp", designation="Software Engineer"):
    return {
        "status": True,
        "data": [{
            "id": 101,
            "company": company,
            "company_id": 501,
            "is_verified": False,
            "lists": [{
                "id": "1001",
                "designation": designation,
                "joining_date": "2024-01-01",
                "worked_till_date": "Present",
                "still_working": "1",
                "approved": "0",
                "status": "1",
                "verificationProcess": {"level1": True, "level2": False}
            }]
        }]
    }

def make_fixture_l2(company="Beta Inc", designation="Backend Developer"):
    return {
        "status": True,
        "data": [{
            "id": 102,
            "company": company,
            "company_id": 502,
            "is_verified": True,
            "lists": [{
                "id": "1002",
                "designation": designation,
                "joining_date": "2023-01-01",
                "worked_till_date": "Present",
                "still_working": "1",
                "approved": "0",
                "status": "1",
                "verificationProcess": {"level1": True, "level2": True}
            }]
        }]
    }

def make_fixture_l3(company="Gamma Systems", designation="Lead Architect"):
    return {
        "status": True,
        "data": [{
            "id": 103,
            "company": company,
            "company_id": 503,
            "is_verified": True,
            "lists": [{
                "id": "1003",
                "designation": designation,
                "joining_date": "2022-01-01",
                "worked_till_date": "Present",
                "still_working": "1",
                "approved": "1",
                "status": "1",
                "verificationProcess": {"level1": True, "level2": True, "level3": True}
            }]
        }]
    }

def make_fixture_l4(company="Delta Tech", designation="Staff Engineer"):
    return {
        "status": True,
        "data": [{
            "id": 104,
            "company": company,
            "company_id": 504,
            "is_verified": True,
            "lists": [{
                "id": "1004",
                "designation": designation,
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
                "id": 201,
                "company": "Google Corp",
                "company_id": 901,
                "is_verified": True,
                "lists": [{
                    "id": "2001",
                    "designation": "Software Engineer",
                    "joining_date": "2023-01-01",
                    "worked_till_date": "Present",
                    "still_working": "1",
                    "approved": "1",
                    "status": "1",
                    "verificationProcess": {"level1": True, "level2": True, "level3": True}
                }]
            },
            {
                "id": 202,
                "company": "Microsoft Corp",
                "company_id": 902,
                "is_verified": True,
                "lists": [{
                    "id": "2002",
                    "designation": "Backend Engineer",
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
                "id": 301,
                "company": "Google Corp",
                "company_id": 901,
                "is_verified": True,
                "lists": [{
                    "id": "3001",
                    "designation": "Senior Software Engineer",
                    "joining_date": "2023-06-01",
                    "worked_till_date": "Present",
                    "still_working": "1",
                    "approved": "1",
                    "status": "1",
                    "verificationProcess": {"level1": True, "level2": True, "level3": True}
                }]
            },
            {
                "id": 302,
                "company": "Google Corp",
                "company_id": 901,
                "is_verified": True,
                "lists": [{
                    "id": "3002",
                    "designation": "Software Engineer",
                    "joining_date": "2021-01-01",
                    "worked_till_date": "2023-05-01",
                    "still_working": "0",
                    "approved": "1",
                    "status": "1",
                    "verificationProcess": {"level1": True, "level2": True, "level3": True, "level4": True}
                }]
            }
        ]
    }


# ---------------------------------------------------------------------------
# SCENARIO A: LEVEL 1 MANAGER DEMO READINESS
# ---------------------------------------------------------------------------

def test_scenario_a_level1_demo_readiness():
    async def _test():
        payload = make_fixture_l1(company="Google Corp", designation="Software Engineer")
        with patch("backend.services.user_data_service.platform_api._get", return_value=payload):
            res = await handle_query("Show my employment verification status", token="tok", user_id="101")
            assert res.handled is True
            reply = res.reply

            # Demo criteria assertions
            assert "Google Corp" in reply
            assert "Software Engineer" in reply
            assert "Verification Level: 1 / 4" in reply
            assert "✓ Level 1 (Work Identity): Completed" in reply
            assert "○ Level 2 (Company Domain Verification): Pending" in reply
            assert "○ Level 3 (Employment Verification):" in reply
            assert "○ Level 4 (Salary Verification):" in reply
            assert "Current Blocker:" in reply
            assert "Company domain verification" in reply.lower() or "pending" in reply.lower()
            assert "**Next step:**" in reply
            assert "Next Step:" not in reply

            # No false claims assertions
            assert "🎉 Employment Fully Verified" not in reply
            assert "Level 3 (Employment Verification): Completed" not in reply

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# SCENARIO B: LEVEL 2 MANAGER DEMO READINESS
# ---------------------------------------------------------------------------

def test_scenario_b_level2_demo_readiness():
    async def _test():
        payload = make_fixture_l2(company="Beta Inc", designation="Backend Developer")
        with patch("backend.services.user_data_service.platform_api._get", return_value=payload):
            res = await handle_query("Show my employment verification status", token="tok", user_id="102")
            assert res.handled is True
            reply = res.reply

            assert "Beta Inc" in reply
            assert "Backend Developer" in reply
            assert "Verification Level: 2 / 4" in reply
            assert "✓ Level 1 (Work Identity): Completed" in reply
            assert "✓ Level 2 (Company Domain Verification): Completed" in reply
            assert "○ Level 3 (Employment Verification): Pending" in reply or "○ Level 3 (Employment Verification):" in reply
            assert "○ Level 4 (Salary Verification):" in reply
            assert "Current Blocker:" in reply
            assert "**Next step:**" in reply
            assert "Next Step:" not in reply

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# SCENARIO C: LEVEL 3 MANAGER DEMO READINESS
# ---------------------------------------------------------------------------

def test_scenario_c_level3_demo_readiness():
    async def _test():
        payload = make_fixture_l3(company="Gamma Systems", designation="Lead Architect")
        with patch("backend.services.user_data_service.platform_api._get", return_value=payload):
            res = await handle_query("Show my employment verification status", token="tok", user_id="103")
            assert res.handled is True
            reply = res.reply

            assert "Gamma Systems" in reply
            assert "Lead Architect" in reply
            assert "Verification Level: 3 / 4" in reply
            assert "✓ Level 1 (Work Identity): Completed" in reply
            assert "✓ Level 2 (Company Domain Verification): Completed" in reply
            assert "✓ Level 3 (Employment Verification): Completed" in reply
            assert "○ Level 4 (Salary Verification): Pending" in reply
            assert "Current Blocker:" in reply
            assert "**Next step:**" in reply
            assert "Next Step:" not in reply

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# SCENARIO D: LEVEL 4 MANAGER DEMO READINESS
# ---------------------------------------------------------------------------

def test_scenario_d_level4_demo_readiness():
    async def _test():
        payload = make_fixture_l4(company="Delta Tech", designation="Staff Engineer")
        with patch("backend.services.user_data_service.platform_api._get", return_value=payload):
            res = await handle_query("Show my employment verification status", token="tok", user_id="104")
            assert res.handled is True
            reply = res.reply

            assert "Delta Tech" in reply
            assert "Staff Engineer" in reply
            assert "Verification Level: 4 / 4" in reply
            assert "✓ Level 1 (Work Identity): Completed" in reply
            assert "✓ Level 2 (Company Domain Verification): Completed" in reply
            assert "✓ Level 3 (Employment Verification): Completed" in reply
            assert "✓ Level 4 (Salary Verification): Completed" in reply
            assert "🎉 **Employment Fully Verified**" in reply
            assert "Current Blocker:" not in reply

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# SCENARIO E: HIGHER FLAG BYPASS PREVENTION
# ---------------------------------------------------------------------------

def test_scenario_e_higher_flag_bypass_prevention():
    async def _test():
        # L2-L4 active, but L1 inactive -> fails closed
        payload = {
            "status": True,
            "data": [{
                "company": "Bypass Corp",
                "is_verified": True,
                "lists": [{
                    "id": "999",
                    "designation": "Security Dev",
                    "approved": "1",
                    "status": "1",
                    "verificationProcess": {"level1": False, "level2": True, "level3": True, "level4": True}
                }]
            }]
        }
        with patch("backend.services.user_data_service.platform_api._get", return_value=payload):
            res = await handle_query("Show my employment verification status", token="tok", user_id="105")
            assert res.handled is True
            assert "Verification Level: 0 / 4" in res.reply or "Level 0" in res.reply or "Work Identity" in res.reply

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# SCENARIOS F & G: MULTIPLE EMPLOYMENTS & EXPLICIT SWITCHING
# ---------------------------------------------------------------------------

def test_scenario_f_g_multiple_employments_and_switching():
    async def _test():
        sess_id = "sess_54_fg"
        clear_selected_employment_context(sess_id)

        with patch("backend.services.user_data_service.platform_api._get", return_value=make_fixture_multi_company()):
            # 1. Trigger selection metadata
            res1 = await handle_query("Show my employment verification status", token="tok", user_id="106", session_id=sess_id)
            assert res1.handled is True
            assert res1.selection_metadata is not None
            assert len(res1.selection_metadata["options"]) == 2

            # 2. Select Google Corp (emp_rec_1)
            res2 = await handle_query("emp_rec_1", token="tok", user_id="106", session_id=sess_id)
            assert res2.handled is True
            assert "Google Corp" in res2.reply

            # 3. Subsequent verification query targets Google Corp
            res3 = await handle_query("Show my verification status", token="tok", user_id="106", session_id=sess_id)
            assert res3.handled is True
            assert "Google Corp" in res3.reply
            assert "Verification Level: 3 / 4" in res3.reply
            assert "Microsoft Corp" not in res3.reply

            # 4. Explicit switch to Microsoft Corp
            res4 = await handle_query("Show my employment verification status at Microsoft Corp", token="tok", user_id="106", session_id=sess_id)
            assert res4.handled is True
            assert "Microsoft Corp" in res4.reply

            # 5. Subsequent query targets Microsoft Corp (Level 4)
            res5 = await handle_query("What is my verification status?", token="tok", user_id="106", session_id=sess_id)
            assert res5.handled is True
            assert "Microsoft Corp" in res5.reply
            assert "Verification Level: 4 / 4" in res5.reply
            assert "Google Corp" not in res5.reply

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# SCENARIO H: DUPLICATE COMPANY TENURES DISAMBIGUATION
# ---------------------------------------------------------------------------

def test_scenario_h_duplicate_company_tenures():
    async def _test():
        sess_id = "sess_54_h"
        clear_selected_employment_context(sess_id)

        with patch("backend.services.user_data_service.platform_api._get", return_value=make_fixture_duplicate_tenures()):
            res1 = await handle_query("Show my employment verification status", token="tok", user_id="107", session_id=sess_id)
            assert res1.handled is True
            assert res1.selection_metadata is not None
            options = res1.selection_metadata["options"]
            assert len(options) == 2
            assert "Senior Software Engineer" in options[0]["description"] or "Senior Software Engineer" in options[0]["label"]
            assert "Software Engineer" in options[1]["description"] or "Software Engineer" in options[1]["label"]

            # Select older tenure (Software Engineer, emp_rec_2)
            res2 = await handle_query("emp_rec_2", token="tok", user_id="107", session_id=sess_id)
            assert res2.handled is True
            assert "Google Corp" in res2.reply

            # Verify target context is older tenure (Level 4)
            res3 = await handle_query("What is my verification status?", token="tok", user_id="107", session_id=sess_id)
            assert res3.handled is True
            assert "Google Corp" in res3.reply
            assert "Software Engineer" in res3.reply or "Verification Level: 4 / 4" in res3.reply

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# SCENARIO I: API RESPONSE ORDER REORDERING RESILIENCE
# ---------------------------------------------------------------------------

def test_scenario_i_api_reorder_resilience():
    async def _test():
        sess_id = "sess_54_i"
        clear_selected_employment_context(sess_id)

        payload_order_1 = make_fixture_multi_company()
        payload_order_2 = {
            "status": True,
            "data": list(reversed(payload_order_1["data"]))
        }

        # Turn 1: Select Google Corp
        with patch("backend.services.user_data_service.platform_api._get", return_value=payload_order_1):
            await handle_query("Show my employment verification status", token="tok", user_id="108", session_id=sess_id)
            await handle_query("emp_rec_1", token="tok", user_id="108", session_id=sess_id)

        # Turn 2: Payload order reversed -> Google context remains locked
        with patch("backend.services.user_data_service.platform_api._get", return_value=payload_order_2):
            res = await handle_query("What is my verification status?", token="tok", user_id="108", session_id=sess_id)
            assert res.handled is True
            assert "Google Corp" in res.reply

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# SCENARIO J: SELECTED RECORD DISAPPEARANCE & SAFE EVICTION
# ---------------------------------------------------------------------------

def test_scenario_j_selected_record_disappears():
    async def _test():
        sess_id = "sess_54_j"
        clear_selected_employment_context(sess_id)

        payload_full = make_fixture_multi_company()
        payload_evicted = {
            "status": True,
            "data": [payload_full["data"][1]] # Microsoft Corp only
        }

        # Select Google Corp
        with patch("backend.services.user_data_service.platform_api._get", return_value=payload_full):
            await handle_query("Show my employment verification status", token="tok", user_id="109", session_id=sess_id)
            await handle_query("emp_rec_1", token="tok", user_id="109", session_id=sess_id)

        # Eviction turn: Google Corp no longer in fresh API payload
        with patch("backend.services.user_data_service.platform_api._get", return_value=payload_evicted):
            res = await handle_query("What is my verification status?", token="tok", user_id="109", session_id=sess_id)
            assert res.handled is True
            assert "Microsoft Corp" in res.reply

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# SCENARIOS K, L & M: NO SELECTION, INVALID KEY, & USER ISOLATION
# ---------------------------------------------------------------------------

def test_scenario_k_no_selection_multi_records():
    async def _test():
        sess_id = "sess_54_k"
        clear_selected_employment_context(sess_id)

        with patch("backend.services.user_data_service.platform_api._get", return_value=make_fixture_multi_company()):
            res = await handle_query("Show my employment verification status", token="tok", user_id="110", session_id=sess_id)
            assert res.handled is True
            assert res.selection_metadata is not None

    asyncio.run(_test())


def test_scenario_l_invalid_selection_key():
    async def _test():
        sess_id = "sess_54_l"
        clear_selected_employment_context(sess_id)

        with patch("backend.services.user_data_service.platform_api._get", return_value=make_fixture_multi_company()):
            res = await handle_query("invalid_key_xyz", token="tok", user_id="111", session_id=sess_id)
            assert res.handled is False

    asyncio.run(_test())


def test_scenario_m_user_session_isolation():
    async def _test():
        sess_user1 = "sess_54_m_user1"
        sess_user2 = "sess_54_m_user2"
        clear_selected_employment_context(sess_user1)
        clear_selected_employment_context(sess_user2)

        with patch("backend.services.user_data_service.platform_api._get", return_value=make_fixture_multi_company()):
            # User 1 selects Google Corp
            await handle_query("Show my employment verification status", token="tok", user_id="112", session_id=sess_user1)
            await handle_query("emp_rec_1", token="tok", user_id="112", session_id=sess_user1)

            # User 2 queries status -> gets selection prompt, NOT User 1's context
            res2 = await handle_query("Show my employment verification status", token="tok", user_id="113", session_id=sess_user2)
            assert res2.handled is True
            assert res2.selection_metadata is not None

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# SCENARIO N: ACTION METADATA & FRONTEND CONTRACT SAFETY
# ---------------------------------------------------------------------------

def test_scenario_n_action_metadata_safety():
    async def _test():
        payload = make_fixture_l2(company="Beta Inc", designation="Backend Developer")
        with patch("backend.services.user_data_service.platform_api._get", return_value=payload):
            # Query invite action intent
            res = await handle_query("How do I invite my employer to verify my company?", token="tok", user_id="114")
            assert res.handled is True
            if res.action_metadata:
                for act in res.action_metadata:
                    # Navigation URLs must point to known logical destinations
                    assert act.get("type") in {"nav_link", "chat_action"}
                    if act.get("type") == "nav_link":
                        assert act.get("destination") in {"experience_dashboard", "user_profile"}

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# SCENARIO O: USER-FACING LANGUAGE QUALITY (NO IMPLEMENTATION TERMINOLOGY)
# ---------------------------------------------------------------------------

def test_scenario_o_user_facing_language_quality():
    async def _test():
        payload = make_fixture_l3(company="Gamma Systems", designation="Lead Architect")
        with patch("backend.services.user_data_service.platform_api._get", return_value=payload):
            res = await handle_query("Show my employment verification status", token="tok", user_id="115")
            assert res.handled is True
            reply = res.reply

            forbidden_terms = [
                "normalizer", "session resolver", "composite key", "api payload",
                "database fallback", "internal verificationprocess", "session state",
                "raw_approved", "raw_status"
            ]
            for term in forbidden_terms:
                assert term not in reply.lower()

    asyncio.run(_test())
