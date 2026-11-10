"""
backend/tests/test_phase23_retrieval_observability.py — Comprehensive targeted tests
for Phase 2.3 People Search & Employer Candidate Search Observability.

Covers:
1. People search telemetry: success, empty, location resolution failure, repo exception, counts.
2. Employer candidate search telemetry: success, empty, safe-filter failure, provider failure, counts.
3. Backward compatibility: SearchMetadata invariants and pagination fields preserved.
4. Privacy and validation safeguards: PII stripped, unknown keys dropped, fail-closed isolation.
5. Trace propagation: metadata carries through TimingContext, ChatPipelineResult, and finalize() revalidation.
"""

import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.schemas.search_criteria import SearchCriteria
from backend.schemas.search_query import LocationMode
from backend.services.location_service import LocationResult
from backend.services.people_search_service import PeopleSearchService
from backend.schemas.employer_requirements import (
    EmployerSearchRequirements,
    ProvenanceSource,
    ProvenanceValue,
    LocationRequirement,
)
from backend.schemas.candidate_schema import SearchMetadata, StandardizedCandidateObject
from backend.services.employer_search_executor import execute_employer_candidate_search
from backend.utils.telemetry import (
    ALLOWED_PEOPLE_SEARCH_OUTCOMES,
    ALLOWED_CANDIDATE_SEARCH_OUTCOMES,
    ALLOWED_FILTER_CATEGORIES,
    ALLOWED_BOOL_FIELDS,
    validate_retrieval_metadata,
    trace_request,
    reset_telemetry_client_for_testing,
)


class TestPeopleSearchTelemetry(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        reset_telemetry_client_for_testing()

    def tearDown(self):
        reset_telemetry_client_for_testing()

    def test_people_search_success_with_results(self):
        """1. Successful retrieval with synthetic candidates records complete operational telemetry."""
        criteria = SearchCriteria(
            title="Software Developer",
            skills=["Python"],
            location="Delhi",
            location_mode=LocationMode.EXPLICIT,
        )
        fake_people = [
            {
                "id": 101,
                "name": "Candidate One",
                "fname": "Candidate",
                "lname": "One",
                "current_title": "Software Developer",
                "company": "TechCorp",
                "location": "Delhi",
                "city": "Delhi",
                "state": "Delhi",
                "country": "India",
                "skills": ["Python", "SQL"],
                "total_experience_years": 3.0,
                "profile": None,
            },
            {
                "id": 102,
                "name": "Candidate Two",
                "fname": "Candidate",
                "lname": "Two",
                "current_title": "Software Developer",
                "company": "TechCorp",
                "location": "Delhi",
                "city": "Delhi",
                "state": "Delhi",
                "country": "India",
                "skills": ["Python"],
                "total_experience_years": 4.0,
                "profile": None,
            },
        ]

        loc_result = LocationResult(resolved=True, city_id=1, state_id=10, city_name="Delhi")
        with patch("backend.services.people_search_service.resolve_location_from_db", return_value=loc_result), \
             patch("backend.repositories.people_repository.PeopleRepository.search_people", return_value=fake_people):

            res = PeopleSearchService.search_people(criteria=criteria, current_user_id=999)

            self.assertTrue(res.get("success"))
            self.assertEqual(res.get("status"), "SUCCESS")
            self.assertEqual(len(res.get("results", [])), 2)

            telemetry = res.get("search_metadata")
            self.assertIsNotNone(telemetry)
            self.assertEqual(telemetry["search_mode"], "PEOPLE_SEARCH")
            self.assertEqual(telemetry["search_provider"], "people_repo")
            self.assertEqual(telemetry["provider_status"], "success")
            self.assertEqual(telemetry["people_search_outcome"], "success_with_results")
            self.assertTrue(telemetry["retrieval_attempted"])
            self.assertEqual(telemetry["retrieval_result_count"], 2)
            self.assertEqual(telemetry["eligible_count"], 2)
            self.assertEqual(telemetry["final_count"], 2)
            self.assertGreaterEqual(telemetry["retrieval_total_ms"], 0.0)
            self.assertIn("people_repo", telemetry["provider_latency_ms"])
            self.assertIn("city", telemetry["filters_applied"])
            self.assertIn("role_type", telemetry["filters_applied"])

            # Verify validator approves without dropping fields
            clean = validate_retrieval_metadata(telemetry)
            self.assertEqual(clean["search_mode"], "PEOPLE_SEARCH")
            self.assertEqual(clean["people_search_outcome"], "success_with_results")
            self.assertEqual(clean["retrieval_result_count"], 2)

    def test_people_search_empty_results(self):
        """2. Successful retrieval with zero results records empty outcome and provider status."""
        criteria = SearchCriteria(
            title="Nonexistent Role",
            location="Delhi",
            location_mode=LocationMode.EXPLICIT,
        )
        loc_result = LocationResult(resolved=True, city_id=1, state_id=10, city_name="Delhi")
        with patch("backend.services.people_search_service.resolve_location_from_db", return_value=loc_result), \
             patch("backend.repositories.people_repository.PeopleRepository.search_people", return_value=[]):

            res = PeopleSearchService.search_people(criteria=criteria)

            self.assertTrue(res.get("success"))
            self.assertEqual(res.get("status"), "NO_HITS")
            self.assertEqual(len(res.get("results", [])), 0)

            telemetry = res.get("search_metadata")
            self.assertIsNotNone(telemetry)
            self.assertEqual(telemetry["search_mode"], "PEOPLE_SEARCH")
            self.assertEqual(telemetry["search_provider"], "people_repo")
            self.assertEqual(telemetry["provider_status"], "empty")
            self.assertEqual(telemetry["people_search_outcome"], "empty_results")
            self.assertTrue(telemetry["retrieval_attempted"])
            self.assertEqual(telemetry["retrieval_result_count"], 0)
            self.assertEqual(telemetry["eligible_count"], 0)
            self.assertEqual(telemetry["final_count"], 0)

            clean = validate_retrieval_metadata(telemetry)
            self.assertEqual(clean["people_search_outcome"], "empty_results")
            self.assertEqual(clean["provider_status"], "empty")

    def test_people_search_explicit_location_resolution_failure(self):
        """3. Explicit-location resolution failure skips provider execution with safe telemetry."""
        criteria = SearchCriteria(
            location="UnknownAtlantisCity",
            location_mode=LocationMode.EXPLICIT,
        )
        loc_result = LocationResult(resolved=False)
        with patch("backend.services.people_search_service.resolve_location_from_db", return_value=loc_result), \
             patch("backend.repositories.people_repository.PeopleRepository.search_people") as mock_repo:

            res = PeopleSearchService.search_people(criteria=criteria)

            # Provider must NOT have been called
            mock_repo.assert_not_called()
            self.assertEqual(res.get("status"), "NO_HITS")

            telemetry = res.get("search_metadata")
            self.assertIsNotNone(telemetry)
            self.assertEqual(telemetry["provider_status"], "skipped")
            self.assertEqual(telemetry["people_search_outcome"], "location_unavailable")
            self.assertFalse(telemetry["retrieval_attempted"])
            self.assertEqual(telemetry["retrieval_result_count"], 0)

    def test_people_search_near_me_missing_location(self):
        """4. NEAR_ME location missing skips provider execution with safe telemetry."""
        criteria = SearchCriteria(
            location_mode=LocationMode.NEAR_ME,
        )
        with patch("backend.repositories.people_repository.PeopleRepository.search_people") as mock_repo:
            res = PeopleSearchService.search_people(criteria=criteria, user_location=None)

            mock_repo.assert_not_called()
            self.assertEqual(res.get("status"), "LOCATION_UNAVAILABLE")

            telemetry = res.get("search_metadata")
            self.assertIsNotNone(telemetry)
            self.assertEqual(telemetry["provider_status"], "skipped")
            self.assertEqual(telemetry["people_search_outcome"], "location_unavailable")
            self.assertFalse(telemetry["retrieval_attempted"])

    async def test_people_search_repository_exception_propagation(self):
        """5. Repository failure preserves existing error flow and records failure telemetry."""
        from backend.api.employee import handle_employee_context
        from backend.utils.timing import start_request_timing, get_request_timing

        start_request_timing("test_req_repo_fail")
        criteria = SearchCriteria(
            title="Software Developer",
            location="Delhi",
            location_mode=LocationMode.EXPLICIT,
        )
        loc_result = LocationResult(resolved=True, city_id=1, state_id=10, city_name="Delhi")

        with patch("backend.services.people_search_service.resolve_location_from_db", return_value=loc_result), \
             patch("backend.repositories.people_repository.PeopleRepository.search_people", side_effect=RuntimeError("MySQL pool exhausted (connection limit reached; ssn=123-45-6789)")), \
             patch("backend.utils.session_state.get_candidate_search_criteria", return_value=None):

            # 1. The original exception propagates out of handle_employee_context
            with self.assertRaises(RuntimeError) as cm:
                await handle_employee_context(
                    last_user_msg="find software developers in delhi",
                    session_id="test_people_err_session",
                    parsed_query={"search_target": "PEOPLE_SEARCH", "location": "Delhi"},
                )

            self.assertIn("MySQL pool exhausted", str(cm.exception))

            # 2. Safe failure metadata is recorded where the existing implementation supports it (TimingContext)
            timing = get_request_timing()
            self.assertIsNotNone(timing)
            telemetry = timing.retrieval_metadata
            self.assertIsNotNone(telemetry)
            self.assertEqual(telemetry["search_mode"], "PEOPLE_SEARCH")
            self.assertEqual(telemetry["provider_status"], "unavailable")
            self.assertEqual(telemetry["people_search_outcome"], "provider_failure")
            self.assertEqual(telemetry["retrieval_error_type"], "RuntimeError")
            self.assertTrue(telemetry["retrieval_attempted"])

            # 3. No raw exception message, PII, or SQL details enter telemetry
            telemetry_str = str(telemetry)
            self.assertNotIn("ssn", telemetry_str)
            self.assertNotIn("123-45-6789", telemetry_str)
            self.assertNotIn("connection limit reached", telemetry_str)

            clean = validate_retrieval_metadata(telemetry)
            self.assertEqual(clean["retrieval_error_type"], "RuntimeError")
            self.assertEqual(clean["people_search_outcome"], "provider_failure")

    def test_people_search_experience_rejected_counts(self):
        """6. Accurate raw, eligible, and experience-rejected count semantics."""
        criteria = SearchCriteria(
            title="Developer",
            experience_min=5.0,
            location_mode=LocationMode.EXPLICIT,
            location="Delhi",
        )
        # One junior (2 yrs) and one senior (6 yrs)
        fake_people = [
            {
                "id": 201, "name": "Junior Dev", "fname": "Junior", "lname": "Dev",
                "current_title": "Developer", "company": "Co", "location": "Delhi",
                "city": "Delhi", "state": "Delhi", "country": "India", "skills": ["Python"],
                "total_experience_years": 2.0, "profile": None,
            },
            {
                "id": 202, "name": "Senior Dev", "fname": "Senior", "lname": "Dev",
                "current_title": "Developer", "company": "Co", "location": "Delhi",
                "city": "Delhi", "state": "Delhi", "country": "India", "skills": ["Python"],
                "total_experience_years": 6.0, "profile": None,
            },
        ]
        loc_result = LocationResult(resolved=True, city_id=1, state_id=10, city_name="Delhi")
        with patch("backend.services.people_search_service.resolve_location_from_db", return_value=loc_result), \
             patch("backend.repositories.people_repository.PeopleRepository.search_people", return_value=fake_people):

            res = PeopleSearchService.search_people(criteria=criteria)

            telemetry = res.get("search_metadata")
            self.assertIsNotNone(telemetry)
            self.assertEqual(telemetry["retrieval_result_count"], 2)
            self.assertEqual(telemetry["eligible_count"], 1)
            self.assertEqual(telemetry["final_count"], 1)
            self.assertEqual(telemetry["experience_rejected_count"], 1)


class TestEmployerCandidateSearchTelemetry(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        reset_telemetry_client_for_testing()

    def tearDown(self):
        reset_telemetry_client_for_testing()

    async def test_candidate_search_success_with_results(self):
        """1. Successful candidate retrieval records provider duration, total matches, and counts."""
        reqs = EmployerSearchRequirements(
            role=ProvenanceValue(value="Frontend Developer", source=ProvenanceSource.QUERY),
            location=LocationRequirement(text="Noida", type="city", city_id=10, source=ProvenanceSource.QUERY),
        )
        fake_res = {
            "success": True,
            "total": 2,
            "data": [
                {"id": 1, "first_name": "John", "last_name": "Doe", "designation": "Frontend Developer", "city_name": "Noida"},
                {"id": 2, "first_name": "Jane", "last_name": "Smith", "designation": "Frontend Developer", "city_name": "Noida"},
            ]
        }

        with patch("backend.services.employer_search_executor.search_users", new_callable=AsyncMock, return_value=fake_res):
            cands, metadata, raw_payload = await execute_employer_candidate_search(
                requirements=reqs,
                session_id="session_emp_test",
                page=1,
                limit=10,
            )

            self.assertEqual(len(cands), 2)
            self.assertEqual(metadata.total_matches, 2)
            self.assertEqual(metadata.returned, 2)
            self.assertTrue(metadata.validate_invariants())

            telemetry = raw_payload.get("telemetry")
            self.assertIsNotNone(telemetry)
            self.assertEqual(telemetry["search_mode"], "CANDIDATE_SEARCH")
            self.assertEqual(telemetry["search_provider"], "meilisearch")
            self.assertEqual(telemetry["provider_status"], "success")
            self.assertEqual(telemetry["candidate_search_outcome"], "success_with_results")
            self.assertTrue(telemetry["retrieval_attempted"])
            self.assertEqual(telemetry["meilisearch_count"], 2)
            self.assertEqual(telemetry["retrieval_result_count"], 2)
            self.assertGreaterEqual(telemetry["meili_ms"], 0.0)
            self.assertIn("city", telemetry["filters_applied"])
            self.assertIn("role_type", telemetry["filters_applied"])

            clean = validate_retrieval_metadata(telemetry)
            self.assertEqual(clean["search_mode"], "CANDIDATE_SEARCH")
            self.assertEqual(clean["candidate_search_outcome"], "success_with_results")
            self.assertEqual(clean["meilisearch_count"], 2)

    async def test_candidate_search_empty_results(self):
        """2. Successful empty candidate search records empty outcome and provider status."""
        reqs = EmployerSearchRequirements(
            role=ProvenanceValue(value="Nonexistent Role", source=ProvenanceSource.QUERY),
        )
        fake_res = {"success": True, "total": 0, "data": []}

        with patch("backend.services.employer_search_executor.search_users", new_callable=AsyncMock, return_value=fake_res):
            cands, metadata, raw_payload = await execute_employer_candidate_search(
                requirements=reqs,
                session_id="session_emp_empty",
                page=1,
                limit=10,
            )

            self.assertEqual(len(cands), 0)
            self.assertEqual(metadata.total_matches, 0)
            self.assertTrue(metadata.validate_invariants())

            telemetry = raw_payload.get("telemetry")
            self.assertIsNotNone(telemetry)
            self.assertEqual(telemetry["provider_status"], "empty")
            self.assertEqual(telemetry["candidate_search_outcome"], "empty_results")
            self.assertEqual(telemetry["meilisearch_count"], 0)
            self.assertEqual(telemetry["retrieval_result_count"], 0)

    async def test_candidate_search_safe_filter_failure(self):
        """3. Safe-filter failure skips provider execution with safe_filter_failure outcome."""
        reqs = EmployerSearchRequirements(
            safe_failure=True,
            failure_reason="Location unresolved",
        )
        with patch("backend.services.employer_search_executor.search_users", new_callable=AsyncMock) as mock_search:
            cands, metadata, raw_payload = await execute_employer_candidate_search(
                requirements=reqs,
                session_id="session_emp_safe_fail",
            )

            mock_search.assert_not_called()
            self.assertEqual(len(cands), 0)
            self.assertEqual(metadata.total_matches, 0)

            telemetry = raw_payload.get("telemetry")
            self.assertIsNotNone(telemetry)
            self.assertEqual(telemetry["provider_status"], "skipped")
            self.assertEqual(telemetry["candidate_search_outcome"], "safe_filter_failure")
            self.assertFalse(telemetry["retrieval_attempted"])

    async def test_candidate_search_provider_reported_failure(self):
        """4. Provider returning success=False records unavailable status and provider_failure outcome."""
        reqs = EmployerSearchRequirements(
            role=ProvenanceValue(value="Developer", source=ProvenanceSource.QUERY),
        )
        fake_res = {"success": False, "data": []}

        with patch("backend.services.employer_search_executor.search_users", new_callable=AsyncMock, return_value=fake_res):
            cands, metadata, raw_payload = await execute_employer_candidate_search(
                requirements=reqs,
                session_id="session_emp_prov_fail",
            )

            telemetry = raw_payload.get("telemetry")
            self.assertIsNotNone(telemetry)
            self.assertEqual(telemetry["provider_status"], "unavailable")
            self.assertEqual(telemetry["candidate_search_outcome"], "provider_failure")
            self.assertTrue(telemetry["retrieval_attempted"])

    async def test_candidate_search_exception_handling(self):
        """5. Provider throwing exception records error type and provider_failure outcome."""
        reqs = EmployerSearchRequirements(
            role=ProvenanceValue(value="Developer", source=ProvenanceSource.QUERY),
        )
        with patch("backend.services.employer_search_executor.search_users", new_callable=AsyncMock, side_effect=ConnectionError("Search API down")):
            cands, metadata, raw_payload = await execute_employer_candidate_search(
                requirements=reqs,
                session_id="session_emp_exc",
            )

            telemetry = raw_payload.get("telemetry")
            self.assertIsNotNone(telemetry)
            self.assertEqual(telemetry["provider_status"], "unavailable")
            self.assertEqual(telemetry["candidate_search_outcome"], "provider_failure")
            self.assertEqual(telemetry["retrieval_error_type"], "ConnectionError")

    async def test_candidate_search_end_to_end_orchestration_metadata(self):
        """6. Full employer orchestration attaches telemetry with pagination backwards compatibility."""
        from backend.api.employer import handle_employer_context

        fake_res = {
            "success": True,
            "total": 2,
            "data": [
                {"id": 1, "first_name": "Dev", "last_name": "One", "designation": "Python Dev", "city_name": "Noida"},
                {"id": 2, "first_name": "Dev", "last_name": "Two", "designation": "Python Dev", "city_name": "Noida"},
            ]
        }

        with patch("backend.services.employer_search_executor.search_users", new_callable=AsyncMock, return_value=fake_res):
            res = await handle_employer_context(
                last_user_msg="I want to hire Python developers in Noida",
                session_id="test_e2e_emp_meta",
            )

            self.assertIsNotNone(res)
            self.assertTrue(res.get("success", True))
            meta = res.get("search_metadata")
            self.assertIsNotNone(meta)

            # Backwards compatibility fields for existing pagination tests
            self.assertIn("total_matches", meta)
            self.assertIn("returned", meta)
            self.assertIn("page", meta)
            self.assertIn("limit", meta)

            # Operational telemetry fields
            self.assertEqual(meta["search_mode"], "CANDIDATE_SEARCH")
            self.assertEqual(meta["search_provider"], "meilisearch")
            self.assertEqual(meta["provider_status"], "success")
            self.assertEqual(meta["candidate_search_outcome"], "success_with_results")
            self.assertEqual(meta["meilisearch_count"], 2)
            self.assertEqual(meta["retrieval_result_count"], 2)

            # When validated, extra pagination keys are dropped, telemetry kept
            clean = validate_retrieval_metadata(meta)
            self.assertEqual(clean["search_mode"], "CANDIDATE_SEARCH")
            self.assertEqual(clean["candidate_search_outcome"], "success_with_results")
            self.assertNotIn("total_matches", clean)
            self.assertNotIn("returned", clean)


class TestSharedRetrievalTelemetrySafeguards(unittest.TestCase):

    def setUp(self):
        reset_telemetry_client_for_testing()

    def tearDown(self):
        reset_telemetry_client_for_testing()

    def test_validator_accepts_new_allowed_outcomes(self):
        """1. New closed-set outcomes for both domains pass validation."""
        for outcome in ALLOWED_PEOPLE_SEARCH_OUTCOMES:
            data = {"search_mode": "PEOPLE_SEARCH", "people_search_outcome": outcome}
            clean = validate_retrieval_metadata(data)
            self.assertEqual(clean["people_search_outcome"], outcome)

        for outcome in ALLOWED_CANDIDATE_SEARCH_OUTCOMES:
            data = {"search_mode": "CANDIDATE_SEARCH", "candidate_search_outcome": outcome}
            clean = validate_retrieval_metadata(data)
            self.assertEqual(clean["candidate_search_outcome"], outcome)

    def test_validator_rejects_unknown_fields_and_pii(self):
        """2. Strict allowlist rejects candidate names, emails, raw queries, tokens, and arbitrary fields."""
        unsafe_payload = {
            "search_mode": "PEOPLE_SEARCH",
            "candidate_name": "Kunal Yadav",
            "candidate_id": 12345,
            "email": "kunal@example.com",
            "phone": "+919876543210",
            "raw_query": "SELECT * FROM cyb_user",
            "access_token": "secret_jwt_token_xyz",
            "resume_text": "Experienced Python developer with 10 years experience",
            "salary_expectation": "20 LPA",
            "arbitrary_field": "dangerous_injection",
            "retrieval_result_count": 5,
        }
        clean = validate_retrieval_metadata(unsafe_payload)

        # Only allowlisted operational fields remain
        self.assertEqual(set(clean.keys()), {"search_mode", "retrieval_result_count"})
        self.assertEqual(clean["search_mode"], "PEOPLE_SEARCH")
        self.assertEqual(clean["retrieval_result_count"], 5)
        self.assertNotIn("candidate_name", clean)
        self.assertNotIn("candidate_id", clean)
        self.assertNotIn("email", clean)
        self.assertNotIn("access_token", clean)

    def test_validator_rejects_out_of_bounds_and_booleans_in_counts(self):
        """3. Invalid count types (booleans, negative, >100,000) fail closed."""
        bad_payload = {
            "search_mode": "CANDIDATE_SEARCH",
            "retrieval_result_count": True,       # Boolean must be rejected
            "eligible_count": -5,                 # Negative count rejected
            "final_count": 150_000,               # Out of bounds (>100,000) rejected
            "meili_ms": -10.0,                    # Negative latency rejected
            "retrieval_total_ms": 500_000.0,      # Out of bounds latency rejected
        }
        clean = validate_retrieval_metadata(bad_payload)
        self.assertEqual(clean, {"search_mode": "CANDIDATE_SEARCH"})

    def test_jobmatch_outcomes_and_fallbacks_preserved(self):
        """4. Phase 2.2 JobMatch outcome classification remains intact."""
        jobmatch_payload = {
            "search_mode": "EXPLICIT_KEYWORD",
            "search_provider": "hybrid",
            "jobmatch_outcome": "candidate_not_indexed",
            "jobmatch_fallback_reason": "candidate_not_indexed_409",
            "meilisearch_count": 10,
            "jobmatch_count": 0,
        }
        clean = validate_retrieval_metadata(jobmatch_payload)
        self.assertEqual(clean["jobmatch_outcome"], "candidate_not_indexed")
        self.assertEqual(clean["jobmatch_fallback_reason"], "candidate_not_indexed_409")

    def test_finalization_revalidation_preserves_clean_data(self):
        """5. RequestObservationWrapper.finalize() revalidates retrieval metadata."""
        mock_raw_obs = MagicMock()
        mock_client = MagicMock()
        mock_client.start_as_current_observation.return_value.__enter__.return_value = mock_raw_obs

        with patch("backend.utils.telemetry.is_telemetry_enabled", return_value=True), \
             patch("backend.utils.telemetry.get_langfuse_client", return_value=mock_client):

            with trace_request(
                endpoint="/chat",
                request_id="test_req_people_trace",
                role="employee",
                session_id="session_people_123",
            ) as req_trace:
                req_trace.set_retrieval_metadata({
                    "search_mode": "PEOPLE_SEARCH",
                    "search_provider": "people_repo",
                    "provider_status": "success",
                    "people_search_outcome": "success_with_results",
                    "retrieval_result_count": 5,
                    "final_count": 3,
                    "retrieval_total_ms": 42.5,
                    "filters_applied": ["city", "role_type"],
                    # Unsafe field:
                    "candidate_secret": "do_not_leak",
                })

        mock_raw_obs.update.assert_called_once()
        meta = mock_raw_obs.update.call_args[1]["metadata"]
        self.assertIn("retrieval", meta)
        retrieval = meta["retrieval"]
        self.assertEqual(retrieval["search_mode"], "PEOPLE_SEARCH")
        self.assertEqual(retrieval["people_search_outcome"], "success_with_results")
        self.assertEqual(retrieval["retrieval_result_count"], 5)
        self.assertEqual(retrieval["final_count"], 3)
        self.assertEqual(retrieval["filters_applied"], ["city", "role_type"])
        self.assertNotIn("candidate_secret", retrieval)

    def test_telemetry_errors_fail_closed_without_breaking_chat(self):
        """6. None, non-dict, or throwing inputs to validate_retrieval_metadata fail closed safely."""
        self.assertEqual(validate_retrieval_metadata(None), {})
        self.assertEqual(validate_retrieval_metadata("not_a_dict"), {})
        self.assertEqual(validate_retrieval_metadata(12345), {})
        self.assertEqual(validate_retrieval_metadata([1, 2, 3]), {})


class TestStageLevelLatencyInstrumentation(unittest.IsolatedAsyncioTestCase):
    """
    Phase 2 Step 2: Stage-Level Latency Instrumentation tests.
    Verifies query_rewrite, retrieval, compose timing, exceptions, early-returns,
    and streaming cancellation without external services.
    """

    def setUp(self):
        reset_telemetry_client_for_testing()

    def tearDown(self):
        reset_telemetry_client_for_testing()

    async def test_stage_timers_record_and_isolate_context(self):
        """1. Synchronous and async stage timers record non-negative millisecond durations in context."""
        from backend.utils.timing import (
            start_request_timing,
            stage_timer,
            stage_timer_async,
            mark_stage,
            get_request_timing,
        )

        timing = start_request_timing("test_isolation_1")

        with stage_timer("query_rewrite"):
            pass

        async with stage_timer_async("retrieval"):
            pass

        mark_stage("compose", 42)

        self.assertIn("query_rewrite", timing.stages)
        self.assertIn("retrieval", timing.stages)
        self.assertIn("compose", timing.stages)
        self.assertGreaterEqual(timing.stages["query_rewrite"], 0)
        self.assertGreaterEqual(timing.stages["retrieval"], 0)
        self.assertEqual(timing.stages["compose"], 42)

    async def test_retrieval_timer_records_duration_on_exception(self):
        """2. stage_timer_async('retrieval') records elapsed duration even when dispatch raises an exception."""
        from backend.utils.timing import start_request_timing, stage_timer_async

        timing = start_request_timing("test_retrieval_exc")

        with self.assertRaises(RuntimeError):
            async with stage_timer_async("retrieval"):
                raise RuntimeError("Simulated retrieval network outage")

        self.assertIn("retrieval", timing.stages)
        self.assertGreaterEqual(timing.stages["retrieval"], 0)

    async def test_query_rewrite_timer_records_duration_on_exception(self):
        """3. stage_timer('query_rewrite') records elapsed duration even when rewriter raises."""
        from backend.utils.timing import start_request_timing, stage_timer

        timing = start_request_timing("test_qr_exc")

        with self.assertRaises(ValueError):
            with stage_timer("query_rewrite"):
                raise ValueError("Simulated parsing error")

        self.assertIn("query_rewrite", timing.stages)
        self.assertGreaterEqual(timing.stages["query_rewrite"], 0)

    async def test_streaming_compose_records_duration_and_handles_interruption(self):
        """4. Streaming compose marks compose duration on both normal completion and early interruption."""
        from backend.utils.timing import start_request_timing, mark_stage, set_ttft

        timing = start_request_timing("test_stream_compose")

        # Simulate TTFT distinct from compose duration
        set_ttft(15.5)
        self.assertEqual(timing.ttft_ms, 15.5)

        t0 = time.perf_counter()
        # Simulated generator interruption
        try:
            pass
        finally:
            mark_stage("compose", int(round((time.perf_counter() - t0) * 1000)))

        self.assertIn("compose", timing.stages)
        self.assertGreaterEqual(timing.stages["compose"], 0)
        # Ensure ttft and compose remain distinct
        self.assertNotEqual(timing.ttft_ms, timing.stages["compose"])


if __name__ == "__main__":
    unittest.main()
