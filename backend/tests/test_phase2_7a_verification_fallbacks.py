"""
test_phase2_7a_verification_fallbacks.py

Regression test suite for Phase 2.7A-1 minimal fixes:
- GAP-01: Explicit Level 1 verification evidence required (verificationProcess=None / missing key -> L1 incomplete)
- GAP-02: Centralized employment status normalization (supported booleans, status strings, unknown handling)
"""

import pytest
from backend.services.user_data_extractor import (
    normalize_employment_verification,
    is_truthy,
    is_falsy,
    is_current_role,
    is_previous_role,
)
from backend.services.user_data_formatter import _classify_employment_records, format_single_intent
from backend.services.user_data_intent_parser import UserIntent


# ==============================================================================
# GAP-01 TESTS: Explicit Level 1 Verification Evidence Required
# ==============================================================================

def test_gap01_case1_level1_true():
    """1. verificationProcess={"level1": True} -> L1 completed."""
    result = normalize_employment_verification("0", "0", {"level1": True})
    assert result["levels_breakdown"]["level1"]["completed"] is True
    assert result["highest_independent_verified_level"] == 1


def test_gap01_case2_level1_false():
    """2. verificationProcess={"level1": False} -> L1 incomplete."""
    result = normalize_employment_verification("0", "0", {"level1": False})
    assert result["levels_breakdown"]["level1"]["completed"] is False
    assert result["highest_independent_verified_level"] is None


def test_gap01_case3_empty_dict():
    """3. verificationProcess={} -> L1 incomplete."""
    result = normalize_employment_verification("0", "0", {})
    assert result["levels_breakdown"]["level1"]["completed"] is False
    assert result["highest_independent_verified_level"] is None


def test_gap01_case4_none():
    """4. verificationProcess=None -> L1 incomplete."""
    result = normalize_employment_verification("0", "0", None)
    assert result["levels_breakdown"]["level1"]["completed"] is False
    assert result["highest_independent_verified_level"] is None


def test_gap01_case5_missing_key_via_raw_payload():
    """5. Missing verificationProcess key -> L1 incomplete."""
    result = normalize_employment_verification(raw_approved=None, raw_status=None, verif_process=None)
    assert result["levels_breakdown"]["level1"]["completed"] is False
    assert result["highest_independent_verified_level"] is None


def test_gap01_case6_missing_level1_with_other_levels_present():
    """6. Missing/null level1 with other verification levels present -> L1 incomplete; valid other levels remain independently completed."""
    result = normalize_employment_verification("0", "0", {"level2": True, "level3": True})
    assert result["levels_breakdown"]["level1"]["completed"] is False
    assert result["levels_breakdown"]["level2"]["completed"] is True
    assert result["levels_breakdown"]["level3"]["completed"] is True
    assert result["highest_independent_verified_level"] == 3
    # Sequential level stops at None because L1 is missing
    assert result["highest_completed_level"] is None


def test_gap01_case7_l1_missing_with_l3_l4_complete_preserves_out_of_order():
    """7. L1 missing while L3/L4 are complete -> preserve out-of-order completion and recommend next incomplete level."""
    result = normalize_employment_verification("1", "1", {"level4": True})
    assert result["levels_breakdown"]["level1"]["completed"] is False
    assert result["levels_breakdown"]["level3"]["completed"] is True
    assert result["levels_breakdown"]["level4"]["completed"] is True
    assert result["highest_independent_verified_level"] == 4
    # Out of order gap: next required sequential level is 1
    assert result["next_level"] == 1


def test_gap01_case8_generic_progress_across_roles():
    """8. Generic progress across multiple roles returns every role and correct summary counts."""
    # Role A: L1 complete, L2 complete. Role B: verificationProcess missing (L1 incomplete).
    record_a = {
        "company": "Alpha Corp",
        "roles": [{
            "id": "101",
            "designation": "Developer",
            "still_working": "1",
            "approved": "0",
            "status": "0",
            "verificationProcess": {"level1": True, "level2": True}
        }]
    }
    record_b = {
        "company": "Beta Inc",
        "roles": [{
            "id": "102",
            "designation": "Analyst",
            "still_working": "0",
            "approved": "0",
            "status": "0"
            # verificationProcess missing completely
        }]
    }

    reply_text = format_single_intent(
        intent=UserIntent.EMPLOYMENT_VERIFICATION,
        extracted_data={"employment_history": [record_a, record_b]},
        api_response={"data": [record_a, record_b]},
        query="show my verification progress"
    )

    assert "2 records" in reply_text
    assert "Alpha Corp" in reply_text
    assert "Beta Inc" in reply_text


# ==============================================================================
# GAP-02 TESTS: Employment Status Normalization
# ==============================================================================

@pytest.mark.parametrize("input_val, expected", [
    ("yes", True),
    ("Yes  ", True),
    ("Y", True),
    ("y", True),
    ("1", True),
    (1, True),
    ("true", True),
    (True, True),
    ("no", False),
    ("  No", False),
    ("N", False),
    ("0", False),
    (0, False),
    ("false", False),
    (False, False),
    (None, False),
    ("unknown_string", False),
])
def test_gap02_is_truthy_and_falsy(input_val, expected):
    """Test boolean truthiness helper with whitespace and case normalization."""
    assert is_truthy(input_val) == expected


@pytest.mark.parametrize("role_dict, is_curr, is_prev", [
    ({"still_working": "yes"}, True, False),
    ({"still_working": "Y "}, True, False),
    ({"still_working": "1"}, True, False),
    ({"employment_status": "active"}, True, False),
    ({"employment_status": " CURRENT "}, True, False),
    ({"worked_till_date": "Present"}, True, False),
    ({"worked_till_date": "current"}, True, False),
    ({"still_working": "no"}, False, True),
    ({"still_working": "0"}, False, True),
    ({"employment_status": "previous"}, False, True),
    ({"employment_status": "PAST"}, False, True),
    ({"worked_till_date": "2022-12-31"}, False, True),
    ({"still_working": None, "employment_status": "unknown_val"}, False, False),
    ({}, False, False),
])
def test_gap02_employment_status_classification(role_dict, is_curr, is_prev):
    """Test categorical role classification into current, previous, or unknown."""
    assert is_current_role(role_dict) == is_curr
    assert is_previous_role(role_dict) == is_prev


def test_gap02_classify_records_helper():
    """Assert _classify_employment_records correctly buckets current, previous, and unknown roles."""
    curr_rec = {"id": "1", "company": "Comp A", "still_working": "yes"}
    prev_rec = {"id": "2", "company": "Comp B", "still_working": "no"}
    unk_rec = {"id": "3", "company": "Comp C", "still_working": None, "employment_status": "invalid_status"}

    currents, previouses, unknowns = _classify_employment_records([curr_rec, prev_rec, unk_rec])

    assert len(currents) == 1
    assert currents[0]["id"] == "1"
    assert len(previouses) == 1
    assert previouses[0]["id"] == "2"
    assert len(unknowns) == 1
    assert unknowns[0]["id"] == "3"
