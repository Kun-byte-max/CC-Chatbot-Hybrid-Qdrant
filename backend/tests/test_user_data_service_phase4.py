import unittest
from unittest.mock import MagicMock, patch
import pytest

from backend.services.user_data_service import (
    _parse_positive_int_id,
    _get_employment_db_fallback,
    _get_user_db_fallback,
    _get_user_skills_db_fallback
)


class TestUserDataServicePhase4(unittest.TestCase):


    def test_parse_positive_int_id_valid(self):
        self.assertEqual(_parse_positive_int_id(123), 123)
        self.assertEqual(_parse_positive_int_id("456"), 456)

    def test_parse_positive_int_id_invalid(self):
        self.assertIsNone(_parse_positive_int_id(True))
        self.assertIsNone(_parse_positive_int_id(False))
        self.assertIsNone(_parse_positive_int_id(None))
        self.assertIsNone(_parse_positive_int_id(0))
        self.assertIsNone(_parse_positive_int_id(-5))
        self.assertIsNone(_parse_positive_int_id("abc"))

    @patch("backend.repositories.db.get_db")
    def test_get_employment_db_fallback_verification_semantics(self, mock_get_db):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor

        # Mock dict rows returned by DictCursor
        mock_rows = [
            # Verified role: approved=1, status=1
            {
                "id": 1,
                "company_id": "10",
                "company_name": "Acme Corp",
                "designation": "Software Engineer",
                "joining_date": "2020-01-01",
                "worked_till_date": "2022-01-01",
                "still_working": "0",
                "salary": 1000,
                "salary_inhand": 900,
                "salary_mode": "bank",
                "approved": "1",
                "status": "1",
                "company_label": "Acme Corp"
            },
            # Unverified role (approved=0, status=1)
            {
                "id": 2,
                "company_id": "10",
                "company_name": "Acme Corp",
                "designation": "Senior Engineer",
                "joining_date": "2022-01-02",
                "worked_till_date": None,
                "still_working": "1",
                "salary": 1500,
                "salary_inhand": 1400,
                "salary_mode": "bank",
                "approved": "0",
                "status": "1",
                "company_label": "Acme Corp"
            },
            # Unverified role (approved=1, status=0)
            {
                "id": 3,
                "company_id": "20",
                "company_name": "Beta Inc",
                "designation": "Consultant",
                "joining_date": "2019-01-01",
                "worked_till_date": "2019-12-31",
                "still_working": "0",
                "salary": 2000,
                "salary_inhand": 1800,
                "salary_mode": "bank",
                "approved": "1",
                "status": "0",
                "company_label": "Beta Inc"
            }
        ]
        mock_cursor.fetchall.return_value = mock_rows

        res = _get_employment_db_fallback(123)

        self.assertTrue(res["status"])
        data = res["data"]
        self.assertEqual(len(data), 2)  # Two unique companies (Acme Corp company_id "10", Beta Inc company_id "20")

        # Acme Corp (id 10): Company is_verified is None (unknown), while individual roles are evaluated independently
        acme = next(c for c in data if c["company_id"] == "10")
        self.assertIsNone(acme["is_verified"])
        self.assertEqual(len(acme["lists"]), 2)

        role1 = next(r for r in acme["lists"] if r["id"] == "1")
        self.assertTrue(role1["is_verified"])

        role2 = next(r for r in acme["lists"] if r["id"] == "2")
        self.assertFalse(role2["is_verified"])

        # Beta Inc (id 20): Company is_verified is None (unknown), role is not verified
        beta = next(c for c in data if c["company_id"] == "20")
        self.assertIsNone(beta["is_verified"])

        role3 = beta["lists"][0]
        self.assertFalse(role3["is_verified"])

    @patch("backend.repositories.db.get_db")
    def test_get_employment_db_fallback_db_error_raises_runtime_error(self, mock_get_db):
        mock_get_db.side_effect = Exception("Database connection failed")

        with self.assertRaises(RuntimeError) as ctx:
            _get_employment_db_fallback(123)
        self.assertIn("Database error during employment fallback execution", str(ctx.exception))


if __name__ == "__main__":

    unittest.main()
