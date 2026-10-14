"""
test_phase2_7b_option_mapping_routing.py — Integration tests verifying session option mapping routing safety.
"""

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from backend.main import app
from backend.utils.session_state import set_session_option_mapping
from backend.services.user_data_intent_parser import parse_user_data_intents_deterministic, UserIntent
from backend.api.auth import verify_token, get_current_user

CLIENT = TestClient(app)

MOCK_TOKEN_PAYLOAD = {"uid": 123, "id": 123, "email": "test@example.com", "role": "employee"}
app.dependency_overrides[verify_token] = lambda: MOCK_TOKEN_PAYLOAD
app.dependency_overrides[get_current_user] = lambda: MOCK_TOKEN_PAYLOAD

HEADERS = {
    "Authorization": "Bearer mock_valid_test_token_1234567890",
    "X-User-Id": "123"
}


def test_job_search_with_active_employment_options():
    """A. Job search with active employment options must NOT be hijacked into user_data verification."""
    session_id = "test_sess_job_search_opt_map"
    set_session_option_mapping(session_id, {"emp_rec_1": "company_a::role_1", "emp_rec_2": "company_b::role_2"})
    
    query = "show me jobs with python as the skill"
    
    intents = parse_user_data_intents_deterministic(query)
    assert intents == []

    with patch("backend.services.user_data_service.handle_query") as mock_uds_handle:
        response = CLIENT.post(
            "/chat",
            json={"session_id": session_id, "messages": [{"role": "user", "content": query}]},
            headers=HEADERS
        )
        mock_uds_handle.assert_not_called()
        assert response.status_code == 200


def test_profile_skill_job_search_with_active_employment_options():
    """B. Profile-skill job search with active employment options must bypass option mapping fallback."""
    session_id = "test_sess_profile_skill_search"
    set_session_option_mapping(session_id, {"emp_rec_1": "company_a::role_1"})

    query = "find jobs according to my skills"

    intents = parse_user_data_intents_deterministic(query)
    assert intents == []

    with patch("backend.services.user_data_service.handle_query") as mock_uds_handle:
        response = CLIENT.post(
            "/chat",
            json={"session_id": session_id, "messages": [{"role": "user", "content": query}]},
            headers=HEADERS
        )
        mock_uds_handle.assert_not_called()
        assert response.status_code == 200


def test_candidate_search_with_active_employment_options():
    """C. Candidate search with active employment options must NOT be hijacked into user_data verification."""
    session_id = "test_sess_candidate_search"
    set_session_option_mapping(session_id, {"emp_rec_1": "company_a::role_1"})

    query = "find candidates with python as a skill"

    intents = parse_user_data_intents_deterministic(query)
    assert intents == []

    with patch("backend.services.user_data_service.handle_query") as mock_uds_handle:
        response = CLIENT.post(
            "/chat",
            json={"session_id": session_id, "messages": [{"role": "user", "content": query}]},
            headers=HEADERS
        )
        mock_uds_handle.assert_not_called()
        assert response.status_code == 200


def test_genuine_employment_query():
    """D. Genuine employment query must still resolve to user_data intent."""
    query = "show my employment history"
    intents = parse_user_data_intents_deterministic(query)
    assert UserIntent.EMPLOYMENT_HISTORY in intents


def test_valid_numeric_selection():
    """E. Valid numeric selection (e.g. '1') matching active option mapping triggers user_data verification."""
    session_id = "test_sess_valid_numeric"
    set_session_option_mapping(session_id, {"emp_rec_1": "company_a::role_1", "emp_rec_2": "company_b::role_2"})

    query = "1"

    with patch("backend.services.user_data_service.handle_query") as mock_uds_handle:
        mock_uds_handle.return_value = MagicMock(
            handled=True,
            reply="Selected Option 1",
            results_payload=[],
            result_type_payload="employment_verification",
            presentation_mode="FOCUSED",
            action_metadata=None,
            selection_metadata=None,
            requested_level=None
        )
        response = CLIENT.post(
            "/chat",
            json={"session_id": session_id, "messages": [{"role": "user", "content": query}]},
            headers=HEADERS
        )
        mock_uds_handle.assert_called_once()
        assert response.status_code == 200


def test_valid_explicit_option_identifier():
    """F. Valid explicit option identifier ('emp_rec_1') triggers user_data verification."""
    session_id = "test_sess_valid_key"
    set_session_option_mapping(session_id, {"emp_rec_1": "company_a::role_1"})

    query = "emp_rec_1"

    with patch("backend.services.user_data_service.handle_query") as mock_uds_handle:
        mock_uds_handle.return_value = MagicMock(
            handled=True,
            reply="Selected emp_rec_1",
            results_payload=[],
            result_type_payload="employment_verification",
            presentation_mode="FOCUSED",
            action_metadata=None,
            selection_metadata=None,
            requested_level=None
        )
        response = CLIENT.post(
            "/chat",
            json={"session_id": session_id, "messages": [{"role": "user", "content": query}]},
            headers=HEADERS
        )
        mock_uds_handle.assert_called_once()
        assert response.status_code == 200


def test_invalid_selection_does_not_trigger_user_data():
    """H. An unrelated message must not be converted into employment verification merely because mapping exists."""
    session_id = "test_sess_unrelated"
    set_session_option_mapping(session_id, {"emp_rec_1": "company_a::role_1"})

    query = "tell me a joke"

    with patch("backend.services.user_data_service.handle_query") as mock_uds_handle:
        response = CLIENT.post(
            "/chat",
            json={"session_id": session_id, "messages": [{"role": "user", "content": query}]},
            headers=HEADERS
        )
        mock_uds_handle.assert_not_called()
        assert response.status_code == 200


def test_no_mapping_behavior():
    """I. Verify ordinary routing remains unchanged when no option mapping exists."""
    session_id = "test_sess_no_map"
    query = "show me jobs with python as the skill"

    with patch("backend.services.user_data_service.handle_query") as mock_uds_handle:
        response = CLIENT.post(
            "/chat",
            json={"session_id": session_id, "messages": [{"role": "user", "content": query}]},
            headers=HEADERS
        )
        mock_uds_handle.assert_not_called()
        assert response.status_code == 200
