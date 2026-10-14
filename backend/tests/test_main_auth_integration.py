import unittest
from unittest.mock import MagicMock, patch
import jwt
from fastapi.testclient import TestClient

from backend.main import app
from backend.config.config import JWT_SECRET, JWT_ALGORITHM


class TestMainAuthIntegration(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(app)

    @patch("backend.api.auth.get_db")
    @patch("backend.services.token_resolver.get_db")
    @patch("httpx.Client")
    def test_chat_jwt_auth_does_not_forward_jwt_to_platform_api(self, mock_httpx, mock_resolver_get_db, mock_auth_get_db):
        db_user_row = {
            "id": 888,
            "individual_id": "IND888",
            "fname": "Alice",
            "lname": "Smith",
            "email": "alice@example.com",
            "user_type": 1,
            "status": 1,
            "is_deleted": 0,
            "token": "db_resolved_platform_token_xyz"
        }

        # Mock DB for auth.py
        mock_auth_conn = MagicMock()
        mock_auth_cursor = MagicMock()
        mock_auth_get_db.return_value = mock_auth_conn
        mock_auth_conn.cursor.return_value = mock_auth_cursor
        mock_auth_cursor.fetchall.return_value = [db_user_row]

        # Mock DB for token_resolver.py
        mock_res_conn = MagicMock()
        mock_res_cursor = MagicMock()
        mock_resolver_get_db.return_value = mock_res_conn
        mock_res_conn.cursor.return_value = mock_res_cursor
        mock_res_cursor.fetchone.return_value = {"token": "db_resolved_platform_token_xyz"}

        # Mock outbound HTTP client
        mock_http_instance = MagicMock()
        mock_httpx.return_value.__enter__.return_value = mock_http_instance
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": True, "data": {"fname": "Alice"}}
        mock_http_instance.get.return_value = mock_response

        # Generate a valid Chatbot JWT
        jwt_token = jwt.encode(
            {"id": 888, "uid": 888, "email": "alice@example.com", "role": "employee"},
            JWT_SECRET,
            algorithm=JWT_ALGORITHM
        )

        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "X-Auth-Token": "untrusted_header_token"
        }

        payload = {
            "messages": [{"role": "user", "content": "show my employment history"}],
            "session_id": "sess_888"
        }

        # Make Request to /chat
        res = self.client.post("/chat", json=payload, headers=headers)

        self.assertEqual(res.status_code, 200)

        # Assert Outbound HTTP headers
        self.assertTrue(mock_http_instance.get.called)
        for call_args in mock_http_instance.get.call_args_list:
            outbound_headers = call_args.kwargs.get("headers", {})
            outbound_auth = outbound_headers.get("Authorization", "")

            # PROOF: Chatbot JWT must NEVER be forwarded to platform APIs
            self.assertNotIn(jwt_token, outbound_auth)
            self.assertNotIn("untrusted_header_token", outbound_auth)

            # PROOF: Outbound request uses the resolved DB platform token
            self.assertEqual(outbound_auth, "Bearer db_resolved_platform_token_xyz")

    @patch("backend.api.auth.get_db")
    @patch("backend.services.token_resolver.get_db")
    @patch("httpx.Client")
    def test_chat_raw_token_auth_passthrough_compatibility(self, mock_httpx, mock_resolver_get_db, mock_auth_get_db):
        """
        Verifies full end-to-end flow when client authenticates via raw platform token:
        1. auth.py authenticates request via raw token lookup against cyb_user.token.
        2. main.py propagates canonical db_user_id with token=None.
        3. platform_api/token_resolver dynamically resolves cyb_user.token for the outbound request.
        """
        raw_platform_token = "raw_platform_token_secret_123"
        db_user_row = {
            "id": 777,
            "individual_id": "IND777",
            "fname": "Charlie",
            "lname": "Brown",
            "email": "charlie@example.com",
            "user_type": 1,
            "status": 1,
            "is_deleted": 0,
            "token": raw_platform_token
        }

        # Mock DB for auth.py (raw token lookup)
        mock_auth_conn = MagicMock()
        mock_auth_cursor = MagicMock()
        mock_auth_get_db.return_value = mock_auth_conn
        mock_auth_conn.cursor.return_value = mock_auth_cursor
        mock_auth_cursor.fetchall.return_value = [db_user_row]

        # Mock DB for token_resolver.py
        mock_res_conn = MagicMock()
        mock_res_cursor = MagicMock()
        mock_resolver_get_db.return_value = mock_res_conn
        mock_res_conn.cursor.return_value = mock_res_cursor
        mock_res_cursor.fetchone.return_value = {"token": raw_platform_token}

        # Mock outbound HTTP client
        mock_http_instance = MagicMock()
        mock_httpx.return_value.__enter__.return_value = mock_http_instance
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": True, "data": {}}
        mock_http_instance.get.return_value = mock_response

        headers = {
            "Authorization": f"Bearer {raw_platform_token}"
        }

        payload = {
            "messages": [{"role": "user", "content": "show my employment history"}],
            "session_id": "sess_777"
        }

        res = self.client.post("/chat", json=payload, headers=headers)
        self.assertEqual(res.status_code, 200)

        # Assert outbound HTTP request uses the authenticated user's DB platform token
        self.assertTrue(mock_http_instance.get.called)
        for call_args in mock_http_instance.get.call_args_list:
            outbound_headers = call_args.kwargs.get("headers", {})
            self.assertEqual(outbound_headers.get("Authorization"), f"Bearer {raw_platform_token}")

    @patch("backend.api.auth.get_db")
    @patch("backend.services.token_resolver.get_db")
    @patch("httpx.Client")
    def test_chat_conflicting_headers_ignores_untrusted_header(self, mock_httpx, mock_resolver_get_db, mock_auth_get_db):
        db_user_row = {
            "id": 999,
            "individual_id": "IND999",
            "fname": "Bob",
            "lname": "Jones",
            "email": "bob@example.com",
            "user_type": 1,
            "status": 1,
            "is_deleted": 0,
            "token": "valid_db_token_999"
        }

        # Mock DB for auth.py
        mock_auth_conn = MagicMock()
        mock_auth_cursor = MagicMock()
        mock_auth_get_db.return_value = mock_auth_conn
        mock_auth_conn.cursor.return_value = mock_auth_cursor
        mock_auth_cursor.fetchall.return_value = [db_user_row]

        # Mock DB for token_resolver.py
        mock_res_conn = MagicMock()
        mock_res_cursor = MagicMock()
        mock_resolver_get_db.return_value = mock_res_conn
        mock_res_conn.cursor.return_value = mock_res_cursor
        mock_res_cursor.fetchone.return_value = {"token": "valid_db_token_999"}

        mock_http_instance = MagicMock()
        mock_httpx.return_value.__enter__.return_value = mock_http_instance
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": True, "data": {}}
        mock_http_instance.get.return_value = mock_response

        jwt_token = jwt.encode(
            {"id": 999, "uid": 999, "email": "bob@example.com", "role": "employee"},
            JWT_SECRET,
            algorithm=JWT_ALGORITHM
        )

        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "X-Auth-Token": "attacker_supplied_token"
        }

        payload = {
            "messages": [{"role": "user", "content": "show my employment history"}],
            "session_id": "sess_999"
        }

        res = self.client.post("/chat", json=payload, headers=headers)
        self.assertEqual(res.status_code, 200)

        # Assert X-Auth-Token is ignored and resolved DB token is used outbound
        for call_args in mock_http_instance.get.call_args_list:
            outbound_headers = call_args.kwargs.get("headers", {})
            self.assertNotEqual(outbound_headers.get("X-Auth-Token"), "attacker_supplied_token")
            self.assertEqual(outbound_headers.get("Authorization"), "Bearer valid_db_token_999")


if __name__ == "__main__":
    unittest.main()
