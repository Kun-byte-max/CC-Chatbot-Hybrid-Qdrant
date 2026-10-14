"""
test_v3_payload_contracts.py — Payload Contract Unit Test Suite for User Data Service.
All external API calls are fully mocked. Async functions invoke via asyncio.run().
"""

import pytest
import asyncio
from unittest.mock import patch, MagicMock
from backend.services.user_data_service import handle_query, ServiceResponse
from backend.services.user_data_intent_parser import UserIntent


def test_employment_history_results_payload_populated():
    """Verify handle_query populates results_payload and result_type_payload='employment_history' for employment history intent."""
    async def _test():
        mock_api_resp = {
            "status": True,
            "data": [
                {
                    "id": 1,
                    "company": "Tech Corp",
                    "company_id": 101,
                    "is_verified": 1,
                    "totalExperienceMonths": 24,
                    "lists": [
                        {
                            "id": "71533",
                            "designation": "Sr. Backend Developer",
                            "employment_type": "Full Time",
                            "joining_date": "2021-06-01",
                            "worked_till_date": "Present",
                            "still_working": "1",
                            "salary": "90000",
                            "approved": "1",
                            "status": "1",
                            "verificationProcess": {"level2": True, "level4": True},
                            "skill": [{"id": 12, "name": "Python"}]
                        }
                    ]
                }
            ]
        }

        with patch("backend.services.user_data_service.platform_api._get") as mock_get:
            mock_get.return_value = mock_api_resp

            res = await handle_query("show my employment history", token="test_token", user_id="123")

            assert res.handled is True
            assert res.reply == "Here is your employment history:"
            assert res.result_type_payload == "employment_history"
            assert len(res.results_payload) == 1

            card = res.results_payload[0]
            assert card["company"] == "Tech Corp"
            assert card["company_name"] == "Tech Corp"
            assert card["company_id"] == 101
            assert card["designation"] == "Sr. Backend Developer"
            assert card["approved"] == "1"
            assert card["status"] == "1"
            assert card["status_label"] == "Verified"
            assert card["verification"]["status"] == "verified"
            assert card["verification"]["level"] == 4
            assert card["verification"]["level_label"] == "Level 4"
            assert card["verification"]["is_verified"] is True
            assert card["url"] == "https://www.collarcheck.com/dashboard/user/employment"

    asyncio.run(_test())


def test_employment_verification_intent_priority():
    """Verify handle_query sets result_type_payload='employment_verification' when UserIntent.EMPLOYMENT_VERIFICATION is present."""
    async def _test():
        mock_api_resp = {
            "status": True,
            "data": [
                {
                    "company": "Acme Inc",
                    "company_id": 202,
                    "lists": [
                        {
                            "id": "82444",
                            "designation": "DevOps Engineer",
                            "approved": "0",
                            "status": "1",
                            "verificationProcess": {"level2": True}
                        }
                    ]
                }
            ]
        }

        with patch("backend.services.user_data_service.platform_api._get") as mock_get:
            mock_get.return_value = mock_api_resp

            res = await handle_query("check my employment verification status", token="test_token", user_id="123")

            assert res.handled is True
            assert res.result_type_payload == "employment_verification"
            assert len(res.results_payload) == 1

            card = res.results_payload[0]
            assert card["company"] == "Acme Inc"
            assert card["status_label"] == "Not Verified"
            assert card["verification"]["status"] == "not_verified"
            assert card["verification"]["level"] == 2

    asyncio.run(_test())


def test_empty_employment_history_no_fabricated_cards():
    """Verify that a valid API response with empty employment data yields results_payload=[] and result_type_payload=None."""
    async def _test():
        mock_api_resp = {"status": True, "data": []}

        with patch("backend.services.user_data_service.platform_api._get") as mock_get:
            mock_get.return_value = mock_api_resp

            res = await handle_query("show my employment history", token="test_token", user_id="123")

            assert res.handled is True
            assert "couldn't find any employment history" in res.reply
            assert res.results_payload == []
            assert res.result_type_payload is None

    asyncio.run(_test())


def test_valid_api_response_empty_extraction_no_cards():
    """Verify valid API response structure returning missing/None lists yields empty results payload without fabricating cards."""
    async def _test():
        mock_api_resp = {"status": True, "data": [{"company": "Empty Corp", "lists": None}]}

        with patch("backend.services.user_data_service.platform_api._get") as mock_get:
            mock_get.return_value = mock_api_resp

            res = await handle_query("show my employment history", token="test_token", user_id="123")

            assert res.handled is True
            assert res.results_payload == []
            assert res.result_type_payload is None

    asyncio.run(_test())


def test_api_failure_graceful_handling():
    """Verify API failure (status=False) is handled gracefully with empty results payload."""
    async def _test():
        mock_api_resp = {"status": False, "message": "API Error"}

        with patch("backend.services.user_data_service.platform_api._get") as mock_get:
            mock_get.return_value = mock_api_resp

            res = await handle_query("show my employment history", token="test_token", user_id="123")

            assert res.handled is True
            assert res.results_payload == []
            assert res.result_type_payload is None

    asyncio.run(_test())


def test_multi_intent_education_and_employment():
    """Verify multi-intent queries (education + employment) append both card types into results_payload."""
    async def _test():
        mock_user_detail = {"status": True, "data": {"slug": "john-doe"}}

        def _mock_get(endpoint, token=None, user_id=None):
            if "user-detail" in endpoint:
                return mock_user_detail
            elif "allEmployementNew" in endpoint:
                return {
                    "status": True,
                    "data": [
                        {
                            "company": "Tech Corp",
                            "lists": [{"id": "1", "designation": "Developer", "approved": "1", "status": "1"}]
                        }
                    ]
                }
            elif "user-profile" in endpoint:
                return {
                    "status": True,
                    "data": {
                        "all_education": [
                            {"id": "10", "course": "B.Tech", "university": "MIT", "course_type": "Full Time"}
                        ]
                    }
                }
            return {"status": False}

        with patch("backend.services.user_data_service.platform_api._get", side_effect=_mock_get):
            res = await handle_query("show my education and employment history", token="test_token", user_slug="john-doe", user_id="123")

            assert res.handled is True
            assert len(res.results_payload) == 2
            # Education card
            assert res.results_payload[0]["qualification"] == "B.Tech"
            # Employment card
            assert res.results_payload[1]["company"] == "Tech Corp"
            assert res.results_payload[1]["designation"] == "Developer"

    asyncio.run(_test())


def test_verification_matrix_parity_and_accounting():
    """
    Validates:
    - Approved/status combinations (1/1 -> verified, 0/1 -> not_verified, 1/0 -> not_verified, missing/None/empty/space -> unknown).
    - Cross-intent parity: EMPLOYMENT_HISTORY and EMPLOYMENT_VERIFICATION produce identical card payloads.
    - Summary counting accounting: verified + not_verified + unknown == total records without double counting.
    - Robustness against missing/invalid verification objects.
    """
    async def _test():
        mock_api_resp = {
            "status": True,
            "data": [
                {
                    "company": "Company A",
                    "company_id": 101,
                    "lists": [
                        {"id": "1", "designation": "Verified Role", "approved": "1", "status": "1", "verificationProcess": {"level4": True}},
                        {"id": "2", "designation": "Not Verified Role (0,1)", "approved": "0", "status": "1", "verificationProcess": {"level2": True}},
                        {"id": "3", "designation": "Not Verified Role (1,0)", "approved": "1", "status": "0", "verificationProcess": {}},
                        {"id": "4", "designation": "Unknown Role (None)", "approved": None, "status": "1"},
                        {"id": "5", "designation": "Unknown Role (empty)", "approved": "1", "status": ""},
                        {"id": "6", "designation": "Unknown Role (whitespace)", "approved": "   ", "status": "1"}
                    ]
                }
            ]
        }

        with patch("backend.services.user_data_service.platform_api._get") as mock_get:
            mock_get.return_value = mock_api_resp

            res_hist = await handle_query("show my employment history", token="tok", user_id="1")
            res_verif = await handle_query("what is my verification status?", token="tok", user_id="1")

            assert res_hist.handled is True
            assert res_verif.handled is True

            # Assert reply string summary count parity for verification query
            assert res_verif.reply == "Here is your employment verification status (6 records: 1 verified, 2 not verified, 3 status unknown):"

            # Assert identical card payloads between history and verification intents
            assert len(res_hist.results_payload) == 6
            assert len(res_verif.results_payload) == 6
            assert res_hist.results_payload == res_verif.results_payload

            cards = res_verif.results_payload
            assert cards[0]["verification"]["status"] == "verified"
            assert cards[0]["status_label"] == "Verified"
            assert cards[0]["verification"]["level_label"] == "Level 4"

            assert cards[1]["verification"]["status"] == "not_verified"
            assert cards[1]["status_label"] == "Not Verified"

            assert cards[2]["verification"]["status"] == "not_verified"
            assert cards[2]["status_label"] == "Not Verified"

            assert cards[3]["verification"]["status"] == "unknown"
            assert cards[3]["status_label"] == "Status Unknown"

            assert cards[4]["verification"]["status"] == "unknown"

            assert cards[5]["verification"]["status"] == "unknown"

    asyncio.run(_test())
