"""
test_phase3_6b_employment_selection.py — Unit, integration, security and contract tests for Phase 3.6B Multiple Employment Selection & Context Management.
"""

import asyncio
import pytest
from pathlib import Path

from backend.services.user_data_intent_parser import UserIntent, parse_user_data_intents
from backend.services.user_data_extractor import extract_employment_history
from backend.services.user_data_formatter import (
    format_single_intent,
    format_multi_intent_response,
    resolve_employment_selection_context,
    build_selection_metadata
)
from backend.services.user_data_service import handle_query, ServiceResponse
from backend.schemas.schemas import ChatResponse
from backend.utils.session_state import (
    get_selected_employment_context,
    set_selected_employment_context,
    clear_selected_employment_context,
    get_session_state
)


@pytest.fixture
def frontend_html_content() -> str:
    html_path = Path(__file__).parent.parent.parent / "frontend" / "index.html"
    assert html_path.exists(), "frontend/index.html must exist"
    return html_path.read_text(encoding="utf-8")


class TestPhase36BScenarioSelectionAndContext:
    """Tests covering all 10 Phase 3.6B scenarios."""

    @pytest.fixture
    def multi_emp_history(self):
        return [
            {
                "id": "101",
                "company": "Google",
                "is_verified": True,
                "roles": [{
                    "designation": "Software Engineer",
                    "approved": "1",
                    "status": "1",
                    "still_working": "1",
                    "verification": {
                        "level": 3,
                        "highest_completed_label": "Level 3 (Employment Verification)",
                        "next_level": 4,
                        "next_level_label": "Level 4 (Salary Verification)",
                        "next_action_owner": "employee",
                        "next_action": "Upload salary proof",
                        "incomplete_reason": "Salary verification evidence pending.",
                        "guidance_status": "action_required",
                        "levels_breakdown": {
                            "level1": {"status": "Completed"},
                            "level2": {"status": "Completed"},
                            "level3": {"status": "Completed"},
                            "level4": {"status": "Pending"}
                        }
                    }
                }]
            },
            {
                "id": "102",
                "company": "Microsoft",
                "is_verified": True,
                "roles": [{
                    "designation": "Backend Developer",
                    "approved": None,
                    "status": "1",
                    "still_working": "0",
                    "worked_till_date": "2023-05-01",
                    "verification": {
                        "level": 2,
                        "highest_completed_label": "Level 2 (Company Domain Verification)",
                        "next_level": 3,
                        "next_level_label": "Level 3 (Employment Verification)",
                        "next_action_owner": "company",
                        "next_action": "Employer needs to confirm role.",
                        "incomplete_reason": "Role employment confirmation pending.",
                        "guidance_status": "waiting_on_company",
                        "levels_breakdown": {
                            "level1": {"status": "Completed"},
                            "level2": {"status": "Completed"},
                            "level3": {"status": "Pending"},
                            "level4": {"status": "Not Completed"}
                        }
                    }
                }]
            },
            {
                "id": "103",
                "company": "Amazon",
                "is_verified": False,
                "roles": [{
                    "designation": "SDE-1",
                    "approved": None,
                    "status": "1",
                    "still_working": "0",
                    "worked_till_date": "2021-12-01",
                    "verification": {
                        "level": 1,
                        "highest_completed_label": "Level 1 (Work Identity)",
                        "next_level": 2,
                        "next_level_label": "Level 2 (Company Domain Verification)",
                        "next_action_owner": "company",
                        "next_action": "Company needs to join CollarCheck.",
                        "incomplete_reason": "Company domain verification pending.",
                        "guidance_status": "waiting_on_company",
                        "levels_breakdown": {
                            "level1": {"status": "Completed"},
                            "level2": {"status": "Pending"},
                            "level3": {"status": "Not Completed"},
                            "level4": {"status": "Not Completed"}
                        }
                    }
                }]
            }
        ]

    def test_scenario_1_single_employment_record_auto_selects(self):
        single_history = [{
            "id": "101",
            "company": "SingleCorp",
            "is_verified": True,
            "roles": [{
                "designation": "Manager",
                "approved": "1",
                "status": "1",
                "still_working": "1",
                "verification": {
                    "level": 3,
                    "highest_completed_label": "Level 3 (Employment Verification)",
                    "next_level": 4,
                    "next_level_label": "Level 4 (Salary Verification)",
                    "next_action_owner": "employee",
                    "next_action": "Upload salary proof",
                    "incomplete_reason": "Salary verification evidence pending.",
                    "guidance_status": "action_required"
                }
            }]
        }]
        res_str, meta = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION,
            {"employment_history": single_history},
            {},
            query="Show my employment verification status.",
            session_id="sess_single_123",
            return_metadata=True
        )
        assert meta is None
        assert "SingleCorp" in res_str
        assert "Level 3" in res_str
        # Verify stored context auto-set
        assert get_selected_employment_context("sess_single_123") == "SingleCorp"

    def test_scenario_2_multiple_records_explicit_company(self, multi_emp_history):
        res_str, meta = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION,
            {"employment_history": multi_emp_history},
            {},
            query="Show my employment verification status for Google.",
            session_id="sess_multi_2",
            return_metadata=True
        )
        assert meta is None
        assert "Google" in res_str
        assert "Microsoft" not in res_str
        assert "Amazon" not in res_str
        assert get_selected_employment_context("sess_multi_2") == "Google"

    def test_scenario_3_multiple_records_ambiguous_generic_query(self, multi_emp_history):
        sess_id = "sess_ambig_3"
        clear_selected_employment_context(sess_id)
        res_str, meta = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION,
            {"employment_history": multi_emp_history},
            {},
            query="Show my Level 3 verification status.",
            session_id=sess_id,
            return_metadata=True
        )
        assert meta is not None
        assert meta["type"] == "employment_selection"
        assert "Which employment record would you like to check?" in meta["prompt"]
        assert len(meta["options"]) == 3
        option_labels = [opt["label"] for opt in meta["options"]]
        assert any("Google" in opt for opt in option_labels)
        assert any("Microsoft" in opt for opt in option_labels)
        assert any("Amazon" in opt for opt in option_labels)

    def test_scenario_4_selection_through_frontend_control(self, multi_emp_history):
        sess_id = "sess_select_4"
        # Simulate user clicking "Google" button
        res_str, meta = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION,
            {"employment_history": multi_emp_history},
            {},
            query="Google",
            session_id=sess_id,
            return_metadata=True
        )
        assert meta is None
        assert "Google" in res_str
        assert get_selected_employment_context(sess_id) == "Google"

    def test_scenario_5_follow_up_question_with_selected_context(self, multi_emp_history):
        sess_id = "sess_followup_5"
        # Step 1: Select Google
        set_selected_employment_context(sess_id, "Google")

        # Step 2: Ask generic follow-up "What is pending?"
        res_str, meta = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS,
            {"employment_history": multi_emp_history},
            {},
            query="What is pending?",
            session_id=sess_id,
            return_metadata=True
        )
        assert meta is None
        assert "Google" in res_str
        assert "Software Engineer" in res_str
        assert "Upload salary proof" in res_str
        assert "Microsoft" not in res_str

    def test_scenario_6_explicit_company_switch(self, multi_emp_history):
        sess_id = "sess_switch_6"
        set_selected_employment_context(sess_id, "Google")
        assert get_selected_employment_context(sess_id) == "Google"

        # Explicit switch to Microsoft
        res_str, meta = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION,
            {"employment_history": multi_emp_history},
            {},
            query="Now show Microsoft.",
            session_id=sess_id,
            return_metadata=True
        )
        assert meta is None
        assert "Microsoft" in res_str
        assert "Backend Developer" in res_str
        assert get_selected_employment_context(sess_id) == "Microsoft"

    def test_scenario_7_unknown_company_handling(self, multi_emp_history):
        sess_id = "sess_unknown_7"
        res_str, meta = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION,
            {"employment_history": multi_emp_history},
            {},
            query="Show my status for Tesla.",
            session_id=sess_id,
            return_metadata=True
        )
        assert meta is not None
        assert "I couldn't find an employment record for 'Tesla'" in res_str or "Tesla" in meta.get("prompt", "")
        assert len(meta["options"]) == 3

    def test_scenario_8_similar_company_names_distinguished(self):
        dup_history = [
            {
                "id": "1",
                "company": "Google",
                "roles": [{"designation": "Software Engineer", "still_working": "1"}]
            },
            {
                "id": "2",
                "company": "Google",
                "roles": [{"designation": "Product Manager", "still_working": "0"}]
            }
        ]
        meta = build_selection_metadata(dup_history)
        assert len(meta["options"]) == 2
        option_texts = [f"{opt.get('label', '')} {opt.get('description', '')}" for opt in meta["options"]]
        assert any("Software Engineer" in t for t in option_texts)
        assert any("Product Manager" in t for t in option_texts)

    def test_scenario_9_multiple_current_records_ask_selection(self):
        multi_current = [
            {"id": "1", "company": "Corp A", "roles": [{"still_working": "1", "designation": "Dev 1"}]},
            {"id": "2", "company": "Corp B", "roles": [{"still_working": "1", "designation": "Dev 2"}]}
        ]
        res_str, meta = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION,
            {"employment_history": multi_current},
            {},
            query="Show my Level 3 verification status.",
            session_id="sess_curr_9",
            return_metadata=True
        )
        assert meta is not None
        assert len(meta["options"]) == 2

    def test_scenario_10_no_employment_records(self):
        res_str, meta = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION,
            {"employment_history": []},
            {},
            query="Show my verification status.",
            session_id="sess_none_10",
            return_metadata=True
        )
        assert meta is None
        assert "I couldn't find any employment verification records." in res_str


class TestPhase36BVerificationIsolation:
    """Tests verifying 100% verification status isolation between employment records."""

    def test_verification_isolation_across_different_companies(self):
        records = [
            {
                "id": "1",
                "company": "Google",
                "roles": [{
                    "designation": "Dev",
                    "approved": "1",
                    "status": "1",
                    "verification": {
                        "level": 4,
                        "highest_completed_label": "Level 4 (Salary Verification)",
                        "next_level": None,
                        "next_action_owner": "none"
                    }
                }]
            },
            {
                "id": "2",
                "company": "Microsoft",
                "roles": [{
                    "designation": "Architect",
                    "approved": None,
                    "status": "1",
                    "verification": {
                        "level": 2,
                        "highest_completed_label": "Level 2 (Company Domain Verification)",
                        "next_level": 3,
                        "next_action_owner": "company"
                    }
                }]
            }
        ]

        # Query Google
        res_g, _ = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION,
            {"employment_history": records},
            {},
            query="Google",
            session_id="sess_iso_1",
            return_metadata=True
        )
        assert "Google" in res_g
        assert "Level 4" in res_g
        assert "Microsoft" not in res_g

        # Query Microsoft
        res_m, _ = format_single_intent(
            UserIntent.EMPLOYMENT_VERIFICATION,
            {"employment_history": records},
            {},
            query="Microsoft",
            session_id="sess_iso_2",
            return_metadata=True
        )
        assert "Microsoft" in res_m
        assert "Level 2" in res_m
        assert "Google" not in res_m


class TestFrontendSelectionMetadataRendering:
    """Frontend static checks verifying selection control integration."""

    def test_frontend_has_renderSelectionControls(self, frontend_html_content: str):
        assert "renderSelectionControls" in frontend_html_content
        assert "data.selection_metadata" in frontend_html_content
        assert "employment_selection" in frontend_html_content
        assert "quick-chip-btn" in frontend_html_content
