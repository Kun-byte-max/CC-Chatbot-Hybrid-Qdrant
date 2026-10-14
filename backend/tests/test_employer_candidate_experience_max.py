"""
Phase 4.5: Employer Candidate Search Parity — Experience Upper-Bound & Sticky Refinement Tests.
"""

import pytest
from backend.schemas.employer_requirements import EmployerSearchRequirements, ExperienceRequirement
from backend.services.employer_search_parser import parse_employer_query
from backend.recommendation.employer_recommendation_engine import EmployerRecommendationEngine
from backend.schemas.candidate_schema import StandardizedCandidateObject


def test_employer_parser_upper_bound_keyword_forms():
    """Verify parse_employer_query extracts experience.max for upper-bound keywords."""
    queries_max = [
        "Find candidates under 5 years experience",
        "Find candidates at most 5 years experience",
        "Find candidates less than 5 years experience",
        "Find candidates up to 5 years experience",
        "Find candidates with maximum 5 years experience",
    ]
    for q in queries_max:
        reqs = parse_employer_query(q)
        assert reqs.experience is not None, f"Failed on query: {q}"
        assert reqs.experience.max == 5.0, f"Failed max=5.0 assertion on query: {q}, got max={reqs.experience.max}"
        assert reqs.experience.min is None, f"Failed min=None assertion on query: {q}, got min={reqs.experience.min}"


def test_employer_parser_lower_bound_keyword_forms():
    """Verify parse_employer_query preserves lower-bound experience_min parsing."""
    queries_min = [
        "Find candidates with at least 3 years experience",
        "Find candidates minimum 3 years experience",
        "Find candidates with 3+ years experience",
    ]
    for q in queries_min:
        reqs = parse_employer_query(q)
        assert reqs.experience is not None, f"Failed on query: {q}"
        assert reqs.experience.min == 3.0, f"Failed min=3.0 assertion on query: {q}, got min={reqs.experience.min}"
        assert reqs.experience.max is None, f"Failed max=None assertion on query: {q}, got max={reqs.experience.max}"


def test_employer_parser_range_forms():
    """Verify parse_employer_query parses range experience requirements."""
    queries_range = [
        "Find candidates between 2 and 5 years experience",
        "Find candidates 2 to 5 years experience",
    ]
    for q in queries_range:
        reqs = parse_employer_query(q)
        assert reqs.experience is not None, f"Failed on query: {q}"
        assert reqs.experience.min == 2.0, f"Failed min=2.0 on query: {q}"
        assert reqs.experience.max == 5.0, f"Failed max=5.0 on query: {q}"


def test_employer_engine_hard_experience_max_filtering():
    """Verify EmployerRecommendationEngine hard-filters candidates exceeding experience.max."""
    engine = EmployerRecommendationEngine()
    reqs = EmployerSearchRequirements()
    reqs.experience = ExperienceRequirement(min=None, max=5.0)

    # Candidates: 4.9 (pass), 5.0 (pass), 5.1 (fail)
    cand_49 = StandardizedCandidateObject(candidate_id="1", name="Cand 4.9", experience_years=4.9, skills=["Python"])
    cand_50 = StandardizedCandidateObject(candidate_id="2", name="Cand 5.0", experience_years=5.0, skills=["Python"])
    cand_51 = StandardizedCandidateObject(candidate_id="3", name="Cand 5.1", experience_years=5.1, skills=["Python"])

    recs = engine.recommend_candidates(reqs, [cand_49, cand_50, cand_51])
    
    # Check match status for each candidate
    match_dict = {rec.candidate_id: rec for rec in recs}
    
    from backend.recommendation.types import MatchStatus, RecommendationDimension
    assert match_dict["1"].match_results[RecommendationDimension.EXPERIENCE].status in (MatchStatus.NOT_REQUESTED, MatchStatus.MATCH)
    assert match_dict["2"].match_results[RecommendationDimension.EXPERIENCE].status in (MatchStatus.NOT_REQUESTED, MatchStatus.MATCH)
    assert match_dict["3"].match_results[RecommendationDimension.EXPERIENCE].status == MatchStatus.MISMATCH


def test_employer_multiturn_experience_refinement():
    """Verify multi-turn accumulation of experience min and max bounds for employer query."""
    # Turn 1: Find Python candidates with at least 3 years experience
    turn1 = parse_employer_query("Find Python candidates with at least 3 years experience")
    assert turn1.experience.min == 3.0
    assert turn1.experience.max is None

    # Turn 2: Under 6 years
    turn2 = parse_employer_query("Under 6 years experience", existing_requirements=turn1)
    assert turn2.experience.min == 3.0
    assert turn2.experience.max == 6.0

    # Turn 3: Under 5 years (replaces max only)
    turn3 = parse_employer_query("Under 5 years experience", existing_requirements=turn2)
    assert turn3.experience.min == 3.0
    assert turn3.experience.max == 5.0


def test_employer_explicit_job_switch():
    """Verify that an explicit job query after employer candidate context switches target appropriately."""
    from backend.utils.utils import is_employer_hiring_query
    
    # Candidate query triggers employer hiring flow
    assert is_employer_hiring_query("Find Python candidates") is True

    # Career job search query without hiring keywords is blocked for employer or switches away from candidate search
    from backend.api.employer import check_employer_access
    def mock_is_career_query(msg):
        return "jobs" in msg.lower() or "hiring" in msg.lower()

    # Explicit job search query returns False for employer candidate hiring context (switches/blocks candidate pipeline)
    access_allowed = check_employer_access("Find Python jobs", session_id="test_employer_switch_session", is_career_query_func=mock_is_career_query)
    assert access_allowed is False
