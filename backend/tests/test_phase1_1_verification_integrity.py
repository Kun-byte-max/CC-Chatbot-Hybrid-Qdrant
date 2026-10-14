"""
test_phase1_1_verification_integrity.py — Test suite for Phase 1.1 employment verification integrity and contract fixes.
"""

import pytest
from unittest.mock import patch
from backend.services.user_data_extractor import (
    normalize_employment_verification,
    extract_employment_history
)
from backend.services.user_data_service import (
    _get_employment_db_fallback,
    fetch_endpoint_data,
    handle_query
)
from backend.services.user_data_intent_parser import UserIntent
from backend.services.user_data_formatter import format_single_intent


def test_scenario_1_company_unknown_role_verified():
    """Scenario 1: Company is_verified is None (unknown), while individual role is verified."""
    raw_api_payload = {
        "status": True,
        "data": [
            {
                "id": 101,
                "company": "Acme Corp",
                "company_id": 501,
                "is_verified": None,
                "lists": [
                    {
                        "id": 1001,
                        "designation": "Backend Engineer",
                        "approved": "1",
                        "status": "1",
                        "verificationProcess": {"level1": True, "level2": True, "level3": True}
                    }
                ]
            }
        ]
    }
    extracted = extract_employment_history(raw_api_payload)
    assert len(extracted) == 1
    company = extracted[0]
    assert company["company"] == "Acme Corp"
    assert company["is_verified"] is None

    role = company["roles"][0]
    assert role["designation"] == "Backend Engineer"
    assert role["approved"] == "1"
    assert role["status"] == "1"
    assert role["status_label"] == "Verified"
    assert role["verification"]["is_verified"] is True
    assert role["verification"]["level"] == 3


def test_scenario_2_company_unknown_role_not_verified():
    """Scenario 2: Company is_verified is None (unknown), while individual role is not verified."""
    raw_api_payload = {
        "status": True,
        "data": [
            {
                "id": 102,
                "company": "Beta Systems",
                "company_id": 502,
                "is_verified": None,
                "lists": [
                    {
                        "id": 1002,
                        "designation": "Frontend Dev",
                        "approved": "0",
                        "status": "1",
                        "verificationProcess": {}
                    }
                ]
            }
        ]
    }
    extracted = extract_employment_history(raw_api_payload)
    company = extracted[0]
    assert company["is_verified"] is None

    role = company["roles"][0]
    assert role["status_label"] == "Not Verified"
    assert role["verification"]["is_verified"] is False


def test_scenario_3_company_unknown_mixed_roles():
    """Scenario 3: Company is_verified is None (unknown), with multiple roles in mixed verification states."""
    raw_api_payload = {
        "status": True,
        "data": [
            {
                "id": 103,
                "company": "Gamma Tech",
                "company_id": 503,
                "is_verified": None,
                "lists": [
                    {"id": 2001, "designation": "DevOps", "approved": "1", "status": "1"},
                    {"id": 2002, "designation": "QA", "approved": "0", "status": "1"}
                ]
            }
        ]
    }
    extracted = extract_employment_history(raw_api_payload)
    company = extracted[0]
    assert company["is_verified"] is None
    assert len(company["roles"]) == 2
    assert company["roles"][0]["verification"]["is_verified"] is True
    assert company["roles"][1]["verification"]["is_verified"] is False


def test_scenario_4_company_false_role_verified():
    """Scenario 4: Company is_verified is False (unverified corporate claim), but individual role is verified."""
    raw_api_payload = {
        "status": True,
        "data": [
            {
                "id": 104,
                "company": "Unverified Startup LLC",
                "company_id": 504,
                "is_verified": 0,
                "lists": [
                    {
                        "id": 3001,
                        "designation": "Lead Engineer",
                        "approved": "1",
                        "status": "1"
                    }
                ]
            }
        ]
    }
    extracted = extract_employment_history(raw_api_payload)
    company = extracted[0]
    assert company["is_verified"] == 0  # Preserved company unverified status

    role = company["roles"][0]
    assert role["verification"]["is_verified"] is True  # Independent role verification


def test_scenario_5_company_true_role_not_verified():
    """Scenario 5: Company is_verified is True (verified enterprise), but individual candidate role is not verified."""
    raw_api_payload = {
        "status": True,
        "data": [
            {
                "id": 105,
                "company": "Global Enterprise Inc",
                "company_id": 505,
                "is_verified": 1,
                "lists": [
                    {
                        "id": 4001,
                        "designation": "Contractor",
                        "approved": "0",
                        "status": "1"
                    }
                ]
            }
        ]
    }
    extracted = extract_employment_history(raw_api_payload)
    company = extracted[0]
    assert company["is_verified"] == 1  # Verified company

    role = company["roles"][0]
    assert role["verification"]["is_verified"] is False  # Candidate role not verified


def test_db_fallback_does_not_derive_company_verification():
    """Verify DB fallback returns is_verified=None for company level and does NOT derive it from roles."""
    mock_db_rows = [
        {
            "id": 77,
            "company_id": "901",
            "company_name": "DB Company",
            "company_label": "DB Company Ltd",
            "designation": "Software Dev",
            "joining_date": "2022-01-01",
            "worked_till_date": "Present",
            "still_working": "1",
            "salary": 80000,
            "salary_inhand": 70000,
            "salary_mode": "Monthly",
            "approved": "1",
            "status": "1"
        }
    ]

    with patch("backend.repositories.db.get_db") as mock_get_db:
        mock_conn = mock_get_db.return_value
        mock_cursor = mock_conn.cursor.return_value
        mock_cursor.fetchall.return_value = mock_db_rows

        res = _get_employment_db_fallback(user_id=12345)

        assert res["status"] is True
        assert len(res["data"]) == 1
        comp = res["data"][0]
        assert comp["company"] == "DB Company Ltd"
        assert comp["is_verified"] is None  # Must NOT be True/False, must be None (unknown)
        assert len(comp["lists"]) == 1
        assert comp["lists"][0]["is_verified"] is True  # Role verification preserved independently


def test_handle_query_preserves_company_is_verified_and_employment_type():
    """Verify handle_query includes company_is_verified and preserves None for missing employment_type."""
    raw_api_payload = {
        "status": True,
        "data": [
            {
                "id": 201,
                "company": "OmniCorp",
                "company_id": 801,
                "is_verified": 1,
                "lists": [
                    {
                        "id": 5001,
                        "designation": "Architect",
                        "employment_type": None,  # Missing employment type
                        "approved": "1",
                        "status": "1"
                    }
                ]
            }
        ]
    }

    with patch("backend.services.user_data_service.platform_api._get", return_value=raw_api_payload):
        import asyncio
        async def _test():
            res = await handle_query("show my employment history", token="tok", user_id="123")
            assert res.handled is True
            assert len(res.results_payload) == 1
            item = res.results_payload[0]
            assert item["company_name"] == "OmniCorp"
            assert item["company_is_verified"] == 1  # Preserved company verification flag
            assert item["employment_type"] is None   # No fabricated "Full Time" default
        asyncio.run(_test())


def test_fetch_endpoint_data_graceful_db_error_handling():
    """Verify fetch_endpoint_data returns safe status=False response when external API fails and DB raises exception."""
    with patch("backend.services.user_data_service.platform_api._get", return_value={"status": False, "message": "API Error"}), \
         patch("backend.services.user_data_service._get_employment_db_fallback", side_effect=RuntimeError("DB Connection Error")):

        res = fetch_endpoint_data("allEmployementNew", token="tok", user_id=123)
        assert res["status"] is False
        assert res["message"] == "All Employment unavailable"
        assert res["data"] == []
