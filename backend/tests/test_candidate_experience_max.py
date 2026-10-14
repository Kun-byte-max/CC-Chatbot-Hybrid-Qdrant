"""
test_candidate_experience_max.py — Phase 4.2B Experience Max Test Suite
Verifies:
1. SearchCriteria parsing & from_parsed_dict for experience_min and experience_max.
2. Boundary eligibility testing (1.9 excluded, 2.0 included, 5.0 included, 5.1 excluded for 2-5 yrs range).
3. Invalid min > max validation handling.
4. Multi-turn refinement & replacement for experience_max.
5. New search state reset for experience bounds.
"""

import pytest
from backend.schemas.search_criteria import SearchCriteria
from backend.services.people_scoring_engine import EmployeePeopleScoringEngine
from backend.utils.session_state import merge_candidate_search_criteria

def test_search_criteria_experience_bounds_parsing():
    """1. Test parsing experience_min and experience_max from parsed dictionary."""
    # Min only
    c1 = SearchCriteria.from_parsed_dict({"experience_min": 5})
    assert c1.experience_min == 5
    assert c1.experience_max is None

    # Max only
    c2 = SearchCriteria.from_parsed_dict({"experience_max": 5})
    assert c2.experience_min is None
    assert c2.experience_max == 5

    # Range
    c3 = SearchCriteria.from_parsed_dict({"experience_min": 2, "experience_max": 5})
    assert c3.experience_min == 2
    assert c3.experience_max == 5


def test_experience_boundary_eligibility():
    """2. Boundary testing for 2 to 5 years range (inclusive)."""
    req_min = 2.0
    req_max = 5.0

    # 1.9 -> excluded
    _, ok_19 = EmployeePeopleScoringEngine.match_experience(req_min, 1.9, requested_exp_max=req_max)
    assert ok_19 is False

    # 2.0 -> included
    _, ok_20 = EmployeePeopleScoringEngine.match_experience(req_min, 2.0, requested_exp_max=req_max)
    assert ok_20 is True

    # 3.0 -> included
    _, ok_30 = EmployeePeopleScoringEngine.match_experience(req_min, 3.0, requested_exp_max=req_max)
    assert ok_30 is True

    # 5.0 -> included
    _, ok_50 = EmployeePeopleScoringEngine.match_experience(req_min, 5.0, requested_exp_max=req_max)
    assert ok_50 is True

    # 5.1 -> excluded
    _, ok_51 = EmployeePeopleScoringEngine.match_experience(req_min, 5.1, requested_exp_max=req_max)
    assert ok_51 is False


def test_invalid_experience_range_handling():
    """3. Invalid range min > max (e.g. min=7, max=3) must be ineligible."""
    _, ok_invalid = EmployeePeopleScoringEngine.match_experience(7.0, 4.0, requested_exp_max=3.0)
    assert ok_invalid is False

    # In criteria merge
    prev = SearchCriteria(experience_min=7)
    curr = SearchCriteria(experience_max=3)
    merged = merge_candidate_search_criteria(prev, curr, query_type="REFINEMENT")
    assert merged.experience_max is None or merged.experience_min > merged.experience_max is False


def test_multiturn_experience_max_refinement_matrix():
    """4. Multi-turn refinement for experience_max."""
    # Test A: Python -> Under 5 years
    t1 = SearchCriteria(skills=["Python"])
    t2 = SearchCriteria(experience_max=5)
    m_a = merge_candidate_search_criteria(t1, t2, query_type="REFINEMENT")
    assert m_a.skills == ["Python"]
    assert m_a.experience_max == 5

    # Test B: Python min 2 -> Under 5 years
    t3 = SearchCriteria(skills=["Python"], experience_min=2)
    t4 = SearchCriteria(experience_max=5)
    m_b = merge_candidate_search_criteria(t3, t4, query_type="REFINEMENT")
    assert m_b.skills == ["Python"]
    assert m_b.experience_min == 2
    assert m_b.experience_max == 5

    # Test C: Python max 5 -> At least 3 years
    t5 = SearchCriteria(skills=["Python"], experience_max=5)
    t6 = SearchCriteria(experience_min=3)
    m_c = merge_candidate_search_criteria(t5, t6, query_type="REFINEMENT")
    assert m_c.skills == ["Python"]
    assert m_c.experience_min == 3
    assert m_c.experience_max == 5

    # Test D: Python 2 to 5 -> Find Java (New Search Reset)
    t7 = SearchCriteria(skills=["Python"], experience_min=2, experience_max=5)
    t8 = SearchCriteria(skills=["Java"])
    m_d = merge_candidate_search_criteria(t7, t8, query_type="NEW_SEARCH")
    assert m_d.skills == ["Java"]
    assert m_d.experience_min is None
    assert m_d.experience_max is None
