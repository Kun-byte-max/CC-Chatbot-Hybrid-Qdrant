"""
test_phase7_e2e_auth_validation.py — Phase 7B End-to-End Regression Test Suite.

Verifies end-to-end integration across FastAPI authentication, canonical DB user resolution,
dynamic token resolution, outbound platform API header formatting, Chatbot JWT non-forwarding,
cross-user token isolation, missing platform token handling, auth failures, 401/403 fail-closed responses,
and user-scoped local DB fallbacks.
"""

import pytest
import jwt
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient

from backend.main import app
from backend.config.config import JWT_SECRET, JWT_ALGORITHM

client = TestClient(app)


def test_e2e_authenticated_chat_request_dynamic_token():
    """
    Scenario A: Authenticated Chat Request with Dynamic Token.
    Verifies:
    1. /chat endpoint accepts valid Chatbot JWT.
    2. Canonical DB user ID is extracted and propagated.
    3. cyb_user.token is dynamically resolved from database for outbound request.
    4. Chatbot JWT is NEVER forwarded outbound.
    5. Response matches standard ChatResponse contract.
    """
    user_id = 888
    db_platform_token = "synthetic_db_platform_token_888"
    jwt_token = jwt.encode(
        {"id": user_id, "uid": user_id, "email": "user888@example.com", "role": "employee"},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM
    )

    db_user_row = {
        "id": user_id,
        "individual_id": "IND888",
        "fname": "Alice",
        "lname": "User",
        "email": "user888@example.com",
        "user_type": 1,
        "status": 1,
        "is_deleted": 0,
        "token": db_platform_token
    }

    # Mock DB for auth.py and token_resolver.py
    mock_auth_conn = MagicMock()
    mock_auth_cursor = MagicMock()
    mock_auth_conn.cursor.return_value = mock_auth_cursor
    mock_auth_cursor.fetchall.return_value = [db_user_row]

    mock_res_conn = MagicMock()
    mock_res_cursor = MagicMock()
    mock_res_conn.cursor.return_value = mock_res_cursor
    mock_res_cursor.fetchone.return_value = {"token": db_platform_token}

    mock_http_instance = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "status": True,
        "data": [
            {
                "company": "Tech Corp",
                "company_id": "101",
                "is_verified": True,
                "lists": [
                    {
                        "id": "501",
                        "designation": "Software Engineer",
                        "joining_date": "2022-01-01",
                        "still_working": "1",
                        "approved": "1",
                        "status": "1",
                        "is_verified": True
                    }
                ]
            }
        ]
    }
    mock_http_instance.get.return_value = mock_response

    with patch("backend.api.auth.get_db", return_value=mock_auth_conn), \
         patch("backend.services.token_resolver.get_db", return_value=mock_res_conn), \
         patch("httpx.Client") as mock_httpx:

        mock_httpx.return_value.__enter__.return_value = mock_http_instance

        headers = {"Authorization": f"Bearer {jwt_token}"}
        payload = {
            "messages": [{"role": "user", "content": "show my employment history"}],
            "session_id": "sess_888"
        }

        response = client.post("/chat", json=payload, headers=headers)
        assert response.status_code == 200
        data = response.json()

        assert data.get("success") is True
        assert "reply" in data
        assert isinstance(data.get("results"), list)

        # Assert outbound HTTP request details
        assert mock_http_instance.get.called
        call_args = mock_http_instance.get.call_args_list[0]
        outbound_headers = call_args.kwargs.get("headers", {})

        # Assert Chatbot JWT is NOT forwarded and resolved platform token is sent
        assert jwt_token not in outbound_headers.get("Authorization", "")
        assert outbound_headers.get("Authorization") == f"Bearer {db_platform_token}"
        assert outbound_headers.get("X-User-Id") == str(user_id)
def test_e2e_cross_user_token_isolation():
    """
    Scenario B: Cross-User Token Isolation.
    Verifies that User A's token is never used for User B and no tokens leak across sessions.
    """
    user_a_id = 101
    token_a = "synthetic_token_user_A_101"
    jwt_a = jwt.encode({"id": user_a_id, "email": "usera@example.com"}, JWT_SECRET, algorithm=JWT_ALGORITHM)
    row_a = {"id": user_a_id, "individual_id": "IND101", "email": "usera@example.com", "status": 1, "is_deleted": 0, "token": token_a}

    user_b_id = 202
    token_b = "synthetic_token_user_B_202"
    jwt_b = jwt.encode({"id": user_b_id, "email": "userb@example.com"}, JWT_SECRET, algorithm=JWT_ALGORITHM)
    row_b = {"id": user_b_id, "individual_id": "IND202", "email": "userb@example.com", "status": 1, "is_deleted": 0, "token": token_b}

    captured_outbound_tokens = []

    def mock_http_get(url, **kwargs):
        headers = kwargs.get("headers", {})
        if "api.collarcheck.com" in url or "allEmployementNew" in url or "user-detail" in url:
            captured_outbound_tokens.append(headers.get("Authorization"))
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": True, "data": []}
        return mock_resp

    mock_http_instance = MagicMock()
    mock_http_instance.get.side_effect = mock_http_get

    with patch("httpx.Client") as mock_httpx, \
         patch("httpx.AsyncClient") as mock_async_httpx:

        mock_httpx.return_value.__enter__.return_value = mock_http_instance
        mock_async_client = AsyncMock()
        mock_async_client.post.return_value.status_code = 200
        mock_async_client.post.return_value.json.return_value = {"choices": [{"message": {"content": "Mocked LLM reply"}}]}
        mock_async_httpx.return_value.__aenter__.return_value = mock_async_client

        # Test User A
        mock_auth_conn_a = MagicMock()
        mock_auth_cursor_a = MagicMock()
        mock_auth_conn_a.cursor.return_value = mock_auth_cursor_a
        mock_auth_cursor_a.fetchall.return_value = [row_a]

        mock_res_conn_a = MagicMock()
        mock_res_cursor_a = MagicMock()
        mock_res_conn_a.cursor.return_value = mock_res_cursor_a
        mock_res_cursor_a.fetchone.return_value = {"token": token_a}

        with patch("backend.api.auth.get_db", return_value=mock_auth_conn_a), \
             patch("backend.services.token_resolver.get_db", return_value=mock_res_conn_a):
            res_a = client.post("/chat", json={"messages": [{"role": "user", "content": "show my employment history"}]}, headers={"Authorization": f"Bearer {jwt_a}"})
            assert res_a.status_code == 200

        # Test User B
        mock_auth_conn_b = MagicMock()
        mock_auth_cursor_b = MagicMock()
        mock_auth_conn_b.cursor.return_value = mock_auth_cursor_b
        mock_auth_cursor_b.fetchall.return_value = [row_b]

        mock_res_conn_b = MagicMock()
        mock_res_cursor_b = MagicMock()
        mock_res_conn_b.cursor.return_value = mock_res_cursor_b
        mock_res_cursor_b.fetchone.return_value = {"token": token_b}

        with patch("backend.api.auth.get_db", return_value=mock_auth_conn_b), \
             patch("backend.services.token_resolver.get_db", return_value=mock_res_conn_b):
            res_b = client.post("/chat", json={"messages": [{"role": "user", "content": "show my employment history"}]}, headers={"Authorization": f"Bearer {jwt_b}"})
            assert res_b.status_code == 200

    assert len(captured_outbound_tokens) >= 2
    assert captured_outbound_tokens[0] == f"Bearer {token_a}"
    assert captured_outbound_tokens[1] == f"Bearer {token_b}"
    assert token_b not in captured_outbound_tokens[0]
    assert token_a not in captured_outbound_tokens[1]


def test_e2e_missing_platform_token_handling():
    """
    Scenario C: Missing Platform Token.
    Verifies that when an authenticated user has no resolved platform token (token=None),
    the endpoint skips user-specific outbound platform API calls and returns status=False safely.
    """
    user_id = 303
    jwt_token = jwt.encode({"id": user_id, "email": "notoken@example.com"}, JWT_SECRET, algorithm=JWT_ALGORITHM)
    db_user_row = {"id": user_id, "individual_id": "IND303", "email": "notoken@example.com", "status": 1, "is_deleted": 0, "token": None}

    mock_auth_conn = MagicMock()
    mock_auth_cursor = MagicMock()
    mock_auth_conn.cursor.return_value = mock_auth_cursor
    mock_auth_cursor.fetchall.return_value = [db_user_row]

    mock_res_conn = MagicMock()
    mock_res_cursor = MagicMock()
    mock_res_conn.cursor.return_value = mock_res_cursor
    mock_res_cursor.fetchone.return_value = {"token": None}

    mock_http_instance = MagicMock()

    mock_emp_conn = MagicMock()
    mock_emp_cursor = MagicMock()
    mock_emp_conn.cursor.return_value = mock_emp_cursor
    mock_emp_cursor.fetchall.return_value = []

    with patch("backend.api.auth.get_db", return_value=mock_auth_conn), \
         patch("backend.services.token_resolver.get_db", return_value=mock_res_conn), \
         patch("backend.repositories.db.get_db", return_value=mock_emp_conn), \
         patch("httpx.Client") as mock_httpx, \
         patch("httpx.AsyncClient") as mock_async_httpx:

        mock_httpx.return_value.__enter__.return_value = mock_http_instance
        mock_async_res = MagicMock()
        mock_async_res.status_code = 200
        mock_async_res.json.return_value = {"choices": [{"message": {"content": "Mocked LLM reply"}}]}
        mock_async_client = AsyncMock()
        mock_async_client.post.return_value = mock_async_res
        mock_async_httpx.return_value.__aenter__.return_value = mock_async_client

        headers = {"Authorization": f"Bearer {jwt_token}"}
        payload = {"messages": [{"role": "user", "content": "show my employment history"}]}

        response = client.post("/chat", json=payload, headers=headers)
        assert response.status_code == 200
        data = response.json()

        # Outbound HTTP call must be skipped when user has no token
        assert not mock_http_instance.get.called
        assert data.get("success") is True




def test_e2e_authentication_failure_rejected_before_downstream():

    """
    Scenario D: Authentication Failure.
    Verifies that invalid or expired JWTs are rejected at the FastAPI boundary with HTTP 401
    and do NOT reach user data processing or platform API clients.
    """
    invalid_jwt = "invalid.jwt.token.string"
    mock_http_instance = MagicMock()

    with patch("httpx.Client") as mock_httpx:
        mock_httpx.return_value.__enter__.return_value = mock_http_instance

        headers = {"Authorization": f"Bearer {invalid_jwt}"}
        payload = {"messages": [{"role": "user", "content": "show my employment history"}]}

        response = client.post("/chat", json=payload, headers=headers)
        assert response.status_code == 401
        assert "detail" in response.json()

        # Zero outbound HTTP calls made
        assert not mock_http_instance.get.called


def test_e2e_platform_api_unauthorized_fails_closed():
    """
    Scenario E: Platform API Unauthorized Response (401/403).
    Verifies that when external platform API returns 401/403, the chatbot fails closed
    without retrying using admin credentials or exposing unauthorized personal DB fallback data.
    """
    user_id = 404
    token = "expired_platform_token_404"
    jwt_token = jwt.encode({"id": user_id, "email": "unauth@example.com"}, JWT_SECRET, algorithm=JWT_ALGORITHM)
    db_user_row = {"id": user_id, "individual_id": "IND404", "email": "unauth@example.com", "status": 1, "is_deleted": 0, "token": token}

    mock_auth_conn = MagicMock()
    mock_auth_cursor = MagicMock()
    mock_auth_conn.cursor.return_value = mock_auth_cursor
    mock_auth_cursor.fetchall.return_value = [db_user_row]

    mock_res_conn = MagicMock()
    mock_res_cursor = MagicMock()
    mock_res_conn.cursor.return_value = mock_res_cursor
    mock_res_cursor.fetchone.return_value = {"token": token}

    mock_http_instance = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.json.return_value = {"status": False, "message": "Unauthorized"}
    mock_http_instance.get.return_value = mock_response

    with patch("backend.api.auth.get_db", return_value=mock_auth_conn), \
         patch("backend.services.token_resolver.get_db", return_value=mock_res_conn), \
         patch("httpx.Client") as mock_httpx:

        mock_httpx.return_value.__enter__.return_value = mock_http_instance

        headers = {"Authorization": f"Bearer {jwt_token}"}
        payload = {"messages": [{"role": "user", "content": "show my employment history"}]}

        response = client.post("/chat", json=payload, headers=headers)
        assert response.status_code == 200
        data = response.json()

        # Exactly 1 outbound call made (zero admin retry)
        assert mock_http_instance.get.call_count == 1
        assert data.get("success") is True
        assert data.get("results") == []


def test_e2e_user_scoped_db_fallback_on_network_failure():
    """
    Scenario F: User-Scoped DB Fallback on Network/5xx Failure.
    Verifies that on network exceptions during platform API calls, local DB fallback
    is safely scoped strictly to the authenticated canonical user ID.
    """
    user_id = 505
    token = "valid_platform_token_505"
    jwt_token = jwt.encode({"id": user_id, "email": "fallback@example.com"}, JWT_SECRET, algorithm=JWT_ALGORITHM)
    db_user_row = {"id": user_id, "individual_id": "IND505", "email": "fallback@example.com", "status": 1, "is_deleted": 0, "token": token}

    mock_auth_conn = MagicMock()
    mock_auth_cursor = MagicMock()
    mock_auth_conn.cursor.return_value = mock_auth_cursor
    mock_auth_cursor.fetchall.return_value = [db_user_row]

    mock_res_conn = MagicMock()
    mock_res_cursor = MagicMock()
    mock_res_conn.cursor.return_value = mock_res_cursor
    mock_res_cursor.fetchone.return_value = {"token": token}

    # Mock DB query for employment fallback
    mock_emp_conn = MagicMock()
    mock_emp_cursor = MagicMock()
    mock_emp_conn.cursor.return_value = mock_emp_cursor
    mock_emp_cursor.fetchall.return_value = [
        {
            "id": 901,
            "company": 12,
            "company_name": "Fallback Corp",
            "designation": "Staff Engineer",
            "joining_date": "2021-05-01",
            "worked_till_date": None,
            "still_working": "1",
            "salary": 120000,
            "salary_inhand": 100000,
            "salary_mode": "bank",
            "approved": "1",
            "status": "1",
            "company_label": "Fallback Corp"
        }
    ]

    mock_http_instance = MagicMock()
    mock_http_instance.get.side_effect = Exception("Platform API Network Timeout")

    with patch("backend.api.auth.get_db", return_value=mock_auth_conn), \
         patch("backend.services.token_resolver.get_db", return_value=mock_res_conn), \
         patch("backend.repositories.db.get_db", return_value=mock_emp_conn), \
         patch("httpx.Client") as mock_httpx:

        mock_httpx.return_value.__enter__.return_value = mock_http_instance

        headers = {"Authorization": f"Bearer {jwt_token}"}
        payload = {"messages": [{"role": "user", "content": "show my employment history"}]}

        response = client.post("/chat", json=payload, headers=headers)
        assert response.status_code == 200
        data = response.json()

        assert data.get("success") is True
        assert isinstance(data.get("results"), list)
        assert len(data.get("results")) > 0
        assert data["results"][0].get("company") == "Fallback Corp"

        # Assert DB query filter strictly scoped to valid_uid 505
        executed_sqls = [call[0][0] for call in mock_emp_cursor.execute.call_args_list]
        executed_params = [call[0][1] for call in mock_emp_cursor.execute.call_args_list]
        assert any("user = %s" in sql for sql in executed_sqls)
        assert (505,) in executed_params

