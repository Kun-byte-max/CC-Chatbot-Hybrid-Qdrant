"""
test_phase231_people_search_exception_path.py — Phase 2.3.1 Regression & Exception Propagation Tests.

Verifies:
1. A successful People Search still returns its existing result shape and metadata.
2. A repository exception propagates instead of becoming a successful empty result or LLM-composed response.
3. A scoring exception follows the same historical exception behavior.
4. Telemetry failure does not mask the original search exception.
5. Exception messages and sensitive data are not added to telemetry.
6. Existing employer search failure behavior remains unchanged.
"""

import unittest
from unittest.mock import patch, MagicMock
import pytest

from backend.schemas.search_criteria import SearchCriteria, LocationMode
from backend.services.location_service import LocationResult
from backend.api.employee import handle_employee_context
from backend.api.employer import handle_employer_context
from backend.utils.timing import start_request_timing, get_request_timing
from backend.utils.telemetry import validate_retrieval_metadata


class TestPhase231PeopleSearchExceptionPath(unittest.IsolatedAsyncioTestCase):
    """Targeted regression tests for Phase 2.3.1 failure-path correction."""

    async def test_1_successful_people_search_shape_and_metadata(self):
        """1. A successful People Search still returns its existing result shape and metadata."""
        start_request_timing("test_req_success")
        criteria = SearchCriteria(
            title="Software Developer",
            location="Delhi",
            location_mode=LocationMode.EXPLICIT,
        )
        fake_people = [
            {
                "id": 101,
                "name": "Aarav Sharma",
                "fname": "Aarav",
                "lname": "Sharma",
                "current_title": "Software Developer",
                "company": "Tech Corp",
                "location": "Delhi",
                "city": "Delhi",
                "state": "Delhi",
                "country": "India",
                "skills": ["Python", "FastAPI"],
                "total_experience_years": 4.0,
                "profile": None,
            }
        ]
        loc_result = LocationResult(resolved=True, city_id=1, state_id=10, city_name="Delhi")

        with patch("backend.services.people_search_service.resolve_location_from_db", return_value=loc_result), \
             patch("backend.repositories.people_repository.PeopleRepository.search_people", return_value=fake_people), \
             patch("backend.utils.session_state.get_candidate_search_criteria", return_value=None), \
             patch("backend.utils.session_state.set_candidate_search_criteria"):

            res = await handle_employee_context(
                last_user_msg="find software developers in delhi",
                session_id="test_success_session",
                parsed_query={"search_target": "PEOPLE_SEARCH", "location": "Delhi"},
            )

            self.assertIsNotNone(res)
            self.assertEqual(res.get("result_type"), "people")
            self.assertIn("db_context", res)
            self.assertEqual(len(res.get("results", [])), 1)
            self.assertEqual(res["results"][0]["name"], "Aarav Sharma")

            telemetry = res.get("search_metadata")
            self.assertIsNotNone(telemetry)
            self.assertEqual(telemetry["search_mode"], "PEOPLE_SEARCH")
            self.assertEqual(telemetry["people_search_outcome"], "success_with_results")
            self.assertEqual(telemetry["provider_status"], "success")
            self.assertTrue(telemetry["retrieval_attempted"])
            self.assertEqual(telemetry["retrieval_result_count"], 1)
            self.assertEqual(telemetry["final_count"], 1)

    async def test_2_repository_exception_propagates_without_swallowing(self):
        """2. A repository exception propagates instead of becoming a successful empty result or LLM-composed response."""
        start_request_timing("test_req_repo_fail")
        loc_result = LocationResult(resolved=True, city_id=1, state_id=10, city_name="Delhi")

        with patch("backend.services.people_search_service.resolve_location_from_db", return_value=loc_result), \
             patch("backend.repositories.people_repository.PeopleRepository.search_people", side_effect=RuntimeError("MySQL connection dead")), \
             patch("backend.utils.session_state.get_candidate_search_criteria", return_value=None), \
             patch("backend.utils.session_state.set_candidate_search_criteria"):

            # The original exception MUST propagate out of handle_employee_context
            with self.assertRaises(RuntimeError) as cm:
                await handle_employee_context(
                    last_user_msg="find software developers in delhi",
                    session_id="test_repo_fail_session",
                    parsed_query={"search_target": "PEOPLE_SEARCH", "location": "Delhi"},
                )

            # Confirm exception message is preserved
            self.assertIn("MySQL connection dead", str(cm.exception))

            # Confirm safe privacy-compliant telemetry was recorded on the timing context before raising
            timing = get_request_timing()
            self.assertIsNotNone(timing)
            telemetry = timing.retrieval_metadata
            self.assertIsNotNone(telemetry)
            self.assertEqual(telemetry.get("search_mode"), "PEOPLE_SEARCH")
            self.assertEqual(telemetry.get("provider_status"), "unavailable")
            self.assertEqual(telemetry.get("people_search_outcome"), "provider_failure")
            self.assertEqual(telemetry.get("retrieval_error_type"), "RuntimeError")
            self.assertTrue(telemetry.get("retrieval_attempted"))

    async def test_3_scoring_exception_propagates(self):
        """3. A scoring exception follows the same historical exception behavior."""
        start_request_timing("test_req_score_fail")
        loc_result = LocationResult(resolved=True, city_id=1, state_id=10, city_name="Delhi")
        fake_people = [
            {
                "id": 102,
                "name": "Priya Singh",
                "fname": "Priya",
                "lname": "Singh",
                "current_title": "Developer",
                "company": "Co",
                "location": "Delhi",
                "skills": ["Java"],
                "total_experience_years": 3.0,
                "profile": None,
            }
        ]

        with patch("backend.services.people_search_service.resolve_location_from_db", return_value=loc_result), \
             patch("backend.repositories.people_repository.PeopleRepository.search_people", return_value=fake_people), \
             patch("backend.services.people_scoring_engine.EmployeePeopleScoringEngine.score_candidate", side_effect=ValueError("Corrupted scoring weights")), \
             patch("backend.utils.session_state.get_candidate_search_criteria", return_value=None), \
             patch("backend.utils.session_state.set_candidate_search_criteria"):

            with self.assertRaises(ValueError) as cm:
                await handle_employee_context(
                    last_user_msg="find java developers",
                    session_id="test_scoring_fail_session",
                    parsed_query={"search_target": "PEOPLE_SEARCH", "skills": ["Java"]},
                )

            self.assertEqual(str(cm.exception), "Corrupted scoring weights")

            timing = get_request_timing()
            self.assertIsNotNone(timing)
            telemetry = timing.retrieval_metadata
            self.assertIsNotNone(telemetry)
            self.assertEqual(telemetry.get("retrieval_error_type"), "ValueError")
            self.assertEqual(telemetry.get("people_search_outcome"), "provider_failure")

    async def test_4_telemetry_failure_does_not_mask_search_exception(self):
        """4. Telemetry failure does not mask the original search exception."""
        start_request_timing("test_req_telemetry_fail")
        loc_result = LocationResult(resolved=True, city_id=1, state_id=10, city_name="Delhi")

        with patch("backend.services.people_search_service.resolve_location_from_db", return_value=loc_result), \
             patch("backend.repositories.people_repository.PeopleRepository.search_people", side_effect=KeyError("db_col_missing")), \
             patch("backend.utils.timing.set_retrieval_metadata", side_effect=Exception("Telemetry internal crash")), \
             patch("backend.utils.session_state.get_candidate_search_criteria", return_value=None), \
             patch("backend.utils.session_state.set_candidate_search_criteria"):

            # The search exception KeyError must still be raised, NOT the telemetry Exception
            with self.assertRaises(KeyError) as cm:
                await handle_employee_context(
                    last_user_msg="find software developers",
                    session_id="test_telem_err_session",
                    parsed_query={"search_target": "PEOPLE_SEARCH"},
                )

            self.assertIn("db_col_missing", str(cm.exception))

    async def test_5_exception_messages_and_sensitive_data_excluded_from_telemetry(self):
        """5. Exception messages, SQL queries, and sensitive data are not added to telemetry."""
        start_request_timing("test_req_pii_safety")
        loc_result = LocationResult(resolved=True, city_id=1, state_id=10, city_name="Delhi")
        sensitive_error_msg = "SQL Error: SELECT * FROM users WHERE ssn='123-45-6789' AND password_hash='super_secret_token'"

        with patch("backend.services.people_search_service.resolve_location_from_db", return_value=loc_result), \
             patch("backend.repositories.people_repository.PeopleRepository.search_people", side_effect=RuntimeError(sensitive_error_msg)), \
             patch("backend.utils.session_state.get_candidate_search_criteria", return_value=None), \
             patch("backend.utils.session_state.set_candidate_search_criteria"):

            with self.assertRaises(RuntimeError):
                await handle_employee_context(
                    last_user_msg="find software developers",
                    session_id="test_pii_session",
                    parsed_query={"search_target": "PEOPLE_SEARCH"},
                )

            timing = get_request_timing()
            self.assertIsNotNone(timing)
            telemetry = timing.retrieval_metadata
            self.assertIsNotNone(telemetry)

            telemetry_str = str(telemetry)
            self.assertNotIn("123-45-6789", telemetry_str)
            self.assertNotIn("super_secret_token", telemetry_str)
            self.assertNotIn("SELECT * FROM", telemetry_str)

            # Error type must only be the class name
            self.assertEqual(telemetry.get("retrieval_error_type"), "RuntimeError")

            # Must pass validator cleanly
            clean = validate_retrieval_metadata(telemetry)
            self.assertEqual(clean.get("retrieval_error_type"), "RuntimeError")
            self.assertEqual(clean.get("people_search_outcome"), "provider_failure")

    async def test_6_employer_search_failure_behavior_remains_unchanged(self):
        """6. Existing employer search failure behavior remains unchanged."""
        with patch("backend.services.employer_search_executor.execute_employer_candidate_search", side_effect=ConnectionError("Meilisearch down")):
            res = await handle_employer_context(
                last_user_msg="find java candidates",
                session_id="test_employer_sess_unchanged",
                parsed_query={"search_target": "CANDIDATE_SEARCH", "skills": ["Java"]},
            )

            # Employer candidate search maintains its existing error response dictionary contract
            self.assertIsNotNone(res)
            self.assertFalse(res.get("success"))
            self.assertEqual(res.get("status"), "unavailable")
            self.assertEqual(res.get("message"), "I couldn't complete the candidate search right now.")

            telemetry = res.get("search_metadata")
            self.assertIsNotNone(telemetry)
            self.assertEqual(telemetry.get("search_mode"), "CANDIDATE_SEARCH")
            self.assertEqual(telemetry.get("provider_status"), "unavailable")
            self.assertEqual(telemetry.get("candidate_search_outcome"), "provider_failure")
            self.assertEqual(telemetry.get("retrieval_error_type"), "ConnectionError")
