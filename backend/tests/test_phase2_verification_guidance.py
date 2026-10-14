"""
test_phase2_verification_guidance.py — Comprehensive Test Suite for Phase 2 Interactive Employee Verification Guidance.

Covers all 18 mandatory test scenarios:
A. Verification overview.
B. Next-step progression.
C. Level 2 to Level 3 guidance.
D. Level 3 to Level 4 guidance.
E. Level 4 already completed.
F. Company verification incomplete.
G. Employment approval pending.
H. Employment declined.
I. Unknown verification data.
J. API unavailable.
K. Multiple employment records.
L. Current versus previous company.
M. Ambiguous employment selection.
N. Follow-up questions.
O. Company switching.
P. Employee isolation.
Q. Existing employee FAQ regression.
R. Employer-side PEOPLE_SEARCH regression.
"""

import pytest
from backend.services.user_data_intent_parser import parse_user_data_intents, UserIntent
from backend.services.user_data_extractor import normalize_employment_verification, extract_employment_history
from backend.services.user_data_formatter import format_single_intent


# Scenario A: Verification Overview
def test_scenario_a_verification_overview():
    intents = parse_user_data_intents("What is my employment verification status?")
    assert UserIntent.EMPLOYMENT_VERIFICATION in intents or UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS in intents


# Scenario B: Next-step Progression
def test_scenario_b_next_step_progression():
    intents = parse_user_data_intents("What should I do next for my verification?")
    assert UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS in intents

    norm = normalize_employment_verification("1", "1", {"level1": True, "level2": True}, True)
    assert norm["highest_completed_level"] == 2
    assert norm["next_level"] == 3
    assert norm["next_action_owner"] == "company"


# Scenario C: Level 2 to Level 3 Guidance
def test_scenario_c_level2_to_level3_guidance():
    intents = parse_user_data_intents("How can I reach Level 3?")
    assert UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS in intents or UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS in intents

    norm = normalize_employment_verification(None, "1", {"level1": True, "level2": True}, True)
    assert norm["highest_completed_level"] == 2
    assert norm["next_level"] == 3
    assert norm["next_action_owner"] == "company"
    assert "employer needs to confirm" in norm["next_action"].lower() or "confirm" in norm["next_action"].lower()


# Scenario D: Level 3 to Level 4 Guidance
def test_scenario_d_level3_to_level4_guidance():
    intents = parse_user_data_intents("How can I reach Level 4?")
    assert UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS in intents or UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS in intents

    norm = normalize_employment_verification("1", "1", {"level1": True, "level2": True, "level3": True}, True)
    assert norm["highest_completed_level"] == 3
    assert norm["next_level"] == 4
    assert norm["next_action_owner"] == "employee"
    assert "salary evidence" in norm["next_action"].lower() or "salary" in norm["next_action"].lower()


# Scenario E: Level 4 Already Completed
def test_scenario_e_level4_completed():
    norm = normalize_employment_verification("1", "1", {"level1": True, "level2": True, "level3": True, "level4": True}, True)
    assert norm["highest_completed_level"] == 4
    assert norm["next_level"] is None
    assert norm["next_action_owner"] == "none"
    assert norm["guidance_status"] == "completed"


# Scenario F: Company Verification Incomplete
def test_scenario_f_company_verification_incomplete():
    intents = parse_user_data_intents("Why is my company not verified?")
    assert UserIntent.EMPLOYMENT_VERIFICATION_WHY_INCOMPLETE in intents or UserIntent.EMPLOYMENT_VERIFICATION in intents

    norm = normalize_employment_verification("1", "1", {"level1": True, "level2": False}, False)
    assert norm["highest_completed_level"] == 1
    assert norm["next_level"] == 2
    assert norm["next_action_owner"] == "company"
    assert norm["guidance_status"] == "waiting_on_company"


# Scenario G: Employment Approval Pending
def test_scenario_g_employment_approval_pending():
    intents = parse_user_data_intents("Why is Level 3 pending?")
    assert UserIntent.EMPLOYMENT_VERIFICATION_WHY_INCOMPLETE in intents or UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS in intents

    norm = normalize_employment_verification(None, "1", {"level1": True, "level2": True}, True)
    assert norm["highest_completed_level"] == 2
    assert norm["next_level"] == 3
    assert norm["guidance_status"] == "waiting_on_company"


# Scenario H: Employment Declined
def test_scenario_h_employment_declined():
    norm = normalize_employment_verification("0", "2", {"level1": True, "level2": True}, True)
    assert norm["highest_completed_level"] == 2
    assert norm["next_level"] == 3
    assert norm["guidance_status"] == "declined"
    assert "declined" in norm["incomplete_reason"].lower()


# Scenario I: Unknown Verification Data
def test_scenario_i_unknown_verification_data():
    intents = parse_user_data_intents("What is preventing my verification from progressing?")
    assert UserIntent.EMPLOYMENT_VERIFICATION_WHY_INCOMPLETE in intents or UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS in intents

    norm = normalize_employment_verification(None, None, {}, None)
    assert norm["status"] == "unknown"
    assert norm["guidance_status"] == "unknown"


# Scenario J: API Unavailable / Extractor Fallback
def test_scenario_j_api_unavailable():
    extracted = extract_employment_history({"status": 500, "data": None})
    assert extracted == []


# Scenario K: Multiple Employment Records
def test_scenario_k_multiple_employment_records():
    raw_api_data = {
        "data": [
            {
                "company": "Current Corp",
                "is_verified": True,
                "lists": [{
                    "designation": "Senior Engineer",
                    "approved": "1",
                    "status": "1",
                    "still_working": "1",
                    "verificationProcess": {"level1": True, "level2": True, "level3": True}
                }]
            },
            {
                "company": "Previous Inc",
                "is_verified": False,
                "lists": [{
                    "designation": "Junior Developer",
                    "approved": "0",
                    "status": "1",
                    "still_working": "0",
                    "verificationProcess": {"level1": True}
                }]
            }
        ]
    }
    extracted = extract_employment_history(raw_api_data)
    assert len(extracted) == 2

    # Check independent progression levels
    role1_verif = extracted[0]["roles"][0]["verification"]
    role2_verif = extracted[1]["roles"][0]["verification"]

    assert role1_verif["highest_completed_level"] == 3
    assert role2_verif["highest_completed_level"] == 1


# Scenario L: Current vs Previous Company Matching
def test_scenario_l_current_vs_previous_company_matching():
    raw_api_data = {
        "data": [
            {
                "company": "Current Corp",
                "is_verified": True,
                "lists": [{
                    "designation": "Senior Engineer",
                    "approved": "1",
                    "status": "1",
                    "still_working": "1",
                    "verificationProcess": {"level1": True, "level2": True}
                }]
            },
            {
                "company": "Previous Inc",
                "is_verified": True,
                "lists": [{
                    "designation": "Developer",
                    "approved": "1",
                    "status": "1",
                    "still_working": "0",
                    "verificationProcess": {"level1": True, "level2": True, "level3": True}
                }]
            }
        ]
    }
    extracted = extract_employment_history(raw_api_data)

    res_prev = format_single_intent(
        UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS,
        {"employment_history": extracted},
        {},
        query="What is my next step for my previous company?"
    )
    assert "Previous Inc" in res_prev

    res_curr = format_single_intent(
        UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS,
        {"employment_history": extracted},
        {},
        query="What is my next step at Current Corp?"
    )
    assert "Current Corp" in res_curr


# Scenario M: Ambiguous Employment Selection
def test_scenario_m_ambiguous_employment_selection():
    raw_api_data = {
        "data": [
            {
                "company": "Alpha LLC",
                "is_verified": True,
                "lists": [{"designation": "Lead", "approved": "1", "status": "1", "verificationProcess": {"level1": True, "level2": True}}]
            },
            {
                "company": "Beta LLC",
                "is_verified": True,
                "lists": [{"designation": "Architect", "approved": "1", "status": "1", "verificationProcess": {"level1": True, "level2": True}}]
            }
        ]
    }
    extracted = extract_employment_history(raw_api_data)
    res = format_single_intent(
        UserIntent.EMPLOYMENT_VERIFICATION,
        {"employment_history": extracted},
        {},
        query="What is my verification status?"
    )
    assert "Alpha LLC" in res
    assert "Beta LLC" in res


# Scenario N: Follow-up Questions
def test_scenario_n_follow_up_questions():
    intents = parse_user_data_intents("Do I need to do anything?")
    assert UserIntent.EMPLOYMENT_VERIFICATION_OWNERSHIP in intents or UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS in intents


# Scenario O: Company Switching
def test_scenario_o_company_switching():
    intents1 = parse_user_data_intents("What about my previous company?")
    assert UserIntent.EMPLOYMENT_VERIFICATION in intents1 or UserIntent.EMPLOYMENT_HISTORY in intents1


# Scenario P: Employee Isolation
def test_scenario_p_employee_isolation():
    # Verify that candidate/people search queries do NOT trigger employment verification intents
    intents = parse_user_data_intents("Find Python developers with 5 years experience")
    assert UserIntent.EMPLOYMENT_VERIFICATION not in intents
    assert UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS not in intents


# Scenario Q: Existing Employee FAQ Regression
def test_scenario_q_existing_faq_regression():
    intents = parse_user_data_intents("What is CollarCheck and how does it work?")
    assert UserIntent.EMPLOYMENT_VERIFICATION not in intents


# Scenario R: Employer-Side PEOPLE_SEARCH Regression
def test_scenario_r_employer_search_regression():
    # Verify candidate/employer search queries do NOT trigger employee verification intents
    intents = parse_user_data_intents("Find Python developers with 5 years experience in Bangalore")
    assert UserIntent.EMPLOYMENT_VERIFICATION not in intents
    assert UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS not in intents
    assert UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS not in intents
    assert UserIntent.EMPLOYMENT_VERIFICATION_WHY_INCOMPLETE not in intents
    assert UserIntent.EMPLOYMENT_VERIFICATION_OWNERSHIP not in intents
