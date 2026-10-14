"""
Unit tests for backend/services/token_resolver.py — Phase 1 verification.
"""

import pytest
from unittest.mock import MagicMock, patch

from backend.services.token_resolver import (
    get_user_platform_token,
    TokenResolutionStatus,
    TokenResolutionResult,
)


def test_token_resolver_input_validation_invalid_types():
    """Verify that boolean, None, string, zero, negative, float, and complex data types are rejected."""
    invalid_inputs = [True, False, None, "200014", 0, -1, -200014, 14.5, [200014], {"id": 200014}]
    for val in invalid_inputs:
        res = get_user_platform_token(val)
        assert res.status == TokenResolutionStatus.USER_NOT_FOUND, f"Failed for input: {val!r}"
        assert res.token is None


@patch("backend.services.token_resolver.get_db")
def test_token_resolver_user_not_found(mock_get_db):
    """Verify outcome when DB returns no row for valid positive int ID."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = None
    mock_conn.cursor.return_value = mock_cursor
    mock_get_db.return_value = mock_conn

    res = get_user_platform_token(200014)
    assert res.status == TokenResolutionStatus.USER_NOT_FOUND
    assert res.token is None
    mock_cursor.close.assert_called_once()
    mock_conn.close.assert_called_once()


@patch("backend.services.token_resolver.get_db")
def test_token_resolver_token_missing(mock_get_db):
    """Verify outcome when active user row exists but token column is empty or 'none'."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = {"token": ""}
    mock_conn.cursor.return_value = mock_cursor
    mock_get_db.return_value = mock_conn

    res = get_user_platform_token(200014)
    assert res.status == TokenResolutionStatus.TOKEN_MISSING
    assert res.token is None

    # Test "none" string
    mock_cursor.fetchone.return_value = {"token": "none"}
    res_none = get_user_platform_token(200014)
    assert res_none.status == TokenResolutionStatus.TOKEN_MISSING
    assert res_none.token is None


@patch("backend.services.token_resolver.get_db")
def test_token_resolver_success(mock_get_db):
    """Verify outcome when active user has a valid platform token."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = {"token": "valid_bearer_token_xyz_123"}
    mock_conn.cursor.return_value = mock_cursor
    mock_get_db.return_value = mock_conn

    res = get_user_platform_token(200014)
    assert res.status == TokenResolutionStatus.SUCCESS
    assert res.token == "valid_bearer_token_xyz_123"


@patch("backend.services.token_resolver.get_db")
def test_token_resolver_database_error_sanitized(mock_get_db, caplog):
    """Verify outcome when DB query raises an exception: returns DATABASE_ERROR status and sanitizes logs."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.execute.side_effect = Exception("OperationalError: (1049, 'Unknown database secret_db')")
    mock_conn.cursor.return_value = mock_cursor
    mock_get_db.return_value = mock_conn

    res = get_user_platform_token(200014)
    assert res.status == TokenResolutionStatus.DATABASE_ERROR
    assert res.token is None
    assert "secret_db" not in caplog.text
    assert "Token resolution encountered a database infrastructure error." in caplog.text
