"""
test_phase1a_employment_cards.py — Test Suite for Phase 1A Backend Employment Cards Contract.
"""

import pytest
from backend.services.user_data_service import handle_query, ServiceResponse
from backend.services.user_data_intent_parser import UserIntent
from backend.services.user_data_extractor import extract_employment_history, normalize_employment_verification


def test_single_employment_record_produces_one_structured_result():
    mock_payload = {
        "data": [
            {
                "id": "100",
                "company": "Alpha Corp",
                "company_id": "50",
                "company_logo": "https://example.com/logo.png",
                "is_verified": True,
                "lists": [
                    {
                        "id": "1001",
                        "designation": "Staff Engineer",
                        "employment_type": "Full-time",
                        "joining_date": "2022-01-01",
                        "worked_till_date": "Present",
                        "still_working": "1",
                        "approved": "1",
                        "status": "1",
                        "verificationProcess": {"level1": True, "level2": True, "level3": True, "level4": True},
                        "skill": [{"id": "1", "name": "Python"}],
                        "haveSalary": True,
                        "haveDocument": False,
                        "haveReview": False
                    }
                ]
            }
        ]
    }
    extracted = extract_employment_history(mock_payload)
    assert len(extracted) == 1
    roles = extracted[0]["roles"]
    assert len(roles) == 1
    role = roles[0]

    assert role["designation"] == "Staff Engineer"
    assert role["verification"]["status"] == "verified"
    assert role["verification"]["level"] == 4


def test_multiple_companies_produce_multiple_structured_results():
    mock_payload = {
        "data": [
            {
                "id": "101",
                "company": "Company A",
                "lists": [{"id": "1", "designation": "Role A", "still_working": "1", "approved": "1", "status": "1", "verificationProcess": {"level1": True}}]
            },
            {
                "id": "102",
                "company": "Company B",
                "lists": [{"id": "2", "designation": "Role B", "still_working": "0", "approved": "1", "status": "1", "verificationProcess": {"level1": True}}]
            }
        ]
    }
    history = extract_employment_history(mock_payload)
    assert len(history) == 2
    assert history[0]["company"] == "Company A"
    assert history[1]["company"] == "Company B"


def test_company_containing_multiple_roles_produces_one_result_per_role():
    mock_payload = {
        "data": [
            {
                "id": "200",
                "company": "SP HUMANS PRIVATE LIMITED",
                "company_id": "39256",
                "is_verified": False,
                "lists": [
                    {"id": "5217", "designation": "technician and operator", "still_working": 0, "approved": "1", "status": "1", "verificationProcess": {"level1": True, "level2": True, "level3": True, "level4": True}},
                    {"id": "5219", "designation": "Teacher assistant", "still_working": 0, "approved": "1", "status": "1", "verificationProcess": {"level1": True, "level2": True, "level3": True, "level4": True}},
                    {"id": "5218", "designation": "Flutter Developer", "still_working": 0, "approved": "1", "status": "1", "verificationProcess": {"level1": True, "level2": True, "level3": True, "level4": True}}
                ]
            }
        ]
    }
    history = extract_employment_history(mock_payload)
    assert len(history) == 1
    assert len(history[0]["roles"]) == 3


def test_employment_status_normalization_current_and_previous():
    mock_payload = {
        "data": [
            {
                "company": "TechCorp",
                "lists": [
                    {"id": "1", "designation": "Current Dev", "still_working": "1", "worked_till_date": "Present", "approved": "1", "status": "1"},
                    {"id": "2", "designation": "Former Dev", "still_working": "0", "worked_till_date": "2025-01-01", "approved": "1", "status": "1"}
                ]
            }
        ]
    }
    history = extract_employment_history(mock_payload)
    roles = history[0]["roles"]
    assert roles[0]["still_working"] == "1"
    assert roles[1]["still_working"] == "0"


def test_verification_status_outcomes():
    res_verified = normalize_employment_verification("1", "1", {"level1": True})
    assert res_verified["status"] == "verified"

    res_unapproved = normalize_employment_verification("0", "1", {"level1": True})
    assert res_unapproved["status"] == "not_verified"

    res_unknown = normalize_employment_verification(None, "1", {"level1": True})
    assert res_unknown["status"] == "unknown"


def test_verification_process_flags_preserved_faithfully():
    vp = {"level1": False, "level2": False, "level3": True, "level4": True}
    res = normalize_employment_verification("1", "1", vp, company_is_verified=True)
    assert res["levels_breakdown"]["level1"]["completed"] is False
    assert res["levels_breakdown"]["level2"]["completed"] is False
    assert res["levels_breakdown"]["level3"]["completed"] is True
    assert res["levels_breakdown"]["level4"]["completed"] is True
    assert res["highest_completed_level"] == 4
    assert res["level"] == 4


def test_company_is_verified_exposed_separately():
    res_comp_unverified = normalize_employment_verification("1", "1", {"level1": True, "level2": True}, company_is_verified=False)
    # Employment level is based on source verificationProcess level flags independently
    assert res_comp_unverified["levels_breakdown"]["level2"]["completed"] is True
    assert res_comp_unverified["highest_completed_level"] == 2


def test_phase1a1_real_staging_data_test_case():
    vp = {
        "level1": False,
        "level2": False,
        "level3": True,
        "level4": True
    }
    res = normalize_employment_verification("1", "1", vp)
    assert res["levels_breakdown"]["level1"]["completed"] is False
    assert res["levels_breakdown"]["level2"]["completed"] is False
    assert res["levels_breakdown"]["level3"]["completed"] is True
    assert res["levels_breakdown"]["level4"]["completed"] is True
    assert res["highest_completed_level"] == 4
    assert res["level"] == 4


def test_phase1a1_verification_combinations():
    # Case A — Nothing verified
    res_a = normalize_employment_verification("1", "1", {"level1": False, "level2": False, "level3": False, "level4": False})
    assert res_a["highest_completed_level"] is None

    # Case B — Only Level 1
    res_b = normalize_employment_verification("1", "1", {"level1": True, "level2": False, "level3": False, "level4": False})
    assert res_b["highest_completed_level"] == 1

    # Case C — Only Level 3
    res_c = normalize_employment_verification("1", "1", {"level1": False, "level2": False, "level3": True, "level4": False})
    assert res_c["highest_completed_level"] == 3

    # Case D — Level 3 + Level 4
    res_d = normalize_employment_verification("1", "1", {"level1": False, "level2": False, "level3": True, "level4": True})
    assert res_d["highest_completed_level"] == 4

    # Case E — All levels
    res_e = normalize_employment_verification("1", "1", {"level1": True, "level2": True, "level3": True, "level4": True})
    assert res_e["highest_completed_level"] == 4

