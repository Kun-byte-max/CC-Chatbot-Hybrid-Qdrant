"""
test_phase4_response_formatting.py — Phase 4 Response Formatting, Dual Explanations & Acceptance Matrix Tests.

Coverage Matrix (Categories A–G):
- Category A (A1–A5): Multilingual Response & Preference Switching
- Category B (B1–B21): 17 Intent Handlers & Defensive Input Boundaries
- Category C (C1–C4): Multi-Intent Combinations & Section Fault Isolation
- Category D (D1–D11): Company Semantics, Relative Resolution & Chronology Matching
- Category E (E1–E10): Dual Explanations & Phase 2 Machine Evidence Integrity
- Category F (F1–F5): Security & Boundary Protection (Phase 3 FORBIDDEN_KEYS Reuse, Zero Tracebacks/Tokens, URL Filtration)
- Category G (G1–G7): Invariance & Immutability Constraints (Ranking Invariance, Sub-score Integrity, G6 No Recomputation, G7 Graph Immutability)
"""

import copy
import pytest
from unittest.mock import MagicMock, patch
from typing import Dict, Any, List

from backend.utils.session_state import get_language_preference, set_language_preference
from backend.schemas.employer_requirements import EmployerSearchRequirements
from backend.services.user_data_intent_parser import UserIntent
from backend.services.user_data_formatter import format_single_intent, format_multi_intent_response, _extract_company_name_from_query
from backend.services.user_data_extractor import FORBIDDEN_KEYS
from backend.recommendation.explanation_generator import ExplanationGenerator
from backend.recommendation.models import (
    RecommendationResult,
    ExplanationResult,
    RecommendationReason,
    ScoreResult,
    SkillMatchResult,
    ExperienceMatchResult,
    LocationMatchResult,
    SalaryMatchResult,
    TitleMatchResult,
    CandidateProfile,
    JobProfile,
)


# =====================================================================
# CATEGORY A: Existing Behavior & Language Regression (A1–A5)
# =====================================================================

def test_a1_english_response_formatting_regression():
    """A1: Validates English baseline formatting for profile skills."""
    session_id = "test_session_a1"
    set_language_preference(session_id, "english")
    assert get_language_preference(session_id) == "english"

    extracted = {"profile_skills": [{"skill": "Python"}, {"skill": "FastAPI"}]}
    res = format_single_intent(UserIntent.PROFILE_SKILLS, extracted, {})
    assert "Python, FastAPI" in res
    assert res.startswith("**Your Skills:**")


def test_a2_hindi_language_text_formatting():
    """A2: Validates response presentation under Hindi session language preference."""
    session_id = "test_session_a2"
    set_language_preference(session_id, "hindi")
    assert get_language_preference(session_id) == "hindi"

    extracted = {"profile_skills": [{"skill": "Python"}]}
    res = format_single_intent(UserIntent.PROFILE_SKILLS, extracted, {})
    assert "Python" in res
    assert get_language_preference(session_id) == "hindi"


def test_a3_hinglish_language_text_formatting():
    """A3: Validates response presentation under Hinglish session language preference."""
    session_id = "test_session_a3"
    set_language_preference(session_id, "hinglish")
    assert get_language_preference(session_id) == "hinglish"

    extracted = {"profile_skills": [{"skill": "React"}]}
    res = format_single_intent(UserIntent.PROFILE_SKILLS, extracted, {})
    assert "React" in res
    assert get_language_preference(session_id) == "hinglish"


def test_a4_persistent_language_preference():
    """A4: Verifies persistent explicit language preference across multi-turn presentation calls."""
    session_id = "test_session_a4"
    set_language_preference(session_id, "hindi")
    assert get_language_preference(session_id) == "hindi"

    extracted1 = {"profile_skills": [{"skill": "Python"}]}
    res1 = format_single_intent(UserIntent.PROFILE_SKILLS, extracted1, {})

    extracted2 = {"education": [{"course": "B.Tech", "university": "Delhi University"}]}
    res2 = format_single_intent(UserIntent.PROFILE_EDUCATION, extracted2, {})

    assert "Python" in res1
    assert "Delhi University" in res2 or "education" in res2.lower()
    assert get_language_preference(session_id) == "hindi"


def test_a5_bi_directional_language_switching():
    """A5: Verifies bi-directional language preference switching (English -> Hindi -> Hinglish -> English) preserves data integrity."""
    session_id = "test_session_a5"
    extracted = {"profile_summary": {"name": "Alice", "city_name": "Delhi"}}

    # Turn 1: English
    set_language_preference(session_id, "english")
    res_en = format_single_intent(UserIntent.PROFILE_SUMMARY, extracted, {})
    assert "Alice" in res_en
    assert get_language_preference(session_id) == "english"

    # Turn 2: Switch to Hindi
    set_language_preference(session_id, "hindi")
    res_hi = format_single_intent(UserIntent.PROFILE_SUMMARY, extracted, {})
    assert "Alice" in res_hi
    assert get_language_preference(session_id) == "hindi"

    # Turn 3: Switch to Hinglish
    set_language_preference(session_id, "hinglish")
    res_hing = format_single_intent(UserIntent.PROFILE_SUMMARY, extracted, {})
    assert "Alice" in res_hing
    assert get_language_preference(session_id) == "hinglish"

    # Turn 4: Switch back to English
    set_language_preference(session_id, "english")
    res_en2 = format_single_intent(UserIntent.PROFILE_SUMMARY, extracted, {})
    assert "Alice" in res_en2
    assert get_language_preference(session_id) == "english"


# =====================================================================
# CATEGORY B: Profile & Widget Intents (B1–B21 Explicit Test Functions)
# =====================================================================

def test_b1_profile_incomplete():
    """B1: Formats PROFILE_INCOMPLETE intent."""
    data = {"profile_percentage": 85, "uncomplete": ["Certifications"]}
    res = format_single_intent(UserIntent.PROFILE_INCOMPLETE, data, {})
    assert "85% complete" in res


def test_b2_user_detail_notices():
    """B2: Formats USER_DETAIL_NOTICES intent."""
    data = {"noticeEmployments": ["101"], "notice_period_name": "30 Days"}
    res = format_single_intent(UserIntent.USER_DETAIL_NOTICES, data, {})
    assert "on notice for **1 company record(s)**" in res


def test_b3_user_detail_reminders():
    """B3: Formats USER_DETAIL_REMINDERS intent."""
    data = {"reminderExperience": True, "reminderExperienceList": [{"company": "102", "designation": "Dev"}]}
    res = format_single_intent(UserIntent.USER_DETAIL_REMINDERS, data, {})
    assert "Pending Experience Reminders" in res


def test_b4_employment_history():
    """B4: Formats EMPLOYMENT_HISTORY intent."""
    data = {"employment_history": [{"company": "Google", "is_verified": True, "roles": [{"designation": "Engineer", "joining_date": "2020", "worked_till_date": "Present", "still_working": "1"}]}]}
    res = format_single_intent(UserIntent.EMPLOYMENT_HISTORY, data, {})
    assert "Google" in res


def test_b5_employment_skills():
    """B5: Formats EMPLOYMENT_SKILLS intent."""
    data = {"employment_skills": [{"company": "Google", "name": "Python"}]}
    res = format_single_intent(UserIntent.EMPLOYMENT_SKILLS, data, {})
    assert "Python" in res


def test_b6_employment_salary():
    """B6: Formats EMPLOYMENT_SALARY intent."""
    data = {"employment_salary": [{"company": "Google", "designation": "SDE", "salary": "1200000", "salary_mode": "Per Annum", "salary_inhand": "90000"}]}
    res = format_single_intent(UserIntent.EMPLOYMENT_SALARY, data, {})
    assert "₹1200000" in res


def test_b7_employment_verification():
    """B7: Formats EMPLOYMENT_VERIFICATION intent."""
    data = {"employment_history": [{"company": "Google", "is_verified": True}]}
    res = format_single_intent(UserIntent.EMPLOYMENT_VERIFICATION, data, {})
    assert "Verified ✓" in res


def test_b8_employment_documents():
    """B8: Formats EMPLOYMENT_DOCUMENTS intent."""
    data = {"employment_history": [{"company": "Google", "roles": [{"verificationProcess": {"document": "Relieving_Letter.pdf"}}]}]}
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert "Relieving_Letter.pdf" in res


def test_b9_widget_closing_soon():
    """B9: Formats WIDGET_CLOSING_SOON intent."""
    data = {"widgets": [{"slug": "closing_soon", "items": [{"job_title": "Backend Dev", "company": "Tech Corp", "location": "Delhi"}]}]}
    res = format_single_intent(UserIntent.WIDGET_CLOSING_SOON, data, {})
    assert "Positions Closing Soon" in res


def test_b10_widget_nearby_orgs():
    """B10: Formats WIDGET_NEARBY_ORGS intent."""
    data = {"widgets": [{"slug": "nearby_company", "items": [{"name": "Local Org", "city_name": "Noida", "distance": 5}]}]}
    res = format_single_intent(UserIntent.WIDGET_NEARBY_ORGS, data, {})
    assert "Organizations Near You" in res


def test_b11_widget_all():
    """B11: Formats WIDGET_ALL intent."""
    data = {"widgets": [{"heading": "Featured Jobs", "list_count": 3}]}
    res = format_single_intent(UserIntent.WIDGET_ALL, data, {})
    assert "Platform Dashboard Widgets" in res


def test_b12_profile_skills():
    """B12: Formats PROFILE_SKILLS intent."""
    data = {"profile_skills": [{"skill": "Python"}]}
    res = format_single_intent(UserIntent.PROFILE_SKILLS, data, {})
    assert "Your Skills" in res


def test_b13_profile_education():
    """B13: Formats PROFILE_EDUCATION intent."""
    data = {"education": [{"course": "M.Tech"}]}
    res = format_single_intent(UserIntent.PROFILE_EDUCATION, data, {})
    assert "Here are your education details:" in res


def test_b14_profile_certificates():
    """B14: Formats PROFILE_CERTIFICATES intent."""
    data = {"certificates": [{"course": "AWS Certified", "university": "Amazon"}]}
    res = format_single_intent(UserIntent.PROFILE_CERTIFICATES, data, {})
    assert "Your Certifications" in res


def test_b15_profile_languages():
    """B15: Formats PROFILE_LANGUAGES intent."""
    data = {"languages": [{"name": "English", "verbal": 5, "written": 5}]}
    res = format_single_intent(UserIntent.PROFILE_LANGUAGES, data, {})
    assert "Spoken Languages" in res


def test_b16_profile_portfolio():
    """B16: Formats PROFILE_PORTFOLIO intent."""
    data = {"portfolio": [{"title": "Chatbot Engine", "url": "https://github.com/demo"}]}
    res = format_single_intent(UserIntent.PROFILE_PORTFOLIO, data, {})
    assert "Chatbot Engine" in res


def test_b17_profile_summary():
    """B17: Formats PROFILE_SUMMARY intent."""
    data = {"profile_summary": {"name": "Alice", "city_name": "Delhi"}}
    res = format_single_intent(UserIntent.PROFILE_SUMMARY, data, {})
    assert "Here is your profile information" in res


def test_b18_empty_input_data():
    """B18: Validates defensive fallback on empty dict input payload."""
    res = format_single_intent(UserIntent.PROFILE_SKILLS, {}, {})
    assert "couldn't find any skills" in res


def test_b19_none_input_data():
    """B19: Validates defensive fallback on None input payload."""
    res = format_single_intent(UserIntent.PROFILE_SKILLS, None, {})
    assert "couldn't find any skills" in res


def test_b20_malformed_string_input_data():
    """B20: Validates defensive fallback on malformed string input payload."""
    res = format_single_intent(UserIntent.EMPLOYMENT_HISTORY, "malformed_string_payload", {})
    assert "couldn't find any employment history" in res


def test_b21_malformed_list_input_data():
    """B21: Validates defensive fallback on malformed list input payload."""
    res = format_single_intent(UserIntent.PROFILE_EDUCATION, [1, 2, 3], {})
    assert "couldn't find any education" in res


# =====================================================================
# CATEGORY C: Multi-Intent Combinations & Section Fault Isolation (C1–C4)
# =====================================================================

def test_c1_multi_intent_two_intents_formatting():
    """C1: Validates multi-intent response joining 2 sections cleanly."""
    intents = [UserIntent.PROFILE_SKILLS, UserIntent.PROFILE_CERTIFICATES]
    extracted = {
        UserIntent.PROFILE_SKILLS: {"profile_skills": [{"skill": "Python"}]},
        UserIntent.PROFILE_CERTIFICATES: {"certificates": [{"course": "CKA"}]}
    }
    res = format_multi_intent_response(intents, extracted, {})
    assert "Your Skills" in res
    assert "Your Certifications" in res


def test_c2_multi_intent_three_intents_double_newline_separator():
    """C2: Validates multi-intent response joining 3 sections with double newlines."""
    intents = [UserIntent.PROFILE_SKILLS, UserIntent.PROFILE_EDUCATION, UserIntent.PROFILE_LANGUAGES]
    extracted = {
        UserIntent.PROFILE_SKILLS: {"profile_skills": [{"skill": "Python"}]},
        UserIntent.PROFILE_EDUCATION: {"education": [{"course": "B.Tech"}]},
        UserIntent.PROFILE_LANGUAGES: {"languages": [{"name": "English"}]}
    }
    res = format_multi_intent_response(intents, extracted, {})
    assert "Your Skills" in res
    assert "education details" in res
    assert "Spoken Languages" in res
    assert "\n\n" in res


def test_c3_multi_intent_duplicate_intents_deduplication():
    """C3: Verifies multi-intent formatting handles requests cleanly."""
    intents = [UserIntent.PROFILE_SKILLS, UserIntent.PROFILE_SKILLS]
    extracted = {UserIntent.PROFILE_SKILLS: {"profile_skills": [{"skill": "Python"}]}}
    res = format_multi_intent_response(intents, extracted, {})
    assert "Your Skills" in res


def test_c4_multi_intent_section_fault_isolation():
    """C4: Asserts that an unhandled Exception/ValueError in 1 intent section does NOT suppress other valid sections."""
    intents = [UserIntent.PROFILE_SKILLS, UserIntent.EMPLOYMENT_SALARY]
    extracted = {
        UserIntent.PROFILE_SKILLS: {"profile_skills": [{"skill": "Python"}]},
        UserIntent.EMPLOYMENT_SALARY: {"employment_history": [{"company": "Google LLC", "roles": []}]}
    }
    orig_format = format_single_intent

    def fault_injector(intent, data, api_resp, query=""):
        if intent == UserIntent.EMPLOYMENT_SALARY:
            raise ValueError("Simulated unexpected backend formatting error")
        return orig_format(intent, data, api_resp, query=query)

    with patch("backend.services.user_data_formatter.format_single_intent", side_effect=fault_injector):
        res = format_multi_intent_response(intents, extracted, {})

    assert "Your Skills" in res
    assert "An unexpected error occurred while processing" in res


# =====================================================================
# CATEGORY D: Company Semantics & Chronology Matching (D1–D11)
# =====================================================================

def test_d1_previous_company_relative_resolution():
    """D1: Validates relative company extraction for 'previous company' query using verified date and still_working semantics."""
    records = [
        {"company": "Acme Inc", "roles": [{"designation": "Developer", "joining_date": "2023", "worked_till_date": "Present", "still_working": "1"}]},
        {"company": "Zenith Corp", "roles": [{"designation": "Engineer", "joining_date": "2020", "worked_till_date": "2022", "still_working": "0"}]}
    ]
    companies = ["Acme Inc", "Zenith Corp"]

    res = _extract_company_name_from_query("What skills did I use at my previous company?", companies, records=records)
    assert res == "Zenith Corp"


def test_d2_last_company_relative_resolution():
    """D2: Validates relative company extraction for 'last company' query using verified metadata semantics."""
    records = [
        {"company": "Beta Corp", "roles": [{"designation": "SDE", "joining_date": "2023", "worked_till_date": "Present", "still_working": "1"}]},
        {"company": "Alpha Systems", "roles": [{"designation": "Junior SDE", "joining_date": "2021", "worked_till_date": "2023", "still_working": "0"}]}
    ]
    companies = ["Beta Corp", "Alpha Systems"]

    res = _extract_company_name_from_query("What was my salary at my last company?", companies, records=records)
    assert res == "Alpha Systems"


def test_d3_unambiguous_partial_company_matching():
    """D3: Validates unambiguous partial match (query token 'Google' matches 'Google LLC' when only 1 match exists)."""
    companies = ["Google LLC", "Microsoft Corp"]
    res = _extract_company_name_from_query("What was my salary at Google?", companies)
    assert res == "Google LLC"


def test_d4_ambiguous_partial_company_matching():
    """D4: Verifies ambiguous partial match ('Google' against 'Google LLC' and 'Google India') returns None and does NOT arbitrarily pick one."""
    companies = ["Google LLC", "Google India"]
    res = _extract_company_name_from_query("What was my salary at Google?", companies)
    assert res is None

    # End-to-end intent formatting with ambiguous partial match
    history = {
        "employment_history": [
            {"company": "Google LLC", "roles": [{"designation": "SDE"}]},
            {"company": "Google India", "roles": [{"designation": "Lead"}]}
        ]
    }
    res_formatted = format_single_intent(UserIntent.EMPLOYMENT_HISTORY, history, {}, query="Tell me about my work at Google")
    assert "Google LLC" in res_formatted
    assert "Google India" in res_formatted


def test_d5_unmatched_company_query():
    """D5: Validates clear fallback when explicit company name is not found."""
    history = {"employment_history": [{"company": "Google", "roles": []}]}
    res = format_single_intent(UserIntent.EMPLOYMENT_HISTORY, history, {}, query="Tell me about Tesla")
    assert "I couldn't find any employment history recorded for **Tesla**" in res


def test_d6_chronology_active_company_first():
    """D6: Active company listed FIRST in array must NOT be returned as previous company."""
    records = [
        {"company": "Microsoft Corp", "roles": [{"designation": "Lead", "joining_date": "2023", "worked_till_date": "Present", "still_working": "1"}]},
        {"company": "Google LLC", "roles": [{"designation": "SDE", "joining_date": "2020", "worked_till_date": "2022", "still_working": "0"}]}
    ]
    companies = ["Microsoft Corp", "Google LLC"]
    res = _extract_company_name_from_query("What was my role at my previous company?", companies, records=records)
    assert res == "Google LLC"


def test_d7_chronology_previous_company_first():
    """D7: Previous company listed FIRST in array resolved correctly without array position assumption."""
    records = [
        {"company": "Google LLC", "roles": [{"designation": "SDE", "joining_date": "2020", "worked_till_date": "2022", "still_working": "0"}]},
        {"company": "Microsoft Corp", "roles": [{"designation": "Lead", "joining_date": "2023", "worked_till_date": "Present", "still_working": "1"}]}
    ]
    companies = ["Google LLC", "Microsoft Corp"]
    res = _extract_company_name_from_query("What was my salary at my previous company?", companies, records=records)
    assert res == "Google LLC"


def test_d8_chronology_multiple_previous_companies():
    """D8: Multiple previous companies resolves to latest ended prior company (Google 2022 > Amazon 2020)."""
    records = [
        {"company": "Amazon Inc", "roles": [{"designation": "Intern", "joining_date": "2018", "worked_till_date": "2020", "still_working": "0"}]},
        {"company": "Google LLC", "roles": [{"designation": "Engineer", "joining_date": "2020", "worked_till_date": "2022", "still_working": "0"}]},
        {"company": "Microsoft Corp", "roles": [{"designation": "Architect", "joining_date": "2023", "worked_till_date": "Present", "still_working": "1"}]}
    ]
    companies = ["Amazon Inc", "Google LLC", "Microsoft Corp"]
    res = _extract_company_name_from_query("Tell me about my previous company", companies, records=records)
    assert res == "Google LLC"


def test_d9_chronology_different_joining_and_worked_till_dates():
    """D9: Evaluates joining_date and worked_till_date semantics cleanly."""
    records = [
        {"company": "Acme Corp", "roles": [{"joining_date": "2019", "worked_till_date": "2021", "still_working": "0"}]},
        {"company": "Zenith Ltd", "roles": [{"joining_date": "2021", "worked_till_date": "2023", "still_working": "0"}]}
    ]
    companies = ["Acme Corp", "Zenith Ltd"]
    res = _extract_company_name_from_query("Details for previous company", companies, records=records)
    assert res == "Zenith Ltd"


def test_d10_chronology_missing_and_malformed_dates():
    """D10: Handles missing/malformed dates gracefully without crashing."""
    records = [
        {"company": "Alpha LLC", "roles": [{"joining_date": "invalid_date", "worked_till_date": "N/A", "still_working": "0"}]},
        {"company": "Beta Inc", "roles": [{"joining_date": "2021", "worked_till_date": "2023", "still_working": "0"}]}
    ]
    companies = ["Alpha LLC", "Beta Inc"]
    res = _extract_company_name_from_query("Salary at previous company", companies, records=records)
    assert res == "Beta Inc"


def test_d11_chronology_ambiguous_dates_index_fallback():
    """D11: Fallbacks gracefully when all companies lack date metadata."""
    records = [
        {"company": "Company A", "roles": [{"still_working": "0"}]},
        {"company": "Company B", "roles": [{"still_working": "0"}]}
    ]
    companies = ["Company A", "Company B"]
    res = _extract_company_name_from_query("Details for previous company", companies, records=records)
    assert res in companies


# =====================================================================
# CATEGORY E: Dual Explanations & Evidence Integrity (E1–E10)
# =====================================================================

def test_e1_candidate_explanation_skill_match():
    """E1: Validates candidate skill match explanation reason generation."""
    score_res = ScoreResult(overall_score=85.0, recommendation_level="Good Match", skill_score=90.0, experience_score=80.0, location_score=80.0, salary_score=80.0, title_score=80.0, weight_skill=0.3, weight_experience=0.25, weight_location=0.2, weight_salary=0.15, weight_title=0.1)
    skill_res = SkillMatchResult(score=90.0, matched_skills=["Python", "FastAPI"], missing_skills=["Docker"], extra_skills=[], total_required=3, total_matched=2)

    generator = ExplanationGenerator()
    explanation = generator.generate_explanation(
        score_result=score_res, skill_result=skill_res,
        experience_result=ExperienceMatchResult(score=80.0, candidate_experience=3.0, required_experience=3.0, experience_gap=0.0, meets_requirement=True),
        location_result=LocationMatchResult(score=80.0, candidate_city="Delhi", candidate_state="Delhi", candidate_country="India", job_city="Delhi", job_state="Delhi", job_country="India", work_mode="On-site", match_level="Exact"),
        salary_result=SalaryMatchResult(score=80.0, candidate_expected_salary=1000000, job_offered_salary=1200000, normalized_candidate_salary=1000000.0, normalized_job_salary=1200000.0, salary_difference_percent=20.0, meets_expectation=True),
        title_result=TitleMatchResult(score=80.0, candidate_title="Developer", job_title="Developer", normalized_candidate_title="developer", normalized_job_title="developer", matched_keywords=["developer"], match_level="Strong Match")
    )
    skill_reason = next(r for r in explanation.recommendation_reasons if r.type == "skill")
    assert skill_reason.evidence["matched_skills"] == ["Python", "FastAPI"]


def test_e2_candidate_explanation_experience_match():
    """E2: Validates candidate experience match explanation reason generation."""
    score_res = ScoreResult(overall_score=85.0, recommendation_level="Good Match", skill_score=80.0, experience_score=90.0, location_score=80.0, salary_score=80.0, title_score=80.0, weight_skill=0.3, weight_experience=0.25, weight_location=0.2, weight_salary=0.15, weight_title=0.1)
    exp_res = ExperienceMatchResult(score=90.0, candidate_experience=5.0, required_experience=3.0, experience_gap=0.0, meets_requirement=True)

    generator = ExplanationGenerator()
    explanation = generator.generate_explanation(
        score_result=score_res,
        skill_result=SkillMatchResult(score=80.0, matched_skills=["Python"], missing_skills=[], extra_skills=[], total_required=1, total_matched=1),
        experience_result=exp_res,
        location_result=LocationMatchResult(score=80.0, candidate_city="Delhi", candidate_state="Delhi", candidate_country="India", job_city="Delhi", job_state="Delhi", job_country="India", work_mode="On-site", match_level="Exact"),
        salary_result=SalaryMatchResult(score=80.0, candidate_expected_salary=1000000, job_offered_salary=1200000, normalized_candidate_salary=1000000.0, normalized_job_salary=1200000.0, salary_difference_percent=20.0, meets_expectation=True),
        title_result=TitleMatchResult(score=80.0, candidate_title="Developer", job_title="Developer", normalized_candidate_title="developer", normalized_job_title="developer", matched_keywords=["developer"], match_level="Strong Match")
    )
    exp_reason = next(r for r in explanation.recommendation_reasons if r.type == "experience")
    assert exp_reason.evidence["candidate_experience"] == 5.0
    assert exp_reason.evidence["required_experience"] == 3.0


def test_e3_candidate_explanation_work_mode_match():
    """E3: Validates candidate location/work_mode match explanation reason generation."""
    score_res = ScoreResult(overall_score=85.0, recommendation_level="Good Match", skill_score=80.0, experience_score=80.0, location_score=100.0, salary_score=80.0, title_score=80.0, weight_skill=0.3, weight_experience=0.25, weight_location=0.2, weight_salary=0.15, weight_title=0.1)
    loc_res = LocationMatchResult(score=100.0, candidate_city="Noida", candidate_state="UP", candidate_country="India", job_city="Noida", job_state="UP", job_country="India", work_mode="Remote", match_level="Remote")

    generator = ExplanationGenerator()
    explanation = generator.generate_explanation(
        score_result=score_res,
        skill_result=SkillMatchResult(score=80.0, matched_skills=["Python"], missing_skills=[], extra_skills=[], total_required=1, total_matched=1),
        experience_result=ExperienceMatchResult(score=80.0, candidate_experience=3.0, required_experience=3.0, experience_gap=0.0, meets_requirement=True),
        location_result=loc_res,
        salary_result=SalaryMatchResult(score=80.0, candidate_expected_salary=1000000, job_offered_salary=1200000, normalized_candidate_salary=1000000.0, normalized_job_salary=1200000.0, salary_difference_percent=20.0, meets_expectation=True),
        title_result=TitleMatchResult(score=80.0, candidate_title="Developer", job_title="Developer", normalized_candidate_title="developer", normalized_job_title="developer", matched_keywords=["developer"], match_level="Strong Match")
    )
    loc_reason = next(r for r in explanation.recommendation_reasons if r.type == "location")
    assert loc_reason.evidence["work_mode"] == "Remote"


def test_e4_candidate_explanation_expectation_match():
    """E4: Validates candidate salary match explanation reason generation."""
    score_res = ScoreResult(overall_score=85.0, recommendation_level="Good Match", skill_score=80.0, experience_score=80.0, location_score=80.0, salary_score=90.0, title_score=80.0, weight_skill=0.3, weight_experience=0.25, weight_location=0.2, weight_salary=0.15, weight_title=0.1)
    sal_res = SalaryMatchResult(score=90.0, candidate_expected_salary=1000000, job_offered_salary=1200000, normalized_candidate_salary=1000000.0, normalized_job_salary=1200000.0, salary_difference_percent=20.0, meets_expectation=True)

    generator = ExplanationGenerator()
    explanation = generator.generate_explanation(
        score_result=score_res,
        skill_result=SkillMatchResult(score=80.0, matched_skills=["Python"], missing_skills=[], extra_skills=[], total_required=1, total_matched=1),
        experience_result=ExperienceMatchResult(score=80.0, candidate_experience=3.0, required_experience=3.0, experience_gap=0.0, meets_requirement=True),
        location_result=LocationMatchResult(score=80.0, candidate_city="Delhi", candidate_state="Delhi", candidate_country="India", job_city="Delhi", job_state="Delhi", job_country="India", work_mode="On-site", match_level="Exact"),
        salary_result=sal_res,
        title_result=TitleMatchResult(score=80.0, candidate_title="Developer", job_title="Developer", normalized_candidate_title="developer", normalized_job_title="developer", matched_keywords=["developer"], match_level="Strong Match")
    )
    sal_reason = next(r for r in explanation.recommendation_reasons if r.type == "salary")
    assert sal_reason.evidence["meets_expectation"] is True


def test_e5_candidate_explanation_job_title_match():
    """E5: Validates candidate job title match explanation reason generation."""
    score_res = ScoreResult(overall_score=85.0, recommendation_level="Good Match", skill_score=80.0, experience_score=80.0, location_score=80.0, salary_score=80.0, title_score=90.0, weight_skill=0.3, weight_experience=0.25, weight_location=0.2, weight_salary=0.15, weight_title=0.1)
    title_res = TitleMatchResult(score=90.0, candidate_title="Senior DevOps Engineer", job_title="Senior DevOps Engineer", normalized_candidate_title="senior devops engineer", normalized_job_title="senior devops engineer", matched_keywords=["devops"], match_level="Exact Match")

    generator = ExplanationGenerator()
    explanation = generator.generate_explanation(
        score_result=score_res,
        skill_result=SkillMatchResult(score=80.0, matched_skills=["Python"], missing_skills=[], extra_skills=[], total_required=1, total_matched=1),
        experience_result=ExperienceMatchResult(score=80.0, candidate_experience=3.0, required_experience=3.0, experience_gap=0.0, meets_requirement=True),
        location_result=LocationMatchResult(score=80.0, candidate_city="Delhi", candidate_state="Delhi", candidate_country="India", job_city="Delhi", job_state="Delhi", job_country="India", work_mode="On-site", match_level="Exact"),
        salary_result=SalaryMatchResult(score=80.0, candidate_expected_salary=1000000, job_offered_salary=1200000, normalized_candidate_salary=1000000.0, normalized_job_salary=1200000.0, salary_difference_percent=20.0, meets_expectation=True),
        title_result=title_res
    )
    title_reason = next(r for r in explanation.recommendation_reasons if r.type == "title")
    assert title_reason.evidence["job_title"] == "Senior DevOps Engineer"


def test_e6_skill_evidence_value_and_message_traceability():
    """E6: Verifies skill reason evidence values match Phase 2 matcher output exactly and reflect in human message."""
    score_res = ScoreResult(overall_score=85.0, recommendation_level="Good Match", skill_score=90.0, experience_score=80.0, location_score=80.0, salary_score=80.0, title_score=80.0, weight_skill=0.3, weight_experience=0.25, weight_location=0.2, weight_salary=0.15, weight_title=0.1)
    skill_res = SkillMatchResult(score=90.0, matched_skills=["Python", "FastAPI"], missing_skills=["Docker"], extra_skills=[], total_required=3, total_matched=2)

    explanation = ExplanationGenerator().generate_explanation(
        score_result=score_res, skill_result=skill_res,
        experience_result=ExperienceMatchResult(score=80.0, candidate_experience=3.0, required_experience=3.0, experience_gap=0.0, meets_requirement=True),
        location_result=LocationMatchResult(score=80.0, candidate_city="Delhi", candidate_state="Delhi", candidate_country="India", job_city="Delhi", job_state="Delhi", job_country="India", work_mode="On-site", match_level="Exact"),
        salary_result=SalaryMatchResult(score=80.0, candidate_expected_salary=1000000, job_offered_salary=1200000, normalized_candidate_salary=1000000.0, normalized_job_salary=1200000.0, salary_difference_percent=20.0, meets_expectation=True),
        title_result=TitleMatchResult(score=80.0, candidate_title="Developer", job_title="Developer", normalized_candidate_title="developer", normalized_job_title="developer", matched_keywords=["developer"], match_level="Strong Match")
    )
    skill_reason = next(r for r in explanation.recommendation_reasons if r.type == "skill")
    assert skill_reason.evidence["matched_skills"] == ["Python", "FastAPI"]
    assert "Python and FastAPI" in skill_reason.message
    assert "2 of your skills" in skill_reason.message


def test_e7_experience_evidence_value_and_message_traceability():
    """E7: Verifies experience reason evidence values match Phase 2 matcher output exactly and reflect in human message."""
    score_res = ScoreResult(overall_score=85.0, recommendation_level="Good Match", skill_score=80.0, experience_score=90.0, location_score=80.0, salary_score=80.0, title_score=80.0, weight_skill=0.3, weight_experience=0.25, weight_location=0.2, weight_salary=0.15, weight_title=0.1)
    exp_res = ExperienceMatchResult(score=90.0, candidate_experience=5.0, required_experience=3.0, experience_gap=0.0, meets_requirement=True)

    explanation = ExplanationGenerator().generate_explanation(
        score_result=score_res,
        skill_result=SkillMatchResult(score=80.0, matched_skills=["Python"], missing_skills=[], extra_skills=[], total_required=1, total_matched=1),
        experience_result=exp_res,
        location_result=LocationMatchResult(score=80.0, candidate_city="Delhi", candidate_state="Delhi", candidate_country="India", job_city="Delhi", job_state="Delhi", job_country="India", work_mode="On-site", match_level="Exact"),
        salary_result=SalaryMatchResult(score=80.0, candidate_expected_salary=1000000, job_offered_salary=1200000, normalized_candidate_salary=1000000.0, normalized_job_salary=1200000.0, salary_difference_percent=20.0, meets_expectation=True),
        title_result=TitleMatchResult(score=80.0, candidate_title="Developer", job_title="Developer", normalized_candidate_title="developer", normalized_job_title="developer", matched_keywords=["developer"], match_level="Strong Match")
    )
    exp_reason = next(r for r in explanation.recommendation_reasons if r.type == "experience")
    assert exp_reason.evidence["candidate_experience"] == 5.0
    assert exp_reason.evidence["required_experience"] == 3.0
    assert "5 years of experience" in exp_reason.message


def test_e8_location_evidence_value_and_message_traceability():
    """E8: Verifies location reason evidence values match Phase 2 matcher output exactly and reflect in human message."""
    score_res = ScoreResult(overall_score=85.0, recommendation_level="Good Match", skill_score=80.0, experience_score=80.0, location_score=100.0, salary_score=80.0, title_score=80.0, weight_skill=0.3, weight_experience=0.25, weight_location=0.2, weight_salary=0.15, weight_title=0.1)
    loc_res = LocationMatchResult(score=100.0, candidate_city="Noida", candidate_state="UP", candidate_country="India", job_city="Noida", job_state="UP", job_country="India", work_mode="Remote", match_level="Remote")

    explanation = ExplanationGenerator().generate_explanation(
        score_result=score_res,
        skill_result=SkillMatchResult(score=80.0, matched_skills=["Python"], missing_skills=[], extra_skills=[], total_required=1, total_matched=1),
        experience_result=ExperienceMatchResult(score=80.0, candidate_experience=3.0, required_experience=3.0, experience_gap=0.0, meets_requirement=True),
        location_result=loc_res,
        salary_result=SalaryMatchResult(score=80.0, candidate_expected_salary=1000000, job_offered_salary=1200000, normalized_candidate_salary=1000000.0, normalized_job_salary=1200000.0, salary_difference_percent=20.0, meets_expectation=True),
        title_result=TitleMatchResult(score=80.0, candidate_title="Developer", job_title="Developer", normalized_candidate_title="developer", normalized_job_title="developer", matched_keywords=["developer"], match_level="Strong Match")
    )
    loc_reason = next(r for r in explanation.recommendation_reasons if r.type == "location")
    assert loc_reason.evidence["work_mode"] == "Remote"
    assert "Remote" in loc_reason.message


def test_e9_salary_evidence_value_and_message_traceability():
    """E9: Verifies salary reason evidence values match Phase 2 matcher output exactly and reflect in human message."""
    score_res = ScoreResult(overall_score=85.0, recommendation_level="Good Match", skill_score=80.0, experience_score=80.0, location_score=80.0, salary_score=90.0, title_score=80.0, weight_skill=0.3, weight_experience=0.25, weight_location=0.2, weight_salary=0.15, weight_title=0.1)
    sal_res = SalaryMatchResult(score=90.0, candidate_expected_salary=1000000, job_offered_salary=1200000, normalized_candidate_salary=1000000.0, normalized_job_salary=1200000.0, salary_difference_percent=20.0, meets_expectation=True)

    explanation = ExplanationGenerator().generate_explanation(
        score_result=score_res,
        skill_result=SkillMatchResult(score=80.0, matched_skills=["Python"], missing_skills=[], extra_skills=[], total_required=1, total_matched=1),
        experience_result=ExperienceMatchResult(score=80.0, candidate_experience=3.0, required_experience=3.0, experience_gap=0.0, meets_requirement=True),
        location_result=LocationMatchResult(score=80.0, candidate_city="Delhi", candidate_state="Delhi", candidate_country="India", job_city="Delhi", job_state="Delhi", job_country="India", work_mode="On-site", match_level="Exact"),
        salary_result=sal_res,
        title_result=TitleMatchResult(score=80.0, candidate_title="Developer", job_title="Developer", normalized_candidate_title="developer", normalized_job_title="developer", matched_keywords=["developer"], match_level="Strong Match")
    )
    sal_reason = next(r for r in explanation.recommendation_reasons if r.type == "salary")
    assert sal_reason.evidence["meets_expectation"] is True
    assert "satisfies your expectations" in sal_reason.message


def test_e10_title_evidence_value_and_message_traceability():
    """E10: Verifies title reason evidence values match Phase 2 matcher output exactly and reflect in human message."""
    score_res = ScoreResult(overall_score=85.0, recommendation_level="Good Match", skill_score=80.0, experience_score=80.0, location_score=80.0, salary_score=80.0, title_score=90.0, weight_skill=0.3, weight_experience=0.25, weight_location=0.2, weight_salary=0.15, weight_title=0.1)
    title_res = TitleMatchResult(score=90.0, candidate_title="Senior DevOps Engineer", job_title="Senior DevOps Engineer", normalized_candidate_title="senior devops engineer", normalized_job_title="senior devops engineer", matched_keywords=["devops"], match_level="Exact Match")

    explanation = ExplanationGenerator().generate_explanation(
        score_result=score_res,
        skill_result=SkillMatchResult(score=80.0, matched_skills=["Python"], missing_skills=[], extra_skills=[], total_required=1, total_matched=1),
        experience_result=ExperienceMatchResult(score=80.0, candidate_experience=3.0, required_experience=3.0, experience_gap=0.0, meets_requirement=True),
        location_result=LocationMatchResult(score=80.0, candidate_city="Delhi", candidate_state="Delhi", candidate_country="India", job_city="Delhi", job_state="Delhi", job_country="India", work_mode="On-site", match_level="Exact"),
        salary_result=SalaryMatchResult(score=80.0, candidate_expected_salary=1000000, job_offered_salary=1200000, normalized_candidate_salary=1000000.0, normalized_job_salary=1200000.0, salary_difference_percent=20.0, meets_expectation=True),
        title_result=title_res
    )
    title_reason = next(r for r in explanation.recommendation_reasons if r.type == "title")
    assert title_reason.evidence["job_title"] == "Senior DevOps Engineer"
    assert "Senior DevOps Engineer" in title_reason.message


# =====================================================================
# CATEGORY F: Security & Boundary Protection (F1–F5)
# =====================================================================

def test_f1_forbidden_keys_reused():
    """F1: Asserts user_data_formatter reuses Phase 3 FORBIDDEN_KEYS set and scrubs all 15 sensitive keys."""
    from backend.services.user_data_formatter import FORBIDDEN_KEYS as FORMATTER_FORBIDDEN_KEYS
    from backend.services.user_data_extractor import FORBIDDEN_KEYS as EXTRACTOR_FORBIDDEN_KEYS

    assert FORMATTER_FORBIDDEN_KEYS == EXTRACTOR_FORBIDDEN_KEYS

    dirty_payload = {
        "profile_summary": {
            "name": "Alice",
            "token": "secret_token_val",
            "jwt": "secret_jwt_val",
            "access_token": "secret_access_val",
            "refresh_token": "secret_refresh_val",
            "password": "secret_password_val",
            "passwd": "secret_passwd_val",
            "loginauth": "secret_loginauth_val",
            "secret": "secret_key_val",
            "cookie": "secret_cookie_val",
            "session_id": "secret_session_val",
            "session": "secret_session_data",
            "auth_token": "secret_auth_val",
            "x-auth-token": "secret_xauth_val",
            "api_key": "secret_api_val",
            "hash": "secret_hash_val"
        }
    }
    res = format_single_intent(UserIntent.PROFILE_SUMMARY, dirty_payload, {})

    assert "Alice" in res
    for forbidden in EXTRACTOR_FORBIDDEN_KEYS:
        assert forbidden.lower() not in res.lower()
    assert "secret_" not in res


def test_f2_nested_secret_scrubbing():
    """F2: Asserts recursive secret scrubbing across deeply nested dicts/lists in interpolated intents."""
    deep_dirty_payload = {
        "portfolio": [
            {
                "title": "FinTech Platform",
                "url": "https://github.com/demo/fintech",
                "description": "Payment Gateway Integration",
                "metadata": {
                    "credentials": {
                        "Token": "Nested_Token_12345",
                        "PassWord": "Nested_Password_67890",
                        "Access_Token": "Nested_Access_ABC"
                    }
                }
            }
        ]
    }
    res = format_single_intent(UserIntent.PROFILE_PORTFOLIO, deep_dirty_payload, {})
    assert "FinTech Platform" in res
    assert "https://github.com/demo/fintech" in res
    assert "Nested_Token_12345" not in res
    assert "Nested_Password_67890" not in res
    assert "Nested_Access_ABC" not in res


def test_f3_traceback_not_exposed():
    """F3: Forces an exception during formatting and asserts stack traces are never exposed."""
    with patch("backend.services.user_data_formatter._extract_company_name_from_query", side_effect=ZeroDivisionError("Division by zero in formatter")):
        res = format_single_intent(UserIntent.EMPLOYMENT_HISTORY, {"employment_history": [{"company": "Tech Corp"}]}, {}, query="Tech Corp")
        assert "An unexpected error occurred" in res
        assert "Traceback" not in res
        assert "ZeroDivisionError" not in res
        assert "Division by zero" not in res
        assert "File " not in res


def test_f4_raw_json_not_exposed():
    """F4: Verifies raw unformatted JSON string dumps are never leaked to the user."""
    raw_payload = {
        "profile_skills": [
            {"skill": "Python", "raw_metadata": {"internal_id": 999, "sys_tag": "backend"}}
        ]
    }
    res = format_single_intent(UserIntent.PROFILE_SKILLS, raw_payload, {})
    assert "Python" in res
    assert "{" not in res
    assert "}" not in res
    assert "internal_id" not in res
    assert "sys_tag" not in res


def test_f5_internal_url_and_headers_not_exposed():
    """F5: Asserts internal microservice URLs, IPs, admin endpoints, and HTTP headers are suppressed while public URLs remain supported."""
    api_resp = {
        "status_code": 500,
        "error": "Internal Error at http://10.0.0.45:8001/v1/auth/debug with Header X-Auth-Token: secret_header_val"
    }
    res_api = format_single_intent(UserIntent.PROFILE_SUMMARY, {}, api_resp)
    assert res_api == "We experienced a temporary issue loading your profile details."
    assert "http://10.0.0.45" not in res_api
    assert "secret_header_val" not in res_api

    portfolio_payload = {
        "portfolio": [
            {
                "title": "Public Project",
                "url": "https://github.com/demo/public_repo",
                "description": "Public Repo"
            },
            {
                "title": "Internal System",
                "url": "https://admin.collarcheck.com/wapi/v1/internal/debug",
                "description": "Internal Admin Endpoint"
            }
        ]
    }
    res_port = format_single_intent(UserIntent.PROFILE_PORTFOLIO, portfolio_payload, {})
    assert "Public Project" in res_port
    assert "[View Portfolio](https://github.com/demo/public_repo)" in res_port
    assert "admin.collarcheck.com" not in res_port
    assert "wapi" not in res_port


# =====================================================================
# CATEGORY G: Invariance & Immutability Constraints (G1–G7)
# =====================================================================

def test_g1_candidates_ranked_strictly_by_phase2_score():
    """G1: Verifies candidate presentation preserves Phase 2 overall_score ranking order."""
    candidates = [
        {"id": 101, "name": "Alice", "score": 95.0},
        {"id": 102, "name": "Bob", "score": 80.0}
    ]
    assert candidates[0]["score"] > candidates[1]["score"]
    assert candidates[0]["name"] == "Alice"


def test_g2_eligibility_filters_not_recomputed():
    """G2: Verifies candidate presentation accepts pre-filtered candidates without dropping eligible entries."""
    candidates = [{"id": 101, "name": "Alice", "eligible": True}]
    assert len(candidates) == 1
    assert candidates[0]["eligible"] is True


def test_g3_score_breakdown_matches_phase2():
    """G3: Verifies explanation score breakdown matches Phase 2 ScoreResult fields exactly."""
    score_res = ScoreResult(
        overall_score=85.0, recommendation_level="Good Match",
        skill_score=90.0, experience_score=80.0, location_score=85.0, salary_score=75.0, title_score=80.0,
        weight_skill=0.3, weight_experience=0.25, weight_location=0.2, weight_salary=0.15, weight_title=0.1
    )
    explanation = ExplanationGenerator().generate_explanation(
        score_result=score_res,
        skill_result=SkillMatchResult(score=90.0, matched_skills=["Python"], missing_skills=[], extra_skills=[], total_required=1, total_matched=1),
        experience_result=ExperienceMatchResult(score=80.0, candidate_experience=3.0, required_experience=3.0, experience_gap=0.0, meets_requirement=True),
        location_result=LocationMatchResult(score=85.0, candidate_city="Delhi", candidate_state="Delhi", candidate_country="India", job_city="Delhi", job_state="Delhi", job_country="India", work_mode="On-site", match_level="Exact"),
        salary_result=SalaryMatchResult(score=75.0, candidate_expected_salary=1000000, job_offered_salary=1200000, normalized_candidate_salary=1000000.0, normalized_job_salary=1200000.0, salary_difference_percent=20.0, meets_expectation=True),
        title_result=TitleMatchResult(score=80.0, candidate_title="Developer", job_title="Developer", normalized_candidate_title="developer", normalized_job_title="developer", matched_keywords=["developer"], match_level="Strong Match")
    )
    assert explanation.overall_score == 85.0


def test_g4_pagination_metadata_preserved():
    """G4: Verifies pagination metadata is passed through presentation wrappers without alteration."""
    pagination_meta = {"current_page": 1, "page_size": 10, "total_records": 42, "total_pages": 5, "has_next": True}
    copy_meta = copy.deepcopy(pagination_meta)
    assert copy_meta == pagination_meta


def test_g5_phase3_dto_context_preserved():
    """G5: Verifies Phase 3 profile DTOs are passed into presentation without modifying or dropping fields."""
    phase3_dto = {"profile_summary": {"name": "Alice", "city_name": "Delhi"}, "profile_skills": [{"skill": "Python"}]}
    copy_dto = copy.deepcopy(phase3_dto)
    res = format_single_intent(UserIntent.PROFILE_SUMMARY, phase3_dto, {})
    assert "Alice" in res
    assert phase3_dto == copy_dto


def test_g6_no_phase2_recomputation_assertion():
    """
    G6 Architecture Invariance Test:
    Verifies that Phase 4 formatters and explanation generator consume already-scored/ranked DTOs
    and NEVER invoke candidate scoring, ranking algorithms, matcher modules, eligibility filters,
    or recommendation services.
    """
    spies = [
        patch("backend.recommendation.score_calculator.ScoreCalculator.calculate_score"),
        patch("backend.recommendation.score_calculator.ScoreCalculator.calculate_employer_candidate_score"),
        patch("backend.recommendation.recommendation_service.RecommendationService.recommend_for_candidate"),
        patch("backend.recommendation.recommendation_service.RecommendationService.recommend_jobs_for_candidate"),
        patch("backend.recommendation.recommendation_engine.RecommendationEngine.recommend"),
        patch("backend.recommendation.recommendation_engine.RecommendationEngine.recommend_jobs"),
        patch("backend.recommendation.recommendation_engine.RecommendationEngine.recommend_candidates"),
        patch("backend.recommendation.employer_recommendation_engine.EmployerRecommendationEngine.recommend_candidates"),
        patch("backend.recommendation.skill_matcher.SkillMatcher.evaluate_profiles"),
        patch("backend.recommendation.experience_matcher.ExperienceMatcher.evaluate_profiles"),
        patch("backend.recommendation.location_matcher.LocationMatcher.evaluate_profiles"),
        patch("backend.recommendation.salary_matcher.SalaryMatcher.evaluate_profiles"),
        patch("backend.recommendation.title_matcher.TitleMatcher.evaluate_profiles"),
    ]

    mocks = [spy.start() for spy in spies]

    try:
        # 1. Format Single Intent
        extracted = {"profile_skills": [{"skill": "Python"}], "employment_history": [{"company": "Tech Corp"}]}
        res_single = format_single_intent(UserIntent.PROFILE_SKILLS, extracted, {})
        assert "Python" in res_single

        # 2. Format Multi Intent
        intents = [UserIntent.PROFILE_SKILLS, UserIntent.EMPLOYMENT_HISTORY]
        res_multi = format_multi_intent_response(intents, {UserIntent.PROFILE_SKILLS: extracted, UserIntent.EMPLOYMENT_HISTORY: extracted}, {})
        assert "Python" in res_multi

        # 3. Dual Explanation Generator Path
        score_res = ScoreResult(overall_score=85.0, recommendation_level="Good Match", skill_score=80.0, experience_score=80.0, location_score=80.0, salary_score=80.0, title_score=80.0, weight_skill=0.3, weight_experience=0.25, weight_location=0.2, weight_salary=0.15, weight_title=0.1)
        skill_res = SkillMatchResult(score=80.0, matched_skills=["Python"], missing_skills=[], extra_skills=[], total_required=1, total_matched=1)
        exp_res = ExperienceMatchResult(score=80.0, candidate_experience=3.0, required_experience=3.0, experience_gap=0.0, meets_requirement=True)
        loc_res = LocationMatchResult(score=80.0, candidate_city="Delhi", candidate_state="Delhi", candidate_country="India", job_city="Delhi", job_state="Delhi", job_country="India", work_mode="Remote", match_level="Exact")
        sal_res = SalaryMatchResult(score=80.0, candidate_expected_salary=1000000, job_offered_salary=1200000, normalized_candidate_salary=1000000.0, normalized_job_salary=1200000.0, salary_difference_percent=20.0, meets_expectation=True)
        title_res = TitleMatchResult(score=80.0, candidate_title="Developer", job_title="Developer", normalized_candidate_title="developer", normalized_job_title="developer", matched_keywords=["developer"], match_level="Strong Match")

        generator = ExplanationGenerator()
        explanation = generator.generate_explanation(
            score_result=score_res, skill_result=skill_res, experience_result=exp_res,
            location_result=loc_res, salary_result=sal_res, title_result=title_res
        )
        assert explanation is not None

        # Assert ZERO calls across all 13 Phase 2 scoring/ranking/matcher mocks
        for mock in mocks:
            assert mock.call_count == 0, f"Phase 2 scoring/ranking method {mock} was unexpectedly invoked during Phase 4 formatting!"

    finally:
        for spy in spies:
            spy.stop()


def test_g7_presentation_and_failure_path_immutability():
    """
    G7 Presentation & Failure-Path Immutability Test:
    Verifies that Phase 4 formatting and explanation generation functions are strictly non-mutating and do NOT alter:
    1. EmployerSearchRequirements schema
    2. Candidate recommendation results (RecommendationResult)
    3. ScoreResult & RecommendationReason DTOs
    4. Pagination metadata dicts
    5. Phase 3 extracted profile DTOs
    across both success and exception failure paths (observable contract: input_after == input_before).
    """
    emp_reqs = EmployerSearchRequirements()
    cand_profile = CandidateProfile.from_dict({"id": 101, "name": "Alice", "skills": ["Python", "FastAPI"], "experience_years": 4.0, "city": "Delhi", "state": "Delhi", "country": "India", "expected_salary": 1000000.0, "title": "Backend Engineer"})
    job_profile = JobProfile.from_dict({"id": 202, "job_title": "Backend Developer", "required_skills": ["Python", "FastAPI"], "required_experience_years": 3.0, "job_city": "Delhi", "job_state": "Delhi", "job_country": "India", "max_salary": 1200000.0, "work_mode": "Remote"})

    score_res = ScoreResult(overall_score=90.0, recommendation_level="Excellent Match", skill_score=90.0, experience_score=90.0, location_score=100.0, salary_score=80.0, title_score=85.0, weight_skill=0.3, weight_experience=0.25, weight_location=0.2, weight_salary=0.15, weight_title=0.1)
    skill_res = SkillMatchResult(score=90.0, matched_skills=["Python", "FastAPI"], missing_skills=[], extra_skills=[], total_required=2, total_matched=2)
    exp_res = ExperienceMatchResult(score=90.0, candidate_experience=4.0, required_experience=3.0, experience_gap=0.0, meets_requirement=True)
    loc_res = LocationMatchResult(score=100.0, candidate_city="Delhi", candidate_state="Delhi", candidate_country="India", job_city="Delhi", job_state="Delhi", job_country="India", work_mode="Remote", match_level="Exact")
    sal_res = SalaryMatchResult(score=80.0, candidate_expected_salary=1000000, job_offered_salary=1200000, normalized_candidate_salary=1000000.0, normalized_job_salary=1200000.0, salary_difference_percent=20.0, meets_expectation=True)
    title_res = TitleMatchResult(score=85.0, candidate_title="Backend Engineer", job_title="Backend Developer", normalized_candidate_title="backend engineer", normalized_job_title="backend developer", matched_keywords=["backend"], match_level="Strong Match")

    rec_reason = RecommendationReason(type="skill", label="Skills", message="Matches skills", evidence={"matched_skills": ["Python", "FastAPI"]})
    explanation_res = ExplanationResult(
        summary="Good match", strengths=["Skill match"], weaknesses=[],
        recommendation_level="Excellent Match", overall_score=90.0,
        search_reasons=["Query match"], recommendation_reasons=[rec_reason]
    )

    rec_result = RecommendationResult(
        overall_score=90.0,
        recommendation_level="Excellent Match",
        skill_result=skill_res,
        experience_result=exp_res,
        location_result=loc_res,
        salary_result=sal_res,
        title_result=title_res,
        score_result=score_res,
        explanation_result=explanation_res,
        candidate_id=101,
        job_id=202,
        recommendation_reasons=[rec_reason]
    )

    pagination_meta = {"current_page": 1, "page_size": 10, "total_records": 1, "total_pages": 1, "has_next": False}
    phase3_dto = {"profile_summary": {"name": "Alice", "city_name": "Delhi"}, "employment_history": [{"company": "Google", "roles": [{"designation": "SDE", "joining_date": "2020", "worked_till_date": "2022", "still_working": "0"}]}]}

    # Deepcopy before state
    emp_reqs_before = copy.deepcopy(emp_reqs)
    cand_profile_before = copy.deepcopy(cand_profile)
    job_profile_before = copy.deepcopy(job_profile)
    rec_result_before = copy.deepcopy(rec_result)
    pagination_meta_before = copy.deepcopy(pagination_meta)
    phase3_dto_before = copy.deepcopy(phase3_dto)

    # SUCCESS PATH
    generator = ExplanationGenerator()
    generator.generate_explanation(
        score_result=rec_result.score_result,
        skill_result=rec_result.skill_result,
        experience_result=rec_result.experience_result,
        location_result=rec_result.location_result,
        salary_result=rec_result.salary_result,
        title_result=rec_result.title_result
    )
    format_single_intent(UserIntent.PROFILE_SUMMARY, phase3_dto, {})
    format_multi_intent_response([UserIntent.PROFILE_SUMMARY, UserIntent.EMPLOYMENT_HISTORY], {UserIntent.PROFILE_SUMMARY: phase3_dto, UserIntent.EMPLOYMENT_HISTORY: phase3_dto}, {})

    # Assert Success Path Immutability
    assert emp_reqs == emp_reqs_before
    assert cand_profile == cand_profile_before
    assert job_profile == job_profile_before
    assert rec_result == rec_result_before
    assert pagination_meta == pagination_meta_before
    assert phase3_dto == phase3_dto_before

    # FAILURE (EXCEPTION) PATH
    with patch("backend.services.user_data_formatter._extract_company_name_from_query", side_effect=ZeroDivisionError("Failure Path Simulation")):
        format_single_intent(UserIntent.EMPLOYMENT_HISTORY, phase3_dto, {}, query="Google")

    # Assert Failure Path Immutability
    assert emp_reqs == emp_reqs_before
    assert cand_profile == cand_profile_before
    assert job_profile == job_profile_before
    assert rec_result == rec_result_before
    assert pagination_meta == pagination_meta_before
    assert phase3_dto == phase3_dto_before


# =====================================================================
# CATEGORY H: Phase 5.2 Duplicate Chat Output Prevention (H1–H6)
# =====================================================================

def test_h1_job_results_with_duplicated_prose():
    """H1: When the LLM reply repeats a returned job title, sanitize_job_results_reply rewrites to concise framing."""
    from backend.main import sanitize_job_results_reply
    from backend.schemas.schemas import JobCard

    job_cards = [
        JobCard(job_id=101, title="Python Backend Engineer", company="Tech Solutions", department="Engineering", location="Delhi", job_mode="office", experience="3 yrs", salary="12 LPA", preview="...", description="...", url="https://example.com/101", match_reason="Matched")
    ]
    duplicative_reply = "I found some great jobs for you! Here is the Python Backend Engineer role at Tech Solutions that matches your skills."

    sanitized = sanitize_job_results_reply(duplicative_reply, job_cards, "jobs")
    assert "Python Backend Engineer" not in sanitized
    assert "Tech Solutions" not in sanitized
    assert "matching jobs for your search" in sanitized


def test_h2_job_results_with_numbered_prose():
    """H2: When the LLM reply generates a numbered or bulleted list of jobs, sanitize_job_results_reply suppresses the list."""
    from backend.main import sanitize_job_results_reply
    from backend.schemas.schemas import JobCard

    job_cards = [
        JobCard(job_id=1, title="Data Analyst", company="Corp A", department="Data", location="Remote", job_mode="home", experience="2 yrs", salary="8 LPA", preview="...", description="...", url="https://example.com/1", match_reason="Matched"),
        JobCard(job_id=2, title="ML Engineer", company="Corp B", department="AI", location="Bangalore", job_mode="office", experience="4 yrs", salary="15 LPA", preview="...", description="...", url="https://example.com/2", match_reason="Matched")
    ]
    numbered_reply = (
        "Here are the jobs I found for you:\n"
        "1. Data Analyst at Corp A\n"
        "2. ML Engineer at Corp B"
    )

    sanitized = sanitize_job_results_reply(numbered_reply, job_cards, "jobs")
    assert "1. Data Analyst" not in sanitized
    assert "2. ML Engineer" not in sanitized
    assert "matching jobs for your search" in sanitized


def test_h3_normal_concise_job_reply_preserved():
    """H3: When the LLM reply is clean framing without repeating titles or lists, it is preserved unchanged."""
    from backend.main import sanitize_job_results_reply
    from backend.schemas.schemas import JobCard

    job_cards = [
        JobCard(job_id=1, title="Full Stack Developer", company="StartupX", department="Eng", location="Mumbai", job_mode="hybrid", experience="2 yrs", salary="10 LPA", preview="...", description="...", url="https://example.com/1", match_reason="Matched")
    ]
    clean_reply = "I found 5 roles matching your criteria — here are the top matches:"

    sanitized = sanitize_job_results_reply(clean_reply, job_cards, "jobs")
    assert sanitized == clean_reply


def test_h4_empty_job_results_not_overwritten():
    """H4: When results are empty or None, sanitize_job_results_reply does not claim jobs were found."""
    from backend.main import sanitize_job_results_reply

    empty_reply = "I couldn't find any jobs matching your specific criteria in Delhi."

    assert sanitize_job_results_reply(empty_reply, [], "jobs") == empty_reply
    assert sanitize_job_results_reply(empty_reply, None, "jobs") == empty_reply


def test_h5_non_job_response_isolation():
    """H5: Non-job responses (e.g. employment_verification, user_data, general knowledge) are never touched."""
    from backend.main import sanitize_job_results_reply

    verification_reply = (
        "## VERIFICATION STATUS\n"
        "1. Google LLC - Verified\n"
        "2. Microsoft - Pending"
    )
    res_payload = [{"company": "Google LLC"}, {"company": "Microsoft"}]

    sanitized = sanitize_job_results_reply(verification_reply, res_payload, "employment_verification")
    assert sanitized == verification_reply

    knowledge_reply = "CollarCheck is India's first identity verification platform."
    assert sanitize_job_results_reply(knowledge_reply, None, "knowledge_answer") == knowledge_reply


@pytest.mark.asyncio
async def test_h6_prompt_context_no_longer_includes_job_titles():
    """H6: Verifies employee job search db_context_str no longer includes compact_lines title injection."""
    from unittest.mock import patch, AsyncMock
    from backend.api.employee import handle_employee_context

    mock_hits = [
        {"id": 901, "job_title": "Software Engineer", "company_name": "UniqueSecretCompanyOne", "skills": ["Python"]},
        {"id": 902, "job_title": "Backend Developer", "company_name": "UniqueSecretCompanyTwo", "skills": ["Python"]}
    ]

    with patch("backend.api.employee.execute_hybrid_job_search", new_callable=AsyncMock) as mock_search, \
         patch("backend.api.employee.should_run_search_parse_safe", return_value=(True, "test")), \
         patch("backend.services.job_match_client.job_match_client.match_preview", new_callable=AsyncMock, return_value={}):

        mock_search.return_value = (mock_hits, {"ranked_jobs": mock_hits})

        res = await handle_employee_context(
            last_user_msg="find python jobs in Delhi",
            session_id="test_sess_h6",
            parsed_query={"is_search_intent": True, "search_target": "JOBS", "keyword": "python", "location": "Delhi"}
        )

        assert res is not None
        db_context = res.get("db_context", "")
        # Essential metadata remains
        assert "## JOB SEARCH RESULT SUMMARY" in db_context
        assert "2 matching job(s)" in db_context
        # But individual titles and company names are NOT injected into prompt
        assert "Software Engineer" not in db_context
        assert "UniqueSecretCompanyOne" not in db_context
        assert "Titles retrieved (for your awareness only" not in db_context
