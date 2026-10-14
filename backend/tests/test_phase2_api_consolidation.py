"""
test_phase2_api_consolidation.py — Test suite for Phase 2 API integration consolidation and fallback reliability.
"""

import pytest
from unittest.mock import patch, MagicMock
import httpx
from backend.services.user_data_service import (
    fetch_endpoint_data,
    _is_auth_refusal,
    _get_employment_db_fallback,
    handle_query
)
from backend.services import platform_api


def test_1_http_401_prevents_db_fallback():
    """Verify HTTP 401 status code prevents local DB fallback."""
    api_resp = {
        "status": False,
        "data": [],
        "error": "Client error '401 Unauthorized' for url 'https://admin.collarcheck.com/wapi/employee/allEmployementNew'",
        "status_code": 401
    }
    with patch("backend.services.user_data_service.platform_api._get", return_value=api_resp), \
         patch("backend.services.user_data_service._get_employment_db_fallback") as mock_fallback:

        res = fetch_endpoint_data("allEmployementNew", token="invalid_tok", user_id=123)
        mock_fallback.assert_not_called()
        assert res["status"] is False
        assert res["data"] == []


def test_2_http_403_prevents_db_fallback():
    """Verify HTTP 403 status code prevents local DB fallback."""
    api_resp = {
        "status": False,
        "data": [],
        "error": "Client error '403 Forbidden' for url 'https://admin.collarcheck.com/wapi/employee/allEmployementNew'",
        "status_code": 403
    }
    with patch("backend.services.user_data_service.platform_api._get", return_value=api_resp), \
         patch("backend.services.user_data_service._get_employment_db_fallback") as mock_fallback:

        res = fetch_endpoint_data("allEmployementNew", token="forbidden_tok", user_id=123)
        mock_fallback.assert_not_called()
        assert res["status"] is False


def test_3_explicit_unauthorized_message_prevents_db_fallback():
    """Verify explicit unauthorized message in response prevents local DB fallback."""
    api_resp = {
        "status": False,
        "message": "Unauthorized access token provided.",
        "data": None
    }
    with patch("backend.services.user_data_service.platform_api._get", return_value=api_resp), \
         patch("backend.services.user_data_service._get_employment_db_fallback") as mock_fallback:

        res = fetch_endpoint_data("allEmployementNew", token="bad_tok", user_id=123)
        mock_fallback.assert_not_called()
        assert res["status"] is False


def test_4_explicit_forbidden_message_prevents_db_fallback():
    """Verify explicit forbidden message in response prevents local DB fallback."""
    api_resp = {
        "status": False,
        "message": "Forbidden access to resource.",
        "data": None
    }
    with patch("backend.services.user_data_service.platform_api._get", return_value=api_resp), \
         patch("backend.services.user_data_service._get_employment_db_fallback") as mock_fallback:

        res = fetch_endpoint_data("allEmployementNew", token="bad_tok", user_id=123)
        mock_fallback.assert_not_called()
        assert res["status"] is False


def test_5_missing_token_prevents_db_fallback():
    """Verify missing user token returns early without invoking platform HTTP call or DB fallback."""
    # platform_api._get returns authorization refusal payload when token is missing for user-specific path
    api_resp = {"status": False, "message": "No authenticated user token available.", "data": None}
    with patch("backend.services.platform_api._get", return_value=api_resp), \
         patch("backend.services.user_data_service._get_employment_db_fallback") as mock_fallback:

        res = fetch_endpoint_data("allEmployementNew", token=None, user_id=None)
        mock_fallback.assert_not_called()
        assert res["status"] is False


def test_6_network_failure_permits_db_fallback_for_authenticated_user():
    """Verify network connection error permits local DB fallback for authenticated user."""
    api_resp = {
        "status": False,
        "data": [],
        "error": "ConnectError: Failed to establish connection",
        "status_code": None
    }
    mock_db_res = {
        "status": True,
        "message": "All Employment (DB Fallback)",
        "data": [{"company": "Fallback Corp", "company_id": "1", "is_verified": None, "lists": []}]
    }
    with patch("backend.services.user_data_service.platform_api._get", return_value=api_resp), \
         patch("backend.services.user_data_service._get_employment_db_fallback", return_value=mock_db_res) as mock_fallback:

        res = fetch_endpoint_data("allEmployementNew", token="valid_tok", user_id=123)
        mock_fallback.assert_called_once_with(123)
        assert res["status"] is True
        assert res["data"][0]["company"] == "Fallback Corp"


def test_7_http_500_permits_db_fallback_for_authenticated_user():
    """Verify HTTP 500 Server Error permits local DB fallback for authenticated user."""
    api_resp = {
        "status": False,
        "data": [],
        "error": "Server error '500 Internal Server Error' for url 'https://admin.collarcheck.com/wapi/employee/allEmployementNew'",
        "status_code": 500
    }
    mock_db_res = {
        "status": True,
        "message": "All Employment (DB Fallback)",
        "data": [{"company": "500 Fallback Ltd", "company_id": "2", "is_verified": None, "lists": []}]
    }
    with patch("backend.services.user_data_service.platform_api._get", return_value=api_resp), \
         patch("backend.services.user_data_service._get_employment_db_fallback", return_value=mock_db_res) as mock_fallback:

        res = fetch_endpoint_data("allEmployementNew", token="valid_tok", user_id=123)
        mock_fallback.assert_called_once_with(123)
        assert res["status"] is True
        assert res["data"][0]["company"] == "500 Fallback Ltd"


def test_8_empty_successful_api_response_does_not_trigger_fallback():
    """Verify successful API response with empty data array returned without triggering DB fallback."""
    api_resp = {
        "status": True,
        "message": "All Employment",
        "data": []
    }
    with patch("backend.services.user_data_service.platform_api._get", return_value=api_resp), \
         patch("backend.services.user_data_service._get_employment_db_fallback") as mock_fallback:

        res = fetch_endpoint_data("allEmployementNew", token="valid_tok", user_id=123)
        mock_fallback.assert_not_called()
        assert res["status"] is True
        assert res["data"] == []


def test_9_db_fallback_failure_returns_safe_response():
    """Verify DB fallback infrastructure exception returns safe failure dict without exposing DB error."""
    api_resp = {
        "status": False,
        "error": "TimeoutError",
        "status_code": None
    }
    with patch("backend.services.user_data_service.platform_api._get", return_value=api_resp), \
         patch("backend.services.user_data_service._get_employment_db_fallback", side_effect=RuntimeError("DB table cyb_user_experience missing")):

        res = fetch_endpoint_data("allEmployementNew", token="valid_tok", user_id=123)
        assert res["status"] is False
        assert res["message"] == "All Employment unavailable"
        assert res["data"] == []


def test_10_malformed_api_responses_handled_safely():
    """Verify non-dict or malformed API responses handled safely without crashing."""
    assert _is_auth_refusal(None) is False
    assert _is_auth_refusal([]) is False
    assert _is_auth_refusal("string_error") is False
    assert _is_auth_refusal({}) is False


def test_11_authenticated_user_id_passed_correctly_to_fallback():
    """Verify valid authenticated user ID is passed to _get_employment_db_fallback."""
    api_resp = {"status": False, "error": "Server Error", "status_code": 503}
    with patch("backend.services.user_data_service.platform_api._get", return_value=api_resp), \
         patch("backend.services.user_data_service._get_employment_db_fallback") as mock_fallback:

        fetch_endpoint_data("allEmployementNew", token="tok", user_id="41031")
        mock_fallback.assert_called_once_with(41031)


def test_12_unrelated_error_text_with_401_or_403_not_treated_as_auth_refusal():
    """Verify error messages containing 401 or 403 in unrelated numbers (e.g. port 8401 or job ID 4031) are NOT misclassified as auth refusal."""
    res_port = {"status": False, "error": "Failed to connect to proxy port 8401", "status_code": 502}
    res_job = {"status": False, "error": "Job record 4031 query timeout", "status_code": 504}

    assert _is_auth_refusal(res_port) is False
    assert _is_auth_refusal(res_job) is False


def test_13_phase1_1_verification_semantics_preserved():
    """Verify Phase 1.1 company is_verified=None and role verification rules remain preserved."""
    raw_api_payload = {
        "status": True,
        "data": [
            {
                "id": 99,
                "company": "Integrity Corp",
                "company_id": 999,
                "is_verified": None,
                "lists": [
                    {"id": 1, "designation": "Staff Engineer", "approved": "1", "status": "1"}
                ]
            }
        ]
    }
    with patch("backend.services.user_data_service.platform_api._get", return_value=raw_api_payload):
        import asyncio
        async def _test():
            res = await handle_query("check my employment verification status", token="tok", user_id="123")
            assert res.handled is True
            assert len(res.results_payload) == 1
            item = res.results_payload[0]
            assert item["company_name"] == "Integrity Corp"
            assert item["company_is_verified"] is None
            assert item["verification"]["is_verified"] is True
        asyncio.run(_test())


# -------------------------------------------------------------------------
# PHASE 2.2: Direct HTTP Client Boundary Integration Tests for platform_api._get
# (Mocking httpx.Client.get so actual platform_api._get() implementation executes)
# -------------------------------------------------------------------------

def test_14_platform_api_get_http_401_boundary():
    """Directly test platform_api._get() with HTTP 401 client error boundary (real _get executes)."""
    mock_req = httpx.Request("GET", "https://admin.collarcheck.com/wapi/employee/allEmployementNew")
    mock_res = httpx.Response(401, request=mock_req)

    with patch("httpx.Client.get", return_value=mock_res):
        res = platform_api._get("employee/allEmployementNew", token="invalid_tok", user_id=123)

        assert res["status"] is False
        assert res["status_code"] == 401
        assert "401" in str(res["error"])
        assert res["data"] == []


def test_15_platform_api_get_http_403_boundary():
    """Directly test platform_api._get() with HTTP 403 client error boundary (real _get executes)."""
    mock_req = httpx.Request("GET", "https://admin.collarcheck.com/wapi/employee/allEmployementNew")
    mock_res = httpx.Response(403, request=mock_req)

    with patch("httpx.Client.get", return_value=mock_res):
        res = platform_api._get("employee/allEmployementNew", token="forbidden_tok", user_id=123)

        assert res["status"] is False
        assert res["status_code"] == 403
        assert "403" in str(res["error"])
        assert res["data"] == []


def test_16_platform_api_get_http_500_boundary():
    """Directly test platform_api._get() with HTTP 500 server error boundary (real _get executes)."""
    mock_req = httpx.Request("GET", "https://admin.collarcheck.com/wapi/employee/allEmployementNew")
    mock_res = httpx.Response(500, request=mock_req)

    with patch("httpx.Client.get", return_value=mock_res):
        res = platform_api._get("employee/allEmployementNew", token="valid_tok", user_id=123)

        assert res["status"] is False
        assert res["status_code"] == 500
        assert "500" in str(res["error"])
        assert _is_auth_refusal(res) is False  # HTTP 500 is NOT misclassified as auth refusal!


def test_17_platform_api_get_http_200_success_boundary():
    """Directly test platform_api._get() with HTTP 200 success response boundary (real _get executes)."""
    mock_req = httpx.Request("GET", "https://admin.collarcheck.com/wapi/employee/allEmployementNew")
    expected_payload = {"status": True, "message": "All Employment", "data": [{"id": 1, "company": "Test Co"}]}
    mock_res = httpx.Response(200, json=expected_payload, request=mock_req)

    with patch("httpx.Client.get", return_value=mock_res):
        res = platform_api._get("employee/allEmployementNew", token="valid_tok", user_id=123)

        assert res == expected_payload  # Unmodified decoded JSON return
        assert res["status"] is True
        assert len(res["data"]) == 1
