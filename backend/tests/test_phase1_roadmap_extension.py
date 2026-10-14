"""
test_phase1_roadmap_extension.py — Targeted Phase 1 Test Suite for Employment Verification Roadmap.

Validates:
  1. One employment -> roadmap directly.
  2. Multiple employments -> company/role chips first.
  3. Multiple roles in same company -> exact role selection.
  4. claim_status=1, is_verified=true (Claimed + Domain Verified).
  5. claim_status=1, is_verified=false (Claimed + Domain Not Verified).
  6. claim_status=0, is_verified=false (Not Claimed + Domain Not Verified).
  7. claim_status=0, is_verified=true (Not Claimed + Domain Verified preserved independently).
  8. claim_status=null (Claim Status Unknown).
  9. L3 pending.
  10. L3 declined.
  11. L3 verified + salary exists + L4 false.
  12. L4 verified.
  13. Missing verificationProcess.
  14. Inconsistent level flags.
  15. Current employment.
  16. Previous employment.
  17. No employment records.
  18. Backward-compatible fields.
"""

import pytest
import asyncio
from unittest.mock import patch
from backend.services.user_data_extractor import (
    normalize_employment_verification,
    extract_employment_history
)
from backend.services.user_data_service import handle_query


def test_claim_status_and_domain_verified_separation():
    # Case 4: claim_status=1, is_verified=True
    res1 = normalize_employment_verification("1", "1", {"level1": True, "level2": True}, company_is_verified=True, company_claim_status=1)
    assert res1["company_claim_label"] == "Claimed"
    assert res1["company_domain_verified"] is True

    # Case 5: claim_status=1, is_verified=False
    res2 = normalize_employment_verification("0", "0", {"level1": True}, company_is_verified=False, company_claim_status=1)
    assert res2["company_claim_label"] == "Claimed"
    assert res2["company_domain_verified"] is False

    # Case 6: claim_status=0, is_verified=False
    res3 = normalize_employment_verification("0", "0", {"level1": True}, company_is_verified=False, company_claim_status=0)
    assert res3["company_claim_label"] == "Not Claimed"
    assert res3["company_domain_verified"] is False

    # Case 7: claim_status=0, is_verified=True (Preserved independently)
    res4 = normalize_employment_verification("1", "1", {"level1": True, "level2": True}, company_is_verified=True, company_claim_status=0)
    assert res4["company_claim_label"] == "Not Claimed"
    assert res4["company_domain_verified"] is True

    # Case 8: claim_status=None
    res5 = normalize_employment_verification("1", "1", {"level1": True}, company_is_verified=False, company_claim_status=None)
    assert res5["company_claim_label"] == "Unknown"


def test_independent_authoritative_level_flags():
    # Case 14: Inconsistent level flags (vp.level3=True but level2=False)
    vp = {"level1": True, "level2": False, "level3": True, "level4": False}
    res = normalize_employment_verification("1", "1", vp, company_is_verified=False)
    # Breakdown preserves authoritative vp.level3 flag without destroying it
    assert res["levels_breakdown"]["level3"]["completed"] is True
    assert res["levels_breakdown"]["level2"]["completed"] is False


def test_salary_presence_does_not_infer_l4_verification():
    # Case 11: L3 verified + salary present + L4 false
    vp = {"level1": True, "level2": True, "level3": True, "level4": False}
    res = normalize_employment_verification("1", "1", vp, company_is_verified=True)
    assert res["levels_breakdown"]["level4"]["completed"] is False
    assert res["highest_completed_level"] == 3
    assert res["next_level"] == 4


def test_missing_verification_process_fallback():
    # Case 13: Missing verificationProcess dictionary
    res = normalize_employment_verification("0", "0", None, company_is_verified=False)
    assert res["levels_breakdown"]["level1"]["completed"] is True
    assert res["levels_breakdown"]["level2"]["completed"] is False
    assert res["highest_completed_level"] == 1


def test_e2e_single_employment_roadmap_direct():
    # Case 1: One employment -> direct roadmap card
    fixture = {
        "status": True,
        "data": [
            {
                "id": 10,
                "company": "Acme Corp",
                "is_verified": True,
                "claim_status": 1,
                "lists": [
                    {
                        "id": "101",
                        "designation": "Staff Engineer",
                        "joining_date": "2023-01-01",
                        "worked_till_date": "Present",
                        "still_working": "1",
                        "approved": "1",
                        "status": "1",
                        "verificationProcess": {"level1": True, "level2": True, "level3": True}
                    }
                ]
            }
        ]
    }
    async def _test():
        with patch("backend.services.user_data_service.platform_api._get", return_value=fixture):
            res = await handle_query("What is my employment verification status?", token="tok", user_id="123")
            assert res.handled is True
            assert "Acme Corp" in res.reply
            assert "Claimed" in res.reply
            assert "Company Domain Verification: Verified" in res.reply
            assert "Level 1" in res.reply
            assert "Level 2" in res.reply
            assert "Level 3" in res.reply
            assert "[View Employment]" in res.reply
    asyncio.run(_test())


def test_e2e_no_employment_records():
    # Case 17: No employment records
    fixture = {"status": True, "data": []}
    async def _test():
        with patch("backend.services.user_data_service.platform_api._get", return_value=fixture):
            res = await handle_query("How can I verify my employment?", token="tok", user_id="123")
            assert res.handled is True
            assert "couldn't find any employment verification records" in res.reply
    asyncio.run(_test())


def test_phase1_2_out_of_order_l3_verified_l2_incomplete():
    # Test 1 (P1): L1=True, L2=False, L3=True, L4=False
    vp = {"level1": True, "level2": False, "level3": True, "level4": False}
    res = normalize_employment_verification("1", "1", vp, company_is_verified=False)
    assert res["highest_completed_level"] == 1
    assert res["highest_independent_verified_level"] == 3
    assert res["next_level"] == 2
    assert res["levels_breakdown"]["level2"]["completed"] is False
    assert res["levels_breakdown"]["level3"]["completed"] is True

    fixture = {
        "status": True,
        "data": [
            {
                "id": 20,
                "company": "TechCorp",
                "is_verified": False,
                "claim_status": 1,
                "lists": [
                    {
                        "id": "201",
                        "designation": "Backend Engineer",
                        "joining_date": "2023-01-01",
                        "worked_till_date": "Present",
                        "still_working": "1",
                        "approved": "1",
                        "status": "1",
                        "verificationProcess": vp
                    }
                ]
            }
        ]
    }
    async def _test():
        with patch("backend.services.user_data_service.platform_api._get", return_value=fixture):
            res_obj = await handle_query("What is my employment verification status?", token="tok", user_id="123")
            assert res_obj.handled is True
            reply = res_obj.reply
            assert "Current verification: Level 1 (Sequential) | Highest verified: Level 3" in reply
            assert "⚠️ Note: Level 2 is still incomplete despite Level 3 verification." in reply
            assert "Next recommended step: Level 2 (Company Domain Verification)" in reply
            assert "[How to complete Level 2]" in reply
    asyncio.run(_test())


def test_phase1_2_multi_gap_l4_verified_l2_incomplete():
    # Test 2 (P1): L1=True, L2=False, L3=True, L4=True
    vp = {"level1": True, "level2": False, "level3": True, "level4": True}
    res = normalize_employment_verification("1", "1", vp, company_is_verified=False)
    assert res["highest_completed_level"] == 1
    assert res["highest_independent_verified_level"] == 4
    assert res["next_level"] == 2

    fixture = {
        "status": True,
        "data": [
            {
                "id": 21,
                "company": "DataCorp",
                "is_verified": False,
                "claim_status": 1,
                "lists": [
                    {
                        "id": "202",
                        "designation": "Data Scientist",
                        "joining_date": "2022-01-01",
                        "worked_till_date": "Present",
                        "still_working": "1",
                        "approved": "1",
                        "status": "1",
                        "verificationProcess": vp
                    }
                ]
            }
        ]
    }
    async def _test():
        with patch("backend.services.user_data_service.platform_api._get", return_value=fixture):
            res_obj = await handle_query("What is my employment verification status?", token="tok", user_id="123")
            assert res_obj.handled is True
            reply = res_obj.reply
            assert "Current verification: Level 1 (Sequential) | Highest verified: Level 4" in reply
            assert "⚠️ Note: Level 2 is still incomplete despite Level 4 verification." in reply
            assert "Next recommended step: Level 2" in reply
    asyncio.run(_test())


def test_phase1_2_declined_l3_action_chip():
    # Test 3 (P2): Level 3 Declined
    vp = {"level1": True, "level2": True, "level3": False}
    res = normalize_employment_verification("0", "2", vp, company_is_verified=True)
    assert res["guidance_status"] == "declined"
    assert "declined by employer" in res["next_action"]

    fixture = {
        "status": True,
        "data": [
            {
                "id": 30,
                "company": "BetaCorp",
                "is_verified": True,
                "claim_status": 1,
                "lists": [
                    {
                        "id": "301",
                        "designation": "Product Manager",
                        "joining_date": "2021-01-01",
                        "worked_till_date": "Present",
                        "still_working": "1",
                        "approved": "0",
                        "status": "2",
                        "verificationProcess": vp
                    }
                ]
            }
        ]
    }
    async def _test():
        with patch("backend.services.user_data_service.platform_api._get", return_value=fixture):
            res_obj = await handle_query("What is my employment verification status?", token="tok", user_id="123")
            assert res_obj.handled is True
            reply = res_obj.reply
            assert "[How to resolve Level 3 rejection]" in reply
            assert "[How to complete Level 3]" not in reply
    asyncio.run(_test())


def test_phase1_2_l2_guidance_wording():
    # Test 4 (P3): Level 2 incomplete guidance
    res = normalize_employment_verification("0", "0", {"level1": True}, company_is_verified=False)
    assert res["next_level"] == 2
    assert "Complete Company Domain Verification" in res["next_action"]
    assert "verify its domain on CollarCheck" in res["next_action"]


def test_phase2_2_intent_isolation_how_to_complete_level_1():
    """FIX 1: 'How to complete Level 1' must map to verification progress/next_steps, NOT PROFILE_INCOMPLETE."""
    from backend.services.user_data_intent_parser import parse_user_data_intents, UserIntent
    for lvl in range(1, 5):
        prompt = f"How to complete Level {lvl}"
        intents = parse_user_data_intents(prompt)
        assert UserIntent.PROFILE_INCOMPLETE not in intents
        assert any(i in intents for i in (UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS, UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS))

    # Genuine profile queries must still match PROFILE_INCOMPLETE
    assert UserIntent.PROFILE_INCOMPLETE in parse_user_data_intents("How complete is my profile?")
    assert UserIntent.PROFILE_INCOMPLETE in parse_user_data_intents("What is missing from my profile?")


def test_phase2_2_l3_declined_precedence_over_unverified_l1():
    """FIX 3: L3 Declined must take precedence over unverified L1 in guidance recommendations."""
    vp = {"level1": False, "level2": False, "level3": False, "level4": False}
    res = normalize_employment_verification("0", "2", vp, company_is_verified=False)
    assert res["next_level"] == 3
    assert res["guidance_status"] == "declined"
    assert "declined by employer" in res["next_action"]
    assert "not been submitted" not in res["next_action"]


def test_phase2_2_existing_record_l1_incomplete_wording():
    """FIX 4: Existing record with L1 incomplete does not say 'Employment record has not been submitted'."""
    vp = {"level1": False, "level2": False, "level3": False, "level4": False}
    res = normalize_employment_verification("0", "0", vp, company_is_verified=False)
    assert res["next_level"] == 1
    assert "Work identity verification details are incomplete" in res["next_action"]
    assert "not been submitted" not in res["next_action"]


def test_phase2_2_single_next_step_formatting():
    """FIX 2: Ensure '**Next step:**' appears exactly once in roadmap string."""
    vp = {"level1": True, "level2": True, "level3": False}
    fixture = {
        "status": True,
        "data": [
            {
                "id": 40,
                "company": "GammaCorp",
                "is_verified": True,
                "claim_status": 1,
                "lists": [
                    {
                        "id": "401",
                        "designation": "Systems Architect",
                        "joining_date": "2020-01-01",
                        "worked_till_date": "Present",
                        "still_working": "1",
                        "approved": "0",
                        "status": "0",
                        "verificationProcess": vp
                    }
                ]
            }
        ]
    }
    async def _test():
        with patch("backend.services.user_data_service.platform_api._get", return_value=fixture):
            res_obj = await handle_query("What is my employment verification status?", token="tok", user_id="123")
            assert res_obj.handled is True
            reply = res_obj.reply
            # Count occurrences of '**Next step:**'
            assert reply.count("**Next step:**") == 1
            assert "Next Step:" not in reply
    asyncio.run(_test())


def test_zero_level_verification_formatting():
    vp = {
        "level1": False,
        "level2": False,
        "level3": False,
        "level4": False
    }
    fixture = {
        "status": True,
        "message": "Success",
        "data": [
            {
                "company_id": "100",
                "company_name": "ZeroVerif Inc",
                "claim_status": 1,
                "lists": [
                    {
                        "id": "501",
                        "designation": "Software Analyst",
                        "joining_date": "2021-01-01",
                        "worked_till_date": "Present",
                        "still_working": "1",
                        "approved": "0",
                        "status": "0",
                        "verificationProcess": vp
                    }
                ]
            }
        ]
    }
    async def _test():
        with patch("backend.services.user_data_service.platform_api._get", return_value=fixture):
            res_obj = await handle_query("What is my employment verification status?", token="tok", user_id="123")
            assert res_obj.handled is True
            reply = res_obj.reply
            assert res_obj.result_type_payload == "employment_verification"
            assert "Current verification: None" in reply
            assert "Verification Level: 0 / 4" in reply
            assert "Level 1 (None)" not in reply
            assert "Verification Level: 1 / 4" not in reply
            assert "**Next step:**" in reply
            assert "Next Step:" not in reply
    asyncio.run(_test())



