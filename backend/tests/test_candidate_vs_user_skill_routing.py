"""
test_candidate_vs_user_skill_routing.py — Dedicated Routing Regression Test Suite
Verifies intent classification distinguishing third-person candidate queries from self-profile queries.
"""

import pytest
from backend.services.user_data_intent_parser import parse_user_data_intents, UserIntent


def test_self_directed_profile_skill_queries():
    """Verify explicit self-directed profile queries route to PROFILE_SKILLS."""
    self_queries = [
        "What are my skills?",
        "Show my skills",
        "show me my skills",
        "what are my Java skills",
        "show my programming skills",
        "What skills do I have?",
        "recommend skills for my profile",
        "suggest skills for my resume",
        "Show my Next.js skill",
        "What skills are on my profile?",
        "Tell me about my profile skills",
        "my skills",
        "skills"
    ]
    for q in self_queries:
        intents = parse_user_data_intents(q)
        assert UserIntent.PROFILE_SKILLS in intents, f"Expected PROFILE_SKILLS for '{q}', got {intents}"


def test_third_person_candidate_skill_queries_bypass_user_data():
    """Verify third-person candidate queries containing 'skill' or tech names bypass user-data parser (return [])."""
    candidate_queries = [
        "suggest me candidates with java as a programming skill",
        "suggest me candidtes with java asa programming skill",
        "recommend me candidates with Java",
        "recommend candidates with Python",
        "find me developers with React",
        "show me candidates with Next.js",
        "give me candidates with Java",
        "get me candidates with Python",
        "list me candidates with React",
        "show me employees with SQL",
        "find me engineers with Java",
        "give me employees with python as a skill",
        "give me employees wth python as a skill",
        "give me employees with Python experience",
        "find employees with Python skills",
        "employees with Python",
        "employees skilled in Python",
        "employees who know Python",
        "candidates with nextjs as a skill",
        "developers skilled in React",
        "people with Python experience",
        "who has Python experience?",
        "find someone who knows Node.js",
        "profiles with React experience",
        "candidates with Python as a skill",
        "professionals having Java experience",
        "engineers with Java experience",
        "who knows python",
        "give me candidates with React",
        "show me developers with Node.js experience",
        "find me people with Node.js experience",
        "send me candidates with Java",
        "get me engineers with Python"
    ]
    for q in candidate_queries:
        intents = parse_user_data_intents(q)
        assert intents == [], f"Expected empty intents for candidate query '{q}', got {intents}"


def test_job_search_for_me_does_not_route_to_people_search_or_profile_skills():
    """Verify 'find developer jobs for me' does NOT route to PROFILE_SKILLS or candidate search."""
    q = "find developer jobs for me"
    intents = parse_user_data_intents(q)
    assert UserIntent.PROFILE_SKILLS not in intents, f"Did not expect PROFILE_SKILLS for job search query '{q}', got {intents}"


def test_ambiguous_skill_queries_preserves_clean_behavior():
    """Verify ambiguous skill questions behave predictably without false profile matching."""
    q = "What skills are required?"
    intents = parse_user_data_intents(q)
    assert UserIntent.PROFILE_SKILLS not in intents
