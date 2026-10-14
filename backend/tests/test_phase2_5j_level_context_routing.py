"""
test_phase2_5j_level_context_routing.py — Targeted test suite for Phase 2.5J context preservation.
"""

import pytest
import asyncio
from unittest.mock import patch

from backend.services.user_data_service import handle_query
from backend.services.user_data_intent_parser import parse_user_data_intents, UserIntent


MULTI_EMP_FIXTURE = {
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
        },
        {
            "id": 20,
            "company": "InnoTech Solution",
            "company_id": "102",
            "is_verified": False,
            "claim_status": 1,
            "lists": [
                {
                    "id": "1002",
                    "designation": "Software analyst",
                    "joining_date": "2022-01-01",
                    "worked_till_date": "Present",
                    "still_working": "1",
                    "approved": "0",
                    "status": "1",
                    "haveSalary": False,
                    "salary": None,
                    "work_email": None,
                    "verificationProcess": {
                        "level1": False,
                        "level2": False,
                        "level3": False,
                        "level4": False
                    }
                }
            ]
        }
    ]
}


def test_1_generic_query_multi_emp_returns_all_roles():
    """Generic queries with multiple companies return overall progress and all roles in results_payload."""
    async def _test():
        with patch("backend.services.user_data_service.platform_api._get", return_value=MULTI_EMP_FIXTURE):
            res = await handle_query("Show my employment verification", token="tok", user_id=123)
            assert res.handled is True
            assert res.presentation_mode == "GENERIC_EMPLOYMENT_VERIFICATION"
            assert res.selection_metadata is None
            assert len(res.results_payload) == 2
            comp_names = {r["company"] for r in res.results_payload}
            assert "SP HUMANS PRIVATE LIMITED" in comp_names
            assert "InnoTech Solution" in comp_names
    asyncio.run(_test())


def test_2_focused_level_1_explicit_company_preserves_guidance():
    """Level 1 query with explicit company preserves focused Level 1 guidance."""
    async def _test():
        with patch("backend.services.user_data_service.platform_api._get", return_value=MULTI_EMP_FIXTURE):
            res = await handle_query("How do I complete Level 1 at SP HUMANS?", token="tok", user_id=123)
            assert res.handled is True
            assert res.presentation_mode == "FOCUSED_EMPLOYMENT_VERIFICATION"
            assert "Work Identity Guidance" in res.reply or "Level 1" in res.reply
            assert "Which employment record would you like to check?" not in res.reply
    asyncio.run(_test())


def test_3_focused_level_2_explicit_company_preserves_guidance():
    """Level 2 query with explicit company preserves focused Level 2 guidance."""
    async def _test():
        with patch("backend.services.user_data_service.platform_api._get", return_value=MULTI_EMP_FIXTURE):
            res = await handle_query("How do I complete Level 2 at SP HUMANS?", token="tok", user_id=123)
            assert res.handled is True
            assert res.presentation_mode == "FOCUSED_EMPLOYMENT_VERIFICATION"
            assert "Company Domain" in res.reply or "Level 2" in res.reply
            assert "Which employment record would you like to check?" not in res.reply
    asyncio.run(_test())


def test_4_focused_level_3_explicit_company_preserves_guidance():
    """Level 3 query with explicit company preserves focused Level 3 guidance."""
    async def _test():
        with patch("backend.services.user_data_service.platform_api._get", return_value=MULTI_EMP_FIXTURE):
            res = await handle_query("How do I complete Level 3 at SP HUMANS?", token="tok", user_id=123)
            assert res.handled is True
            assert res.presentation_mode == "FOCUSED_EMPLOYMENT_VERIFICATION"
            assert "Employment Verification Guidance" in res.reply
            assert "Which employment record would you like to check?" not in res.reply
    asyncio.run(_test())


def test_5_focused_level_4_explicit_company_preserves_guidance():
    """Level 4 query with explicit company preserves focused Level 4 guidance."""
    async def _test():
        with patch("backend.services.user_data_service.platform_api._get", return_value=MULTI_EMP_FIXTURE):
            res = await handle_query("How do I complete Level 4 at SP HUMANS?", token="tok", user_id=123)
            assert res.handled is True
            assert res.presentation_mode == "FOCUSED_EMPLOYMENT_VERIFICATION"
            assert "Salary Verification" in res.reply
            assert "Which employment record would you like to check?" not in res.reply
    asyncio.run(_test())


def test_6_sp_humans_real_api_state_invariant_multi_emp():
    """Verify SP HUMANS out-of-order L3/L4 completed state remains intact."""
    async def _test():
        with patch("backend.services.user_data_service.platform_api._get", return_value=MULTI_EMP_FIXTURE):
            res = await handle_query("How do I complete Level 3 at SP HUMANS?", token="tok", user_id=123)
            assert "SP HUMANS PRIVATE LIMITED" in res.reply
            assert "COMPLETED" in res.reply
    asyncio.run(_test())
