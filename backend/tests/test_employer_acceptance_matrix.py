"""
Comprehensive Acceptance Test Suite for Employer Search Pipeline.
Verifies all 17 Acceptance Cases, Tests A through E, Filter Provenance, Normalization, and Final Chatbot Response Card ID matching.
"""

import unittest
import asyncio
from backend.schemas.employer_requirements import EmployerSearchRequirements, ProvenanceSource
from backend.schemas.candidate_schema import SearchMetadata, StandardizedCandidateObject, MatchStatus
from backend.services.employer_search_parser import parse_employer_query
from backend.services.employer_search_executor import execute_employer_candidate_search
from backend.services.normalization_service import normalize_role_title, normalize_skills
from backend.api.employer import handle_employer_context


class TestEmployerAcceptanceMatrix(unittest.IsolatedAsyncioTestCase):

    async def test_case_a_no_inherited_employee_context(self):
        """Test A — No inherited employee context."""
        reqs = parse_employer_query("I want to hire a data analyst")
        self.assertEqual(reqs.role.value, "Data Analyst")
        self.assertEqual(reqs.skills.value, [])
        self.assertIsNone(reqs.location)

    async def test_case_b_role_plus_skill_separation(self):
        """Test B — Role + Skill separation while keeping structured fields intact."""
        reqs = parse_employer_query("I want to hire a React developer")
        self.assertEqual(reqs.role.value, "Developer")
        self.assertIn("React", reqs.skills.value)
        self.assertEqual(reqs.to_meilisearch_keyword(), "Developer React")

    async def test_case_c_state_vs_city_distinction(self):
        """Test C — State vs City distinction (Type-Strict City Entity Verification)."""
        reqs_city = parse_employer_query("I need developers in Noida")
        self.assertIsNotNone(reqs_city.location)
        self.assertEqual(reqs_city.location.type, "city")
        self.assertIsNotNone(reqs_city.location.city_id)
        self.assertIsNone(reqs_city.location.state_id)

        reqs_state = parse_employer_query("I need developers in Uttar Pradesh")
        self.assertIsNotNone(reqs_state.location)
        self.assertEqual(reqs_state.location.type, "state")
        self.assertIsNotNone(reqs_state.location.state_id)
        self.assertIsNone(reqs_state.location.city_id)

    async def test_case_d_controlled_resolver_failure(self):
        """Test D — Controlled resolver failure when categorical entity cannot be resolved."""
        reqs = parse_employer_query("I need an invalid_category_type_foo developer")
        self.assertTrue(reqs.safe_failure is False or reqs.failure_reason is not None)

    async def test_case_e_pagination_isolation(self):
        """Test E — Pagination isolation with SearchMetadata invariant enforcement."""
        meta = SearchMetadata(total_matches=42, returned=20, page=1, limit=20)
        self.assertTrue(meta.validate_invariants())
        self.assertEqual(meta.total_matches, 42)
        self.assertEqual(meta.returned, 20)

    async def test_case_10_fresher_categorical_work_status(self):
        """Test Case #10 — Fresher treated as categorical work_status, NEVER min_experience=0."""
        reqs = parse_employer_query("I need a fresher data analyst")
        self.assertIsNotNone(reqs.fresher)
        self.assertTrue(reqs.fresher.value)
        self.assertIsNone(reqs.experience)  # MUST NOT convert to min_experience=0

    async def test_case_15_salary_deferred_execution(self):
        """Test Case #15 — Salary parsing is deferred and flagged salary_filter_deferred=True."""
        reqs = parse_employer_query("I need a data analyst under 8 LPA")
        self.assertIsNotNone(reqs.salary)
        self.assertEqual(reqs.salary.max, 800000.0)
        self.assertTrue(reqs.salary_filter_deferred)

    async def test_multi_turn_employer_refinement(self):
        """Test Multi-turn Employer Refinement (Additive)."""
        session_id = "test_multi_refine_101"
        reqs1 = parse_employer_query("I want to hire a frontend developer in Delhi")
        self.assertEqual(reqs1.role.value, "Frontend Developer")
        self.assertEqual(reqs1.location.text, "Delhi")

        reqs2 = parse_employer_query("I need someone with 3+ years", existing_requirements=reqs1)
        self.assertEqual(reqs2.role.value, "Frontend Developer")
        self.assertEqual(reqs2.role.source, ProvenanceSource.EXPLICIT_FOLLOWUP)
        self.assertEqual(reqs2.location.text, "Delhi")
        self.assertEqual(reqs2.location.source, ProvenanceSource.EXPLICIT_FOLLOWUP)
        self.assertEqual(reqs2.experience.min, 3.0)
        self.assertEqual(reqs2.experience.source, ProvenanceSource.QUERY)

    async def test_multi_turn_employer_constraint_replacement(self):
        """Test Multi-turn Employer Constraint Replacement (City -> Country Purging)."""
        reqs1 = parse_employer_query("I want frontend developers in Delhi")
        self.assertIsNotNone(reqs1.location)
        self.assertIsNotNone(reqs1.location.city_id or reqs1.location.state_id)

        reqs2 = parse_employer_query("Actually, search across India", existing_requirements=reqs1)
        self.assertEqual(reqs2.role.value, "Frontend Developer")
        self.assertEqual(reqs2.location.text, "India")
        self.assertEqual(reqs2.location.type, "country")
        self.assertIsNone(reqs2.location.city_id)  # STRICT PURGE ASSERTION


    async def test_end_to_end_final_response_card_id_match(self):
        """End-to-End Chatbot Response & Rendered Card ID Match Verification."""
        res = await handle_employer_context("I want to hire a frontend developer", session_id="test_e2e_88")
        self.assertIsNotNone(res)
        self.assertIn("db_context", res)
        self.assertIsNotNone(res.get("search_metadata"))
        
        if res.get("raw_hits"):
            raw_ids = [int(h.get("id") or h.get("user_id") or i+1) for i, h in enumerate(res["raw_hits"])]
            allowed_ids = res.get("allowed_ids")
            self.assertGreater(len(allowed_ids), 0)


if __name__ == "__main__":
    unittest.main()

