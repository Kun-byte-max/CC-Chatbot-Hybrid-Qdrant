"""
test_phase2_5g_response_contract.py — Comprehensive tests for Phase 2.5G response contract presentation.
Verifies clean separation between GENERIC_EMPLOYMENT_VERIFICATION and FOCUSED_EMPLOYMENT_VERIFICATION presentation modes,
preserves canonical semantic_contract in results payload, prevents duplication, and enforces non-degradation of API states.
"""

import pytest
import asyncio
from unittest.mock import patch, MagicMock

from backend.services.user_data_intent_parser import UserIntent, parse_user_data_intents
from backend.services.user_data_extractor import normalize_employment_verification
from backend.services.user_data_formatter import format_multi_intent_response
from backend.services.user_data_service import handle_query


# Real API-shaped fixture representing out-of-order state (SP HUMANS PRIVATE LIMITED case)
SP_HUMANS_FIXTURE = {
    "status": True,
    "message": "Success",
    "data": [
        {
            "id": 10,
            "company": "SP HUMANS PRIVATE LIMITED",
            "company_id": "101",
            "is_verified": False,
            "claim_status": 1,
            "lists": [
                {
                    "id": "1001",
                    "designation": "Software Developer",
                    "joining_date": "2021-05-01",
                    "worked_till_date": "Present",
                    "still_working": "1",
                    "approved": "1",
                    "status": "1",
                    "haveSalary": True,
                    "salary": "50000",
                    "work_email": None,
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


def test_1_generic_query_returns_employment_results_and_mode():
    """Test 1: Generic verification query returns employment_verification results with GENERIC mode."""
    async def _test():
        with patch("backend.services.user_data_service.platform_api._get", return_value=SP_HUMANS_FIXTURE):
            res = await handle_query("What should I do next for my verification?", token="tok", user_id=123)
            assert res.handled is True
            assert res.result_type_payload == "employment_verification"
            assert res.presentation_mode == "GENERIC_EMPLOYMENT_VERIFICATION"
            assert len(res.results_payload) == 1
            assert res.results_payload[0]["company"] == "SP HUMANS PRIVATE LIMITED"
    asyncio.run(_test())


def test_2_focused_level_3_query_mode_and_guidance():
    """Test 3 & 5: Level 3 query produces focused Level 3 guidance with FOCUSED mode."""
    async def _test():
        sess_id = "sess_g_l3"
        with patch("backend.services.user_data_service.platform_api._get", return_value=SP_HUMANS_FIXTURE):
            res1 = await handle_query("How do I complete Level 3?", token="tok", user_id=123, session_id=sess_id)
            assert res1.handled is True
            assert res1.presentation_mode == "FOCUSED_EMPLOYMENT_VERIFICATION"
            assert res1.selection_metadata is not None

            target_key = res1.selection_metadata["options"][0]["selection_key"]
            res2 = await handle_query(target_key, token="tok", user_id=123, session_id=sess_id)
            assert res2.presentation_mode == "FOCUSED_EMPLOYMENT_VERIFICATION"
            assert "Employment Verification Guidance" in res2.reply
            assert "Level 1 — Work Identity" not in res2.reply
    asyncio.run(_test())


def test_3_focused_level_1_query():
    """Test 3: Level 1 query produces focused Level 1 guidance."""
    async def _test():
        sess_id = "sess_g_l1"
        with patch("backend.services.user_data_service.platform_api._get", return_value=SP_HUMANS_FIXTURE):
            res1 = await handle_query("How do I complete Level 1?", token="tok", user_id=123, session_id=sess_id)
            assert res1.handled is True
            assert res1.presentation_mode == "FOCUSED_EMPLOYMENT_VERIFICATION"
            assert res1.selection_metadata is not None

            target_key = res1.selection_metadata["options"][0]["selection_key"]
            res2 = await handle_query(target_key, token="tok", user_id=123, session_id=sess_id)
            assert res2.presentation_mode == "FOCUSED_EMPLOYMENT_VERIFICATION"
            assert "Work Identity Guidance" in res2.reply or "Level 1" in res2.reply
    asyncio.run(_test())


def test_4_focused_level_2_query():
    """Test 4: Level 2 query produces focused Level 2 guidance."""
    async def _test():
        sess_id = "sess_g_l2"
        with patch("backend.services.user_data_service.platform_api._get", return_value=SP_HUMANS_FIXTURE):
            res1 = await handle_query("How do I complete Level 2?", token="tok", user_id=123, session_id=sess_id)
            assert res1.handled is True
            assert res1.presentation_mode == "FOCUSED_EMPLOYMENT_VERIFICATION"
            assert res1.selection_metadata is not None

            target_key = res1.selection_metadata["options"][0]["selection_key"]
            res2 = await handle_query(target_key, token="tok", user_id=123, session_id=sess_id)
            assert res2.presentation_mode == "FOCUSED_EMPLOYMENT_VERIFICATION"
            assert "Company Domain" in res2.reply or "Level 2" in res2.reply
    asyncio.run(_test())


def test_5_focused_level_4_query():
    """Test 6: Level 4 query produces focused Level 4 guidance."""
    async def _test():
        sess_id = "sess_g_l4"
        with patch("backend.services.user_data_service.platform_api._get", return_value=SP_HUMANS_FIXTURE):
            res1 = await handle_query("How do I complete Level 4?", token="tok", user_id=123, session_id=sess_id)
            assert res1.handled is True
            assert res1.presentation_mode == "FOCUSED_EMPLOYMENT_VERIFICATION"
            assert res1.selection_metadata is not None

            target_key = res1.selection_metadata["options"][0]["selection_key"]
            res2 = await handle_query(target_key, token="tok", user_id=123, session_id=sess_id)
            assert res2.presentation_mode == "FOCUSED_EMPLOYMENT_VERIFICATION"
            assert "Salary Verification Guidance" in res2.reply or "Level 4" in res2.reply
    asyncio.run(_test())


def test_6_semantic_contract_exposed_in_results():
    """Test 8: Ensure semantic_contract is accessible under verification object in results_payload."""
    async def _test():
        with patch("backend.services.user_data_service.platform_api._get", return_value=SP_HUMANS_FIXTURE):
            res = await handle_query("Show my employment verification", token="tok", user_id=123)
            assert res.handled is True
            r0 = res.results_payload[0]
            verif = r0["verification"]
            assert "semantic_contract" in verif
            contract = verif["semantic_contract"]
            assert contract["level1"]["state"] == "NOT_COMPLETED"
            assert contract["level2"]["state"] == "NOT_COMPLETED"
            assert contract["level3"]["state"] == "COMPLETED"
            assert contract["level4"]["state"] == "COMPLETED"
    asyncio.run(_test())


def test_7_sp_humans_real_api_state_invariant():
    """Test 13: SP HUMANS out-of-order L3/L4 completed state remains intact."""
    async def _test():
        with patch("backend.services.user_data_service.platform_api._get", return_value=SP_HUMANS_FIXTURE):
            res = await handle_query("Show my employment verification", token="tok", user_id=123)
            r0 = res.results_payload[0]
            levels = r0["verification"]["levels"]
            assert levels["level1"] is False
            assert levels["level2"] is False
            assert levels["level3"] is True
            assert levels["level4"] is True
    asyncio.run(_test())


def test_8_claim_status_not_equal_to_domain_verified():
    """Test 14: claim_status=1 must not imply company domain verification."""
    async def _test():
        with patch("backend.services.user_data_service.platform_api._get", return_value=SP_HUMANS_FIXTURE):
            res = await handle_query("Show my employment verification", token="tok", user_id=123)
            r0 = res.results_payload[0]
            assert r0["company_verification"]["claim_status"] == 1
            assert r0["company_verification"]["is_verified"] is False
    asyncio.run(_test())


def test_9_salary_presence_not_equal_to_l4_verified():
    """Test 15: haveSalary=True does not cause L4 verification if level4=False."""
    vp = {"level1": True, "level2": True, "level3": True, "level4": False}
    fixture = {
        "status": True,
        "data": [
            {
                "company": "SalaryCorp",
                "is_verified": True,
                "lists": [
                    {
                        "id": "1",
                        "designation": "Analyst",
                        "haveSalary": True,
                        "salary": "100000",
                        "verificationProcess": vp
                    }
                ]
            }
        ]
    }
    async def _test():
        with patch("backend.services.user_data_service.platform_api._get", return_value=fixture):
            res = await handle_query("Show my employment verification", token="tok", user_id=123)
            r0 = res.results_payload[0]
            assert r0["verification"]["levels"]["level4"] is False
            assert r0["evidence"]["salary"] is True
    asyncio.run(_test())
