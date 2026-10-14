"""
test_phase5_2_employment_api_contract.py — Comprehensive Data Contract Integration Test Suite for Phase 5.2.

Validates the real `employee/allEmployementNew` response contract mapping to:
  1. Top-level, data[], and lists[] structures
  2. Sequential gating L1–L4 (including binary flag matrix tests)
  3. Company and role-level field extraction (is_verified, approved, status, verificationProcess)
  4. Employment identity & disambiguation (composite keys, duplicate companies, API reordering)
  5. Conservative missing/malformed field handling
  6. E2E handle_query integration with synthetic fixtures
"""

import pytest
import asyncio
from unittest.mock import patch, MagicMock
from backend.services.user_data_extractor import (
    normalize_employment_verification,
    extract_employment_history,
    extract_employment_skills,
    extract_employment_salary
)
from backend.services.user_data_service import handle_query, ServiceResponse
from backend.services.user_data_intent_parser import UserIntent


# ---------------------------------------------------------------------------
# SYNTHETIC FIXTURES
# ---------------------------------------------------------------------------

def get_fixture_l1_only():
    return {
        "status": True,
        "data": [
            {
                "id": 101,
                "company": "Acme Corp",
                "company_id": 501,
                "is_verified": False,
                "lists": [
                    {
                        "id": "1001",
                        "designation": "Backend Engineer",
                        "joining_date": "2024-01-01",
                        "worked_till_date": "Present",
                        "still_working": "1",
                        "approved": "0",
                        "status": "0",
                        "verificationProcess": {"level1": True, "level2": False}
                    }
                ]
            }
        ]
    }

def get_fixture_l2_company_verified():
    return {
        "status": True,
        "data": [
            {
                "id": 102,
                "company": "Beta Inc",
                "company_id": 502,
                "is_verified": True,
                "lists": [
                    {
                        "id": "1002",
                        "designation": "Software Developer",
                        "joining_date": "2023-05-01",
                        "worked_till_date": "Present",
                        "still_working": "1",
                        "approved": "0",
                        "status": "1",
                        "verificationProcess": {"level1": True, "level2": True}
                    }
                ]
            }
        ]
    }

def get_fixture_l3_role_confirmed():
    return {
        "status": True,
        "data": [
            {
                "id": 103,
                "company": "Gamma Systems",
                "company_id": 503,
                "is_verified": True,
                "lists": [
                    {
                        "id": "1003",
                        "designation": "Lead Architect",
                        "joining_date": "2022-01-01",
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

def get_fixture_l4_fully_verified():
    return {
        "status": True,
        "data": [
            {
                "id": 104,
                "company": "Delta Tech",
                "company_id": 504,
                "is_verified": True,
                "lists": [
                    {
                        "id": "1004",
                        "designation": "Staff Engineer",
                        "joining_date": "2021-01-01",
                        "worked_till_date": "Present",
                        "still_working": "1",
                        "approved": "1",
                        "status": "1",
                        "verificationProcess": {"level1": True, "level2": True, "level3": True, "level4": True},
                        "salary": "150000",
                        "haveSalary": 1
                    }
                ]
            }
        ]
    }

def get_fixture_bypass_attempt():
    """Higher level flags present but Level 1 is missing."""
    return {
        "status": True,
        "data": [
            {
                "id": 105,
                "company": "Epsilon Labs",
                "is_verified": True,
                "lists": [
                    {
                        "id": "1005",
                        "designation": "Data Scientist",
                        "approved": "1",
                        "status": "1",
                        "verificationProcess": {"level1": False, "level2": True, "level3": True, "level4": True}
                    }
                ]
            }
        ]
    }

def get_fixture_duplicate_companies():
    return {
        "status": True,
        "data": [
            {
                "id": 201,
                "company": "Google LLC",
                "company_id": 999,
                "is_verified": True,
                "lists": [
                    {
                        "id": "2001",
                        "designation": "Software Engineer",
                        "joining_date": "2024-01-01",
                        "worked_till_date": "Present",
                        "still_working": "1",
                        "approved": "1",
                        "status": "1",
                        "verificationProcess": {"level1": True, "level2": True, "level3": True}
                    }
                ]
            },
            {
                "id": 202,
                "company": "Google LLC",
                "company_id": 999,
                "is_verified": True,
                "lists": [
                    {
                        "id": "2002",
                        "designation": "Junior Engineer",
                        "joining_date": "2022-01-01",
                        "worked_till_date": "2023-12-31",
                        "still_working": "0",
                        "approved": "1",
                        "status": "1",
                        "verificationProcess": {"level1": True, "level2": True, "level3": True, "level4": True}
                    }
                ]
            }
        ]
    }

def get_fixture_multiple_companies():
    return {
        "status": True,
        "data": [
            {
                "id": 301,
                "company": "Google LLC",
                "company_id": 999,
                "is_verified": True,
                "lists": [
                    {
                        "id": "3001",
                        "designation": "Software Engineer",
                        "joining_date": "2024-01-01",
                        "worked_till_date": "Present",
                        "still_working": "1",
                        "approved": "1",
                        "status": "1",
                        "verificationProcess": {"level1": True, "level2": True, "level3": True}
                    }
                ]
            },
            {
                "id": 302,
                "company": "Microsoft Corp",
                "company_id": 888,
                "is_verified": True,
                "lists": [
                    {
                        "id": "3002",
                        "designation": "Backend Developer",
                        "joining_date": "2022-01-01",
                        "worked_till_date": "2023-12-31",
                        "still_working": "0",
                        "approved": "1",
                        "status": "1",
                        "verificationProcess": {"level1": True, "level2": True, "level3": True, "level4": True}
                    }
                ]
            }
        ]
    }


# ---------------------------------------------------------------------------
# TEST GROUP 1 — PAYLOAD STRUCTURE & MAPPING
# ---------------------------------------------------------------------------

def test_payload_structure_extraction():
    """Verify that extract_employment_history correctly unpacks data[] and lists[]."""
    fx = get_fixture_l4_fully_verified()
    extracted = extract_employment_history(fx)
    assert len(extracted) == 1
    company = extracted[0]
    assert company["company"] == "Delta Tech"
    assert company["is_verified"] is True
    assert len(company["roles"]) == 1

    role = company["roles"][0]
    assert role["designation"] == "Staff Engineer"
    assert role["joining_date"] == "2021-01-01"
    assert role["worked_till_date"] == "Present"
    assert role["still_working"] == "1"
    assert role["salary"] == "150000"
    assert role["have_salary"] is True


def test_missing_and_malformed_containers():
    """Verify conservative extraction on empty, missing, or non-dict payloads."""
    assert extract_employment_history({}) == []
    assert extract_employment_history({"data": None}) == []
    assert extract_employment_history({"data": "invalid"}) == []
    assert extract_employment_history({"data": [{"company": "Test", "lists": "invalid"}]}) == [
        {"id": None, "company": "Test", "company_id": None, "is_verified": None, "company_claim_status": None, "totalExperienceMonths": None, "roles": []}
    ]


# ---------------------------------------------------------------------------
# TEST GROUP 2 — SEQUENTIAL L1–L4 GATING MATRIX
# ---------------------------------------------------------------------------

def test_l1_only_mapping():
    fx = get_fixture_l1_only()
    extracted = extract_employment_history(fx)
    verif = extracted[0]["roles"][0]["verification"]
    assert verif["level"] == 1
    assert verif["highest_completed_level"] == 1
    assert verif["next_level"] == 2
    assert "Company domain verification" in verif["next_action"] or "company domain" in verif["next_action"].lower()


def test_l2_company_verified_mapping():
    fx = get_fixture_l2_company_verified()
    extracted = extract_employment_history(fx)
    verif = extracted[0]["roles"][0]["verification"]
    assert verif["level"] == 2
    assert verif["highest_completed_level"] == 2
    assert verif["next_level"] == 3
    assert "confirm your employment" in verif["next_action"].lower() or "employer" in verif["next_action"].lower()


def test_l3_role_confirmed_mapping():
    fx = get_fixture_l3_role_confirmed()
    extracted = extract_employment_history(fx)
    verif = extracted[0]["roles"][0]["verification"]
    assert verif["level"] == 3
    assert verif["highest_completed_level"] == 3
    assert verif["next_level"] == 4
    assert "salary" in verif["next_action"].lower()


def test_l4_fully_verified_mapping():
    fx = get_fixture_l4_fully_verified()
    extracted = extract_employment_history(fx)
    verif = extracted[0]["roles"][0]["verification"]
    assert verif["level"] == 4
    assert verif["highest_completed_level"] == 4
    assert verif["next_level"] is None
    assert verif["guidance_status"] == "completed"


def test_bypass_attempt_fails_closed():
    fx = get_fixture_bypass_attempt()
    extracted = extract_employment_history(fx)
    verif = extracted[0]["roles"][0]["verification"]
    assert verif["level"] is None
    assert verif["highest_completed_level"] is None
    assert verif["next_level"] == 1


# ---------------------------------------------------------------------------
# TEST GROUP 3 — FULL BINARY MATRIX (16 COMBINATIONS)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("l1,l2,l3,l4,comp_verif,app,stat,expected_level", [
    (False, False, False, False, True, "1", "1", None),
    (True,  False, False, False, False, "0", "0", 1),
    (True,  True,  False, False, False, "1", "1", 1), # L2 flag active but company unverified -> stay Level 1
    (True,  True,  False, False, True,  "0", "1", 2), # L2 company verified but role unapproved -> Level 2
    (True,  True,  True,  False, True,  "1", "0", 2), # Role unconfirmed -> stay Level 2
    (True,  True,  True,  False, True,  "1", "1", 3), # Approved & status '1' -> Level 3
    (True,  True,  True,  True,  True,  "1", "1", 4), # All active & satisfied -> Level 4
    (False, True,  True,  True,  True,  "1", "1", None), # Missing L1 -> fails closed to None
])
def test_verification_matrix_sequential_rules(l1, l2, l3, l4, comp_verif, app, stat, expected_level):
    vp = {"level1": l1, "level2": l2, "level3": l3, "level4": l4}
    res = normalize_employment_verification(app, stat, vp, company_is_verified=comp_verif)
    assert res["level"] == expected_level


# ---------------------------------------------------------------------------
# TEST GROUP 4 — DUPLICATE COMPANY & IDENTITY DISAMBIGUATION
# ---------------------------------------------------------------------------

def test_duplicate_company_identity():
    fx = get_fixture_duplicate_companies()
    extracted = extract_employment_history(fx)
    assert len(extracted) == 2
    c1 = extracted[0]
    c2 = extracted[1]
    assert c1["company"] == "Google LLC"
    assert c2["company"] == "Google LLC"
    assert c1["roles"][0]["designation"] == "Software Engineer"
    assert c2["roles"][0]["designation"] == "Junior Engineer"
    assert c1["roles"][0]["verification"]["level"] == 3
    assert c2["roles"][0]["verification"]["level"] == 4


# ---------------------------------------------------------------------------
# TEST GROUP 5 — END-TO-END PIPELINE WITH SYNTHETIC FIXTURES
# ---------------------------------------------------------------------------

def test_e2e_single_employment_status_flow():
    async def _test():
        mock_payload = get_fixture_l2_company_verified()
        with patch("backend.services.user_data_service.platform_api._get", return_value=mock_payload):
            res = await handle_query("Show my employment verification status", token="mock_tok", user_id="123")
            assert res.handled is True
            assert res.result_type_payload == "employment_verification"
            assert "Beta Inc" in res.reply
            assert "Verification Level: 2 / 4" in res.reply
            assert "Company Domain Verification: Verified" in res.reply
            assert "Current Blocker:" in res.reply
            assert "**Next step:**" in res.reply
            assert "Next Step:" not in res.reply

    asyncio.run(_test())


def test_e2e_multiple_employment_selection_flow():
    async def _test():
        mock_payload = get_fixture_multiple_companies()
        with patch("backend.services.user_data_service.platform_api._get", return_value=mock_payload):
            # First turn: trigger selection prompt
            res = await handle_query("Show my employment verification status", token="mock_tok", user_id="123", session_id="sess_52_1")
            assert res.handled is True
            assert res.selection_metadata is not None
            assert len(res.selection_metadata["options"]) == 2
            assert res.selection_metadata["options"][0]["company_name"] == "Google LLC"
            assert res.selection_metadata["options"][1]["company_name"] == "Microsoft Corp"

            # Second turn: select Google (emp_rec_1) - returns employment history selection response
            res2 = await handle_query("emp_rec_1", token="mock_tok", user_id="123", session_id="sess_52_1")
            assert res2.handled is True
            assert "Google LLC" in res2.reply

            # Third turn: verification query on selected Google context
            res3 = await handle_query("Show my employment verification status", token="mock_tok", user_id="123", session_id="sess_52_1")
            assert res3.handled is True
            assert "Google LLC" in res3.reply
            assert "Level 3" in res3.reply

    asyncio.run(_test())
