"""
test_claimed_company_status.py — Targeted test suite for read-only company claimed status queries.
"""

import pytest
from unittest.mock import patch
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


# ==============================================================================
# CANONICAL CLAIM-STATUS SEMANTICS REGRESSION TESTS (STAGE 4.4)
# ==============================================================================

def test_claim_status_zero_with_is_verified_true():
    """1. claim_status=0 with is_verified=True -> Not Claimed (claim status independent of domain verification)."""
    data = {
        "employment_history": [
            {"company": "Enterprise Corp", "claim_status": 0, "is_verified": True, "roles": []}
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Enterprise Corp claimed?")
    assert "**Enterprise Corp** is not a claimed company." in res


def test_claim_status_one_with_is_verified_false():
    """2. claim_status=1 with is_verified=False -> Claimed (claim status independent of domain verification)."""
    data = {
        "employment_history": [
            {"company": "Acme Startup", "claim_status": 1, "is_verified": False, "roles": []}
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Acme Startup claimed?")
    assert "**Acme Startup** is a claimed company." in res


def test_company_claim_status_precedence_over_claim_status():
    """3. company_claim_status takes precedence over claim_status when both are present."""
    data = {
        "employment_history": [
            {"company": "Primary Tech", "company_claim_status": 1, "claim_status": 0, "roles": []},
            {"company": "Secondary Soft", "company_claim_status": 0, "claim_status": 1, "roles": []}
        ]
    }
    res1 = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Primary Tech claimed?")
    assert "**Primary Tech** is a claimed company." in res1

    res2 = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Secondary Soft claimed?")
    assert "**Secondary Soft** is not a claimed company." in res2


def test_company_claim_status_none_falls_back_to_claim_status():
    """4. company_claim_status=None falls back to non-None claim_status."""
    data = {
        "employment_history": [
            {"company": "Fallback Alpha", "company_claim_status": None, "claim_status": 1, "roles": []},
            {"company": "Fallback Beta", "company_claim_status": None, "claim_status": 0, "roles": []}
        ]
    }
    res1 = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Fallback Alpha claimed?")
    assert "**Fallback Alpha** is a claimed company." in res1

    res2 = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Fallback Beta claimed?")
    assert "**Fallback Beta** is not a claimed company." in res2


def test_legacy_is_verified_fallback_when_claim_fields_missing():
    """5. Both claim-status fields missing -> legacy is_verified compatibility fallback."""
    data = {
        "employment_history": [
            {"company": "Legacy Alpha", "is_verified": True, "roles": []},
            {"company": "Legacy Beta", "is_verified": False, "roles": []}
        ]
    }
    res1 = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Legacy Alpha claimed?")
    assert "**Legacy Alpha** is a claimed company." in res1

    res2 = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Legacy Beta claimed?")
    assert "**Legacy Beta** is not a claimed company." in res2


def test_unknown_when_claim_fields_and_is_verified_missing_or_unsupported():
    """6. Both claim-status fields missing and is_verified missing/unsupported -> Unknown."""
    data = {
        "employment_history": [
            {"company": "Unknown Alpha", "roles": []},
            {"company": "Unknown Beta", "claim_status": None, "is_verified": None, "roles": []},
            {"company": "Unknown Gamma", "claim_status": 99, "is_verified": "pending", "roles": []}
        ]
    }
    res1 = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Unknown Alpha claimed?")
    assert "The claim status for **Unknown Alpha** is currently unknown." in res1

    res2 = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Unknown Beta claimed?")
    assert "The claim status for **Unknown Beta** is currently unknown." in res2

    res3 = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Unknown Gamma claimed?")
    assert "The claim status for **Unknown Gamma** is currently unknown." in res3


# ==============================================================================
# STRICT CLAIM-STATUS MODE REGRESSION TESTS (STAGE 4.9B)
# ==============================================================================

def test_strict_mode_true_is_verified_resolves_unknown():
    """1. Strict true: only is_verified=True -> Unknown (domain verification never implies claim status)."""
    data = {
        "employment_history": [
            {"company": "Legacy Alpha", "is_verified": True, "roles": []}
        ]
    }
    with patch("backend.services.user_data_formatter.is_strict_claim_status_enabled", return_value=True):
        res = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Legacy Alpha claimed?")
        assert "The claim status for **Legacy Alpha** is currently unknown." in res
        assert "is a claimed company" not in res


def test_strict_mode_false_is_verified_resolves_unknown():
    """2. Strict false: only is_verified=False -> Unknown (domain verification never implies claim status)."""
    data = {
        "employment_history": [
            {"company": "Legacy Beta", "is_verified": False, "roles": []}
        ]
    }
    with patch("backend.services.user_data_formatter.is_strict_claim_status_enabled", return_value=True):
        res = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Legacy Beta claimed?")
        assert "The claim status for **Legacy Beta** is currently unknown." in res
        assert "is not a claimed company" not in res


def test_strict_mode_preserves_canonical_precedence_over_conflicting_is_verified():
    """3. Canonical precedence in strict mode: valid company_claim_status is respected even if is_verified conflicts."""
    data = {
        "employment_history": [
            {"company": "Strict Precedence One", "company_claim_status": 1, "is_verified": False, "roles": []},
            {"company": "Strict Precedence Zero", "company_claim_status": 0, "is_verified": True, "roles": []}
        ]
    }
    with patch("backend.services.user_data_formatter.is_strict_claim_status_enabled", return_value=True):
        res1 = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Strict Precedence One claimed?")
        assert "**Strict Precedence One** is a claimed company." in res1

        res0 = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Strict Precedence Zero claimed?")
        assert "**Strict Precedence Zero** is not a claimed company." in res0


def test_strict_mode_secondary_canonical_claim_status():
    """4. Secondary canonical field in strict mode: valid claim_status is respected when company_claim_status is None."""
    data = {
        "employment_history": [
            {"company": "Strict Secondary One", "company_claim_status": None, "claim_status": 1, "is_verified": False, "roles": []},
            {"company": "Strict Secondary Zero", "company_claim_status": None, "claim_status": 0, "is_verified": True, "roles": []}
        ]
    }
    with patch("backend.services.user_data_formatter.is_strict_claim_status_enabled", return_value=True):
        res1 = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Strict Secondary One claimed?")
        assert "**Strict Secondary One** is a claimed company." in res1

        res0 = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Strict Secondary Zero claimed?")
        assert "**Strict Secondary Zero** is not a claimed company." in res0


def test_strict_mode_explicit_canonical_false_remains_unclaimed():
    """5. Explicit canonical false: remains Unclaimed in strict mode (not treated as missing)."""
    data = {
        "employment_history": [
            {"company": "Strict False Corp", "claim_status": False, "roles": []}
        ]
    }
    with patch("backend.services.user_data_formatter.is_strict_claim_status_enabled", return_value=True):
        res = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Strict False Corp claimed?")
        assert "**Strict False Corp** is not a claimed company." in res


def test_legacy_compatibility_when_strict_mode_disabled():
    """6. Legacy compatibility: strict mode disabled retains the existing is_verified fallback."""
    data = {
        "employment_history": [
            {"company": "Legacy Alpha", "is_verified": True, "roles": []},
            {"company": "Legacy Beta", "is_verified": False, "roles": []}
        ]
    }
    with patch("backend.services.user_data_formatter.is_strict_claim_status_enabled", return_value=False):
        res1 = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Legacy Alpha claimed?")
        assert "**Legacy Alpha** is a claimed company." in res1

        res2 = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Legacy Beta claimed?")
        assert "**Legacy Beta** is not a claimed company." in res2


def test_strict_mode_unknown_when_canonical_missing_and_is_verified_unsupported():
    """7. Unknown values: strict mode returns Unknown when both canonical fields are missing and is_verified is present or unsupported."""
    data = {
        "employment_history": [
            {"company": "Unknown Legacy", "is_verified": "pending", "roles": []},
            {"company": "Unknown Strict", "is_verified": True, "roles": []}
        ]
    }
    with patch("backend.services.user_data_formatter.is_strict_claim_status_enabled", return_value=True):
        res1 = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Unknown Legacy claimed?")
        assert "The claim status for **Unknown Legacy** is currently unknown." in res1

        res2 = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Unknown Strict claimed?")
        assert "The claim status for **Unknown Strict** is currently unknown." in res2


def test_strict_mode_isolation_and_configuration_parsing():
    """8. Test isolation and configuration parsing: verifies default setting does not leak between patched invocations."""
    data = {
        "employment_history": [
            {"company": "Isolation Corp", "is_verified": True, "roles": []}
        ]
    }

    # Turn 1: Patch strict mode True -> must resolve to Unknown
    with patch("backend.services.user_data_formatter.is_strict_claim_status_enabled", return_value=True):
        res_strict = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Isolation Corp claimed?")
        assert "The claim status for **Isolation Corp** is currently unknown." in res_strict

    # Turn 2: Unpatched / default mode -> must resolve to Claimed via legacy fallback (proves patch was restored)
    res_default = format_single_intent(UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is Isolation Corp claimed?")
    assert "**Isolation Corp** is a claimed company." in res_default

    # Turn 3: Verify environment variable parsing helper with accepted / rejected values
    import os
    from backend.services.user_data_formatter import is_strict_claim_status_enabled

    # Accepted truthy values
    for truthy in ("true", "True", "TRUE", "1", "yes", "YES"):
        with patch.dict(os.environ, {"STRICT_CLAIM_STATUS_ENABLED": truthy}):
            assert is_strict_claim_status_enabled() is True, f"Failed for truthy value {truthy}"

    # Falsy / default / invalid values
    for falsy in ("false", "False", "0", "no", "", "invalid", "none", "99"):
        with patch.dict(os.environ, {"STRICT_CLAIM_STATUS_ENABLED": falsy}):
            assert is_strict_claim_status_enabled() is False, f"Failed for falsy value {falsy}"

    # Unset environment variable -> defaults to False
    with patch.dict(os.environ, {}, clear=True):
        assert is_strict_claim_status_enabled() is False
