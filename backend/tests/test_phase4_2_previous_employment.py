"""
test_phase4_2_previous_employment.py — Phase 4.2 Multiple Previous Employment Disambiguation Tests

Validates:
1. One previous employer resolves automatically.
2. Multiple previous employers generate selection metadata.
3. Explicit company name resolves the correct previous record.
4. Most recent previous employer resolves using valid dates.
5. Oldest previous employer resolves using valid dates.
6. Missing dates cause safe clarification.
7. Tied dates cause safe clarification.
8. "Other company" does not silently select an arbitrary record.
9. Selecting a previous employer preserves context for verification follow-ups.
10. Selecting a previous employer preserves context for salary follow-ups.
11. Selecting a previous employer preserves context for skills follow-ups.
12. No previous employment records available.
13. Only one current employment record (no previous records).
14. Duplicate company names are handled safely.
15. Invalid selection keys are rejected.
16. Existing Phase 3.6B selection tests remain compatible.
17. Existing Phase 4.1 cross-intent tests remain compatible.
"""

import pytest
from unittest.mock import patch
from backend.services.user_data_intent_parser import UserIntent
from backend.services.user_data_formatter import format_single_intent, resolve_employment_selection_context
from backend.utils.session_state import (
    get_selected_employment_context,
    set_selected_employment_context,
    _SESSION_STATES
)

@pytest.fixture(autouse=True)
def clean_session_state():
    """Clear in-memory RAM sessions before and after each test."""
    _SESSION_STATES.clear()
    yield
    _SESSION_STATES.clear()


@pytest.fixture
def multi_previous_data():
    return {
        "employment_history": [
            {
                "id": "101",
                "company": "Google LLC",
                "is_verified": True,
                "roles": [
                    {
                        "designation": "Staff Engineer",
                        "joining_date": "2024-01-01",
                        "worked_till_date": "Present",
                        "still_working": "1",
                        "salary": "3000000",
                        "approved": "1",
                        "status": "1",
                        "verificationProcess": {"level1": True, "level2": True, "level3": True, "level4": True}
                    }
                ]
            },
            {
                "id": "102",
                "company": "Microsoft",
                "is_verified": True,
                "roles": [
                    {
                        "designation": "Senior Developer",
                        "joining_date": "2021-01-01",
                        "worked_till_date": "2023-12-31",
                        "still_working": "0",
                        "salary": "2200000",
                        "approved": "1",
                        "status": "1",
                        "verificationProcess": {"level1": True, "level2": True, "level3": True, "level4": False}
                    }
                ]
            },
            {
                "id": "103",
                "company": "Amazon",
                "is_verified": False,
                "roles": [
                    {
                        "designation": "Software Engineer",
                        "joining_date": "2018-01-01",
                        "worked_till_date": "2020-12-31",
                        "still_working": "0",
                        "salary": "1500000",
                        "approved": "0",
                        "status": "0",
                        "verificationProcess": {"level1": True, "level2": False, "level3": False, "level4": False}
                    }
                ]
            }
        ],
        "employment_salary": [
            {"company": "Google LLC", "designation": "Staff Engineer", "salary": "3000000", "salary_mode": "Per Annum"},
            {"company": "Microsoft", "designation": "Senior Developer", "salary": "2200000", "salary_mode": "Per Annum"},
            {"company": "Amazon", "designation": "Software Engineer", "salary": "1500000", "salary_mode": "Per Annum"}
        ],
        "employment_skills": [
            {"id": "s1", "name": "Go", "company": "Google LLC", "designation": "Staff Engineer"},
            {"id": "s2", "name": "C#", "company": "Microsoft", "designation": "Senior Developer"},
            {"id": "s3", "name": "Java", "company": "Amazon", "designation": "Software Engineer"}
        ]
    }


class TestPhase42PreviousEmployment:

    def test_1_one_previous_employer_resolves_automatically(self):
        """1. One previous employer resolves automatically without selection prompt."""
        session_id = "sess_p42_1"
        single_prev_data = {
            "employment_history": [
                {
                    "company": "Google LLC",
                    "roles": [{"designation": "Engineer", "joining_date": "2022", "worked_till_date": "Present", "still_working": "1"}]
                },
                {
                    "company": "Microsoft",
                    "roles": [{"designation": "Dev", "joining_date": "2019", "worked_till_date": "2021", "still_working": "0"}]
                }
            ]
        }
        res_tuple = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION, single_prev_data, {}, query="Show my previous employment verification status", session_id=session_id, return_metadata=True
        )
        assert isinstance(res_tuple, tuple)
        text, meta = res_tuple
        assert meta is None  # Auto-selected Microsoft, no selection prompt needed
        assert "Microsoft" in text
        assert get_selected_employment_context(session_id) == "Microsoft"

    def test_2_multiple_previous_employers_generate_selection_metadata(self, multi_previous_data):
        """2. Multiple previous employers generate selection metadata."""
        session_id = "sess_p42_2"
        res_tuple = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION, multi_previous_data, {}, query="Show my previous employment verification status", session_id=session_id, return_metadata=True
        )
        assert isinstance(res_tuple, tuple)
        text, meta = res_tuple
        assert meta is not None
        assert meta["type"] == "employment_selection"
        labels = [opt["label"] for opt in meta["options"]]
        assert "Microsoft" in labels
        assert "Amazon" in labels
        assert "Google LLC" not in labels  # Current employer excluded from previous options

    def test_3_explicit_company_name_resolves_previous_record(self, multi_previous_data):
        """3. Explicit company name resolves the correct previous record."""
        session_id = "sess_p42_3"
        res = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION, multi_previous_data, {}, query="Show my Amazon employment verification status", session_id=session_id
        )
        assert "Amazon" in res
        assert get_selected_employment_context(session_id) == "Amazon"

    def test_4_most_recent_previous_employer_resolves_by_date(self, multi_previous_data):
        """4. Most recent previous employer resolves using valid dates (Microsoft 2023 > Amazon 2020)."""
        session_id = "sess_p42_4"
        res = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION, multi_previous_data, {}, query="Show my most recent previous employment", session_id=session_id
        )
        assert "Microsoft" in res
        assert get_selected_employment_context(session_id) == "Microsoft"

    def test_5_oldest_previous_employer_resolves_by_date(self, multi_previous_data):
        """5. Oldest previous employer resolves using valid dates (Amazon 2018 < Microsoft 2021)."""
        session_id = "sess_p42_5"
        res = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION, multi_previous_data, {}, query="Show my oldest previous employment", session_id=session_id
        )
        assert "Amazon" in res
        assert get_selected_employment_context(session_id) == "Amazon"

    def test_6_missing_dates_cause_safe_clarification(self):
        """6. Missing dates cause safe clarification (selection metadata prompt)."""
        session_id = "sess_p42_6"
        missing_dates_data = {
            "employment_history": [
                {"company": "Google LLC", "roles": [{"still_working": "1"}]},
                {"company": "Microsoft", "roles": [{"still_working": "0"}]},
                {"company": "Amazon", "roles": [{"still_working": "0"}]}
            ]
        }
        res_tuple = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION, missing_dates_data, {}, query="Show my most recent previous employment", session_id=session_id, return_metadata=True
        )
        assert isinstance(res_tuple, tuple)
        text, meta = res_tuple
        assert meta is not None  # Prompts user to select because dates are missing

    def test_7_tied_dates_cause_safe_clarification(self):
        """7. Tied dates cause safe clarification (selection metadata prompt)."""
        session_id = "sess_p42_7"
        tied_dates_data = {
            "employment_history": [
                {"company": "Google LLC", "roles": [{"still_working": "1"}]},
                {"company": "Microsoft", "roles": [{"worked_till_date": "2022", "still_working": "0"}]},
                {"company": "Amazon", "roles": [{"worked_till_date": "2022", "still_working": "0"}]}
            ]
        }
        res_tuple = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION, tied_dates_data, {}, query="Show my most recent previous employment", session_id=session_id, return_metadata=True
        )
        assert isinstance(res_tuple, tuple)
        text, meta = res_tuple
        assert meta is not None

    def test_8_other_company_does_not_arbitrarily_select(self, multi_previous_data):
        """8. "Other company" with multiple candidates prompts for selection."""
        session_id = "sess_p42_8"
        set_selected_employment_context(session_id, "Google LLC")
        res_tuple = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION, multi_previous_data, {}, query="What about the other company?", session_id=session_id, return_metadata=True
        )
        assert isinstance(res_tuple, tuple)
        text, meta = res_tuple
        assert meta is not None

    def test_9_selecting_previous_employer_preserves_verification_followups(self, multi_previous_data):
        """9. Selecting a previous employer (Amazon) preserves context for verification follow-ups."""
        session_id = "sess_p42_9"
        format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION, multi_previous_data, {}, query="Show Amazon verification", session_id=session_id
        )
        assert get_selected_employment_context(session_id) == "Amazon"

        res_followup = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION, multi_previous_data, {}, query="What about Level 3?", session_id=session_id
        )
        assert "Amazon" in res_followup
        assert "Google LLC" not in res_followup

    def test_10_selecting_previous_employer_preserves_salary_followups(self, multi_previous_data):
        """10. Selecting a previous employer preserves context for salary follow-ups."""
        session_id = "sess_p42_10"
        set_selected_employment_context(session_id, "Amazon")

        res_sal = format_single_intent(
            UserIntent.EMPLOYMENT_SALARY, multi_previous_data, {}, query="What was my salary?", session_id=session_id
        )
        assert "Software Engineer" in res_sal
        assert "1500000" in res_sal
        assert "3000000" not in res_sal

    def test_11_selecting_previous_employer_preserves_skills_followups(self, multi_previous_data):
        """11. Selecting a previous employer preserves context for skills follow-ups."""
        session_id = "sess_p42_11"
        set_selected_employment_context(session_id, "Amazon")

        res_sk = format_single_intent(
            UserIntent.EMPLOYMENT_SKILLS, multi_previous_data, {}, query="What skills did I use?", session_id=session_id
        )
        assert "Amazon" in res_sk
        assert "Java" in res_sk
        assert "Go" not in res_sk

    def test_12_no_previous_employment_records(self):
        """12. No previous employment records returns clean explanation."""
        session_id = "sess_p42_12"
        no_prev_data = {
            "employment_history": [
                {"company": "Google LLC", "roles": [{"still_working": "1"}]}
            ]
        }
        res = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION, no_prev_data, {}, query="Show my previous employment verification status", session_id=session_id
        )
        assert "couldn't find any previous employment records" in res or "couldn't find an employment record" in res

    def test_13_only_one_current_employment_record(self):
        """13. Only one current employment record does not generate empty selection prompt."""
        session_id = "sess_p42_13"
        only_current_data = {
            "employment_history": [
                {"company": "Google LLC", "roles": [{"still_working": "1"}]}
            ]
        }
        res_tuple = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION, only_current_data, {}, query="Show my previous employment", session_id=session_id, return_metadata=True
        )
        assert isinstance(res_tuple, tuple)
        text, meta = res_tuple
        assert meta is None  # No prompt generated for non-existent previous records

    def test_14_duplicate_company_names_handled_safely(self):
        """14. Duplicate company names are handled safely with distinguishable role descriptors."""
        dup_data = {
            "employment_history": [
                {"company": "Google LLC", "roles": [{"designation": "Staff Engineer", "still_working": "1"}]},
                {"company": "Google LLC", "roles": [{"designation": "Software Engineer", "still_working": "0"}]}
            ]
        }
        filtered, meta, matched = resolve_employment_selection_context("Show my employment status", dup_data["employment_history"])
        assert meta is not None
        assert len(meta["options"]) == 2
        descs = [opt["description"] for opt in meta["options"]]
        assert any("Current" in d for d in descs)
        assert any("Previous" in d for d in descs)

    def test_15_invalid_selection_keys_rejected(self, multi_previous_data):
        """15. Invalid selection keys do not resolve to arbitrary records."""
        session_id = "sess_p42_15"
        set_selected_employment_context(session_id, "NonExistentCompany")
        res_tuple = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION, multi_previous_data, {}, query="Show my verification status", session_id=session_id, return_metadata=True
        )
        assert isinstance(res_tuple, tuple)
        text, meta = res_tuple
        assert meta is not None  # Prompts selection from valid history

    def test_16_phase3_6b_selection_tests_compatibility(self, multi_previous_data):
        """16. Existing Phase 3.6B selection tests remain compatible."""
        session_id = "sess_p42_16"
        res = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION, multi_previous_data, {}, query="Show Google verification", session_id=session_id
        )
        assert "Google LLC" in res

    def test_17_phase4_1_cross_intent_compatibility(self, multi_previous_data):
        """17. Existing Phase 4.1 cross-intent tests remain compatible."""
        session_id = "sess_p42_17"
        set_selected_employment_context(session_id, "Microsoft")
        res_sal = format_single_intent(
            UserIntent.EMPLOYMENT_SALARY, multi_previous_data, {}, query="What is my salary?", session_id=session_id
        )
        assert "2200000" in res_sal
