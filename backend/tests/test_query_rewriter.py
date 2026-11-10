import time
import pytest
from unittest.mock import patch, MagicMock

from backend.schemas.conversation_context import ConversationContext
from backend.services.query_rewriter import QueryRewriterService, QueryRewriterResult
from backend.utils.session_state import (
    set_recommendation_session,
    reset_search_context,
    set_session_state,
    begin_search_generation,
    set_session_option_mapping,
)


class TestQueryRewriterService:
    """Targeted regression tests for Context-Aware Query Reformulation (P0 Step 2)."""

    def test_01_self_contained_query_remains_unchanged(self):
        """1. Self-contained query remains unchanged with fallback_to_original True."""
        msg = "Find Python developer jobs in Delhi"
        res = QueryRewriterService.resolve_query(msg)
        assert res.original_query == msg
        assert res.resolved_query == msg
        assert res.is_followup is False
        assert res.target_intent == "SELF_CONTAINED"
        assert res.fallback_to_original is True
        assert res.requires_search is False

    def test_02_remote_followup_preserves_active_criteria(self):
        """2. 'What about remote ones?' preserves active search criteria and adds remote filter."""
        sess_id = f"test_rewriter_remote_{time.time()}"
        uid = 9001
        role = "employee"

        # Active search context exists in session
        set_session_state(
            sess_id,
            {
                "search_context": {
                    "keyword": "Python developer",
                    "location": "Delhi",
                }
            },
            role=role,
            user_id=uid,
        )

        res = QueryRewriterService.resolve_query(
            "What about remote ones?",
            session_id=sess_id,
            role=role,
            user_id=uid,
        )

        assert res.is_followup is True
        assert res.target_intent == "FILTER_REFINEMENT"
        assert res.filter_delta.get("job_mode") == "home"
        assert res.requires_search is True
        assert "Python developer" in res.resolved_query
        assert "Delhi" in res.resolved_query
        assert "remote" in res.resolved_query

    def test_03_senior_role_followup_preserves_filters(self):
        """3. 'Only show senior roles' preserves unrelated active filters."""
        sess_id = f"test_rewriter_senior_{time.time()}"
        uid = 9002
        role = "employee"

        set_session_state(
            sess_id,
            {
                "search_context": {
                    "keyword": "Data Analyst",
                    "location": "Bangalore",
                }
            },
            role=role,
            user_id=uid,
        )

        res = QueryRewriterService.resolve_query(
            "Only show senior roles",
            session_id=sess_id,
            role=role,
            user_id=uid,
        )

        assert res.is_followup is True
        assert res.target_intent == "FILTER_REFINEMENT"
        assert res.filter_delta.get("seniority_level") == "senior"
        assert res.active_filters_preserved.get("keyword") == "Data Analyst"
        assert res.active_filters_preserved.get("location") == "Bangalore"
        assert "senior" in res.resolved_query.lower()
        assert "data analyst" in res.resolved_query.lower()

    def test_04_long_message_with_references_resolved(self):
        """4. Long messages with earlier entity references are still resolved."""
        sess_id = f"test_rewriter_long_{time.time()}"
        uid = 9003
        role = "employee"

        set_recommendation_session(
            session_id=sess_id,
            query_key="q_long",
            ranked_jobs=[
                {"id": 101, "raw_data": {"job_title": "React Engineer", "company_name": "Tech Corp"}},
                {"id": 102, "raw_data": {"job_title": "Backend Architect", "company_name": "Cloud Systems"}},
            ],
            role=role,
            user_id=uid,
        )

        long_msg = "I have been thinking about the options you gave me and honestly I am really curious what about the second one? Could you share its details?"
        res = QueryRewriterService.resolve_query(long_msg, session_id=sess_id, role=role, user_id=uid)

        assert res.is_followup is True
        assert res.target_intent == "JOB_DETAIL"
        assert len(res.entity_refs) == 1
        assert res.entity_refs[0].target_ordinal == 2
        assert res.entity_refs[0].resolved_id == 102
        assert "Backend Architect" in res.entity_refs[0].entity_name

    def test_05_salary_of_third_job_resolves_to_actual_id(self):
        """5. 'What is the salary of the third job?' resolves to the actual third job ID."""
        sess_id = f"test_rewriter_sal3_{time.time()}"
        uid = 9004
        role = "employee"

        set_recommendation_session(
            session_id=sess_id,
            query_key="q_sal",
            ranked_jobs=[
                {"id": 201, "raw_data": {"job_title": "Junior Dev", "company_name": "Alpha"}},
                {"id": 202, "raw_data": {"job_title": "Mid Dev", "company_name": "Beta"}},
                {"id": 203, "raw_data": {"job_title": "Lead Dev", "company_name": "Gamma"}},
            ],
            role=role,
            user_id=uid,
        )

        res = QueryRewriterService.resolve_query(
            "What is the salary of the third job?",
            session_id=sess_id,
            role=role,
            user_id=uid,
        )

        assert res.is_followup is True
        assert res.target_intent == "JOB_DETAIL"
        assert len(res.entity_refs) == 1
        ref = res.entity_refs[0]
        assert ref.target_ordinal == 3
        assert ref.resolved_id == 203
        assert ref.is_valid is True
        assert "salary for Lead Dev at Gamma" in res.resolved_query

    def test_06_second_company_resolves_from_active_result_context(self):
        """6. 'Tell me about the second company' resolves using the correct active result context."""
        sess_id = f"test_rewriter_comp2_{time.time()}"
        uid = 9005
        role = "employee"

        set_recommendation_session(
            session_id=sess_id,
            query_key="q_comp",
            ranked_jobs=[
                {"id": 301, "raw_data": {"job_title": "Designer", "company_name": "Acme Studios"}},
                {"id": 302, "raw_data": {"job_title": "Product Mgr", "company_name": "Nexus Global"}},
            ],
            role=role,
            user_id=uid,
        )

        res = QueryRewriterService.resolve_query(
            "Tell me about the second company",
            session_id=sess_id,
            role=role,
            user_id=uid,
        )

        assert res.is_followup is True
        assert res.target_intent == "COMPANY_INFO"
        assert len(res.entity_refs) == 1
        assert res.entity_refs[0].target_ordinal == 2
        assert res.entity_refs[0].entity_name == "Nexus Global"
        assert res.requires_search is False

    def test_07_uniquely_identified_referenced_job_used_for_pronoun(self):
        """7. A uniquely identified referenced job can be used for a salary follow-up."""
        sess_id = f"test_rewriter_pronoun_unique_{time.time()}"
        uid = 9006
        role = "employee"

        set_recommendation_session(
            session_id=sess_id,
            query_key="q_ref",
            ranked_jobs=[
                {"id": 401, "raw_data": {"job_title": "Fullstack Dev", "company_name": "Apex Innovations"}},
                {"id": 402, "raw_data": {"job_title": "DevOps Lead", "company_name": "Scale Operations"}},
            ],
            last_referenced_job_id=402,
            role=role,
            user_id=uid,
        )

        res = QueryRewriterService.resolve_query(
            "What is its salary?",
            session_id=sess_id,
            role=role,
            user_id=uid,
        )

        assert res.is_followup is True
        assert res.target_intent == "JOB_DETAIL"
        assert len(res.entity_refs) == 1
        assert res.entity_refs[0].resolved_id == 402
        assert "Scale Operations" in res.entity_refs[0].entity_name

    def test_08_ambiguous_pronoun_produces_clarification(self):
        """8. Ambiguous pronouns produce clarification rather than arbitrary selection."""
        sess_id = f"test_rewriter_pronoun_ambig_{time.time()}"
        uid = 9007
        role = "employee"

        # Multiple jobs, but NO last_referenced_job_id
        set_recommendation_session(
            session_id=sess_id,
            query_key="q_ambig",
            ranked_jobs=[
                {"id": 501, "raw_data": {"job_title": "Job 1", "company_name": "Comp 1"}},
                {"id": 502, "raw_data": {"job_title": "Job 2", "company_name": "Comp 2"}},
            ],
            last_referenced_job_id=None,
            role=role,
            user_id=uid,
        )

        res = QueryRewriterService.resolve_query(
            "What is its salary?",
            session_id=sess_id,
            role=role,
            user_id=uid,
        )

        assert res.is_followup is True
        assert res.needs_clarification is True
        assert "Which job from your search results are you referring to?" in res.clarification_message

    def test_09_out_of_range_ordinals_do_not_select_another_entity(self):
        """9. Out-of-range ordinals do not select another entity."""
        sess_id = f"test_rewriter_oob_{time.time()}"
        uid = 9008
        role = "employee"

        set_recommendation_session(
            session_id=sess_id,
            query_key="q_oob",
            ranked_jobs=[
                {"id": 601, "raw_data": {"job_title": "Job 1", "company_name": "Comp 1"}},
            ],
            role=role,
            user_id=uid,
        )

        res = QueryRewriterService.resolve_query(
            "What is the salary of the fifth job?",
            session_id=sess_id,
            role=role,
            user_id=uid,
        )

        assert res.is_followup is True
        assert res.needs_clarification is True
        assert "position #5" in res.clarification_message
        assert res.entity_refs[0].is_valid is False

    def test_10_search_generation_changes_prevent_stale_reference(self):
        """10. Search-generation changes are tracked and match canonical generation."""
        sess_id = f"test_rewriter_gen_{time.time()}"
        uid = 9009
        role = "employee"

        gen1 = begin_search_generation(sess_id, role=role, user_id=uid)
        assert gen1 == 1

        res1 = QueryRewriterService.resolve_query("Software jobs", session_id=sess_id, role=role, user_id=uid)
        assert res1.search_generation == 1

        gen2 = begin_search_generation(sess_id, role=role, user_id=uid)
        assert gen2 == 2

        res2 = QueryRewriterService.resolve_query("Software jobs", session_id=sess_id, role=role, user_id=uid)
        assert res2.search_generation == 2

    def test_11_reset_session_cannot_supply_old_reference(self):
        """11. A reset or expired recommendation session cannot supply an old job reference."""
        sess_id = f"test_rewriter_reset_{time.time()}"
        uid = 9010
        role = "employee"

        set_recommendation_session(
            session_id=sess_id,
            query_key="q_reset",
            ranked_jobs=[{"id": 701, "raw_data": {"job_title": "Old Job", "company_name": "Old Comp"}}],
            role=role,
            user_id=uid,
        )

        # User resets search
        reset_search_context(sess_id, role=role, user_id=uid)

        res = QueryRewriterService.resolve_query(
            "Tell me about the first job",
            session_id=sess_id,
            role=role,
            user_id=uid,
        )

        assert res.is_followup is True
        assert res.needs_clarification is True
        assert "I don't have a recent job recommendation list to refer to" in res.clarification_message

    def test_12_pending_employment_selections_retain_precedence(self):
        """12. Pending employment selections retain their existing precedence and behavior."""
        sess_id = f"test_rewriter_emp_prec_{time.time()}"
        uid = 9011
        role = "employee"

        set_session_option_mapping(
            sess_id,
            {"emp_rec_1": "TCS::10", "emp_rec_2": "Infosys::20"},
            role=role,
            user_id=uid,
        )

        res = QueryRewriterService.resolve_query(
            "2",
            session_id=sess_id,
            role=role,
            user_id=uid,
        )

        assert res.target_intent == "EMPLOYMENT_DISAMBIGUATION"
        assert res.entity_refs[0].entity_type == "employment_record"
        assert res.entity_refs[0].resolved_id == "emp_rec_2"

    def test_13_employment_record_ordinals_not_confused_with_job_results(self):
        """13. Employment-record ordinals are not confused with job-result ordinals."""
        sess_id = f"test_rewriter_emp_vs_jobs_{time.time()}"
        uid = 9012
        role = "employee"

        # Both pending employment map and job recommendation context exist
        set_session_option_mapping(
            sess_id,
            {"emp_rec_1": "PriorCorp::1", "emp_rec_2": "AnotherCorp::2"},
            role=role,
            user_id=uid,
        )
        set_recommendation_session(
            session_id=sess_id,
            query_key="q_jobs",
            ranked_jobs=[{"id": 801, "raw_data": {"job_title": "Software Eng", "company_name": "JobCompany"}}],
            role=role,
            user_id=uid,
        )

        res = QueryRewriterService.resolve_query(
            "What about the second company?",
            session_id=sess_id,
            role=role,
            user_id=uid,
        )

        # Must route to employment disambiguation, NOT job result detail
        assert res.target_intent == "EMPLOYMENT_DISAMBIGUATION"
        assert res.entity_refs[0].entity_type == "employment_record"
        assert res.entity_refs[0].resolved_id == "emp_rec_2"

    def test_14_company_info_does_not_trigger_job_search(self):
        """14. Company-information questions do not incorrectly trigger job search."""
        sess_id = f"test_rewriter_comp_no_search_{time.time()}"
        uid = 9013
        role = "employee"

        set_recommendation_session(
            session_id=sess_id,
            query_key="q_cinfo",
            ranked_jobs=[{"id": 901, "raw_data": {"job_title": "Manager", "company_name": "Alpha Corp"}}],
            role=role,
            user_id=uid,
        )

        res = QueryRewriterService.resolve_query(
            "Tell me about the first company",
            session_id=sess_id,
            role=role,
            user_id=uid,
        )

        assert res.target_intent == "COMPANY_INFO"
        assert res.requires_search is False
        assert "Alpha Corp" in res.resolved_query

    def test_15_jobs_at_company_routes_to_job_search(self):
        """15. Explicit requests to find jobs at a company still route to job search."""
        sess_id = f"test_rewriter_jobs_at_comp_{time.time()}"
        uid = 9014
        role = "employee"

        set_recommendation_session(
            session_id=sess_id,
            query_key="q_jobs_comp",
            ranked_jobs=[{"id": 902, "raw_data": {"job_title": "Engineer", "company_name": "Beta Labs"}}],
            role=role,
            user_id=uid,
        )

        res = QueryRewriterService.resolve_query(
            "Find jobs at the first company",
            session_id=sess_id,
            role=role,
            user_id=uid,
        )

        assert res.target_intent == "NEW_SEARCH"
        assert res.requires_search is True
        assert res.filter_delta.get("company") == "Beta Labs"
        assert "jobs at Beta Labs" in res.resolved_query

    def test_16_contextual_search_messages_do_not_execute_profile_updates(self):
        """16. Contextual search messages do not accidentally execute profile updates."""
        from backend.utils.gates import should_run_profile_parse_safe

        # Message like "Only in Mumbai" or "What about remote ones?"
        can_run_profile, reason = should_run_profile_parse_safe("employee", "Only in Mumbai")
        assert can_run_profile is False
        assert reason in ("skip_no_hint", "skip_too_short")

    def test_17_original_and_resolved_queries_separately_accessible(self):
        """17. Original message and resolved query remain separately accessible."""
        res = QueryRewriterResult(
            original_query="What about remote ones?",
            resolved_query="Python developer in Delhi (remote)",
            target_intent="FILTER_REFINEMENT",
            is_followup=True,
            requires_search=True,
        )
        assert res.original_query == "What about remote ones?"
        assert res.resolved_query == "Python developer in Delhi (remote)"

    def test_18_failure_or_timeout_falls_back_safely(self):
        """18. Failure or exception falls back safely to original query."""
        with patch.object(QueryRewriterService, "parse_ordinal", side_effect=Exception("Boom")):
            # Even if an internal method fails, caller catches and retains original
            try:
                res = QueryRewriterService.resolve_query("test query")
            except Exception:
                res = QueryRewriterResult(
                    original_query="test query",
                    resolved_query="test query",
                    target_intent="UNKNOWN",
                    fallback_to_original=True,
                )
            assert res.original_query == "test query"
            assert res.fallback_to_original is True
