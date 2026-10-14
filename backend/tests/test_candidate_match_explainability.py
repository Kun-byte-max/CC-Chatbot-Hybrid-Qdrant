"""
test_candidate_match_explainability.py — Regression Test Suite for Candidate Search Match Explainability
"""

import pytest
from unittest.mock import patch
from backend.schemas.search_criteria import SearchCriteria
from backend.repositories.people_repository import PeopleRepository
from backend.services.people_search_service import PeopleSearchService


def test_matched_skill_is_exposed_in_backend_contract():
    """
    Test 1 — Matched skill is exposed in response contract.
    Given requested skill = Next.js and candidate skills = [Testing, WordPress, Photoshop, Figma, Redux, Next.js],
    assert response candidate card contains matched_skills == ["Next.js"] and match_score == 100.0.
    """
    mock_people = [
        {
            "id": 41940,
            "name": "Yash Sharma",
            "fname": "Yash",
            "lname": "Sharma",
            "current_title": "Professional",
            "company": "CollarCheck",
            "location": "Delhi, Delhi",
            "city": "Delhi",
            "state": "Delhi",
            "country": "India",
            "skills": ["Testing", "WordPress", "Photoshop", "Figma", "Redux", "Next.js"],
            "total_experience_years": 3.0,
            "profile": None,
        }
    ]

    criteria = SearchCriteria(
        skills=["Next.js"],
        search_target="PEOPLE_SEARCH"
    )

    with patch.object(PeopleRepository, "search_people", return_value=mock_people):
        res = PeopleSearchService.search_people(criteria=criteria)

        assert res["success"] is True
        cards = res["results"]
        assert len(cards) == 1
        card = cards[0]

        assert card["matched_skills"] == ["Next.js"]
        assert card["match_score"] == 100.0


def test_full_profile_skills_remain_intact():
    """
    Test 2 — Full profile skills remain intact and untruncated in backend payload.
    """
    mock_skills = ["Testing", "WordPress", "Photoshop", "Figma", "Redux", "Next.js", "Docker", "AWS"]
    mock_people = [
        {
            "id": 41940,
            "name": "Yash Sharma",
            "fname": "Yash",
            "lname": "Sharma",
            "current_title": "Professional",
            "company": "CollarCheck",
            "location": "Delhi",
            "skills": list(mock_skills),
            "total_experience_years": 3.0,
        }
    ]

    criteria = SearchCriteria(skills=["Next.js"], search_target="PEOPLE_SEARCH")

    with patch.object(PeopleRepository, "search_people", return_value=mock_people):
        res = PeopleSearchService.search_people(criteria=criteria)
        card = res["results"][0]

        # Ensure Next.js is in skills and length is unchanged (untruncated in backend)
        assert "Next.js" in card["skills"]
        assert card["skills"] == mock_skills
        assert len(card["skills"]) == 8


def test_multiple_matched_skills_exposed():
    """
    Test 3 — Multiple matched skills are exposed in matched_skills list.
    Given requested skills = ["Python", "Next.js"], both are in candidate skills.
    """
    mock_people = [
        {
            "id": 41031,
            "name": "Anonymous User",
            "current_title": "Fullstack Developer",
            "company": "CollarCheck",
            "location": "Delhi",
            "skills": ["Python", "React", "Next.js", "FastAPI", "PostgreSQL"],
            "total_experience_years": 4.0,
        }
    ]

    criteria = SearchCriteria(skills=["Python", "Next.js"], search_target="PEOPLE_SEARCH")

    with patch.object(PeopleRepository, "search_people", return_value=mock_people):
        res = PeopleSearchService.search_people(criteria=criteria)
        card = res["results"][0]

        assert set(card["matched_skills"]) == {"Python", "Next.js"}
        assert card["match_score"] == 100.0


def test_no_false_matched_skills():
    """
    Test 4 — For a candidate that does not match requested skills, matched_skills is empty.
    """
    mock_people = [
        {
            "id": 40123,
            "name": "Dev User",
            "current_title": "QA Engineer",
            "company": "Testing Inc",
            "location": "Delhi",
            "skills": ["Selenium", "Jira", "Manual Testing"],
            "total_experience_years": 2.0,
        }
    ]

    # Search title = "QA" with skill requested = "Python"
    criteria = SearchCriteria(title="QA", skills=["Python"], search_target="PEOPLE_SEARCH")

    with patch.object(PeopleRepository, "search_people", return_value=mock_people):
        res = PeopleSearchService.search_people(criteria=criteria)
        card = res["results"][0]

        assert card["matched_skills"] == []
