"""
test_phase5_static_token_migration.py — Phase 5B Test Suite.

Verifies:
1. Dynamic token proxy execution.
2. HTTP 401/403 fails closed immediately without admin retries or local DB personal fallback.
3. Chatbot JWT is NEVER forwarded as outbound platform API token.
4. Intentionally public random-widget endpoint behavior.
5. Zero static token references in migrated code.
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient

from backend.main import app

client = TestClient(app)


def test_zero_static_token_references_in_main_py():
    """Verify PLATFORM_ADMIN_TOKEN and PLATFORM_TEST_TOKEN do not exist in backend/main.py execution paths."""
    with open("backend/main.py", "r", encoding="utf-8") as f:
        content = f.read()

    assert "PLATFORM_ADMIN_TOKEN" not in content
    assert "PLATFORM_TEST_TOKEN" not in content


def test_user_details_external_401_fails_closed_without_db_fallback():
    """
    HTTP 401 from platform API MUST fail closed immediately:
    - Zero admin retries
    - NO local DB personal fallback payload returned
    """
    mock_res = MagicMock()
    mock_res.status_code = 401
    mock_res.json.return_value = {"status": False, "message": "Token Missing!"}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_res
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    mock_user = {
        "id": 19,
        "individual_id": "CCE1001",
        "fname": "Test",
        "lname": "User",
        "email": "test@example.com",
        "token": "valid_user_platform_token_12345"
    }

    from backend.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: mock_user

    try:
        with patch("httpx.AsyncClient", return_value=mock_client):

            response = client.get("/api/response/employee/user-detail")
            assert response.status_code == 200
            data = response.json()

            # Must fail closed: data is None and message states unauthorized
            assert data.get("status") is False
            assert data.get("data") is None
            assert "unauthorized" in data.get("message", "").lower()
            
            # Verify AsyncClient.get was called exactly ONCE (no admin retry attempt)
            assert mock_client.get.call_count == 1
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_user_profile_external_403_fails_closed():
    """
    HTTP 403 Forbidden from platform API on user-profile MUST fail closed:
    - Zero admin retries
    - NO DB fallback
    """
    mock_res = MagicMock()
    mock_res.status_code = 403
    mock_res.json.return_value = {"status": False, "message": "Forbidden"}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_res
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    mock_db_user = {"id": 19, "slug": "test-user-slug", "token": "user_tok_123"}
    mock_payload = {"uid": 19, "email": "test@example.com"}

    with patch("backend.main.verify_token", return_value=mock_payload), \
         patch("backend.api.auth.find_user_by_token", return_value=mock_db_user), \
         patch("httpx.AsyncClient", return_value=mock_client):

        headers = {"Authorization": "Bearer valid_jwt_token_123"}
        response = client.get("/wapi/auth/user-profile/test-user-slug", headers=headers)
        assert response.status_code == 200
        data = response.json()

        assert data.get("status") is False
        assert data.get("data") is None
        assert mock_client.get.call_count == 1


def test_random_widget_public_unauthenticated_access():
    """
    /wapi/random-widget is intentionally public:
    - Allows unauthenticated access
    - Falls back to generic public DB widgets on non-200 without exposing personal user data
    """
    mock_res = MagicMock()
    mock_res.status_code = 404

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_res
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    mock_public_widgets = [
        {"slug": "position-closing-soon", "heading": "Position Closing Soon", "list": []},
        {"slug": "organizations-near-you", "heading": "Organizations Near You", "list": []}
    ]

    with patch("httpx.AsyncClient", return_value=mock_client), \
         patch("backend.services.user_data_service._get_random_widgets_fallback", return_value={"status": True, "data": mock_public_widgets}):

        response = client.get("/wapi/random-widget")
        assert response.status_code == 200
        data = response.json()

        assert data.get("status") is True
        assert isinstance(data.get("data"), list)
        # Verify no admin retry occurred
        assert mock_client.get.call_count == 1


def test_chatbot_jwt_never_forwarded_to_platform_api():
    """
    Asserts outbound HTTP headers contain resolved DB platform token, NEVER Chatbot JWT.
    """
    mock_res = MagicMock()
    mock_res.status_code = 200
    mock_res.json.return_value = {"status": True, "data": {"id": 19}}

    captured_headers = {}

    async def mock_get(url, headers=None, **kwargs):
        nonlocal captured_headers
        captured_headers = headers or {}
        return mock_res

    mock_client = AsyncMock()
    mock_client.get.side_effect = mock_get
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    chatbot_jwt = "header_chatbot_jwt_token_secret_xyz"
    db_platform_token = "db_resolved_platform_token_9876"
    mock_user = {"id": 19, "token": db_platform_token}

    from backend.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: mock_user

    try:
        with patch("httpx.AsyncClient", return_value=mock_client):

            headers = {"Authorization": f"Bearer {chatbot_jwt}"}
            response = client.get("/api/response/employee/user-detail", headers=headers)
            assert response.status_code == 200

            # Outbound header MUST contain db_platform_token, NEVER chatbot_jwt
            assert chatbot_jwt not in captured_headers.get("Authorization", "")
            assert chatbot_jwt not in captured_headers.get("X-Auth-Token", "")
            assert db_platform_token in captured_headers.get("Authorization", "")
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_user_details_authenticated_user_missing_platform_token_fails_closed():
    """
    Verifies behavior when an authenticated user has NO resolved platform token (token=None/empty):
    1. Outbound platform API call is made with empty token headers.
    2. Zero static admin/test credentials (PLATFORM_ADMIN_TOKEN / PLATFORM_TEST_TOKEN) are substituted.
    3. External platform API returns 401 Unauthorized.
    4. Handler fails closed immediately with status=False and data=None (NO personal DB fallback data exposed).
    """
    mock_res = MagicMock()
    mock_res.status_code = 401
    mock_res.json.return_value = {"status": False, "message": "Token Missing!"}

    captured_headers = {}

    async def mock_get(url, headers=None, **kwargs):
        nonlocal captured_headers
        captured_headers = headers or {}
        return mock_res

    mock_client = AsyncMock()
    mock_client.get.side_effect = mock_get
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    # Authenticated user record with NO platform token (token=None)
    mock_user_no_token = {
        "id": 19,
        "individual_id": "CCE1001",
        "fname": "Test",
        "lname": "User",
        "email": "test@example.com",
        "token": None
    }

    from backend.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: mock_user_no_token

    try:
        with patch("httpx.AsyncClient", return_value=mock_client):
            response = client.get("/api/response/employee/user-detail")
            assert response.status_code == 200
            data = response.json()

            # Must fail closed: status is False and data is None
            assert data.get("status") is False
            assert data.get("data") is None
            assert "unauthorized" in data.get("message", "").lower()

            # Outbound call verification: exactly 1 call, zero static admin/test token substitution
            auth_header = captured_headers.get("Authorization")
            auth_str = str(auth_header or "")
            x_auth_header = captured_headers.get("X-Auth-Token", "")
            assert auth_header is None or auth_header == ""
            assert x_auth_header == ""
            assert "PLATFORM_ADMIN_TOKEN" not in auth_str
            assert "PLATFORM_TEST_TOKEN" not in auth_str


    finally:
        app.dependency_overrides.pop(get_current_user, None)

