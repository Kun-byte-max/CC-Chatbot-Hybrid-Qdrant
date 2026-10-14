"""
test_phase3_1_action_intents.py — Regression and functional test suite for Phase 3.1 action intent recognition & navigation guidance.
"""

import pytest
from backend.services.user_data_intent_parser import UserIntent, parse_user_data_intents
from backend.services.user_data_service import resolve_required_endpoints, INTENT_ENDPOINT_MAP
from backend.services.user_data_formatter import format_multi_intent_response


class TestEmploymentAdditionActionIntents:
    """Tests for EMPLOYMENT_ACTION_ADD intent recognition."""

    @pytest.mark.parametrize("query", [
        "How do I add my employment?",
        "I want to add previous employment.",
        "Where can I add my job details?",
        "I need to register my current employer.",
        "How do I add my previous employment?",
        "I want to add my current company.",
        "Where can I submit my employment details?",
        "How do I register my work experience?",
    ])
    def test_employment_action_add_queries(self, query: str):
        intents = parse_user_data_intents(query)
        assert UserIntent.EMPLOYMENT_ACTION_ADD in intents, f"Failed to match EMPLOYMENT_ACTION_ADD for query: {query}"


class TestEmployerVerificationActionIntents:
    """Tests for EMPLOYMENT_ACTION_INVITE_EMPLOYER intent recognition."""

    @pytest.mark.parametrize("query", [
        "How can I ask my employer to verify my employment?",
        "How do I invite my HR?",
        "I want my company to verify my work experience.",
        "Where can I request employment verification?",
        "How do I get my employer to verify my employment?",
        "How can I get my employer to approve my employment?",
        "How can I request employment verification?",
    ])
    def test_employer_action_invite_queries(self, query: str):
        intents = parse_user_data_intents(query)
        assert UserIntent.EMPLOYMENT_ACTION_INVITE_EMPLOYER in intents, f"Failed to match EMPLOYMENT_ACTION_INVITE_EMPLOYER for query: {query}"


class TestSalaryVerificationActionIntents:
    """Tests for EMPLOYMENT_ACTION_UPLOAD_SALARY intent recognition."""

    @pytest.mark.parametrize("query", [
        "Where can I upload salary proof?",
        "How do I complete Level 4?",
        "Where can I submit salary verification documents?",
        "How can I verify my salary?",
        "Where can I complete salary verification?",
        "How do I submit salary proof?",
        "Where do I upload my salary documents?",
    ])
    def test_salary_action_upload_queries(self, query: str):
        intents = parse_user_data_intents(query)
        assert UserIntent.EMPLOYMENT_ACTION_UPLOAD_SALARY in intents, f"Failed to match EMPLOYMENT_ACTION_UPLOAD_SALARY for query: {query}"


class TestExistingIntentRegression:
    """Tests to verify Phase 1 and Phase 2 intents are preserved without interception."""

    def test_level_4_status_query(self):
        intents = parse_user_data_intents("What is my Level 4 status?")
        assert UserIntent.EMPLOYMENT_VERIFICATION in intents
        assert UserIntent.EMPLOYMENT_ACTION_UPLOAD_SALARY not in intents

    def test_show_verification_progress_query(self):
        intents = parse_user_data_intents("Show my verification progress.")
        assert UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS in intents

    def test_why_level_3_pending_query(self):
        intents = parse_user_data_intents("Why is my Level 3 pending?")
        assert UserIntent.EMPLOYMENT_VERIFICATION_WHY_INCOMPLETE in intents

    def test_what_should_i_do_next_query(self):
        intents = parse_user_data_intents("What should I do next?")
        assert UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS in intents

    def test_who_needs_to_verify_employment_query(self):
        intents = parse_user_data_intents("Who needs to verify my employment?")
        assert UserIntent.EMPLOYMENT_VERIFICATION_OWNERSHIP in intents

    def test_how_does_salary_verification_work_query(self):
        intents = parse_user_data_intents("How does salary verification work?")
        assert UserIntent.VERIFICATION_LEVEL_DEFINITION in intents
        assert UserIntent.EMPLOYMENT_ACTION_UPLOAD_SALARY not in intents

    def test_what_is_level_4_verification_query(self):
        intents = parse_user_data_intents("What is Level 4 verification?")
        assert UserIntent.VERIFICATION_LEVEL_DEFINITION in intents
        assert UserIntent.EMPLOYMENT_ACTION_UPLOAD_SALARY not in intents


class TestFalsePositiveRegression:
    """Tests to verify unrelated queries do not falsely trigger action intents."""

    def test_remaining_salary_query(self):
        intents = parse_user_data_intents("What is my remaining salary?")
        assert UserIntent.EMPLOYMENT_SALARY in intents
        assert UserIntent.EMPLOYMENT_ACTION_UPLOAD_SALARY not in intents

    def test_remaining_employment_records_query(self):
        intents = parse_user_data_intents("Show my remaining employment records.")
        assert UserIntent.EMPLOYMENT_ACTION_ADD not in intents

    def test_find_employers_in_delhi_query(self):
        intents = parse_user_data_intents("Find employers in Delhi.")
        assert intents == []

    def test_show_candidates_from_my_company_query(self):
        intents = parse_user_data_intents("Show candidates from my company.")
        assert intents == []

    def test_what_is_my_salary_query(self):
        intents = parse_user_data_intents("What is my salary?")
        assert UserIntent.EMPLOYMENT_SALARY in intents
        assert UserIntent.EMPLOYMENT_ACTION_UPLOAD_SALARY not in intents

    def test_tell_me_about_my_previous_employment_history_query(self):
        intents = parse_user_data_intents("Tell me about my previous employment history.")
        assert UserIntent.EMPLOYMENT_HISTORY in intents
        assert UserIntent.EMPLOYMENT_ACTION_ADD not in intents


class TestPipelineZeroApiCallAndFormatting:
    """Pipeline tests verifying zero API calls and correct navigation formatting."""

    def test_action_intents_require_zero_api_endpoints(self):
        req_add = resolve_required_endpoints([UserIntent.EMPLOYMENT_ACTION_ADD])
        assert req_add == [], "EMPLOYMENT_ACTION_ADD must require 0 API endpoints"

        req_invite = resolve_required_endpoints([UserIntent.EMPLOYMENT_ACTION_INVITE_EMPLOYER])
        assert req_invite == [], "EMPLOYMENT_ACTION_INVITE_EMPLOYER must require 0 API endpoints"

        req_upload = resolve_required_endpoints([UserIntent.EMPLOYMENT_ACTION_UPLOAD_SALARY])
        assert req_upload == [], "EMPLOYMENT_ACTION_UPLOAD_SALARY must require 0 API endpoints"

    def test_add_employment_formatting(self):
        resp = format_multi_intent_response([UserIntent.EMPLOYMENT_ACTION_ADD], {}, {})
        assert "**Add Employment Information**" in resp
        assert "https://www.collarcheck.com/dashboard/user/experience" in resp

    def test_employer_verification_formatting(self):
        resp = format_multi_intent_response([UserIntent.EMPLOYMENT_ACTION_INVITE_EMPLOYER], {}, {})
        assert "**Employer Verification**" in resp
        assert "https://www.collarcheck.com/dashboard/user/experience" in resp

    def test_salary_verification_formatting(self):
        resp = format_multi_intent_response([UserIntent.EMPLOYMENT_ACTION_UPLOAD_SALARY], {}, {})
        assert "**Salary Verification**" in resp
        assert "https://www.collarcheck.com/dashboard/user/skills" in resp
