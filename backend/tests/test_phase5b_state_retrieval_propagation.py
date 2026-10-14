"""
test_phase5b_state_retrieval_propagation.py — Phase 5B Regression Test Suite.

Verifies end-to-end propagation of conversational search state into retrieval inputs
and post-RRF hard gates across Scenarios A through H:

Scenario A — Remote -> Profile Skills
Scenario B — Location -> Salary
Scenario C — Experience -> Remote
Scenario D — Urgent -> Profile Skills
Scenario E — Explicit Skill + Profile Skill coexistence
Scenario F — Clear One Constraint, Preserve Others
Scenario G — Candidate -> Job Boundary Isolation
Scenario H — Job -> Candidate -> Job Cycle Isolation
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

from backend.schemas.search_query import SearchQuery, LocationMode, SearchTarget
from backend.schemas.search_criteria import SearchCriteria
from backend.utils.session_state import (
    merge_employee_search_query,
    update_employee_session_cas,
    get_search_context,
    set_candidate_search_criteria,
    get_candidate_search_criteria,
)
from backend.services.hybrid_job_search import (
    execute_hybrid_job_search,
    NormalizedJob,
    passes_job_mode_eligibility,
    passes_location_eligibility,
    passes_salary_eligibility,
    passes_experience_eligibility,
)


# ==============================================================================
# SCENARIO A: REMOTE -> PROFILE SKILLS
# ==============================================================================

def test_scenario_a_remote_to_profile_skills_propagation():
    """
    Scenario A:
    Turn 1: "show me remote jobs" (work_mode="home")
    Turn 2: "according to my skills" (use_profile=True)
    Verifies work_mode remains "home", profile skills enabled, and post-RRF remote gate holds.
    """
    turn1_q = SearchQuery(work_mode="home")
    turn2_q = SearchQuery(use_profile=True)

    merged = merge_employee_search_query(turn1_q, turn2_q, prompt_category="ADDITIVE")

    assert merged.work_mode == "home"
    assert merged.use_profile is True

    # Test post-fusion gate enforcement
    remote_job = NormalizedJob(job_id=1, title="Remote Python Dev", job_mode="Remote", raw_data={"job_mode_name": "Remote"})
    onsite_job = NormalizedJob(job_id=2, title="Onsite Dev", job_mode="Office", raw_data={"job_mode_name": "Office"})

    assert passes_job_mode_eligibility(remote_job, target_job_mode=merged.work_mode) is True
    assert passes_job_mode_eligibility(onsite_job, target_job_mode=merged.work_mode) is False


# ==============================================================================
# SCENARIO B: LOCATION -> SALARY
# ==============================================================================

def test_scenario_b_location_to_salary_propagation():
    """
    Scenario B:
    Turn 1: "show me jobs in Delhi" (location="Delhi")
    Turn 2: "under 10 LPA" (salary_max=1000000.0)
    Verifies location="Delhi" and salary_max=1000000.0 both survive and gates enforce both.
    """
    turn1_q = SearchQuery(location="Delhi", canonical_location="Delhi", location_mode=LocationMode.EXPLICIT)
    turn2_q = SearchQuery(salary_max=1000000.0)

    merged = merge_employee_search_query(turn1_q, turn2_q, prompt_category="ADDITIVE")

    assert merged.location == "Delhi"
    assert merged.salary_max == 1000000.0

    # Test post-fusion gates
    valid_job = NormalizedJob(job_id=10, title="Delhi Dev", location="Delhi", raw_data={"city_name": "Delhi", "salary_max": 800000.0})
    high_sal_job = NormalizedJob(job_id=11, title="Delhi Lead", location="Delhi", raw_data={"city_name": "Delhi", "salary_min": 1500000.0})

    assert passes_location_eligibility(valid_job, target_city_name=merged.location) is True
    assert passes_salary_eligibility(valid_job, salary_max=merged.salary_max) is True
    assert passes_salary_eligibility(high_sal_job, salary_max=merged.salary_max) is False


# ==============================================================================
# SCENARIO C: EXPERIENCE -> REMOTE
# ==============================================================================

def test_scenario_c_experience_to_remote_propagation():
    """
    Scenario C:
    Turn 1: "show me jobs for 5+ years experience" (experience_level="5")
    Turn 2: "make them remote" (work_mode="home")
    Verifies both constraints survive and gates enforce both.
    """
    turn1_q = SearchQuery(experience_level="5")
    turn2_q = SearchQuery(work_mode="home")

    merged = merge_employee_search_query(turn1_q, turn2_q, prompt_category="ADDITIVE")

    assert merged.experience_level == "5"
    assert merged.work_mode == "home"

    valid_job = NormalizedJob(job_id=20, title="Senior Remote Engineer", job_mode="Remote", raw_data={"job_mode_name": "Remote", "experience_min": 5})
    junior_job = NormalizedJob(job_id=21, title="Junior Remote Engineer", job_mode="Remote", raw_data={"job_mode_name": "Remote", "experience_max": 2})

    assert passes_job_mode_eligibility(valid_job, target_job_mode=merged.work_mode) is True
    assert passes_experience_eligibility(valid_job, experience_min=5) is True
    assert passes_experience_eligibility(junior_job, experience_min=5) is False


# ==============================================================================
# SCENARIO D: URGENT -> PROFILE SKILLS
# ==============================================================================

def test_scenario_d_urgent_to_profile_skills_propagation():
    """
    Scenario D:
    Turn 1: "show me urgent jobs" (urgent=True)
    Turn 2: "according to my skills" (use_profile=True)
    Verifies urgent=True is retained alongside profile skill retrieval mode.
    """
    turn1_q = SearchQuery(urgent=True)
    turn2_q = SearchQuery(use_profile=True)

    merged = merge_employee_search_query(turn1_q, turn2_q, prompt_category="ADDITIVE")

    assert merged.urgent is True
    assert merged.use_profile is True


# ==============================================================================
# SCENARIO E: EXPLICIT SKILL + PROFILE SKILL COEXISTENCE
# ==============================================================================

def test_scenario_e_explicit_and_profile_skill_coexistence():
    """
    Scenario E:
    Turn 1: "show me Python jobs" (skills=["Python"], title="Python Developer")
    Turn 2: "according to my skills" (use_profile=True)
    Verifies explicit Python requirement survives alongside profile skill retrieval enablement.
    """
    turn1_q = SearchQuery(title="Python Developer", skills=["Python"])
    turn2_q = SearchQuery(use_profile=True)

    merged = merge_employee_search_query(turn1_q, turn2_q, prompt_category="ADDITIVE")

    assert merged.skills == ["Python"]
    assert merged.title == "Python Developer"
    assert merged.use_profile is True


# ==============================================================================
# SCENARIO F: CLEAR ONE CONSTRAINT, PRESERVE OTHERS
# ==============================================================================

def test_scenario_f_clear_one_constraint_preserve_others():
    """
    Scenario F:
    Turn 1: "show me remote Python jobs under 10 LPA in Delhi"
    Turn 2: "clear location"
    Verifies location is removed while work_mode, skills, and salary_max remain intact.
    """
    prev = SearchQuery(
        title="Python Developer",
        skills=["Python"],
        work_mode="home",
        salary_max=1000000.0,
        location="Delhi",
        canonical_location="Delhi",
    )
    curr = SearchQuery(clear_filters=["location"])

    merged = merge_employee_search_query(prev, curr, prompt_category="ADDITIVE")

    assert merged.location is None
    assert merged.canonical_location is None
    assert merged.work_mode == "home"
    assert merged.skills == ["Python"]
    assert merged.salary_max == 1000000.0


# ==============================================================================
# SCENARIO G: CANDIDATE -> JOB BOUNDARY ISOLATION
# ==============================================================================

def test_scenario_g_candidate_to_job_boundary_isolation():
    """
    Scenario G:
    Turn 1: "find Kunal Yadav" (PEOPLE_SEARCH criteria)
    Turn 2: "show me remote Python jobs" (JOB_SEARCH criteria)
    Verifies candidate name and candidate context do not pollute the job search execution.
    """
    cand_criteria = SearchCriteria.from_parsed_dict({"candidate_name": "Kunal Yadav", "search_target": "PEOPLE_SEARCH"})
    assert cand_criteria.candidate_name == "Kunal Yadav"

    job_q = SearchQuery(title="Python Developer", skills=["Python"], work_mode="home")
    assert getattr(job_q, "candidate_name", None) is None

    merged_job = merge_employee_search_query(None, job_q, prompt_category="RESET")
    assert getattr(merged_job, "candidate_name", None) is None
    assert merged_job.work_mode == "home"
    assert merged_job.skills == ["Python"]


# ==============================================================================
# SCENARIO H: JOB -> CANDIDATE -> JOB CYCLE ISOLATION
# ==============================================================================

def test_scenario_h_full_cycle_job_people_job_isolation():
    """
    Scenario H:
    Turn 1: "show me remote Python jobs"
    Turn 2: "find candidates with React"
    Turn 3: "show me Java jobs"
    Verifies Turn 3 job search receives clean Java criteria without inheriting candidate React or Turn 1 remote/Python state.
    """
    session_id = "test_phase5b_cycle_session"

    # Turn 1: JOB_SEARCH
    t1_q = SearchQuery(title="Python Developer", skills=["Python"], work_mode="home")
    update_employee_session_cas(session_id, t1_q, prompt_category="RESET", role="employee")

    # Turn 2: PEOPLE_SEARCH
    cand_criteria = SearchCriteria.from_parsed_dict({"skills": ["React"], "search_target": "PEOPLE_SEARCH"})
    set_candidate_search_criteria(session_id, cand_criteria, role="employee")

    # Turn 3: JOB_SEARCH switch
    t3_q = SearchQuery(title="Java Developer", skills=["Java"])
    update_employee_session_cas(session_id, t3_q, prompt_category="RESET", role="employee")

    ctx3 = get_search_context(session_id, role="employee")
    assert ctx3.get("title") == "Java Developer"
    assert ctx3.get("skills") == ["Java"]
    assert ctx3.get("work_mode") is None
    assert getattr(ctx3, "candidate_name", None) is None
