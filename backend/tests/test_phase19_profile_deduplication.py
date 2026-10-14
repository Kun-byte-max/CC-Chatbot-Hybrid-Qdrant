import pytest
from unittest.mock import patch, MagicMock
from backend.services.user_data_service import fetch_endpoint_data

def test_phase19_user_profile_fallback_deduplication():
    """
    Verify that fetch_endpoint_data("user-profile") calls _get_user_db_fallback exactly ONCE
    when user_slug is None and platform API returns fallback/None.
    """
    mock_user_row = {
        "id": 1234,
        "individual_id": "IND-1234",
        "fname": "Test",
        "lname": "User",
        "email": "test@example.com",
        "phone": "9999999999",
        "dob": "1990-01-01",
        "profile_description": "Developer",
        "slug": "test-user-slug",
        "city_name": "Delhi",
        "state_name": "Delhi"
    }

    mock_skills = [{"skill": "Python"}, {"skill": "FastAPI"}]

    with patch("backend.services.user_data_service._get_user_db_fallback") as mock_user_db, \
         patch("backend.services.user_data_service._get_user_skills_db_fallback") as mock_skills_db, \
         patch("backend.services.platform_api._get") as mock_platform_get:
        
        mock_user_db.return_value = mock_user_row
        mock_skills_db.return_value = mock_skills
        mock_platform_get.return_value = {"status": False, "message": "API error"}

        res = fetch_endpoint_data("user-profile", token=None, user_slug=None, user_id=1234)

        # A. Deduplication verification
        assert mock_user_db.call_count == 1, f"Expected 1 call to _get_user_db_fallback, got {mock_user_db.call_count}"

        # B. Profile payload preservation
        assert res["status"] is True
        assert res["message"] == "User Profile (DB Fallback)"
        data = res["data"]
        assert data["id"] == "1234"
        assert data["individual_id"] == "IND-1234"
        assert data["fname"] == "Test"
        assert data["lname"] == "User"
        assert data["full_name"] == "Test User"
        assert data["email"] == "test@example.com"
        assert data["phone"] == "9999999999"
        assert data["dob"] == "1990-01-01"
        assert data["profile_description"] == "Developer"
        assert data["user_slug"] == "test-user-slug"
        assert data["city_name"] == "Delhi"
        assert data["state_name"] == "Delhi"
        assert data["skills"] == ["Python", "FastAPI"]

def test_phase19_missing_user_row():
    """C. Verify behavior when _get_user_db_fallback returns None."""
    with patch("backend.services.user_data_service._get_user_db_fallback") as mock_user_db, \
         patch("backend.services.platform_api._get") as mock_platform_get:
        
        mock_user_db.return_value = None
        mock_platform_get.return_value = {"status": False}

        res = fetch_endpoint_data("user-profile", token=None, user_slug=None, user_id=1234)
        assert res == {"status": False, "message": "User profile unavailable", "data": None}
        # When first call returns None, user_row remains None, so fallback check calls it again (2 calls total)
        assert mock_user_db.call_count == 2

def test_phase19_db_exception_behavior():
    """D. Verify exception raising from _get_user_db_fallback is preserved."""
    with patch("backend.services.user_data_service._get_user_db_fallback") as mock_user_db:
        mock_user_db.side_effect = RuntimeError("Database error")

        with pytest.raises(RuntimeError, match="Database error"):
            fetch_endpoint_data("user-profile", token=None, user_slug=None, user_id=1234)

def test_phase19_null_profile_fields():
    """E. Verify null or missing profile fields handle correctly."""
    mock_user_row = {
        "id": 5678,
        "individual_id": None,
        "fname": None,
        "lname": None,
        "email": None,
        "phone": None,
        "dob": None,
        "profile_description": None,
        "slug": "slug-only",
        "city_name": None,
        "state_name": None
    }

    with patch("backend.services.user_data_service._get_user_db_fallback") as mock_user_db, \
         patch("backend.services.user_data_service._get_user_skills_db_fallback") as mock_skills_db, \
         patch("backend.services.platform_api._get") as mock_platform_get:
        
        mock_user_db.return_value = mock_user_row
        mock_skills_db.return_value = []
        mock_platform_get.return_value = {"status": False}

        res = fetch_endpoint_data("user-profile", token=None, user_slug=None, user_id=5678)
        assert res["status"] is True
        data = res["data"]
        assert data["id"] == "5678"
        assert data["full_name"] == "None None"
        assert data["dob"] == ""
        assert data["skills"] == []

def test_phase19_request_isolation():
    """F. Verify that separate requests make their own fallback queries."""
    with patch("backend.services.user_data_service._get_user_db_fallback") as mock_user_db, \
         patch("backend.services.platform_api._get") as mock_platform_get:
        
        mock_user_db.side_effect = [
            {"id": 1, "slug": "user-1"},
            {"id": 2, "slug": "user-2"}
        ]
        mock_platform_get.return_value = {"status": False}

        res1 = fetch_endpoint_data("user-profile", user_id=1)
        res2 = fetch_endpoint_data("user-profile", user_id=2)

        assert res1["data"]["id"] == "1"
        assert res2["data"]["id"] == "2"
        assert mock_user_db.call_count == 2
