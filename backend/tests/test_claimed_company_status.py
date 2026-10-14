"""
test_claimed_company_status.py — Targeted test suite for read-only company claimed status queries.
"""

import pytest
from backend.services.user_data_intent_parser import parse_user_data_intents, UserIntent
from backend.services.user_data_formatter import format_single_intent


def test_intent_parsing_claimed_queries():
    """Verify claimed/unclaimed company queries trigger UserIntent.EMPLOYMENT_COMPANY_CLAIMED."""
    queries = [
        "Is InnoTech Solution claimed?",
        "Is InnoTech Solution a claimed company?",
        "Is InnoTech Solution unclaimed?",
        "Which of my companies are claimed?",
        "Which of my companies are not claimed?",
        "Tell me whether InnoTech Solution is claimed.",
    ]
    for q in queries:
        intents = parse_user_data_intents(q)
        assert UserIntent.EMPLOYMENT_COMPANY_CLAIMED in intents, f"Failed for query '{q}'"
        # Assert routing safety: does NOT leak into job or candidate search
        assert UserIntent.PROFILE_SKILLS not in intents


def test_claimed_status_boolean_true_false():
    """Verify boolean true/false for company.is_verified maps to Claimed / Not Claimed."""
    data = {
        "employment_history": [
            {
                "company": "InnoTech Solution",
                "is_verified": True,
                "roles": [{"designation": "Software Engineer"}]
            },
            {
                "company": "Legacy Corp",
                "is_verified": False,
                "roles": [{"designation": "Developer"}]
            }
        ]
    }

    # Query 1: Claimed company (boolean True)
    res_true = format_single_intent(
        UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is InnoTech Solution claimed?"
    )
    assert "**InnoTech Solution** is a claimed company." in res_true
    assert "Level" not in res_true  # Ensures NO verification workflow text

    # Query 2: Non-claimed company (boolean False)
    res_false = format_single_intent(
        UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Legacy Corp claimed?"
    )
    assert "**Legacy Corp** is not a claimed company." in res_false
    assert "Level" not in res_false


def test_claimed_status_integer_and_string_normalization():
    """Verify integer 1/0 and string '1'/'0' for company.is_verified map correctly."""
    data_int = {
        "employment_history": [
            {"company": "Comp One", "is_verified": 1, "roles": []},
            {"company": "Comp Zero", "is_verified": 0, "roles": []}
        ]
    }
    res_int_1 = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data_int, None, query="Is Comp One claimed?")
    assert "**Comp One** is a claimed company." in res_int_1

    res_int_0 = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data_int, None, query="Is Comp Zero claimed?")
    assert "**Comp Zero** is not a claimed company." in res_int_0

    data_str = {
        "employment_history": [
            {"company": "Comp Str One", "is_verified": "1", "roles": []},
            {"company": "Comp Str Zero", "is_verified": "0", "roles": []}
        ]
    }
    res_str_1 = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data_str, None, query="Is Comp Str One claimed?")
    assert "**Comp Str One** is a claimed company." in res_str_1

    res_str_0 = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data_str, None, query="Is Comp Str Zero claimed?")
    assert "**Comp Str Zero** is not a claimed company." in res_str_0


def test_multiple_companies_claimed_queries():
    """Verify 'Which of my companies are claimed/not claimed?' filtering."""
    data = {
        "employment_history": [
            {"company": "Alpha Tech", "is_verified": True, "roles": []},
            {"company": "Beta Soft", "is_verified": False, "roles": []},
            {"company": "Gamma Systems", "is_verified": True, "roles": []}
        ]
    }

    # Which are claimed?
    res_claimed = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Which of my companies are claimed?")
    assert "The following listed companies are claimed: **Alpha Tech, Gamma Systems**." in res_claimed

    # Which are not claimed?
    res_unclaimed = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Which of my companies are not claimed?")
    assert "The following listed company is not claimed: **Beta Soft**." in res_unclaimed


def test_unknown_company_query():
    """Verify unknown company query returns clean not found message without hallucinating or leaking to search."""
    data = {
        "employment_history": [
            {"company": "InnoTech Solution", "is_verified": True, "roles": []}
        ]
    }
    res_unknown = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is UnknownCorp claimed?")
    assert "I couldn't find an employment record for 'UnknownCorp'." in res_unknown


def test_no_verification_workflow_leakage():
    """Verify claimed query does not trigger L1/L2/L3/L4 workflow response text."""
    data = {
        "employment_history": [
            {
                "company": "InnoTech Solution",
                "is_verified": False,
                "roles": [
                    {
                        "designation": "Software Analyst",
                        "approved": "0",
                        "status": "0",
                        "verificationProcess": {"level1": False}
                    }
                ]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is InnoTech Solution claimed?")
    assert "**InnoTech Solution** is not a claimed company." in res
    assert "Highest Completed Level" not in res
    assert "Next Level" not in res
    assert "Add your employment details" not in res
    assert "Your company needs to join CollarCheck" not in res
