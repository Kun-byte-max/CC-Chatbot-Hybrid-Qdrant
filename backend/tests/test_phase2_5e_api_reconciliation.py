"""
test_phase2_5e_api_reconciliation.py — Targeted test suite for Phase 2.5E
Tests real API-shaped payload scenarios (Cases A through J) and focused level guidance routing.
"""

import pytest
from backend.services.user_data_extractor import normalize_employment_verification, extract_employment_history
from backend.services.user_data_formatter import format_multi_intent_response
from backend.services.user_data_intent_parser import parse_user_data_intents_deterministic, UserIntent


def test_case_a_all_levels_false():
    """CASE A: level1=false, level2=false, level3=false, level4=false"""
    vp = {"level1": False, "level2": False, "level3": False, "level4": False}
    res = normalize_employment_verification("0", "0", vp, company_is_verified=False, company_claim_status=1)

    sem = res["semantic_contract"]
    assert sem["level1"]["state"] == "NOT_COMPLETED"
    assert sem["level2"]["state"] == "NOT_COMPLETED"
    assert sem["level3"]["state"] == "NOT_COMPLETED"
    assert sem["level4"]["state"] == "NOT_COMPLETED"


def test_case_b_out_of_order_real_api_state():
    """CASE B: level1=false, level2=false, level3=true, level4=true (SP HUMANS real API case)"""
    vp = {"level1": False, "level2": False, "level3": True, "level4": True}
    res = normalize_employment_verification("1", "1", vp, company_is_verified=False, company_claim_status=1)

    sem = res["semantic_contract"]
    assert sem["level1"]["state"] == "NOT_COMPLETED"
    assert sem["level2"]["state"] == "NOT_COMPLETED"
    assert sem["level3"]["state"] == "COMPLETED"
    assert sem["level4"]["state"] == "COMPLETED"


def test_case_c_claim_status_not_equal_domain_verified():
    """CASE C: claim_status=1, is_verified=false, level2=false"""
    vp = {"level1": False, "level2": False, "level3": False, "level4": False}
    res = normalize_employment_verification("0", "0", vp, company_is_verified=False, company_claim_status=1)

    assert res["company_claim_status"] == 1
    assert res["company_claim_label"] == "Claimed"
    assert res["company_domain_verified"] is False
    assert res["semantic_contract"]["level2"]["state"] == "NOT_COMPLETED"


def test_case_d_salary_presence_not_equal_salary_verified():
    """CASE D: haveSalary=true, salary="343", level4=false"""
    vp = {"level1": True, "level2": True, "level3": True, "level4": False}
    res = normalize_employment_verification("1", "1", vp, company_is_verified=True)

    assert res["semantic_contract"]["level4"]["state"] == "NOT_COMPLETED"


def test_case_e_salary_level4_true():
    """CASE E: level4=true -> L4 COMPLETED"""
    vp = {"level1": True, "level2": True, "level3": True, "level4": True}
    res = normalize_employment_verification("1", "1", vp, company_is_verified=True)

    assert res["semantic_contract"]["level4"]["state"] == "COMPLETED"


def test_case_f_approved_active_corroborating_rule():
    """CASE F: level3=false in vp, but approved="1", status="1" """
    vp = {"level1": True, "level2": True, "level4": False}
    res = normalize_employment_verification("1", "1", vp, company_is_verified=True)

    assert res["semantic_contract"]["level3"]["state"] == "COMPLETED"


def test_case_g_out_of_order_l3_only():
    """CASE G: level3=true, level1=false, level2=false"""
    vp = {"level1": False, "level2": False, "level3": True, "level4": False}
    res = normalize_employment_verification("0", "0", vp, company_is_verified=False)

    assert res["semantic_contract"]["level3"]["state"] == "COMPLETED"


def test_case_h_missing_level1_flag():
    """CASE H: level1 missing in vp dictionary"""
    vp = {"level2": False, "level3": False, "level4": False}
    res = normalize_employment_verification("0", "0", vp, company_is_verified=False)

    # When level1 is explicitly omitted from vp dict, state is NOT_COMPLETED
    assert res["semantic_contract"]["level1"]["state"] == "NOT_COMPLETED"


def test_case_i_focused_level_3_query():
    """CASE I: Specific query 'How do I complete Level 3?' -> focused Level 3 guidance"""
    mock_data = {
        "employment_history": [
            {
                "company": "InnoTech Solution",
                "is_verified": False,
                "roles": [
                    {
                        "designation": "Software analyst",
                        "still_working": "1",
                        "verification": normalize_employment_verification("0", "1", {"level1": False, "level2": False, "level3": False, "level4": False})
                    }
                ]
            }
        ]
    }
    extracted = {UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS: mock_data}
    api_resp = {"/employee/allEmployementNew": {"status": True, "data": mock_data["employment_history"]}}

    reply = format_multi_intent_response(
        [UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS],
        extracted,
        api_resp,
        query="How do I complete Level 3 at InnoTech Solution?"
    )

    assert "Employment Verification Guidance" in reply
    assert "Level 1 — Work Identity" not in reply  # Focused guidance, not full roadmap


def test_case_j_generic_query_full_roadmap():
    """CASE J: Generic query 'What should I do next for my verification?' -> full generic roadmap"""
    mock_data = {
        "employment_history": [
            {
                "company": "InnoTech Solution",
                "is_verified": False,
                "roles": [
                    {
                        "designation": "Software analyst",
                        "still_working": "1",
                        "verification": normalize_employment_verification("0", "1", {"level1": False, "level2": False, "level3": False, "level4": False})
                    }
                ]
            }
        ]
    }
    extracted = {UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS: mock_data}
    api_resp = {"/employee/allEmployementNew": {"status": True, "data": mock_data["employment_history"]}}

    reply = format_multi_intent_response(
        [UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS],
        extracted,
        api_resp,
        query="What should I do next for my verification?"
    )

    assert "Verification roadmap" in reply
    assert "Level 1 — Work Identity" in reply
    assert "Level 2 — Company Verification" in reply
