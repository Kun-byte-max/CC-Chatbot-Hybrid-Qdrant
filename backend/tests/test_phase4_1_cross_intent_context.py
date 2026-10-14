"""
test_phase4_1_cross_intent_context.py — Phase 4.1 Cross-Intent Employment Context Propagation Tests

Validates:
1. Verification -> salary follow-up with selected Google context.
2. Verification -> skills follow-up with selected Google context.
3. Explicit Microsoft override while Google is selected.
4. Salary -> skills follow-up retaining Microsoft context.
5. Salary query with no selected employment context.
6. Skills query with no selected employment context.
7. Stale selected company missing from fresh employment history.
8. Explicit company name that does not match any authenticated employment record.
9. Unrelated intent does not consume or modify employment context.
10. Two independent sessions with different selected companies.
11. Existing verification selection behaviour remains unchanged.
12. Existing action metadata remains unchanged.
13. Existing selection metadata remains unchanged.
14. Salary and skills data from one company never leak into another company's response.
"""

import pytest
from unittest.mock import patch, MagicMock
from backend.services.user_data_intent_parser import UserIntent
from backend.services.user_data_formatter import format_single_intent, resolve_employment_selection_context
from backend.services import user_data_service
from backend.utils.session_state import (
    get_selected_employment_context,
    set_selected_employment_context,
    clear_selected_employment_context,
    _SESSION_STATES
)

@pytest.fixture(autouse=True)
def clean_session_state():
    """Clear in-memory RAM sessions before and after each test."""
    _SESSION_STATES.clear()
    yield
    _SESSION_STATES.clear()


@pytest.fixture
def multi_company_data():
    return {
        "employment_history": [
            {
                "id": "101",
                "company": "Google LLC",
                "is_verified": True,
                "roles": [
                    {
                        "id": "r1",
                        "designation": "Senior Software Engineer",
                        "joining_date": "2022-01-01",
                        "worked_till_date": "Present",
                        "still_working": "1",
                        "salary": "2500000",
                        "salary_mode": "Per Annum",
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
                        "id": "r2",
                        "designation": "Backend Developer",
                        "joining_date": "2020-01-01",
                        "worked_till_date": "2021-12-31",
                        "still_working": "0",
                        "salary": "1800000",
                        "salary_mode": "Per Annum",
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
                        "id": "r3",
                        "designation": "Cloud Architect",
                        "joining_date": "2018-01-01",
                        "worked_till_date": "2019-12-31",
                        "still_working": "0",
                        "salary": "1200000",
                        "salary_mode": "Per Annum",
                        "approved": "0",
                        "status": "0",
                        "verificationProcess": {"level1": True, "level2": False, "level3": False, "level4": False}
                    }
                ]
            }
        ],
        "employment_salary": [
            {"company": "Google LLC", "designation": "Senior Software Engineer", "salary": "2500000", "salary_mode": "Per Annum"},
            {"company": "Microsoft", "designation": "Backend Developer", "salary": "1800000", "salary_mode": "Per Annum"},
            {"company": "Amazon", "designation": "Cloud Architect", "salary": "1200000", "salary_mode": "Per Annum"}
        ],
        "employment_skills": [
            {"id": "s1", "name": "Python", "company": "Google LLC", "designation": "Senior Software Engineer"},
            {"id": "s2", "name": "C#", "company": "Microsoft", "designation": "Backend Developer"},
            {"id": "s3", "name": "AWS", "company": "Amazon", "designation": "Cloud Architect"}
        ]
    }


class TestPhase41CrossIntentContext:

    def test_1_verification_to_salary_followup(self, multi_company_data):
        """1. Verification -> salary follow-up with selected Google context."""
        session_id = "sess_p41_1"
        # First query: explicit Google verification
        res_verif = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION, multi_company_data, {}, query="Show verification status for Google", session_id=session_id
        )
        assert "Google LLC" in res_verif
        assert get_selected_employment_context(session_id) == "Google LLC"

        # Follow-up query: salary without specifying company
        res_sal = format_single_intent(
            UserIntent.EMPLOYMENT_SALARY, multi_company_data, {}, query="What is my salary?", session_id=session_id
        )
        assert "Senior Software Engineer" in res_sal
        assert "₹2500000" in res_sal
        assert "Microsoft" not in res_sal
        assert "Amazon" not in res_sal

    def test_2_verification_to_skills_followup(self, multi_company_data):
        """2. Verification -> skills follow-up with selected Google context."""
        session_id = "sess_p41_2"
        # First query: explicit Google verification
        format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION, multi_company_data, {}, query="Show verification status for Google", session_id=session_id
        )

        # Follow-up query: skills without specifying company
        res_sk = format_single_intent(
            UserIntent.EMPLOYMENT_SKILLS, multi_company_data, {}, query="What skills did I use?", session_id=session_id
        )
        assert "Google LLC" in res_sk
        assert "Python" in res_sk
        assert "C#" not in res_sk
        assert "AWS" not in res_sk

    def test_3_explicit_company_override(self, multi_company_data):
        """3. Explicit Microsoft override while Google is selected."""
        session_id = "sess_p41_3"
        set_selected_employment_context(session_id, "Google LLC")

        # Explicit salary query for Microsoft
        res_sal = format_single_intent(
            UserIntent.EMPLOYMENT_SALARY, multi_company_data, {}, query="What is my salary at Microsoft?", session_id=session_id
        )
        assert "Backend Developer" in res_sal
        assert "₹1800000" in res_sal
        assert "Google LLC" not in res_sal
        assert get_selected_employment_context(session_id) == "Microsoft"

    def test_4_salary_to_skills_followup_retaining_context(self, multi_company_data):
        """4. Salary -> skills follow-up retaining Microsoft context."""
        session_id = "sess_p41_4"
        format_single_intent(
            UserIntent.EMPLOYMENT_SALARY, multi_company_data, {}, query="What is my salary at Microsoft?", session_id=session_id
        )

        # Follow-up skills query
        res_sk = format_single_intent(
            UserIntent.EMPLOYMENT_SKILLS, multi_company_data, {}, query="What skills did I use there?", session_id=session_id
        )
        assert "Microsoft" in res_sk
        assert "C#" in res_sk
        assert "Python" not in res_sk

    def test_5_salary_query_no_selected_context(self, multi_company_data):
        """5. Salary query with no selected employment context prompts for selection."""
        session_id = "sess_p41_5"
        res_tuple = format_single_intent(
            UserIntent.EMPLOYMENT_SALARY, multi_company_data, {}, query="What is my salary?", session_id=session_id, return_metadata=True
        )
        assert isinstance(res_tuple, tuple)
        text, meta = res_tuple
        assert meta is not None
        assert meta["type"] == "employment_selection"
        assert len(meta["options"]) == 3

    def test_6_skills_query_no_selected_context(self, multi_company_data):
        """6. Skills query with no selected employment context prompts for selection."""
        session_id = "sess_p41_6"
        res_tuple = format_single_intent(
            UserIntent.EMPLOYMENT_SKILLS, multi_company_data, {}, query="Show my work skills", session_id=session_id, return_metadata=True
        )
        assert isinstance(res_tuple, tuple)
        text, meta = res_tuple
        assert meta is not None
        assert meta["type"] == "employment_selection"

    def test_7_stale_selected_company_missing(self, multi_company_data):
        """7. Stale selected company missing from fresh employment history is ignored."""
        session_id = "sess_p41_7"
        set_selected_employment_context(session_id, "NonExistentCompany")

        res_tuple = format_single_intent(
            UserIntent.EMPLOYMENT_SALARY, multi_company_data, {}, query="Show my salary", session_id=session_id, return_metadata=True
        )
        assert isinstance(res_tuple, tuple)
        text, meta = res_tuple
        assert meta is not None  # Prompts for selection from valid records

    def test_8_explicit_unmatched_company(self, multi_company_data):
        """8. Explicit company name that does not match any authenticated record."""
        session_id = "sess_p41_8"
        res = format_single_intent(
            UserIntent.EMPLOYMENT_SALARY, multi_company_data, {}, query="What is my salary at Tesla?", session_id=session_id
        )
        assert "couldn't find an employment record for 'Tesla'" in res

    def test_9_unrelated_intent_does_not_affect_context(self, multi_company_data):
        """9. Unrelated intent does not consume or modify employment context."""
        session_id = "sess_p41_9"
        set_selected_employment_context(session_id, "Google LLC")

        # Unrelated query (Profile Incomplete)
        res_prof = format_single_intent(
            UserIntent.PROFILE_INCOMPLETE, {"profile_percentage": 85}, {}, query="Is my profile complete?", session_id=session_id
        )
        assert "85%" in res_prof
        assert get_selected_employment_context(session_id) == "Google LLC"

    def test_10_two_independent_sessions(self, multi_company_data):
        """10. Two independent sessions with different selected companies."""
        session_a = "sess_user_a"
        session_b = "sess_user_b"

        set_selected_employment_context(session_a, "Google LLC")
        set_selected_employment_context(session_b, "Microsoft")

        res_a = format_single_intent(
            UserIntent.EMPLOYMENT_SALARY, multi_company_data, {}, query="What is my salary?", session_id=session_a
        )
        res_b = format_single_intent(
            UserIntent.EMPLOYMENT_SALARY, multi_company_data, {}, query="What is my salary?", session_id=session_b
        )

        assert "Senior Software Engineer" in res_a
        assert "Backend Developer" in res_b

    def test_11_existing_verification_selection_unchanged(self, multi_company_data):
        """11. Existing verification selection behaviour remains unchanged."""
        session_id = "sess_p41_11"
        res = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION, multi_company_data, {}, query="Show verification status for Google", session_id=session_id
        )
        assert "Level 1" in res
        assert "Google LLC" in res

    def test_12_action_metadata_compatibility(self, multi_company_data):
        """12. Existing action metadata remains intact."""
        import asyncio
        from backend.services.user_data_service import handle_query
        res = asyncio.run(handle_query("How do I add my employment?", token=None))
        assert res.handled is True
        assert res.action_metadata is not None
        assert res.action_metadata[0]["id"] == "employment_add"

    def test_13_selection_metadata_compatibility(self, multi_company_data):
        """13. Existing selection metadata remains intact."""
        from backend.services.user_data_formatter import build_selection_metadata
        meta = build_selection_metadata(multi_company_data["employment_history"])
        assert meta["type"] == "employment_selection"
        assert len(meta["options"]) == 3

    def test_14_data_isolation_no_leakage(self, multi_company_data):
        """14. Salary and skills data from one company never leak into another company's response."""
        session_id = "sess_p41_14"
        format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION, multi_company_data, {}, query="Show verification status for Amazon", session_id=session_id
        )

        res_sal = format_single_intent(
            UserIntent.EMPLOYMENT_SALARY, multi_company_data, {}, query="What is my salary?", session_id=session_id
        )
        res_sk = format_single_intent(
            UserIntent.EMPLOYMENT_SKILLS, multi_company_data, {}, query="What skills did I use?", session_id=session_id
        )

        assert "1200000" in res_sal
        assert "2500000" not in res_sal
        assert "AWS" in res_sk
        assert "Python" not in res_sk
