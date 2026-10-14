"""
test_phase2_4_pipeline_integration.py — End-to-End Pipeline Integration Test Suite for Phase 2.4.

Covers:
- Test Group 1: Extraction routing across all verification intents.
- Test Group 2: Correct response rendering and no duplicate sections for 7 core queries.
- Test Group 3: Regex regression cases (salary, employment history, documents, etc.).
- Test Group 4: API endpoint deduplication & zero API call guarantee for definitions.
- Test Group 5: Normalized verification integrity & sequential Level 1-4 rules.
- Test Group 6: Multi-employment context isolation & explicit company selection.
"""

import pytest
from backend.services.user_data_intent_parser import parse_user_data_intents, UserIntent
from backend.services.user_data_service import resolve_required_endpoints, process_query_resolution
from backend.services.user_data_extractor import extract_by_intent, normalize_employment_verification, extract_employment_history
from backend.services.user_data_formatter import format_multi_intent_response, format_single_intent


# -----------------------------------------------------------------------------
# TEST GROUP 1: EXTRACTION ROUTING
# -----------------------------------------------------------------------------
def test_group1_extraction_routing():
    raw_api_data = {
        "status": True,
        "data": [{
            "company": "TechCorp",
            "is_verified": True,
            "lists": [{
                "designation": "Staff Engineer",
                "approved": "1",
                "status": "1",
                "still_working": "1",
                "verificationProcess": {"level1": True, "level2": True, "level3": True}
            }]
        }]
    }

    intents_to_test = [
        UserIntent.EMPLOYMENT_HISTORY,
        UserIntent.EMPLOYMENT_VERIFICATION,
        UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS,
        UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS,
        UserIntent.EMPLOYMENT_VERIFICATION_WHY_INCOMPLETE,
        UserIntent.EMPLOYMENT_VERIFICATION_OWNERSHIP
    ]

    for intent in intents_to_test:
        extracted = extract_by_intent(intent, raw_api_data)
        assert "employment_history" in extracted, f"Intent {intent} failed extraction routing"
        assert len(extracted["employment_history"]) == 1
        assert extracted["employment_history"][0]["company"] == "TechCorp"
        role_verif = extracted["employment_history"][0]["roles"][0]["verification"]
        assert role_verif["highest_completed_level"] == 3


# -----------------------------------------------------------------------------
# TEST GROUP 2: CORRECT RESPONSES & NO DUPLICATE SECTIONS
# -----------------------------------------------------------------------------
def test_group2_correct_responses_and_no_duplicates():
    raw_api_data = {
        "status": True,
        "data": [{
            "company": "Acme Systems",
            "is_verified": True,
            "lists": [{
                "designation": "Lead Developer",
                "approved": "0",
                "status": "1",
                "still_working": "1",
                "verificationProcess": {"level1": True, "level2": True}
            }]
        }]
    }

    test_queries = [
        ("What is my Level 4 status?", ["EMPLOYMENT_VERIFICATION"], "Highest Completed Level"),
        ("Why is my Level 3 pending?", ["EMPLOYMENT_VERIFICATION_WHY_INCOMPLETE"], "Verification Status Explanation"),
        ("Show my verification progress", ["EMPLOYMENT_VERIFICATION_PROGRESS"], "Level Breakdown"),
        ("What should I do next?", ["EMPLOYMENT_VERIFICATION_NEXT_STEPS"], "Next Verification Step"),
        ("Who needs to verify my employment?", ["EMPLOYMENT_VERIFICATION_OWNERSHIP"], "Verification Ownership"),
        ("How does salary verification work?", ["VERIFICATION_LEVEL_DEFINITION"], "Level 4 — Salary Verification"),
        ("What is Level 4 verification?", ["VERIFICATION_LEVEL_DEFINITION"], "Level 4 — Salary Verification")
    ]

    for query, expected_intents, expected_substring in test_queries:
        resolution = process_query_resolution(query, fetch_data=False)
        parsed_intents = [i.value for i in resolution.intents]
        for exp_i in expected_intents:
            assert exp_i in parsed_intents, f"Query '{query}' missing expected intent {exp_i}"

        extracted_map = {}
        for intent in resolution.intents:
            extracted_map[intent] = extract_by_intent(intent, raw_api_data)

        response = format_multi_intent_response(
            resolution.intents,
            extracted_map,
            {"allEmployementNew": raw_api_data},
            query=query
        )

        assert expected_substring in response, f"Query '{query}' response missing substring '{expected_substring}'"

        # Assert no duplicate header occurrences
        if "EMPLOYMENT_VERIFICATION" not in parsed_intents or len(parsed_intents) == 1:
            assert response.count("Here is your employment verification status") <= 1, f"Query '{query}' produced duplicate response headers"


# -----------------------------------------------------------------------------
# TEST GROUP 3: REGEX REGRESSION CASES
# -----------------------------------------------------------------------------
def test_group3_regex_regression():
    # 1. Show my remaining verification steps -> Verification progress
    i1 = parse_user_data_intents("Show my remaining verification steps")
    assert UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS in i1

    # 2. What verification steps remain? -> Verification progress
    i2 = parse_user_data_intents("What verification steps remain?")
    assert UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS in i2

    # 3. What levels remain in my verification? -> Verification progress
    i3 = parse_user_data_intents("What levels remain in my verification?")
    assert UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS in i3

    # 4. What is my remaining salary? -> Must NOT match verification progress
    i4 = parse_user_data_intents("What is my remaining salary?")
    assert UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS not in i4
    assert UserIntent.EMPLOYMENT_SALARY in i4

    # 5. Show my remaining employment records -> Must NOT match verification progress
    i5 = parse_user_data_intents("Show my remaining employment records")
    assert UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS not in i5
    assert UserIntent.EMPLOYMENT_HISTORY in i5

    # 6. What documents remain for Level 4? -> Documents intent
    i6 = parse_user_data_intents("What documents remain for Level 4?")
    assert UserIntent.EMPLOYMENT_DOCUMENTS in i6

    # 7. How many verification levels remain? -> Verification progress
    i7 = parse_user_data_intents("How many verification levels remain?")
    assert UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS in i7


# -----------------------------------------------------------------------------
# TEST GROUP 4: API ENDPOINT DEDUPLICATION & ZERO CALL GUARANTEE
# -----------------------------------------------------------------------------
def test_group4_api_deduplication():
    # Multi-intent query resolving to allEmployementNew
    res1 = process_query_resolution("Why is my Level 3 pending and what should I do next?", fetch_data=False)
    assert res1.required_endpoints == ["allEmployementNew"]

    # General definition queries -> 0 required endpoints
    res2 = process_query_resolution("How does salary verification work?", fetch_data=False)
    assert res2.required_endpoints == []

    res3 = process_query_resolution("What is Level 4 verification?", fetch_data=False)
    assert res3.required_endpoints == []


# -----------------------------------------------------------------------------
# TEST GROUP 5: NORMALIZED VERIFICATION INTEGRITY
# -----------------------------------------------------------------------------
def test_group5_normalized_verification_integrity():
    # Prerequisite failure: Level 4 flag active, Level 3 not approved -> drops to Level 2
    res_prereq = normalize_employment_verification("0", "1", {"level1": True, "level2": True, "level4": True}, True)
    assert res_prereq["highest_completed_level"] == 2
    assert res_prereq["levels_breakdown"]["level3"]["status"] == "Declined"
    assert res_prereq["levels_breakdown"]["level4"]["status"] == "Not Completed"

    # Missing evidence -> unknown status
    res_unknown = normalize_employment_verification(None, None, {}, None)
    assert res_unknown["status"] == "unknown"
    assert res_unknown["guidance_status"] == "unknown"

    # Company verified vs Role status
    res_comp_only = normalize_employment_verification("0", "1", {"level1": True, "level2": True}, True)
    assert res_comp_only["is_verified"] is False
    assert res_comp_only["highest_completed_level"] == 2
    assert res_comp_only["guidance_status"] == "declined"


# -----------------------------------------------------------------------------
# TEST GROUP 6: MULTI-EMPLOYMENT CONTEXT ISOLATION
# -----------------------------------------------------------------------------
def test_group6_multi_employment_isolation():
    mock_data = {
        "data": [
            {
                "company": "Alpha Corp",
                "is_verified": True,
                "lists": [{
                    "designation": "Principal Engineer",
                    "approved": "1",
                    "status": "1",
                    "still_working": "1",
                    "verificationProcess": {"level1": True, "level2": True, "level3": True}
                }]
            },
            {
                "company": "Beta Inc",
                "is_verified": False,
                "lists": [{
                    "designation": "Junior Developer",
                    "approved": "0",
                    "status": "1",
                    "still_working": "0",
                    "verificationProcess": {"level1": True}
                }]
            }
        ]
    }

    history = extract_employment_history(mock_data)
    extracted = {"employment_history": history}

    # Explicit selection for Beta Inc
    res_beta = format_single_intent(
        UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS,
        extracted,
        {},
        query="What is my next step for Beta Inc?"
    )
    assert "Beta Inc" in res_beta
    assert "Alpha Corp" not in res_beta

    # Current vs Previous company reference
    res_prev = format_single_intent(
        UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS,
        extracted,
        {},
        query="What is my next step for my previous company?"
    )
    assert "Beta Inc" in res_prev
    assert "Alpha Corp" not in res_prev
