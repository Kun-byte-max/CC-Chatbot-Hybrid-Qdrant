"""
test_user_skill_aggregator.py — Focused Unit Tests for Unified User Skill Aggregator (Phase 1)
"""

import pytest
from backend.services.user_skill_aggregator import (
    aggregate_user_skills,
    AggregatedSkillsResult,
    SkillProvenance,
    HIGH_CONFIDENCE_ALIAS_MAP,
)


def test_1_profile_only_skills():
    """1. Profile-only skills are aggregated correctly."""
    user_profile_data = {
        "status": True,
        "data": {
            "all_Skill": [
                {"id": "1", "skill": "Python", "rating": "5"},
                {"id": "2", "skill": "Django", "rating": "4"}
            ]
        }
    }
    res = aggregate_user_skills(user_profile_data=user_profile_data, employment_data=None)

    assert res.effective_skills == ["Python", "Django"]
    assert res.profile_status == "SUCCESS"
    assert res.employment_status == "NOT_FETCHED"
    assert len(res.provenance) == 2
    assert res.provenance[0].sources == ["PROFILE"]
    assert res.provenance[0].profile_details[0]["skill"] == "Python"


def test_2_employment_only_skills():
    """2. Employment-only skills are aggregated correctly."""
    employment_data = {
        "status": True,
        "data": [
            {
                "company": "TechCorp",
                "lists": [
                    {
                        "designation": "Backend Engineer",
                        "skill": [
                            {"id": 10, "name": "PostgreSQL"},
                            {"id": 11, "name": "Docker"}
                        ]
                    }
                ]
            }
        ]
    }
    res = aggregate_user_skills(user_profile_data=None, employment_data=employment_data)

    assert res.effective_skills == ["PostgreSQL", "Docker"]
    assert res.profile_status == "NOT_FETCHED"
    assert res.employment_status == "SUCCESS"
    assert res.provenance[0].sources == ["EMPLOYMENT"]
    assert res.provenance[0].employment_details[0]["company"] == "TechCorp"


def test_3_different_skills_both_sources():
    """3. Disjoint skills from both sources are combined."""
    user_profile = {"status": True, "data": {"all_Skill": [{"skill": "Python"}]}}
    employment = {
        "status": True,
        "data": [{"company": "Co", "lists": [{"skill": [{"name": "Java"}]}]}]
    }
    res = aggregate_user_skills(user_profile_data=user_profile, employment_data=employment)

    assert res.effective_skills == ["Python", "Java"]
    assert res.profile_status == "SUCCESS"
    assert res.employment_status == "SUCCESS"


def test_4_same_skill_both_sources_provenance():
    """4. Same skill across profile and employment is deduplicated and retains dual provenance."""
    user_profile = {"status": True, "data": {"all_Skill": [{"id": "1", "skill": "Python"}]}}
    employment = {
        "status": True,
        "data": [{"company": "PyCompany", "lists": [{"skill": [{"id": 9, "name": "Python"}]}]}]
    }
    res = aggregate_user_skills(user_profile_data=user_profile, employment_data=employment)

    assert res.effective_skills == ["Python"]
    assert len(res.provenance) == 1
    prov = res.provenance[0]
    assert prov.skill_name == "Python"
    assert prov.sources == ["PROFILE", "EMPLOYMENT"]
    assert len(prov.profile_details) == 1
    assert len(prov.employment_details) == 1


def test_5_case_insensitive_deduplication():
    """5. Skills differing only in casing are deduplicated case-insensitively."""
    user_profile = {"status": True, "data": {"all_Skill": [{"skill": "PYTHON"}, {"skill": "python"}]}}
    employment = {
        "status": True,
        "data": [{"company": "C", "lists": [{"skill": [{"name": "PyThOn"}]}]}]
    }
    res = aggregate_user_skills(user_profile_data=user_profile, employment_data=employment)

    assert res.effective_skills == ["PYTHON"]
    assert len(res.provenance) == 1
    assert set(res.provenance[0].raw_names) == {"PYTHON", "python", "PyThOn"}


def test_6_whitespace_normalization():
    """6. Leading/trailing whitespace is trimmed."""
    user_profile = {"status": True, "data": {"all_Skill": [{"skill": "  FastAPI  "}]}}
    res = aggregate_user_skills(user_profile_data=user_profile)

    assert res.effective_skills == ["FastAPI"]


def test_7_empty_and_malformed_skills_rejected():
    """7. Invalid, empty, or placeholder skill names are discarded."""
    user_profile = {
        "status": True,
        "data": {
            "all_Skill": [
                {"skill": ""},
                {"skill": "   "},
                {"skill": None},
                {"skill": "none"},
                {"skill": "null"},
                {"skill": "ValidSkill"}
            ]
        }
    }
    res = aggregate_user_skills(user_profile_data=user_profile)

    assert res.effective_skills == ["ValidSkill"]


def test_8_multiple_employment_records():
    """8. Multiple companies and multiple roles extract skills correctly."""
    employment = {
        "status": True,
        "data": [
            {
                "company": "Company A",
                "lists": [
                    {"designation": "Role 1", "skill": [{"name": "C++"}]},
                    {"designation": "Role 2", "skill": [{"name": "Linux"}]}
                ]
            },
            {
                "company": "Company B",
                "lists": [
                    {"designation": "Role 3", "skill": [{"name": "Kubernetes"}]}
                ]
            }
        ]
    }
    res = aggregate_user_skills(employment_data=employment)

    assert res.effective_skills == ["C++", "Linux", "Kubernetes"]


def test_9_provenance_detail_retention():
    """9. Provenance holds role designation and company metadata."""
    employment = {
        "status": True,
        "data": [
            {
                "company": "Alpha Inc",
                "lists": [
                    {"designation": "Senior Dev", "skill": [{"id": 50, "name": "Rust"}]}
                ]
            }
        ]
    }
    res = aggregate_user_skills(employment_data=employment)

    prov = res.provenance[0]
    assert prov.skill_name == "Rust"
    assert prov.employment_details[0]["company"] == "Alpha Inc"
    assert prov.employment_details[0]["designation"] == "Senior Dev"


def test_10_original_extractor_results_unmodified():
    """10. Input payloads and extracted result lists are not mutated."""
    user_profile = {"status": True, "data": {"all_Skill": [{"skill": "Go"}]}}
    original_copy = dict(user_profile)

    res = aggregate_user_skills(user_profile_data=user_profile)

    assert user_profile == original_copy
    assert res.profile_skills == [{"id": None, "skill": "Go", "rating": None}]


def test_11_successful_empty_responses():
    """11. Valid API responses with 0 skills result in EMPTY status."""
    user_profile = {"status": True, "data": {"all_Skill": []}}
    employment = {"status": True, "data": []}

    res = aggregate_user_skills(user_profile_data=user_profile, employment_data=employment)

    assert res.effective_skills == []
    assert res.profile_status == "EMPTY"
    assert res.employment_status == "EMPTY"


def test_12_source_failures_represented_independently():
    """12. Failed API responses are represented as FAILED status independently."""
    user_profile = {"status": False, "error": "HTTP 500 Server Error"}
    employment = {
        "status": True,
        "data": [{"company": "Co", "lists": [{"skill": [{"name": "Python"}]}]}]
    }

    res = aggregate_user_skills(user_profile_data=user_profile, employment_data=employment)

    assert res.effective_skills == ["Python"]
    assert res.profile_status == "FAILED"
    assert res.employment_status == "SUCCESS"


def test_13_unverified_employment_skills_retained():
    """13. Skills from unverified employment records are retained."""
    employment = {
        "status": True,
        "data": [
            {
                "company": "Unverified Startup",
                "is_verified": False,
                "lists": [
                    {
                        "designation": "Intern",
                        "approved": "0",
                        "status": "0",
                        "skill": [{"id": 99, "name": "GraphQL"}]
                    }
                ]
            }
        ]
    }

    res = aggregate_user_skills(employment_data=employment)

    assert res.effective_skills == ["GraphQL"]
    assert res.provenance[0].employment_details[0]["company"] == "Unverified Startup"


def test_14_alias_mapping_high_confidence():
    """14. Alias mapping converts recognized skill variants to canonical names."""
    user_profile = {
        "status": True,
        "data": {
            "all_Skill": [
                {"skill": "react.js"},
                {"skill": "Postgres"},
                {"skill": "JS"}
            ]
        }
    }

    res = aggregate_user_skills(user_profile_data=user_profile)

    assert res.effective_skills == ["React", "PostgreSQL", "JavaScript"]
    assert res.provenance[0].raw_names == ["react.js"]
    assert res.provenance[0].skill_name == "React"
