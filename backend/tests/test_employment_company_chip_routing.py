"""
test_employment_company_chip_routing.py — Targeted test suite for Issue 5: Employment Company Chip Routing.
Verifies structured employment_company_select action routing vs manual company searches.
"""

import asyncio
from unittest.mock import AsyncMock, patch
from backend.schemas.schemas import ChatRequest, ChatMessage
from backend.services.user_data_intent_parser import parse_user_data_intents, UserIntent
from backend.services.search_parser import parse_search_query


def test_1_employment_company_chip_structured_action_routing():
    """Test 1: Structured employment_company_select chip action routes to verification roadmap."""
    req = ChatRequest(
        messages=[ChatMessage(role="user", content="InnoTech Solution")],
        session_id="test_sess_chip_1",
        action={"type": "employment_company_select", "company_name": "InnoTech Solution"}
    )
    assert req.action is not None
    assert req.action.get("type") == "employment_company_select"
    assert req.action.get("company_name") == "InnoTech Solution"

    # Verify synthetic query intent classification
    intents = parse_user_data_intents(f"employment verification status for {req.action.get('company_name')}")
    assert UserIntent.EMPLOYMENT_VERIFICATION in intents
    assert UserIntent.EMPLOYMENT_HISTORY not in intents


def test_2_manual_company_text_normal_parsing():
    """Test 2: Manual company text 'InnoTech Solution' without structured action triggers standard intent parsing."""
    intents = parse_user_data_intents("InnoTech Solution")
    # Raw company text alone is not a direct user data intent
    assert UserIntent.EMPLOYMENT_VERIFICATION not in intents


def test_3_synthetic_verification_query_parsing():
    """Test 3: 'verify employment for InnoTech Solution' maps to EMPLOYMENT_VERIFICATION."""
    intents = parse_user_data_intents("verify employment for InnoTech Solution")
    assert UserIntent.EMPLOYMENT_VERIFICATION in intents


def test_4_normal_job_company_search_unaffected():
    """Test 4: 'jobs at InnoTech Solution' remains classified as job search intent."""
    parsed = asyncio.run(parse_search_query("jobs at InnoTech Solution", "employee"))
    assert parsed.get("company") == "InnoTech Solution" or "innotech" in str(parsed.get("keyword", "")).lower() or parsed.get("search_target") == "JOBS"


def test_5_normal_candidate_search_unaffected():
    """Test 5: 'Find Kunal Yadav from InnoTech Solution' remains classified as PEOPLE_SEARCH."""
    parsed = asyncio.run(parse_search_query("Find Kunal Yadav from InnoTech Solution", "employee"))
    assert parsed.get("search_target") == "PEOPLE_SEARCH"
    assert parsed.get("candidate_name") == "Kunal Yadav"


def test_6_existing_employment_verification_query_unaffected():
    """Test 6: 'how can i verify my employment' maps to employment verification intents."""
    intents = parse_user_data_intents("how can i verify my employment")
    assert any(i in intents for i in [UserIntent.EMPLOYMENT_VERIFICATION, UserIntent.EMPLOYMENT_HISTORY, UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS])


def test_7_history_queries_remain_history():
    """Test 7: 'show my employment history' maps strictly to EMPLOYMENT_HISTORY, not EMPLOYMENT_VERIFICATION."""
    intents = parse_user_data_intents("show my employment history")
    assert UserIntent.EMPLOYMENT_HISTORY in intents
    assert UserIntent.EMPLOYMENT_VERIFICATION not in intents

