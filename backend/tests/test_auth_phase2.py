"""
Unit tests for backend/api/auth.py — Phase 2 authentication middleware & security verification.
"""

import pytest
import time
import jwt
from unittest.mock import MagicMock, patch
from fastapi import Request, HTTPException

from backend.config.config import JWT_SECRET, JWT_ALGORITHM
from backend.api.auth import find_user_by_token, verify_token


def create_test_jwt(payload: dict, secret: str = JWT_SECRET) -> str:
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


@patch("backend.api.auth.get_db")
def test_jwt_valid_signature_success(mock_get_db):
    """Verify successful JWT authentication maps to single active user."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [{
        "id": 200014,
        "email": "user@example.com",
        "user_type": 1,
        "slug": "user-200014",
        "individual_id": "CCE914539"
    }]
    mock_conn.cursor.return_value = mock_cursor
    mock_get_db.return_value = mock_conn

    token = create_test_jwt({"uid": 200014, "email": "user@example.com"})
    user = find_user_by_token(token)

    assert user is not None
    assert user["id"] == 200014
    assert user["email"] == "user@example.com"


@patch("backend.api.auth.get_db")
def test_invalid_jwt_signature_rejected_without_raw_fallback(mock_get_db):
    """Verify invalid JWT signature is rejected immediately and does NOT call raw token DB query."""
    invalid_token = create_test_jwt({"uid": 200014}, secret="WRONG_SECRET")
    
    user = find_user_by_token(invalid_token)
    assert user is None
    mock_get_db.assert_not_called()


@patch("backend.api.auth.get_db")
def test_malformed_jwt_structure_rejected_without_raw_fallback(mock_get_db):
    """Verify malformed dot-containing strings (e.g. 1 dot, 3 dots, or empty parts) are rejected without DB query."""
    malformed_tokens = [
        "part1.part2",
        "part1.part2.part3.part4",
        "part1..part3",
        ".part2.part3",
        "part1.part2."
    ]
    for tok in malformed_tokens:
        user = find_user_by_token(tok)
        assert user is None, f"Failed to reject malformed token: {tok}"
    
    mock_get_db.assert_not_called()


@patch("backend.api.auth.get_db")
def test_expired_jwt_rejection(mock_get_db):
    """Verify expired JWT is rejected immediately."""
    expired_payload = {
        "uid": 200014,
        "email": "user@example.com",
        "exp": int(time.time()) - 3600
    }
    expired_token = create_test_jwt(expired_payload)
    user = find_user_by_token(expired_token)
    assert user is None
    mock_get_db.assert_not_called()


@patch("backend.api.auth.get_db")
def test_jwt_missing_all_identity_claims_rejection(mock_get_db):
    """Verify JWT lacking uid, id, and email is rejected."""
    token = create_test_jwt({"role": "employee", "foo": "bar"})
    user = find_user_by_token(token)
    assert user is None
    mock_get_db.assert_not_called()


@patch("backend.api.auth.get_db")
def test_jwt_uid_id_conflict_rejection(mock_get_db):
    """Verify JWT with conflicting uid and id claims is rejected."""
    token = create_test_jwt({"uid": 200014, "id": 200015, "email": "user@example.com"})
    user = find_user_by_token(token)
    assert user is None
    mock_get_db.assert_not_called()


@patch("backend.api.auth.get_db")
def test_jwt_uid_email_mismatch_rejection(mock_get_db):
    """Verify JWT where PK row email does not match JWT email is rejected."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [{
        "id": 200014,
        "email": "actual_db_email@example.com",
        "user_type": 1
    }]
    mock_conn.cursor.return_value = mock_cursor
    mock_get_db.return_value = mock_conn

    token = create_test_jwt({"uid": 200014, "email": "spoofed_email@example.com"})
    user = find_user_by_token(token)
    assert user is None


@patch("backend.api.auth.get_db")
def test_jwt_pk_lookup_no_user_found_returns_none(mock_get_db):
    """Verify JWT PK lookup returning 0 DB rows returns None."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_conn.cursor.return_value = mock_cursor
    mock_get_db.return_value = mock_conn

    token = create_test_jwt({"uid": 999999, "email": "nonexistent@example.com"})
    user = find_user_by_token(token)
    assert user is None


@patch("backend.api.auth.get_db")
def test_email_only_jwt_duplicate_user_matches_rejected(mock_get_db):
    """Verify email-only JWT returning >1 active DB rows is rejected."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        {"id": 200014, "email": "dup@example.com"},
        {"id": 200015, "email": "dup@example.com"}
    ]
    mock_conn.cursor.return_value = mock_cursor
    mock_get_db.return_value = mock_conn

    token = create_test_jwt({"email": "dup@example.com"})
    user = find_user_by_token(token)
    assert user is None


@patch("backend.api.auth.get_db")
def test_jwt_invalid_claim_type_rejection(mock_get_db):
    """Verify boolean, float, or invalid claim types are rejected."""
    token_bool = create_test_jwt({"uid": True})
    assert find_user_by_token(token_bool) is None

    token_float = create_test_jwt({"uid": 14.5})
    assert find_user_by_token(token_float) is None

    mock_get_db.assert_not_called()


@patch("backend.api.auth.get_db")
def test_raw_token_authentication_success(mock_get_db):
    """Verify non-JWT raw platform token succeeds when exactly 1 active DB row matches."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [{
        "id": 200014,
        "email": "raw@example.com",
        "user_type": 1,
        "token": "raw_platform_token_string_123"
    }]
    mock_conn.cursor.return_value = mock_cursor
    mock_get_db.return_value = mock_conn

    user = find_user_by_token("raw_platform_token_string_123")
    assert user is not None
    assert user["id"] == 200014


@patch("backend.api.auth.get_db")
def test_raw_token_duplicate_match_rejection(mock_get_db):
    """Verify raw token query returning >1 rows is rejected as ambiguous."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        {"id": 200014, "token": "dup_token"},
        {"id": 200015, "token": "dup_token"}
    ]
    mock_conn.cursor.return_value = mock_cursor
    mock_get_db.return_value = mock_conn

    user = find_user_by_token("dup_token")
    assert user is None


@patch("backend.api.auth.get_db")
def test_database_error_during_lookup_fails_closed(mock_get_db):
    """Verify database exceptions during lookup return None (fail closed) without raising tracebacks."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.execute.side_effect = Exception("Database connection lost")
    mock_conn.cursor.return_value = mock_cursor
    mock_get_db.return_value = mock_conn

    token = create_test_jwt({"uid": 200014, "email": "user@example.com"})
    user = find_user_by_token(token)
    assert user is None


@patch("backend.api.auth.get_db")
def test_verify_token_populates_req_state_db_user_id(mock_get_db):
    """Verify verify_token populates req.state.db_user_id with canonical integer primary key."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [{
        "id": 200014,
        "email": "user@example.com",
        "user_type": 1,
        "slug": "user-200014",
        "individual_id": "CCE914539"
    }]
    mock_conn.cursor.return_value = mock_cursor
    mock_get_db.return_value = mock_conn

    token = create_test_jwt({"uid": 200014, "email": "user@example.com"})

    req = Request({"type": "http", "headers": []})
    credentials = MagicMock()
    credentials.credentials = token

    import asyncio
    payload = asyncio.run(verify_token(req, credentials))

    assert payload["uid"] == 200014
    assert req.state.db_user_id == 200014
    assert req.state.user_id == "200014"
    assert req.state.email == "user@example.com"
    assert req.state.role == "employee"


@patch("backend.api.auth.find_user_by_token")
def test_verify_token_fails_when_user_not_found(mock_find_user):
    """Verify verify_token raises 401 when find_user_by_token returns None."""
    mock_find_user.return_value = None
    req = Request({"type": "http", "headers": []})
    credentials = MagicMock()
    credentials.credentials = "invalid_token"

    import asyncio
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(verify_token(req, credentials))

    assert exc_info.value.status_code == 401
    assert hasattr(req.state, "db_user_id") is False
