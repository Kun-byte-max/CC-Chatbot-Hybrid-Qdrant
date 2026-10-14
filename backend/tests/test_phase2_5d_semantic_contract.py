"""
test_phase2_5d_semantic_contract.py — Targeted verification tests for Phase 2.5D
Reconciled L1–L4 Verification Semantic Contract implementation audit.
"""

import pytest
from backend.services.user_data_extractor import normalize_employment_verification


def test_l1_semantic_state_and_backward_compatibility():
    """Verify L1 returns semantic_contract with explicit evidence and observable flags."""
    res = normalize_employment_verification("1", "1", {"level1": True})

    # Backward compatibility checks
    assert res["highest_completed_level"] == 1
    assert res["status"] == "verified"
    assert res["levels_breakdown"]["level1"]["completed"] is True

    # Structured semantic_contract checks
    sem = res["semantic_contract"]
    assert "level1" in sem
    l1 = sem["level1"]
    assert l1["label"] == "Work Identity"
    assert l1["state"] == "COMPLETED"
    assert l1["primary_actor"] == "employee"
    assert l1["email_verification_observable"] is False
    assert l1["completion_evidence"] in ("verification_process_level1", "employment_record_present")
    assert l1["supported_today"] is True


def test_l2_claimed_vs_domain_verified():
    """Verify L2 distinguishes company claim status from company domain verification."""
    # Company claimed (1) but domain unverified (False)
    res = normalize_employment_verification("0", "0", {"level1": True}, company_is_verified=False, company_claim_status=1)

    assert res["company_claim_status"] == 1
    assert res["company_claim_label"] == "Claimed"
    assert res["company_domain_verified"] is False
    assert res["levels_breakdown"]["level2"]["completed"] is False

    sem_l2 = res["semantic_contract"]["level2"]
    assert sem_l2["state"] == "NOT_COMPLETED"
    assert sem_l2["primary_actor"] == "company"
    assert sem_l2["employee_action"] == "Request company domain verification on CollarCheck"
    assert sem_l2["company_action"] == "Verify company domain on CollarCheck"
    assert sem_l2["supported_today"] is False


def test_l3_approved_active_and_declined():
    """Verify L3 handles role confirmation and explicit employer rejections."""
    # Approved role
    res_app = normalize_employment_verification("1", "1", {"level1": True, "level2": True, "level3": True})
    assert res_app["levels_breakdown"]["level3"]["completed"] is True
    assert res_app["semantic_contract"]["level3"]["state"] == "COMPLETED"
    assert res_app["semantic_contract"]["level3"]["primary_actor"] == "company"

    # Declined role
    res_dec = normalize_employment_verification("declined", "declined", {"level1": True, "level2": True})
    assert res_dec["levels_breakdown"]["level3"]["completed"] is False
    assert res_dec["levels_breakdown"]["level3"]["status"] == "Declined"
    assert res_dec["semantic_contract"]["level3"]["state"] == "DECLINED"
    assert res_dec["semantic_contract"]["level3"]["primary_actor"] == "employee"
    assert res_dec["semantic_contract"]["level3"]["cta_label"] == "Resolve Rejection"


def test_l4_salary_present_vs_verified():
    """Verify L4 distinguishes salary data presence from salary verification."""
    # Salary process flag is False even if salary data exists
    res_unverif = normalize_employment_verification("1", "1", {"level1": True, "level2": True, "level3": True, "level4": False})
    assert res_unverif["levels_breakdown"]["level4"]["completed"] is False
    assert res_unverif["semantic_contract"]["level4"]["state"] == "NOT_COMPLETED"
    assert res_unverif["semantic_contract"]["level4"]["primary_actor"] == "employee"

    # Verified salary
    res_verif = normalize_employment_verification("1", "1", {"level1": True, "level2": True, "level3": True, "level4": True})
    assert res_verif["levels_breakdown"]["level4"]["completed"] is True
    assert res_verif["semantic_contract"]["level4"]["state"] == "COMPLETED"


def test_sequential_gating_preserved():
    """Verify sequential gating algorithm remains strictly preserved."""
    # Level 3 active but Level 2 incomplete -> highest_completed_level remains Level 1
    res = normalize_employment_verification("1", "1", {"level1": True, "level2": False, "level3": True}, company_is_verified=False)
    assert res["highest_completed_level"] == 1
    assert res["highest_independent_verified_level"] == 3
    assert res["next_level"] == 2
