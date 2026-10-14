"""
test_phase2_2_intent_corrections.py — Test suite for Phase 2.2 intent recognition corrections.

Covers scenarios A through T:
A. Personal Level 1 status.
B. Personal Level 2 status.
C. Personal Level 3 status.
D. Personal Level 4 status.
E. General Level 1 definition.
F. General Level 2 definition.
G. General Level 3 definition.
H. General Level 4 definition.
I. How does salary verification work?
J. Show my verification progress.
K. Show my verification status.
L. What should I do next?
M. Why is my Level 3 pending?
N. Who needs to verify my employment?
O. General employment history questions.
P. Unrelated FAQ questions.
Q. Candidate search questions.
R. Current salary personal-data questions.
S. Company verification questions.
T. Mixed personal and general verification phrasing.
"""

import pytest
from backend.services.user_data_intent_parser import parse_user_data_intents, UserIntent
from backend.services.user_data_service import resolve_required_endpoints
from backend.services.user_data_formatter import format_single_intent
from backend.services.user_data_extractor import normalize_employment_verification, extract_employment_history


def test_scenario_a_personal_level1_status():
    intents = parse_user_data_intents("What is my Level 1 status?")
    assert UserIntent.EMPLOYMENT_VERIFICATION in intents
    assert UserIntent.VERIFICATION_LEVEL_DEFINITION not in intents


def test_scenario_b_personal_level2_status():
    intents = parse_user_data_intents("Tell me my Level 2 verification status.")
    assert UserIntent.EMPLOYMENT_VERIFICATION in intents
    assert UserIntent.VERIFICATION_LEVEL_DEFINITION not in intents


def test_scenario_c_personal_level3_status():
    intents = parse_user_data_intents("Is my Level 3 completed?")
    assert UserIntent.EMPLOYMENT_VERIFICATION in intents
    assert UserIntent.VERIFICATION_LEVEL_DEFINITION not in intents


def test_scenario_d_personal_level4_status():
    intents = parse_user_data_intents("What is my Level 4 status?")
    assert UserIntent.EMPLOYMENT_VERIFICATION in intents
    assert UserIntent.VERIFICATION_LEVEL_DEFINITION not in intents


def test_scenario_e_general_level1_definition():
    intents = parse_user_data_intents("Explain Level 1 verification.")
    assert UserIntent.VERIFICATION_LEVEL_DEFINITION in intents
    assert resolve_required_endpoints(intents) == []  # No personal API call


def test_scenario_f_general_level2_definition():
    intents = parse_user_data_intents("What is Level 2 verification?")
    assert UserIntent.VERIFICATION_LEVEL_DEFINITION in intents
    assert resolve_required_endpoints(intents) == []  # No personal API call


def test_scenario_g_general_level3_definition():
    intents = parse_user_data_intents("What is the purpose of Level 3?")
    assert UserIntent.VERIFICATION_LEVEL_DEFINITION in intents
    assert resolve_required_endpoints(intents) == []  # No personal API call


def test_scenario_h_general_level4_definition():
    intents = parse_user_data_intents("What is Level 4 verification?")
    assert UserIntent.VERIFICATION_LEVEL_DEFINITION in intents
    assert resolve_required_endpoints(intents) == []  # No personal API call


def test_scenario_i_how_does_salary_verification_work():
    intents = parse_user_data_intents("How does salary verification work?")
    assert UserIntent.VERIFICATION_LEVEL_DEFINITION in intents
    assert resolve_required_endpoints(intents) == []  # No personal API call


def test_scenario_j_show_my_verification_progress():
    intents = parse_user_data_intents("Show my verification progress.")
    assert UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS in intents


def test_scenario_k_show_my_verification_status():
    intents = parse_user_data_intents("Show my verification status.")
    assert UserIntent.EMPLOYMENT_VERIFICATION in intents


def test_scenario_l_what_should_i_do_next():
    intents = parse_user_data_intents("What should I do next?")
    assert UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS in intents


def test_scenario_m_why_is_level3_pending():
    intents = parse_user_data_intents("Why is my Level 3 pending?")
    assert UserIntent.EMPLOYMENT_VERIFICATION_WHY_INCOMPLETE in intents


def test_scenario_n_who_needs_to_verify_my_employment():
    intents = parse_user_data_intents("Who needs to verify my employment?")
    assert UserIntent.EMPLOYMENT_VERIFICATION_OWNERSHIP in intents


def test_scenario_o_general_employment_history():
    intents = parse_user_data_intents("Show my employment history.")
    assert UserIntent.EMPLOYMENT_HISTORY in intents
    assert UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS not in intents


def test_scenario_p_unrelated_faq():
    intents = parse_user_data_intents("What is CollarCheck and how does it work?")
    assert intents == []  # FAQ intent handled by general chatbot, not self-data service


def test_scenario_q_candidate_search():
    intents = parse_user_data_intents("Find candidates with 5 years Java experience")
    assert intents == []  # Handled by PEOPLE_SEARCH, not self-data service


def test_scenario_r_current_salary_personal_data():
    intents = parse_user_data_intents("What is my current salary?")
    assert UserIntent.EMPLOYMENT_SALARY in intents


def test_scenario_s_company_verification_status():
    intents = parse_user_data_intents("Is my company verified?")
    assert UserIntent.EMPLOYMENT_VERIFICATION in intents


def test_scenario_t_mixed_phrasing_end_to_end_routing():
    # End-to-end routing check for definition query vs personal progress query
    def_intents = parse_user_data_intents("Explain company domain verification.")
    assert UserIntent.VERIFICATION_LEVEL_DEFINITION in def_intents
    def_resp = format_single_intent(def_intents[0], {}, {}, query="Explain company domain verification.")
    assert "Level 2 — Company Domain Verification" in def_resp

    mock_history = extract_employment_history({
        "data": [{
            "company": "CollarCheck",
            "is_verified": True,
            "lists": [{"designation": "Developer", "approved": "1", "status": "1", "verificationProcess": {"level1": True, "level2": True}}]
        }]
    })

    prog_intents = parse_user_data_intents("Show my verification progress.")
    assert UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS in prog_intents
    prog_resp = format_single_intent(prog_intents[0], {"employment_history": mock_history}, {}, query="Show my verification progress.")
    assert "Developer" in prog_resp and "CollarCheck" in prog_resp
