"""
test_people_scoring_engine_phase5.py — Dedicated Unit Test Suite for Candidate Relevance & Ranking Engine (Phase 5).
"""

import pytest
from backend.services.people_scoring_engine import EmployeePeopleScoringEngine, people_rank_key


def test_match_name_tiers():
    """Verify name matching scoring tiers."""
    # 1. Exact match -> 100.0
    assert EmployeePeopleScoringEngine.match_name("John Doe", "John Doe") == 100.0
    assert EmployeePeopleScoringEngine.match_name("john doe", "John Doe") == 100.0

    # 2. Reordered multi-token match -> 95.0
    assert EmployeePeopleScoringEngine.match_name("John Doe", "Doe John") == 95.0
    assert EmployeePeopleScoringEngine.match_name("John John Doe", "John Doe") == 95.0

    # 3. Exact single-token / first / last name match -> 90.0
    assert EmployeePeopleScoringEngine.match_name("John", "John Doe") == 90.0
    assert EmployeePeopleScoringEngine.match_name("Doe", "John Doe") == 90.0

    # 4. Partial token match (1/2 tokens match) -> 50.0
    assert EmployeePeopleScoringEngine.match_name("John Doe", "John Smith") == 50.0
    assert EmployeePeopleScoringEngine.match_name("John Doe", "Alice Doe") == 50.0

    # 5. Token + Initial/Prefix match -> 70.0
    assert EmployeePeopleScoringEngine.match_name("John Doe", "John D") == 70.0

    # 6. Single-token prefix -> 60.0
    assert EmployeePeopleScoringEngine.match_name("John", "Johnathan") == 60.0

    # 7. Unmatched -> 0.0
    assert EmployeePeopleScoringEngine.match_name("John Doe", "Johnathan Brown") == 0.0
    assert EmployeePeopleScoringEngine.match_name("John Doe", "Alice Smith") == 0.0

    # 8. Not requested -> 100.0
    assert EmployeePeopleScoringEngine.match_name(None, "John Doe") == 100.0
    assert EmployeePeopleScoringEngine.match_name("", "John Doe") == 100.0


def test_match_company_normalization():
    """Verify company matching with normalization."""
    # Exact normalized match
    assert EmployeePeopleScoringEngine.match_company("Collar Check Pvt Ltd", "Collarcheck") == 100.0
    assert EmployeePeopleScoringEngine.match_company("Tech Corp", "tech corp") == 100.0

    # Partial match
    assert EmployeePeopleScoringEngine.match_company("Tech Corp", "Tech Corp India") == 80.0

    # Unmatched
    assert EmployeePeopleScoringEngine.match_company("Google", "Microsoft") == 0.0

    # Not requested
    assert EmployeePeopleScoringEngine.match_company(None, "Google") == 100.0


def test_active_weight_denominator_scaling():
    """Verify dynamic active signal denominator scaling."""
    candidate = {
        "id": 1,
        "name": "John Doe",
        "current_title": "Senior Python Developer",
        "company": "Tech Corp",
        "city": "Mumbai",
        "state": "Maharashtra",
        "skills": ["Python", "FastAPI"],
        "total_experience_years": 5.0,
    }

    # 1. Title only requested
    res1 = EmployeePeopleScoringEngine.score_candidate(candidate, requested_title="Python Developer")
    assert res1["name_score"] == 100.0
    assert res1["title_score"] == 85.0  # Related title
    assert res1["final_score"] == 85.0

    # 2. Title + Skills
    res2 = EmployeePeopleScoringEngine.score_candidate(
        candidate, requested_title="Python Developer", requested_skills=["Python"]
    )
    # Title 85 (0.25) + Skill 100 (0.25) / 0.50 = 92.5
    assert res2["final_score"] == 92.5

    # 3. Name + Title + Skills + Company
    # candidate.name="John Doe", requested_name="John" -> Name score is 90.0 (exact single token match)
    # Name 90 (0.25) + Title 85 (0.25) + Skill 100 (0.25) + Company 100 (0.15) / 0.90 = 83.75 / 0.90 = 93.06
    res3 = EmployeePeopleScoringEngine.score_candidate(
        candidate,
        requested_name="John",
        requested_title="Python Developer",
        requested_skills=["Python"],
        requested_company="Tech Corp",
    )
    assert res3["name_score"] == 90.0
    assert res3["title_score"] == 85.0
    assert res3["skill_score"] == 100.0
    assert res3["company_score"] == 100.0
    assert res3["final_score"] == 93.06


def test_seniority_modifier_capping():
    """Verify seniority modifier bounded cap at 100.0."""
    candidate = {
        "id": 1,
        "name": "John Doe",
        "current_title": "Senior Engineer",
        "company": "Tech Corp",
        "skills": ["Python"],
        "total_experience_years": 5.0,
    }

    res = EmployeePeopleScoringEngine.score_candidate(
        candidate, requested_title="Senior Engineer", requested_seniority="senior"
    )
    assert res["seniority_score"] == 15.0
    assert res["final_score"] == 100.0  # Bounded at 100.0


def test_people_rank_key_deterministic_sorting():
    """Verify people_rank_key hierarchy and descending ID tie-breaker."""
    cand1 = {"id": 101, "name": "John Doe"}
    cand2 = {"id": 202, "name": "Jane Smith"}

    scoring1 = {"is_eligible": True, "final_score": 90.0, "name_score": 90.0}
    scoring2 = {"is_eligible": True, "final_score": 90.0, "name_score": 90.0}

    # Higher candidate ID (202) should come before 101 in sorted list
    pairs = [(cand1, scoring1), (cand2, scoring2)]
    pairs.sort(key=lambda p: people_rank_key(p[0], p[1]))

    assert pairs[0][0]["id"] == 202  # 202 ranks above 101 when scores are equal
    assert pairs[1][0]["id"] == 101
