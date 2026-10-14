"""
test_v2_data_extraction.py — Unit and Integration Test Suite for Employment Verification Status & Level Detection.
"""

import pytest
from backend.services.user_data_extractor import (
    is_truthy,
    normalize_employment_verification,
    extract_employment_history
)


def test_is_truthy_explicit_normalization():
    """Verify explicit boolean normalization for strings, ints, bools, and prevents bool('false') == True bug."""
    # True cases
    assert is_truthy(True) is True
    assert is_truthy(1) is True
    assert is_truthy("1") is True
    assert is_truthy("true") is True
    assert is_truthy("TRUE") is True
    assert is_truthy("yes") is True
    assert is_truthy("t") is True
    assert is_truthy("y") is True

    # False cases
    assert is_truthy(False) is False
    assert is_truthy(0) is False
    assert is_truthy("0") is False
    assert is_truthy("false") is False
    assert is_truthy("FALSE") is False
    assert is_truthy("no") is False
    assert is_truthy(None) is False
    assert is_truthy("") is False
    assert is_truthy("   ") is False


def test_normalize_employment_verification_verified():
    """Verify approved='1' and status='1' yields 'verified', 'Verified', is_verified=True."""
    # String "1"
    res1 = normalize_employment_verification(raw_approved="1", raw_status="1", verif_process=None)
    assert res1["status"] == "verified"
    assert res1["status_label"] == "Verified"
    assert res1["is_verified"] is True

    # Int 1
    res2 = normalize_employment_verification(raw_approved=1, raw_status=1, verif_process=None)
    assert res2["status"] == "verified"
    assert res2["status_label"] == "Verified"
    assert res2["is_verified"] is True

    # Whitespace " 1 "
    res3 = normalize_employment_verification(raw_approved=" 1 ", raw_status=" 1 ", verif_process=None)
    assert res3["status"] == "verified"
    assert res3["status_label"] == "Verified"
    assert res3["is_verified"] is True


def test_normalize_employment_verification_not_verified():
    """Verify non-'1' values when both present yield 'not_verified', 'Not Verified', is_verified=False."""
    cases = [
        ("1", "0"),
        ("0", "1"),
        ("0", "0"),
        ("2", "1"),
        (1, 0),
        (0, 1),
    ]
    for app, stat in cases:
        res = normalize_employment_verification(raw_approved=app, raw_status=stat, verif_process=None)
        assert res["status"] == "not_verified", f"Failed for approved={app}, status={stat}"
        assert res["status_label"] == "Not Verified"
        assert res["is_verified"] is False


def test_normalize_employment_verification_unknown():
    """Verify missing, None, empty, or whitespace-only inputs yield 'unknown', 'Status Unknown', is_verified=False."""
    cases = [
        (None, None),
        ("", "1"),
        ("1", ""),
        ("   ", "0"),
        (None, "1"),
        ("1", None),
    ]
    for app, stat in cases:
        res = normalize_employment_verification(raw_approved=app, raw_status=stat, verif_process=None)
        assert res["status"] == "unknown", f"Failed for approved={app}, status={stat}"
        assert res["status_label"] == "Status Unknown"
        assert res["is_verified"] is False


def test_normalize_employment_verification_level_resolution():
    """Verify highest active level in verificationProcess dictionary is resolved correctly."""
    # Level 4 takes precedence over Level 2
    res_l4 = normalize_employment_verification("1", "1", {"level1": True, "level2": True, "level3": True, "level4": True})
    assert res_l4["level"] == 4
    assert res_l4["level_label"] == "Level 4"

    # Level 3 with string boolean
    res_l3 = normalize_employment_verification("1", "1", {"level1": "true", "level3": "true", "level2": "1", "level4": "0"})
    assert res_l3["level"] == 3
    assert res_l3["level_label"] == "Level 3"

    # Level 2 with int 1
    res_l2 = normalize_employment_verification("1", "1", {"level2": 1, "level1": 1})
    assert res_l2["level"] == 2
    assert res_l2["level_label"] == "Level 2"

    # Level 1
    res_l1 = normalize_employment_verification("1", "1", {"level1": True})
    assert res_l1["level"] == 1
    assert res_l1["level_label"] == "Level 1"

    # No level active / missing
    res_none = normalize_employment_verification("1", "1", {})
    assert res_none["level"] is None
    assert res_none["level_label"] is None


def test_extract_employment_history_integration():
    """Integration test verifying extract_employment_history extracts roles and preserves raw approved & status values."""
    mock_payload = {
        "data": [
            {
                "id": 1,
                "company": "Tech Corp",
                "company_id": 101,
                "is_verified": 1,
                "totalExperienceMonths": 24,
                "lists": [
                    {
                        "id": 71533,
                        "designation": "Sr. Backend Developer",
                        "employment_type": "Full Time",
                        "joining_date": "2021-06-01",
                        "worked_till_date": "Present",
                        "still_working": "1",
                        "salary": "90000",
                        "approved": "1",
                        "status": "1",
                        "verificationProcess": {"level1": True, "level2": True, "level3": True, "level4": True},
                        "skill": [{"id": 12, "name": "Python"}]
                    },
                    {
                        "id": 82444,
                        "designation": "Software Engineer",
                        "employment_type": "Full Time",
                        "joining_date": "2019-01-01",
                        "worked_till_date": "2021-05-31",
                        "still_working": "0",
                        "salary": "70000",
                        "approved": "0",
                        "status": "1",
                        "verificationProcess": {"level1": True, "level2": True},
                        "skill": [{"id": 10, "name": "Java"}]
                    }
                ]
            }
        ]
    }

    result = extract_employment_history(mock_payload)
    assert len(result) == 1
    company = result[0]
    assert company["company"] == "Tech Corp"
    assert len(company["roles"]) == 2

    role1 = company["roles"][0]
    assert role1["designation"] == "Sr. Backend Developer"
    assert role1["approved"] == "1"   # Raw API field preserved
    assert role1["status"] == "1"     # Raw API field preserved
    assert role1["status_label"] == "Verified"
    assert role1["verification"]["status"] == "verified"
    assert role1["verification"]["level"] == 4
    assert role1["verification"]["level_label"] == "Level 4"
    assert role1["verification"]["is_verified"] is True

    role2 = company["roles"][1]
    assert role2["designation"] == "Software Engineer"
    assert role2["approved"] == "0"   # Raw API field preserved
    assert role2["status"] == "1"     # Raw API field preserved
    assert role2["status_label"] == "Not Verified"
    assert role2["verification"]["status"] == "not_verified"
    assert role2["verification"]["level"] == 2
    assert role2["verification"]["level_label"] == "Level 2"
    assert role2["verification"]["is_verified"] is False
