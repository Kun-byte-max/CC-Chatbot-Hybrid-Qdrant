"""
test_employment_roadmap_ux.py — Targeted regression suite for read-only Employment Verification Roadmap UX.
"""

import pytest
from backend.services.user_data_intent_parser import parse_user_data_intents, UserIntent
from backend.services.user_data_formatter import format_single_intent


def test_verification_roadmap_query_intent():
    """Case A: 'How can I verify my employment?' triggers EMPLOYMENT_VERIFICATION / EMPLOYMENT_VERIFICATION_PROGRESS."""
    intents = parse_user_data_intents("How can I verify my employment?")
    assert (
        UserIntent.EMPLOYMENT_VERIFICATION in intents or
        UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS in intents or
        UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS in intents
    )
    # Confirm no candidate or job search routing
    assert UserIntent.PROFILE_SKILLS not in intents


def test_selected_claimed_company_level1_roadmap():
    """Case B & C: Selected claimed company at Level 1 shows Claimed status and Level 2 next level."""
    data = {
        "employment_history": [
            {
                "company": "InnoTech Solution",
                "is_verified": True,
                "roles": [
                    {
                        "designation": "Software Analyst",
                        "approved": "0",
                        "status": "0",
                        "verificationProcess": {"level1": True},
                        "verification": {
                            "level": 1,
                            "highest_completed_label": "Work Identity",
                            "next_level": 2,
                            "next_level_label": "Company Domain Verification",
                            "next_action_owner": "company",
                            "next_action": "Your company needs to join CollarCheck and verify its company domain.",
                            "levels_breakdown": {
                                "level1": {"status": "Completed"},
                                "level2": {"status": "Not Completed"},
                                "level3": {"status": "Not Completed"},
                                "level4": {"status": "Not Completed"}
                            }
                        }
                    }
                ]
            }
        ]
    }
    res = format_single_intent(
        UserIntent.EMPLOYMENT_VERIFICATION, data, None, query="verify employment for InnoTech Solution"
    )
    assert "**Company Status:** ✅ Claimed on CollarCheck" in res
    assert "Level 1 (Work Identity): Completed" in res
    assert "• **Highest Completed Level:** Level 1 (Work Identity)" in res
    assert "• **Next Verification Level:** Level 2 (Company Domain Verification)" in res
    assert "• **Action Required By:** Company / Employer" in res


def test_selected_claimed_company_level2_roadmap():
    """Case D: Selected claimed company at Level 2 shows next level = Level 3 (Employment Verification)."""
    data = {
        "employment_history": [
            {
                "company": "InnoTech Solution",
                "is_verified": True,
                "roles": [
                    {
                        "designation": "Software Analyst",
                        "approved": "0",
                        "status": "0",
                        "verificationProcess": {"level1": True, "level2": True},
                        "verification": {
                            "level": 2,
                            "highest_completed_label": "Company Domain Verification",
                            "next_level": 3,
                            "next_level_label": "Employment Verification",
                            "next_action_owner": "company",
                            "next_action": "Your employer needs to confirm your employment and designation.",
                            "levels_breakdown": {
                                "level1": {"status": "Completed"},
                                "level2": {"status": "Completed"},
                                "level3": {"status": "Not Completed"},
                                "level4": {"status": "Not Completed"}
                            }
                        }
                    }
                ]
            }
        ]
    }
    res = format_single_intent(
        UserIntent.EMPLOYMENT_VERIFICATION, data, None, query="verify employment for InnoTech Solution"
    )
    assert "**Company Status:** ✅ Claimed on CollarCheck" in res
    assert "• **Highest Completed Level:** Level 2 (Company Domain Verification)" in res
    assert "• **Next Verification Level:** Level 3 (Employment Verification)" in res


def test_selected_claimed_company_level3_roadmap():
    """Case E: Selected claimed company at Level 3 shows next level = Level 4 (Salary Verification)."""
    data = {
        "employment_history": [
            {
                "company": "InnoTech Solution",
                "is_verified": True,
                "roles": [
                    {
                        "designation": "Software Analyst",
                        "approved": "1",
                        "status": "1",
                        "verificationProcess": {"level1": True, "level2": True, "level3": True},
                        "verification": {
                            "level": 3,
                            "highest_completed_label": "Employment Verification",
                            "next_level": 4,
                            "next_level_label": "Salary Verification",
                            "next_action_owner": "employee",
                            "next_action": "Provide verified salary evidence for this role.",
                            "levels_breakdown": {
                                "level1": {"status": "Completed"},
                                "level2": {"status": "Completed"},
                                "level3": {"status": "Completed"},
                                "level4": {"status": "Not Completed"}
                            }
                        }
                    }
                ]
            }
        ]
    }
    res = format_single_intent(
        UserIntent.EMPLOYMENT_VERIFICATION, data, None, query="verify employment for InnoTech Solution"
    )
    assert "• **Highest Completed Level:** Level 3 (Employment Verification)" in res
    assert "• **Next Verification Level:** Level 4 (Salary Verification)" in res
    assert "• **Action Required By:** Employee (You)" in res


def test_selected_claimed_company_level4_roadmap():
    """Case F: Selected claimed company at Level 4 shows fully verified with no next level."""
    data = {
        "employment_history": [
            {
                "company": "InnoTech Solution",
                "is_verified": True,
                "roles": [
                    {
                        "designation": "Software Analyst",
                        "approved": "1",
                        "status": "1",
                        "verificationProcess": {"level1": True, "level2": True, "level3": True, "level4": True},
                        "verification": {
                            "level": 4,
                            "highest_completed_label": "Salary Verification",
                            "next_level": None,
                            "next_action_owner": "none",
                            "next_action": "All verification levels (Level 1–4) are completed for this employment record.",
                            "levels_breakdown": {
                                "level1": {"status": "Completed"},
                                "level2": {"status": "Completed"},
                                "level3": {"status": "Completed"},
                                "level4": {"status": "Completed"}
                            }
                        }
                    }
                ]
            }
        ]
    }
    res = format_single_intent(
        UserIntent.EMPLOYMENT_VERIFICATION, data, None, query="verify employment for InnoTech Solution"
    )
    assert "🎉 **Employment Fully Verified (All Levels Completed)**" in res
    assert "Next Verification Level" not in res


def test_selected_non_claimed_company_roadmap_prerequisite():
    """Case G: Selected non-claimed company pauses roadmap and shows company registration prerequisite."""
    data = {
        "employment_history": [
            {
                "company": "Legacy Corp",
                "is_verified": False,
                "roles": [
                    {
                        "designation": "Developer",
                        "approved": "0",
                        "status": "0",
                        "verification": {
                            "level": 1,
                            "highest_completed_label": "Work Identity",
                            "next_level": 2,
                            "levels_breakdown": {
                                "level1": {"status": "Completed"},
                                "level2": {"status": "Not Completed"}
                            }
                        }
                    }
                ]
            }
        ]
    }
    res = format_single_intent(
        UserIntent.EMPLOYMENT_VERIFICATION, data, None, query="verify employment for Legacy Corp"
    )
    assert "**Company Status:** ❌ Not Claimed on CollarCheck" in res
    assert "⚠️ **Verification Roadmap Paused:**" in res
    assert "has not registered or claimed its company domain on CollarCheck" in res
    assert "**Next Prerequisite:**" in res
    assert "Ask **Legacy Corp** to register/claim the company domain" in res
    assert "Level 2 (Company Domain Verification)" not in res  # Does NOT render misleading normal level 2 progression line


def test_unknown_company_selection_response():
    """Case H: Unknown company selection returns clean not-found message."""
    data = {
        "employment_history": [
            {"company": "InnoTech Solution", "is_verified": True, "roles": []}
        ]
    }
    res = format_single_intent(
        UserIntent.EMPLOYMENT_VERIFICATION, data, None, query="verify employment for GhostCorp"
    )
    assert "I couldn't find an employment record for 'GhostCorp'." in res


def test_direct_claimed_status_query_still_works():
    """Case L: Direct 'Is InnoTech Solution claimed?' query still returns standalone claimed statement."""
    data = {
        "employment_history": [
            {"company": "InnoTech Solution", "is_verified": True, "roles": []}
        ]
    }
    res = format_single_intent(
        UserIntent.EMPLOYMENT_COMPANY_CLAIMED, data, None, query="Is InnoTech Solution claimed?"
    )
    assert "**InnoTech Solution** is a claimed company." in res
    assert "Verification Roadmap" not in res


def test_handle_query_results_payload_filtering_initial_vs_selected():
    """Verify handle_query returns all companies for initial request and ONLY selected company for company selection."""
    import asyncio
    from backend.services import user_data_service

    async def run_async_test():
        mock_history_payload = {
            "data": [
                {
                    "company": "InnoTech Solution",
                    "is_verified": True,
                    "lists": [
                        {
                            "designation": "Software Analyst",
                            "approved": "0",
                            "status": "0",
                            "verificationProcess": {"level1": True, "level2": True}
                        },
                        {
                            "designation": "Senior Analyst",
                            "approved": "1",
                            "status": "1",
                            "verificationProcess": {"level1": True, "level2": True, "level3": True}
                        }
                    ]
                },
                {
                    "company": "Legacy Corp",
                    "is_verified": False,
                    "lists": [
                        {
                            "designation": "Developer",
                            "approved": "0",
                            "status": "0",
                            "verificationProcess": {"level1": True}
                        }
                    ]
                }
            ]
        }

        from unittest.mock import patch
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=mock_history_payload):
            # 1. Initial query: returns ALL companies (InnoTech Solution + Legacy Corp)
            res_initial = await user_data_service.handle_query("how can i verify my employment", user_id=123)
            comps_initial = [r["company"] for r in res_initial.results_payload]
            assert "InnoTech Solution" in comps_initial
            assert "Legacy Corp" in comps_initial
            assert len(comps_initial) == 3  # 2 roles in InnoTech + 1 role in Legacy

            # 2. Selected InnoTech Solution chip query: returns ONLY InnoTech Solution (both roles), excludes Legacy Corp
            res_selected = await user_data_service.handle_query("verify employment for InnoTech Solution", user_id=123)
            comps_selected = [r["company"] for r in res_selected.results_payload]
            assert all(c == "InnoTech Solution" for c in comps_selected)
            assert len(comps_selected) == 2  # Both InnoTech roles included
            assert "Legacy Corp" not in comps_selected

            # 3. Selected Legacy Corp chip query: returns ONLY Legacy Corp, excludes InnoTech Solution
            res_legacy = await user_data_service.handle_query("verify employment for Legacy Corp", user_id=123)
            comps_legacy = [r["company"] for r in res_legacy.results_payload]
            assert all(c == "Legacy Corp" for c in comps_legacy)
            assert len(comps_legacy) == 1
            assert "InnoTech Solution" not in comps_legacy

            # 4. Unknown company query: returns empty results payload, does NOT fall back to all companies
            res_unknown = await user_data_service.handle_query("verify employment for UnknownCorp", user_id=123)
            assert res_unknown.results_payload == []

    asyncio.run(run_async_test())

