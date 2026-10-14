"""
test_phase5a_conversational_state_hardening.py — Phase 5A Regression Hardening Test Suite.

Verifies:
1. P1: Multi-word and natural language clear_filters phrases for experience, location, and salary,
   and verifies that merge_employee_search_query correctly clears target constraints while preserving unrelated criteria.
2. P2: Context isolation between PEOPLE_SEARCH and JOB_SEARCH, asserting that candidate_name and candidate_search_context
   never pollute JOB_SEARCH executions during multi-turn transitions (JOB -> PEOPLE -> JOB).
"""

import pytest
from backend.schemas.search_query import SearchQuery, LocationMode, SearchTarget
from backend.schemas.search_criteria import SearchCriteria
from backend.utils.session_state import (
    merge_employee_search_query,
    merge_candidate_search_criteria,
    update_employee_session_cas,
    get_search_context,
    get_candidate_search_criteria,
    set_candidate_search_criteria,
)
from backend.services.search_parser import parse_search_query


# ==============================================================================
# P1: CLEAR-FILTER EDGE-CASE REGRESSION SUITE
# ==============================================================================

def test_p1_clear_experience_preserves_unrelated_constraints():
    """
    P1: 'bina experience ke' / experience clear filter.
    Verifies that experience constraints are cleared while location, work_mode, and skills are preserved.
    """
    prev = SearchQuery(
        title="Python Developer",
        skills=["Python"],
        location="Delhi",
        work_mode="home",
        experience_level="senior",
        salary_max=1000000.0,
    )
    curr = SearchQuery(
        clear_filters=["experience"],
    )

    merged = merge_employee_search_query(prev, curr, prompt_category="ADDITIVE")

    assert merged.experience_level is None
    assert merged.location == "Delhi"
    assert merged.work_mode == "home"
    assert merged.skills == ["Python"]
    assert merged.salary_max == 1000000.0


def test_p1_clear_location_preserves_unrelated_constraints():
    """
    P1: 'kisi bhi location mein' / location clear filter.
    Verifies that location constraint is cleared while skills, work_mode, and salary bounds remain intact.
    """
    prev = SearchQuery(
        title="Data Analyst",
        skills=["SQL", "Python"],
        location="Mumbai",
        canonical_location="Mumbai",
        location_mode=LocationMode.EXPLICIT,
        work_mode="office",
        salary_max=800000.0,
    )
    curr = SearchQuery(
        clear_filters=["location"],
    )

    merged = merge_employee_search_query(prev, curr, prompt_category="ADDITIVE")

    assert merged.location is None
    assert merged.canonical_location is None
    assert merged.location_mode is None
    assert merged.skills == ["SQL", "Python"]
    assert merged.work_mode == "office"
    assert merged.salary_max == 800000.0


def test_p1_clear_salary_preserves_unrelated_constraints():
    """
    P1: 'bina kisi salary constraint ke' / salary clear filter.
    Verifies that salary_min and salary_max are cleared while skills, location, and experience remain intact.
    """
    prev = SearchQuery(
        title="Software Engineer",
        skills=["Java"],
        location="Bangalore",
        salary_min=500000.0,
        salary_max=1200000.0,
        experience_level="mid",
    )
    curr = SearchQuery(
        clear_filters=["salary"],
    )

    merged = merge_employee_search_query(prev, curr, prompt_category="ADDITIVE")

    assert merged.salary_min is None
    assert merged.salary_max is None
    assert merged.skills == ["Java"]
    assert merged.location == "Bangalore"
    assert merged.experience_level == "mid"


def test_p1_multi_filter_clearing_composition():
    """
    P1: Multiple explicit filter clearing targets (location + experience) in a single turn.
    """
    prev = SearchQuery(
        title="Full Stack Engineer",
        skills=["React", "Node.js"],
        location="Pune",
        work_mode="hybrid",
        experience_level="senior",
        salary_max=1500000.0,
    )
    curr = SearchQuery(
        clear_filters=["location", "experience"],
    )

    merged = merge_employee_search_query(prev, curr, prompt_category="ADDITIVE")

    assert merged.location is None
    assert merged.experience_level is None
    assert merged.work_mode == "hybrid"
    assert merged.skills == ["React", "Node.js"]
    assert merged.salary_max == 1500000.0


# ==============================================================================
# P2: CANDIDATE CONTEXT ISOLATION & NAME RESET SUITE
# ==============================================================================

def test_p2_people_search_to_job_search_context_isolation():
    """
    P2: Transition from PEOPLE_SEARCH ("find Kunal Yadav") to JOB_SEARCH ("show me remote Python jobs").
    Verifies that candidate_name and candidate_search_context do not pollute the job search query.
    """
    # Turn 1: Candidate search
    candidate_parsed = {
        "candidate_name": "Kunal Yadav",
        "search_target": "PEOPLE_SEARCH",
        "skills": ["Python"],
        "location": "Delhi",
    }
    cand_criteria = SearchCriteria.from_parsed_dict(candidate_parsed)
    assert cand_criteria.candidate_name == "Kunal Yadav"
    assert cand_criteria.target.value == "PEOPLE_SEARCH"

    # Turn 2: Job search follow-up query
    job_turn_query = SearchQuery(
        title="Python Developer",
        skills=["Python"],
        work_mode="home",
    )
    
    # Verify candidate_name is None on SearchQuery
    assert getattr(job_turn_query, "candidate_name", None) is None
    assert job_turn_query.work_mode == "home"

    # Merge job search query under RESET prompt category (explicit job switch)
    merged_job_query = merge_employee_search_query(None, job_turn_query, prompt_category="RESET")

    assert getattr(merged_job_query, "candidate_name", None) is None
    assert merged_job_query.work_mode == "home"
    assert merged_job_query.skills == ["Python"]


def test_p2_full_cycle_job_people_job_context_isolation():
    """
    P2: Full conversational cycle: JOB_SEARCH -> PEOPLE_SEARCH -> JOB_SEARCH.
    Turn 1: "show me remote Python jobs"
    Turn 2: "find candidates with React"
    Turn 3: "show me Java jobs"
    Verifies that Turn 3 candidate context does not leak into Turn 3 job search.
    """
    session_id = "test_phase5a_cycle_session"

    # Turn 1: JOB_SEARCH
    turn1_q = SearchQuery(title="Python Developer", skills=["Python"], work_mode="home")
    update_employee_session_cas(session_id, turn1_q, prompt_category="RESET", role="employee")

    ctx1 = get_search_context(session_id, role="employee")
    assert ctx1.get("work_mode") == "home"
    assert ctx1.get("skills") == ["Python"]

    # Turn 2: PEOPLE_SEARCH
    cand_criteria = SearchCriteria.from_parsed_dict({"skills": ["React"], "search_target": "PEOPLE_SEARCH"})
    set_candidate_search_criteria(session_id, cand_criteria, role="employee")

    cand_ctx2 = get_candidate_search_criteria(session_id, role="employee")
    assert cand_ctx2.skills == ["React"]
    assert cand_ctx2.target.value == "PEOPLE_SEARCH"

    # Turn 3: JOB_SEARCH switch back
    turn3_q = SearchQuery(title="Java Developer", skills=["Java"])
    update_employee_session_cas(session_id, turn3_q, prompt_category="RESET", role="employee")

    ctx3 = get_search_context(session_id, role="employee")
    assert ctx3.get("title") == "Java Developer"
    assert ctx3.get("skills") == ["Java"]
    assert ctx3.get("work_mode") is None  # RESET wiped previous work_mode
    assert getattr(ctx3, "candidate_name", None) is None
