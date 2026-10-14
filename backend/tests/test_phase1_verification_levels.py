"""
test_phase1_verification_levels.py — Test Suite for Phase 1 Employee Verification Levels.
"""

import pytest
from backend.services.user_data_intent_parser import parse_user_data_intents, UserIntent
from backend.services.user_data_extractor import normalize_employment_verification, extract_employment_history
from backend.services.user_data_formatter import format_single_intent


def test_intent_level_definition_questions():
    """Verify general level definition queries trigger VERIFICATION_LEVEL_DEFINITION intent."""
    intents1 = parse_user_data_intents("What is Level 1 verification?")
    assert UserIntent.VERIFICATION_LEVEL_DEFINITION in intents1

    intents2 = parse_user_data_intents("Explain Level 2.")
    assert UserIntent.VERIFICATION_LEVEL_DEFINITION in intents2

    intents3 = parse_user_data_intents("What does Level 3 mean?")
    assert UserIntent.VERIFICATION_LEVEL_DEFINITION in intents3

    intents4 = parse_user_data_intents("What is Level 4 verification?")
    assert UserIntent.VERIFICATION_LEVEL_DEFINITION in intents4

    intents5 = parse_user_data_intents("Explain all four employment verification levels.")
    assert UserIntent.VERIFICATION_LEVEL_DEFINITION in intents5

    intents6 = parse_user_data_intents("What does company domain verification mean?")
    assert UserIntent.VERIFICATION_LEVEL_DEFINITION in intents6


def test_intent_personal_status_questions():
    """Verify employee-specific status queries trigger EMPLOYMENT_VERIFICATION intent."""
    intents1 = parse_user_data_intents("What is my verification level?")
    assert UserIntent.EMPLOYMENT_VERIFICATION in intents1

    intents2 = parse_user_data_intents("Have I reached Level 3?")
    assert UserIntent.EMPLOYMENT_VERIFICATION in intents2

    intents3 = parse_user_data_intents("Is my salary verified?")
    assert UserIntent.EMPLOYMENT_VERIFICATION in intents3

    intents4 = parse_user_data_intents("Is my company verified?")
    assert UserIntent.EMPLOYMENT_VERIFICATION in intents4


def test_intent_progress_questions():
    """Verify progress and next step queries trigger EMPLOYMENT_VERIFICATION_PROGRESS intent."""
    intents1 = parse_user_data_intents("What verification steps are remaining?")
    assert UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS in intents1

    intents2 = parse_user_data_intents("What level have I completed?")
    assert UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS in intents2

    intents3 = parse_user_data_intents("What do I need to complete Level 4?")
    assert UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS in intents3


def test_sequential_level_evaluation_all_four_completed():
    """Verify Level 4 is awarded when all sequential prerequisites (L1->L2->L3->L4) are met."""
    res = normalize_employment_verification(
        raw_approved="1",
        raw_status="1",
        verif_process={"level1": True, "level2": "1", "level3": 1, "level4": True},
        company_is_verified=True
    )
    assert res["level"] == 4
    assert res["level_label"] == "Level 4"
    assert res["is_verified"] is True
    assert res["levels_breakdown"]["level4"]["status"] == "Completed"


def test_independent_level_evaluation_prerequisite_independence():
    """Verify Level 4 is awarded based on level4 flag independently of role approval status or Level 3."""
    # level4 is True, approved="0" (role not approved)
    res = normalize_employment_verification(
        raw_approved="0",
        raw_status="1",
        verif_process={"level1": True, "level2": True, "level3": True, "level4": True},
        company_is_verified=True
    )
    # Under independent level model, highest_level equals highest true flag (Level 4)
    assert res["level"] == 4
    assert res["level_label"] == "Level 4"
    assert res["is_verified"] is False
    assert res["levels_breakdown"]["level3"]["status"] == "Completed"
    assert res["levels_breakdown"]["level4"]["status"] == "Completed"


def test_independent_level_evaluation_company_not_verified():
    """Verify employment verification level is independent of company_is_verified flag."""
    res = normalize_employment_verification(
        raw_approved="1",
        raw_status="1",
        verif_process={"level1": True, "level2": True, "level3": True, "level4": True},
        company_is_verified=False
    )
    # Independent flags yield level 4 based on verificationProcess level4 flag
    assert res["level"] == 4
    assert res["level_label"] == "Level 4"
    assert res["levels_breakdown"]["level2"]["status"] == "Completed"


def test_sequential_level_types_representation():
    """Verify string, integer, and boolean representations are normalized accurately."""
    res = normalize_employment_verification(
        raw_approved=1,
        raw_status="1",
        verif_process={"level1": "1", "level2": 1, "level3": True},
        company_is_verified="1"
    )
    assert res["level"] == 3
    assert res["level_label"] == "Level 3"


def test_multiple_records_independent_levels():
    """Verify multiple employment records evaluate their levels independently."""
    payload = {
        "data": [
            {
                "company": "Tech Corp",
                "is_verified": True,
                "lists": [
                    {
                        "designation": "Staff Engineer",
                        "approved": "1",
                        "status": "1",
                        "verificationProcess": {"level1": True, "level2": True, "level3": True, "level4": True}
                    }
                ]
            },
            {
                "company": "Startup Inc",
                "is_verified": False,
                "lists": [
                    {
                        "designation": "Junior Developer",
                        "approved": "0",
                        "status": "1",
                        "verificationProcess": {"level1": True}
                    }
                ]
            }
        ]
    }
    extracted = extract_employment_history(payload)
    roles = [r for c in extracted for r in c.get("roles", [])]
    assert len(roles) == 2
    assert roles[0]["verification"]["level"] == 4
    assert roles[1]["verification"]["level"] == 1


def test_format_verification_level_definitions():
    """Verify format_single_intent formats general level definition responses correctly."""
    res_all = format_single_intent(UserIntent.VERIFICATION_LEVEL_DEFINITION, {}, None, query="Explain all four levels")
    assert "Level 1 — Work Identity" in res_all
    assert "Level 2 — Company Domain Verification" in res_all
    assert "Level 3 — Employment Verification" in res_all
    assert "Level 4 — Salary Verification" in res_all

    res_l3 = format_single_intent(UserIntent.VERIFICATION_LEVEL_DEFINITION, {}, None, query="What is Level 3 verification?")
    assert "Level 3 — Employment Verification" in res_l3
    assert "Your employer has confirmed your employment and role." in res_l3


def test_format_personal_verification_progress():
    """Verify detailed multi-level breakdown and next step explanations for progress queries."""
    data = {
        "employment_history": [
            {
                "company": "InnoTech Solution",
                "is_verified": True,
                "roles": [
                    {
                        "designation": "Software Engineer",
                        "verification": {
                            "status": "verified",
                            "level": 3,
                            "levels_breakdown": {
                                "level1": {"status": "Completed"},
                                "level2": {"status": "Completed"},
                                "level3": {"status": "Completed"},
                                "level4": {"status": "Pending"}
                            },
                            "next_step": "To reach Level 4 (Salary Verification), provide verified salary evidence for this role."
                        }
                    }
                ]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS, data, None, query="What steps are remaining?")
    assert "Highest Completed Level: Level 3" in res
    assert "Level 1 (Work Identity): Completed" in res
    assert "Level 2 (Company Domain Verification): Completed" in res
    assert "Level 3 (Employment Verification): Completed" in res
    assert "Level 4 (Salary Verification): Pending" in res
    assert "Next Step: To reach Level 4" in res


def test_audit_missing_approved_and_status():
    """Audit 7 & 8: Missing approved or status parameters results in unknown status."""
    res_no_app = normalize_employment_verification(None, "1", {"level1": True})
    assert res_no_app["status"] == "unknown"
    assert res_no_app["is_verified"] is False

    res_no_stat = normalize_employment_verification("1", None, {"level1": True})
    assert res_no_stat["status"] == "unknown"
    assert res_no_stat["is_verified"] is False


def test_audit_level4_true_while_level3_unknown_or_false():
    """Audit 3 & 4: Level 4 true is evaluated independently of Level 3."""
    # Level 3 unknown (approved is None)
    res_unk = normalize_employment_verification(None, "1", {"level1": True, "level2": True, "level3": True, "level4": True}, company_is_verified=True)
    assert res_unk["level"] == 4
    assert res_unk["levels_breakdown"]["level4"]["status"] == "Completed"

    # Level 3 false (approved="0")
    res_fls = normalize_employment_verification("0", "1", {"level1": True, "level2": True, "level3": True, "level4": True}, company_is_verified=True)
    assert res_fls["level"] == 4
    assert res_fls["levels_breakdown"]["level3"]["status"] == "Completed"
    assert res_fls["levels_breakdown"]["level4"]["status"] == "Completed"


def test_audit_company_verified_employment_pending():
    """Audit 5: Company verified while employee role is pending (approved='0', status='1')."""
    res = normalize_employment_verification("0", "1", {"level1": True, "level2": True}, company_is_verified=True)
    assert res["level"] == 2
    assert res["status"] == "not_verified"
    assert res["levels_breakdown"]["level2"]["status"] == "Completed"
    assert res["levels_breakdown"]["level3"]["status"] == "Declined"


def test_audit_company_not_verified_role_flags_true():
    """Audit 6: Company is_verified status remains independent from employment verification level."""
    res = normalize_employment_verification("1", "1", {"level1": True, "level2": True, "level3": True, "level4": True}, company_is_verified=False)
    assert res["level"] == 4
    assert res["levels_breakdown"]["level2"]["status"] == "Completed"


def test_audit_missing_level_flags():
    """Audit 9 & 10: Highest level corresponds to highest active level flag independently."""
    # When Level 2 flag is missing but Level 3 flag is true, highest active flag is Level 3
    res_no_l2 = normalize_employment_verification("1", "1", {"level1": True, "level3": True}, company_is_verified=True)
    assert res_no_l2["level"] == 3

    # When Level 4 flag is missing, highest completed level is 3
    res_no_l4 = normalize_employment_verification("1", "1", {"level1": True, "level2": True, "level3": True}, company_is_verified=True)
    assert res_no_l4["level"] == 3


def test_sixteen_flag_combinations_matrix():
    """Test all 16 binary flag combinations (L1, L2, L3, L4) with independent verification flags."""
    for (l1_val, l2_val, l3_val, l4_val) in [(l1, l2, l3, l4) for l1 in (0, 1) for l2 in (0, 1) for l3 in (0, 1) for l4 in (0, 1)]:
        vp = {
            "level1": bool(l1_val),
            "level2": bool(l2_val),
            "level3": bool(l3_val),
            "level4": bool(l4_val),
        }
        res = normalize_employment_verification(
            raw_approved="1",
            raw_status="1",
            verif_process=vp,
            company_is_verified=True
        )
        # L1 is established by employment record (l1=True always)
        active_levels = [lvl for lvl, act in [(1, 1), (2, l2_val), (3, l3_val), (4, l4_val)] if act]
        expected_level = max(active_levels)
        assert res["level"] == expected_level, f"Failed for combination {(l1_val, l2_val, l3_val, l4_val)}"
        assert res["levels_breakdown"]["level1"]["status"] == "Completed"
        assert (res["levels_breakdown"]["level2"]["status"] == "Completed") == bool(l2_val)
        assert (res["levels_breakdown"]["level3"]["status"] == "Completed") == bool(l3_val)
        assert (res["levels_breakdown"]["level4"]["status"] == "Completed") == bool(l4_val)


def test_explicit_zero_and_false_flags():
    """Verify explicit 0, '0', False, 'false' flags are strictly respected and highest active level is returned."""
    # Explicit 0 for level1 with 1 for level4 -> highest active level is 4
    res1 = normalize_employment_verification("1", "1", {"level1": 0, "level2": 1, "level3": 1, "level4": 1}, company_is_verified=True)
    assert res1["level"] == 4

    # String "0" for level2 with "1" for level3 and level4 -> highest active is 4
    res2 = normalize_employment_verification("1", "1", {"level1": "1", "level2": "0", "level3": "1", "level4": "1"}, company_is_verified=True)
    assert res2["level"] == 4

    # String "false" for level3 with "true" for level4 -> highest active is 4
    res3 = normalize_employment_verification("1", "1", {"level1": "true", "level2": "true", "level3": "false", "level4": "true"}, company_is_verified=True)
    assert res3["level"] == 4

    # None and malformed values
    res4 = normalize_employment_verification("1", "1", {"level1": True, "level2": None, "level3": "invalid", "level4": True}, company_is_verified=True)
    assert res4["level"] == 4


def test_auth_identity_fail_closed_guard():
    """Verify personal-data requests fail closed when canonical db_user_id is missing."""
    from fastapi.testclient import TestClient
    from backend.main import app
    from backend.api.auth import verify_token

    client = TestClient(app)

    # Case 1: Unauthenticated request without token returns 401 Unauthorized
    res1 = client.post("/chat", json={
        "messages": [{"role": "user", "content": "What is my employment verification status?"}],
        "session_id": "test_session_guard"
    })
    assert res1.status_code == 401

    # Case 2: Authenticated token payload missing canonical db_user_id triggers fail-closed guard
    def _override_missing_db_id(request=None, credentials=None):
        if request is not None and hasattr(request, "state"):
            request.state.db_user_id = None
            request.state.user_id = "untrusted_client_id"
            request.state.role = "employee"
        return {"email": "test@test.com", "role": "employee"}

    app.dependency_overrides[verify_token] = _override_missing_db_id
    try:
        res2 = client.post("/chat", json={
            "messages": [{"role": "user", "content": "What is my employment verification status?"}],
            "session_id": "test_session_guard"
        })
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["reply"] == "Please log in to view your profile details."
        assert data2["success"] is True
    finally:
        app.dependency_overrides.pop(verify_token, None)


def test_issue6_l1_missing_flag_progression():
    """
    Issue 6 Regression Test:
    Verify that an existing valid employment record establishes Level 1 (Work Identity)
    regardless of whether verificationProcess.level1 is missing, None, False, 0, "0", "false", or True.
    """
    cases_l1 = [
        ({}, "Case A — missing"),
        ({"level1": None}, "Case B — None"),
        ({"level1": False}, "Case C — False"),
        ({"level1": 0}, "Case D — numeric 0"),
        ({"level1": "0"}, "Case E — string '0'"),
        ({"level1": "false"}, "Case F — string 'false'"),
        ({"level1": True}, "Case G — explicit True"),
    ]

    for vp, label in cases_l1:
        res = normalize_employment_verification(
            raw_approved="0",
            raw_status="0",
            verif_process=vp,
            company_is_verified=False
        )
        assert res["level"] == 1, f"Failed for {label}: level should be 1"
        assert res["level_label"] == "Level 1", f"Failed for {label}: level_label should be 'Level 1'"
        assert res["next_level"] == 2, f"Failed for {label}: next_level should be 2"
        assert res["next_level_label"] == "Company Domain Verification", f"Failed for {label}: next_level_label"
        assert res["next_action"] == "Your company needs to join CollarCheck and verify its company domain.", f"Failed for {label}"
        assert "Add your employment details" not in res["next_action"], f"Failed for {label}"

    # Higher Level Precedence Tests: L4 > L3 > L2 > L1
    # L2 Active
    res_l2 = normalize_employment_verification(
        raw_approved="0",
        raw_status="0",
        verif_process={"level1": False, "level2": True},
        company_is_verified=True
    )
    assert res_l2["level"] == 2
    assert res_l2["next_level"] == 3

    # L3 Active (role approved & status="1", level3 active)
    res_l3 = normalize_employment_verification(
        raw_approved="1",
        raw_status="1",
        verif_process={"level1": False, "level3": True},
        company_is_verified=True
    )
    assert res_l3["level"] == 3
    assert res_l3["next_level"] == 4

    # L4 Active
    res_l4 = normalize_employment_verification(
        raw_approved="1",
        raw_status="1",
        verif_process={"level1": False, "level4": True},
        company_is_verified=True
    )
    assert res_l4["level"] == 4
    assert res_l4["next_level"] is None




