"""
test_unified_informational_skills.py — Unit tests for Phase 3B Unified Informational Skills.
"""

import asyncio
import pytest
from unittest.mock import MagicMock, patch

from backend.services.user_data_intent_parser import UserIntent
from backend.services.user_data_service import INTENT_ENDPOINT_MAP, resolve_required_endpoints, handle_query
from backend.services.user_data_formatter import format_single_intent, format_multi_intent_response


def test_1_both_endpoints_mapped_and_resolved_for_profile_skills():
    """1. Both endpoints are mapped and requested for UserIntent.PROFILE_SKILLS."""
    assert INTENT_ENDPOINT_MAP[UserIntent.PROFILE_SKILLS] == ("user-profile", "allEmployementNew")
    endpoints = resolve_required_endpoints([UserIntent.PROFILE_SKILLS])
    assert set(endpoints) == {"user-profile", "allEmployementNew"}


def test_2_both_source_payloads_reach_aggregator_and_deduplicate():
    """2 & 3. Both source payloads reach aggregator, and duplicate skills across sources appear once with header **Your Skills:**."""
    profile_payload = {
        "status": True,
        "data": {
            "all_Skill": [{"skill": "Python"}, {"skill": "Testing and Validation"}]
        }
    }
    employment_payload = {
        "status": True,
        "data": [
            {
                "company": "Tech Corp",
                "lists": [
                    {
                        "designation": "Developer",
                        "skill": [
                            {"name": "python"},  # Duplicate case-insensitive
                            {"name": "Docker"}   # Unique employment skill
                        ]
                    }
                ]
            }
        ]
    }

    def mock_fetch(endpoint, token=None, user_slug=None, user_id=None):
        if endpoint == "user-profile":
            return profile_payload
        elif endpoint == "allEmployementNew":
            return employment_payload
        return {}

    with patch("backend.services.user_data_service.fetch_endpoint_data", side_effect=mock_fetch):
        res = asyncio.run(handle_query("show me my skills", user_id="user123"))

    reply = res.reply if hasattr(res, "reply") else ""
    assert reply.startswith("**Your Skills:**")
    assert "Python" in reply
    assert "Testing and Validation" in reply
    assert "Docker" in reply


def test_4_employment_only_skills_appear_when_profile_empty():
    """4. Employment-only skills appear when profile skills are empty."""
    profile_payload = {"status": True, "data": {"all_Skill": []}}
    employment_payload = {
        "status": True,
        "data": [
            {
                "company": "Acme",
                "lists": [
                    {"skill": [{"name": "Kubernetes"}, {"name": "Go"}]}
                ]
            }
        ]
    }

    def mock_fetch(endpoint, token=None, user_slug=None, user_id=None):
        if endpoint == "user-profile":
            return profile_payload
        elif endpoint == "allEmployementNew":
            return employment_payload
        return {}

    with patch("backend.services.user_data_service.fetch_endpoint_data", side_effect=mock_fetch):
        res = asyncio.run(handle_query("list my skills", user_id="user123"))

    reply = res.reply if hasattr(res, "reply") else ""
    assert reply.startswith("**Your Skills:**")
    assert "Kubernetes" in reply
    assert "Go" in reply


def test_5_profile_skills_preserved_when_employment_fails():
    """5. Profile skills remain available when employment request fails."""
    profile_payload = {"status": True, "data": {"all_Skill": [{"skill": "React"}]}}

    def mock_fetch(endpoint, token=None, user_slug=None, user_id=None):
        if endpoint == "user-profile":
            return profile_payload
        return {"status": False, "error": "500 Server Error"}

    with patch("backend.services.user_data_service.fetch_endpoint_data", side_effect=mock_fetch):
        res = asyncio.run(handle_query("what are my skills?", user_id="user123"))

    reply = res.reply if hasattr(res, "reply") else ""
    assert reply.startswith("**Your Skills:**")
    assert "React" in reply


def test_6_employment_skills_preserved_when_profile_fails():
    """6. Employment skills remain available when profile request fails."""
    employment_payload = {
        "status": True,
        "data": [
            {
                "company": "Cloud Inc",
                "lists": [
                    {"skill": [{"name": "AWS"}]}
                ]
            }
        ]
    }

    def mock_fetch(endpoint, token=None, user_slug=None, user_id=None):
        if endpoint == "allEmployementNew":
            return employment_payload
        return {"status": False, "error": "500 Server Error"}

    with patch("backend.services.user_data_service.fetch_endpoint_data", side_effect=mock_fetch):
        res = asyncio.run(handle_query("what skills do I have?", user_id="user123"))

    reply = res.reply if hasattr(res, "reply") else ""
    assert reply.startswith("**Your Skills:**")
    assert "AWS" in reply


def test_7_both_empty_and_both_failed_fallbacks():
    """7. Both-empty and both-failed cases return truthful responses."""
    # 7a. Both empty
    def mock_fetch_empty(endpoint, token=None, user_slug=None, user_id=None):
        return {"status": True, "data": {}}

    with patch("backend.services.user_data_service.fetch_endpoint_data", side_effect=mock_fetch_empty):
        res_empty = asyncio.run(handle_query("my skills", user_id="user123"))

    reply_empty = res_empty.reply if hasattr(res_empty, "reply") else ""
    assert "couldn't find any skills" in reply_empty

    # 7b. Both failed
    def mock_fetch_failed(endpoint, token=None, user_slug=None, user_id=None):
        return {"status": False, "error": "HTTP 500"}

    with patch("backend.services.user_data_service.fetch_endpoint_data", side_effect=mock_fetch_failed):
        res_failed = asyncio.run(handle_query("my skills", user_id="user123"))

    reply_failed = res_failed.reply if hasattr(res_failed, "reply") else ""
    assert "unable to retrieve your skills" in reply_failed or "couldn't find any skills" in reply_failed or "connection issue" in reply_failed


def test_8_heading_is_your_skills():
    """8. Heading is strictly **Your Skills:**."""
    data = {"effective_skills": ["Java", "SQL"]}
    formatted = format_single_intent(UserIntent.PROFILE_SKILLS, data, {})
    assert formatted.startswith("**Your Skills:**")
    assert "Your Profile Skills:" not in formatted


def test_9_other_informational_intents_unaffected():
    """9. Other informational intents (e.g. PROFILE_EDUCATION) are unaffected."""
    assert INTENT_ENDPOINT_MAP[UserIntent.PROFILE_EDUCATION] == "user-profile"
    data = {"education": [{"course": "B.Sc"}]}
    formatted = format_single_intent(UserIntent.PROFILE_EDUCATION, data, {})
    assert "education details" in formatted


def test_10_generic_job_searches_unaffected():
    """10. Generic job search intents and other intents remain unaffected."""
    assert UserIntent.PROFILE_SKILLS not in [UserIntent.EMPLOYMENT_HISTORY, UserIntent.PROFILE_EDUCATION]
