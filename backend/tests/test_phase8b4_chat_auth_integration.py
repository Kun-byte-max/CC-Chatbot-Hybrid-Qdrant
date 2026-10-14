"""
test_phase8b4_chat_auth_integration.py — Phase 8B.4.2 Test-Only Auth Integration Suite.

Verifies:
Test A: Authenticated /chat request pipeline with synthetic JWT and dynamic platform token.
Test B: Rejection of invalid/malformed inbound JWT at FastAPI authentication boundary.
Test C: Outbound platform 401 fail-closed behavior without static token fallback.
Test D: Strict cross-user token isolation across separate authenticated /chat sessions.

All database queries, outbound platform HTTP calls, and LLM completions are fully mocked.
No live external API calls or real user tokens are used.
"""

import pytest
import jwt
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient

from backend.main import app
from backend.config.config import JWT_SECRET, JWT_ALGORITHM

client = TestClient(app)


def test_scenario_a_authenticated_chat_pipeline():
    """
    Test A: Authenticated /chat Pipeline.
    Verifies:
    1. Inbound request with valid synthetic Chatbot JWT passes FastAPI verify_token dependency.
    2. Canonical DB user ID (db_user_id) is derived and passed downstream.
    3. Outbound platform client receives the platform token for that synthetic test user.
    4. Inbound Chatbot JWT is NOT forwarded outbound.
    5. Response matches the standard ChatResponse payload contract.
    """
    synthetic_uid = 801
    synthetic_platform_tok = "synth_platform_token_801"
    synthetic_jwt = jwt.encode(
        {"id": synthetic_uid, "uid": synthetic_uid, "email": "user801@example.com", "role": "employee"},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM
    )

    db_row = {
        "id": synthetic_uid,
        "individual_id": "IND801",
        "fname": "Test801",
        "lname": "User",
        "email": "user801@example.com",
        "user_type": 1,
        "status": 1,
        "is_deleted": 0,
        "token": synthetic_platform_tok
    }

    mock_auth_conn = MagicMock()
    mock_auth_cursor = MagicMock()
    mock_auth_conn.cursor.return_value = mock_auth_cursor
    mock_auth_cursor.fetchall.return_value = [db_row]

    mock_res_conn = MagicMock()
    mock_res_cursor = MagicMock()
    mock_res_conn.cursor.return_value = mock_res_cursor
    mock_res_cursor.fetchone.return_value = {"token": synthetic_platform_tok}

    mock_http_instance = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": True,
        "data": [
            {
                "company": "Test Company",
                "company_id": "801",
                "is_verified": True,
                "lists": [
                    {
                        "id": "8001",
                        "designation": "Engineer",
                        "joining_date": "2023-01-01",
                        "still_working": "1",
                        "approved": "1",
                        "status": "1",
                        "is_verified": True
                    }
                ]
            }
        ]
    }
    mock_http_instance.get.return_value = mock_resp

    with patch("backend.api.auth.get_db", return_value=mock_auth_conn), \
         patch("backend.services.token_resolver.get_db", return_value=mock_res_conn), \
         patch("httpx.Client") as mock_httpx, \
         patch("httpx.AsyncClient") as mock_async_httpx:

        mock_httpx.return_value.__enter__.return_value = mock_http_instance
        mock_async_client = AsyncMock()
        mock_async_res = MagicMock()
        mock_async_res.status_code = 200
        mock_async_res.json.return_value = {"choices": [{"message": {"content": "Mocked LLM completion"}}]}
        mock_async_client.post.return_value = mock_async_res
        mock_async_httpx.return_value.__aenter__.return_value = mock_async_client

        headers = {"Authorization": f"Bearer {synthetic_jwt}"}
        payload = {"messages": [{"role": "user", "content": "show my employment history"}]}

        response = client.post("/chat", json=payload, headers=headers)
        assert response.status_code == 200
        data = response.json()

        assert data.get("success") is True
        assert "reply" in data
        assert isinstance(data.get("results"), list)

        # Assert outbound header correctness without leaking secrets in logs
        assert mock_http_instance.get.called
        call_args = mock_http_instance.get.call_args_list[0]
        outbound_headers = call_args.kwargs.get("headers", {})

        assert synthetic_jwt not in outbound_headers.get("Authorization", "")
        assert outbound_headers.get("Authorization") == f"Bearer {synthetic_platform_tok}"
        assert outbound_headers.get("X-User-Id") == str(synthetic_uid)


def test_scenario_b_invalid_inbound_jwt_rejection():
    """
    Test B: Invalid Inbound JWT Rejection.
    Verifies that malformed or signature-invalid JWTs are rejected at the FastAPI auth boundary (HTTP 401),
    and no downstream platform calls or local DB fallbacks occur.
    """
    invalid_jwt = "header.invalid_signature_jwt.payload"
    mock_http_instance = MagicMock()

    with patch("httpx.Client") as mock_httpx, \
         patch("httpx.AsyncClient") as mock_async_httpx:

        mock_httpx.return_value.__enter__.return_value = mock_http_instance
        mock_async_client = AsyncMock()
        mock_async_httpx.return_value.__aenter__.return_value = mock_async_client

        headers = {"Authorization": f"Bearer {invalid_jwt}"}
        payload = {"messages": [{"role": "user", "content": "show my employment history"}]}

        response = client.post("/chat", json=payload, headers=headers)
        assert response.status_code == 401
        assert "detail" in response.json()

        # Zero outbound HTTP calls executed
        assert not mock_http_instance.get.called
        assert not mock_async_client.post.called


def test_scenario_c_outbound_platform_401_fail_closed():
    """
    Test C: Outbound Platform HTTP 401 Fail-Closed.
    Verifies that when the outbound platform API returns HTTP 401 Unauthorized:
    1. Zero static/admin token retries occur.
    2. Response fails closed cleanly (`results: []`) without exposing unauthorized fallback data.
    """
    synthetic_uid = 803
    synthetic_platform_tok = "synth_platform_token_803"
    synthetic_jwt = jwt.encode(
        {"id": synthetic_uid, "uid": synthetic_uid, "email": "user803@example.com", "role": "employee"},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM
    )

    db_row = {"id": synthetic_uid, "individual_id": "IND803", "email": "user803@example.com", "status": 1, "is_deleted": 0, "token": synthetic_platform_tok}

    mock_auth_conn = MagicMock()
    mock_auth_cursor = MagicMock()
    mock_auth_conn.cursor.return_value = mock_auth_cursor
    mock_auth_cursor.fetchall.return_value = [db_row]

    mock_res_conn = MagicMock()
    mock_res_cursor = MagicMock()
    mock_res_conn.cursor.return_value = mock_res_cursor
    mock_res_cursor.fetchone.return_value = {"token": synthetic_platform_tok}

    mock_http_instance = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.json.return_value = {"status": False, "message": "Unauthorized"}
    mock_http_instance.get.return_value = mock_resp

    with patch("backend.api.auth.get_db", return_value=mock_auth_conn), \
         patch("backend.services.token_resolver.get_db", return_value=mock_res_conn), \
         patch("httpx.Client") as mock_httpx, \
         patch("httpx.AsyncClient") as mock_async_httpx:

        mock_httpx.return_value.__enter__.return_value = mock_http_instance
        mock_async_client = AsyncMock()
        mock_async_res = MagicMock()
        mock_async_res.status_code = 200
        mock_async_res.json.return_value = {"choices": [{"message": {"content": "Mocked LLM completion"}}]}
        mock_async_client.post.return_value = mock_async_res
        mock_async_httpx.return_value.__aenter__.return_value = mock_async_client

        headers = {"Authorization": f"Bearer {synthetic_jwt}"}
        payload = {"messages": [{"role": "user", "content": "show my employment history"}]}

        response = client.post("/chat", json=payload, headers=headers)
        assert response.status_code == 200
        data = response.json()

        # Fail-closed check: exactly 1 call, zero admin token retries, empty results list
        assert mock_http_instance.get.call_count == 1
        assert data.get("success") is True
        assert data.get("results") == []


def test_scenario_d_cross_user_token_isolation():
    """
    Test D: Cross-User Token Isolation.
    Verifies that User A's token is never passed to User B's outbound request and no token cross-talk occurs.
    """
    uid_a = 811
    tok_a = "synth_platform_token_A_811"
    jwt_a = jwt.encode({"id": uid_a, "email": "usera@example.com"}, JWT_SECRET, algorithm=JWT_ALGORITHM)
    row_a = {"id": uid_a, "individual_id": "IND811", "email": "usera@example.com", "status": 1, "is_deleted": 0, "token": tok_a}

    uid_b = 822
    tok_b = "synth_platform_token_B_822"
    jwt_b = jwt.encode({"id": uid_b, "email": "userb@example.com"}, JWT_SECRET, algorithm=JWT_ALGORITHM)
    row_b = {"id": uid_b, "individual_id": "IND822", "email": "userb@example.com", "status": 1, "is_deleted": 0, "token": tok_b}

    captured_auth_headers = []

    def mock_http_get(url, **kwargs):
        headers = kwargs.get("headers", {})
        if "api.collarcheck.com" in url or "allEmployementNew" in url or "user-detail" in url:
            captured_auth_headers.append(headers.get("Authorization"))
        mock_r = MagicMock()
        mock_r.status_code = 200
        mock_r.json.return_value = {"status": True, "data": []}
        return mock_r

    mock_http_instance = MagicMock()
    mock_http_instance.get.side_effect = mock_http_get

    with patch("httpx.Client") as mock_httpx, \
         patch("httpx.AsyncClient") as mock_async_httpx:

        mock_httpx.return_value.__enter__.return_value = mock_http_instance
        mock_async_client = AsyncMock()
        mock_async_res = MagicMock()
        mock_async_res.status_code = 200
        mock_async_res.json.return_value = {"choices": [{"message": {"content": "Mocked LLM completion"}}]}
        mock_async_client.post.return_value = mock_async_res
        mock_async_httpx.return_value.__aenter__.return_value = mock_async_client

        # User A Call
        conn_auth_a = MagicMock()
        cur_auth_a = MagicMock()
        conn_auth_a.cursor.return_value = cur_auth_a
        cur_auth_a.fetchall.return_value = [row_a]

        conn_res_a = MagicMock()
        cur_res_a = MagicMock()
        conn_res_a.cursor.return_value = cur_res_a
        cur_res_a.fetchone.return_value = {"token": tok_a}

        with patch("backend.api.auth.get_db", return_value=conn_auth_a), \
             patch("backend.services.token_resolver.get_db", return_value=conn_res_a):
            res_a = client.post("/chat", json={"messages": [{"role": "user", "content": "show my employment history"}]}, headers={"Authorization": f"Bearer {jwt_a}"})
            assert res_a.status_code == 200

        # User B Call
        conn_auth_b = MagicMock()
        cur_auth_b = MagicMock()
        conn_auth_b.cursor.return_value = cur_auth_b
        cur_auth_b.fetchall.return_value = [row_b]

        conn_res_b = MagicMock()
        cur_res_b = MagicMock()
        conn_res_b.cursor.return_value = cur_res_b
        cur_res_b.fetchone.return_value = {"token": tok_b}

        with patch("backend.api.auth.get_db", return_value=conn_auth_b), \
             patch("backend.services.token_resolver.get_db", return_value=conn_res_b):
            res_b = client.post("/chat", json={"messages": [{"role": "user", "content": "show my employment history"}]}, headers={"Authorization": f"Bearer {jwt_b}"})
            assert res_b.status_code == 200

    assert len(captured_auth_headers) >= 2
    assert captured_auth_headers[0] == f"Bearer {tok_a}"
    assert captured_auth_headers[1] == f"Bearer {tok_b}"
    assert tok_b not in captured_auth_headers[0]
    assert tok_a not in captured_auth_headers[1]
