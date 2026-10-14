"""
Unit tests for backend/services/platform_api.py — Phase 3 dynamic token resolution refactoring.
"""

import pytest
from unittest.mock import MagicMock, patch
import httpx

from backend.services.platform_api import (
    _get,
    _parse_positive_int_id,
    fetch_user_profile_by_slug,
    fetch_random_widgets
)
from backend.services.token_resolver import TokenResolutionStatus, TokenResolutionResult


def test_parse_positive_int_id():
    """Verify input validation for user_id parsing."""
    assert _parse_positive_int_id(200014) == 200014
    assert _parse_positive_int_id("200014") == 200014
    assert _parse_positive_int_id(True) is None
    assert _parse_positive_int_id(False) is None
    assert _parse_positive_int_id(None) is None
    assert _parse_positive_int_id(0) is None
    assert _parse_positive_int_id(-1) is None
    assert _parse_positive_int_id("-5") is None
    assert _parse_positive_int_id(14.5) is None
    assert _parse_positive_int_id("abc") is None


@patch("backend.services.platform_api.httpx.Client")
def test_platform_api_uses_explicit_token(mock_httpx):
    """Verify passing explicit token uses it in Authorization header."""
    mock_res = MagicMock()
    mock_res.status_code = 200
    mock_res.json.return_value = {"status": True, "data": {"id": 19}}
    mock_httpx.return_value.__enter__.return_value.get.return_value = mock_res

    res = _get("employee/user-detail", token="explicit_user_token_123", user_id=200014)
    assert res["status"] is True

    call_args = mock_httpx.return_value.__enter__.return_value.get.call_args
    headers = call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer explicit_user_token_123"
    assert headers["X-User-Id"] == "200014"


@patch("backend.services.platform_api.get_user_platform_token")
@patch("backend.services.platform_api.httpx.Client")
def test_platform_api_dynamic_token_resolution_success(mock_httpx, mock_get_token):
    """Verify passing user_id=200014 without token dynamically resolves token from DB."""
    mock_get_token.return_value = TokenResolutionResult(
        status=TokenResolutionStatus.SUCCESS,
        token="db_resolved_user_token_999"
    )

    mock_res = MagicMock()
    mock_res.status_code = 200
    mock_res.json.return_value = {"status": True, "data": []}
    mock_httpx.return_value.__enter__.return_value.get.return_value = mock_res

    res = _get("employee/allEmployementNew", user_id=200014)
    assert res["status"] is True

    mock_get_token.assert_called_once_with(200014)
    call_args = mock_httpx.return_value.__enter__.return_value.get.call_args
    headers = call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer db_resolved_user_token_999"


@patch("backend.services.platform_api.httpx.Client")
def test_platform_api_missing_token_skips_user_endpoint(mock_httpx):
    """Verify user-specific endpoint skips HTTP call when no token is resolved."""
    res = _get("employee/user-detail", token=None, user_id=None)
    assert res["status"] is False
    assert res["message"] == "No authenticated user token available."
    mock_httpx.assert_not_called()


@patch("backend.services.platform_api.get_user_platform_token")
@patch("backend.services.platform_api.httpx.Client")
def test_platform_api_db_error_fails_safely(mock_httpx, mock_get_token):
    """Verify DB exception during token resolution fails safely without HTTP call."""
    mock_get_token.return_value = TokenResolutionResult(
        status=TokenResolutionStatus.DATABASE_ERROR
    )

    res = _get("employee/user-detail", user_id=200014)
    assert res["status"] is False
    assert res["message"] == "No authenticated user token available."
    mock_httpx.assert_not_called()


@patch("backend.services.platform_api.httpx.Client")
def test_platform_api_401_no_admin_retry(mock_httpx):
    """Verify HTTP 401 response fails cleanly without retrying using admin token."""
    mock_res = MagicMock()
    mock_res.status_code = 401
    mock_res.raise_for_status.side_effect = httpx.HTTPStatusError(
        message="401 Unauthorized",
        request=MagicMock(),
        response=mock_res
    )
    mock_httpx.return_value.__enter__.return_value.get.return_value = mock_res

    res = _get("employee/user-detail", token="expired_user_token", user_id=200014)
    assert res["status"] is False
    assert "401 Unauthorized" in res.get("error", "")

    # Assert get was called exactly once (no 2nd retry call with admin token)
    assert mock_httpx.return_value.__enter__.return_value.get.call_count == 1


@patch("backend.services.platform_api.get_user_platform_token")
@patch("backend.services.platform_api.httpx.Client")
def test_fetch_user_profile_by_slug_dynamic_token(mock_httpx, mock_get_token):
    """Verify fetch_user_profile_by_slug uses dynamic token resolution via user_id."""
    mock_get_token.return_value = TokenResolutionResult(
        status=TokenResolutionStatus.SUCCESS,
        token="slug_user_token_777"
    )

    mock_res = MagicMock()
    mock_res.status_code = 200
    mock_res.json.return_value = {"status": True, "data": {"slug": "test-slug"}}
    mock_httpx.return_value.__enter__.return_value.get.return_value = mock_res

    res = fetch_user_profile_by_slug("test-slug", user_id=200014)
    assert res["status"] is True

    mock_get_token.assert_called_once_with(200014)
    call_args = mock_httpx.return_value.__enter__.return_value.get.call_args
    headers = call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer slug_user_token_777"
