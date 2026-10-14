import pytest
from typing import List, Dict, Any

from backend.services.user_data_extractor import parse_employment_skills_raw, aggregate_user_skills


def test_1_profile_and_employment_skills():
    """Test 1 — profile + employment skills aggregation and order preservation."""
    profile_skills = ["Python", "SQL"]
    employment = [
        '[{"id": 18051, "name": "Python"}, {"id": 19390, "name": "Data Analysis"}]'
    ]

    result = aggregate_user_skills(profile_skills, employment)
    assert result == ["Python", "SQL", "Data Analysis"]


def test_2_employment_skills_only():
    """Test 2 — employment skills only when profile skills are empty."""
    profile_skills: List[str] = []
    employment = [
        '[{"id": 1, "name": "Docker"}, {"id": 2, "name": "Kubernetes"}]'
    ]

    result = aggregate_user_skills(profile_skills, employment)
    assert result == ["Docker", "Kubernetes"]


def test_3_duplicate_skills_case_insensitive():
    """Test 3 — case-insensitive deduplication preserving first display casing."""
    profile_skills = ["Python"]
    employment = [
        '[{"id": 1, "name": "python"}, {"id": 2, "name": "PYTHON"}]'
    ]

    result = aggregate_user_skills(profile_skills, employment)
    assert result == ["Python"]


def test_4_malformed_employment_json():
    """Test 4 — malformed employment JSON handled safely without crash."""
    profile_skills = ["FastAPI"]
    employment = [
        'INVALID_JSON_{{',
        None,
        12345,
        '[{"name": "Valid Skill"}]'
    ]

    result = aggregate_user_skills(profile_skills, employment)
    assert result == ["FastAPI", "Valid Skill"]


def test_5_empty_or_null_employment_skills():
    """Test 5 — empty/null employment skills leave profile skills unchanged."""
    profile_skills = ["Go", "Docker"]
    employment: List[Any] = [None, "", "[]", "   "]

    result = aggregate_user_skills(profile_skills, employment)
    assert result == ["Go", "Docker"]


def test_6_multiple_employment_records():
    """Test 6 — multiple employment records aggregated and deduplicated."""
    profile_skills = ["Python"]
    employment = [
        '[{"id": 1, "name": "Python"}, {"id": 2, "name": "Django"}]',
        '[{"id": 3, "name": "React"}, {"id": 4, "name": "Python"}]'
    ]

    result = aggregate_user_skills(profile_skills, employment)
    assert result == ["Python", "Django", "React"]


def test_7_user_isolation():
    """Test 7 — pure function user isolation guarantees skills from another user do not mix."""
    user1_profile = ["Python"]
    user1_emp = ['[{"name": "Django"}]']

    user2_profile = ["Java"]
    user2_emp = ['[{"name": "Spring Boot"}]']

    user1_res = aggregate_user_skills(user1_profile, user1_emp)
    user2_res = aggregate_user_skills(user2_profile, user2_emp)

    assert user1_res == ["Python", "Django"]
    assert user2_res == ["Java", "Spring Boot"]
    assert "Java" not in user1_res
    assert "Django" not in user2_res
