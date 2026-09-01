import unittest
import logging
from backend.services.search_planner import SearchPlanner
from backend.schemas.search_query import LocationMode, ProfileStatus
from backend.utils.session_state import (
    set_search_context,
    get_search_context,
    clear_pagination_state,
    set_pagination_state,
    get_pagination_state,
)

logging.basicConfig(level=logging.INFO)


class TestSearchPlannerSubstitution(unittest.TestCase):
    """
    Comprehensive test suite for role substitution and filter removal in SearchPlanner.
    """

    def setUp(self):
        self.session_id = "test_sub_session_1"
        clear_pagination_state(self.session_id)

    def tearDown(self):
        clear_pagination_state(self.session_id)

    # ------------------------------------------------------------------
    # Test 1: Explicit 3 simple patterns (change, switch, replace)
    # ------------------------------------------------------------------
    def test_explicit_three_patterns(self):
        set_search_context(self.session_id, {"title": "Python", "location": "Delhi"})

        # Pattern A: change Python to Java
        q1, mod1 = SearchPlanner.build_or_refine_query(
            session_id=self.session_id,
            user_message="change Python to Java",
            parsed_query_dict={},
        )
        self.assertTrue(mod1)
        self.assertEqual(q1.title, "Java")
        self.assertEqual(q1.location, "Delhi")

        # Pattern B: switch Java to Go
        q2, mod2 = SearchPlanner.build_or_refine_query(
            session_id=self.session_id,
            user_message="switch Java to Go",
            parsed_query_dict={},
        )
        self.assertTrue(mod2)
        self.assertEqual(q2.title, "Go")
        self.assertEqual(q2.location, "Delhi")

        # Pattern C: replace Go with Rust
        q3, mod3 = SearchPlanner.build_or_refine_query(
            session_id=self.session_id,
            user_message="replace Go with Rust",
            parsed_query_dict={},
        )
        self.assertTrue(mod3)
        self.assertEqual(q3.title, "Rust")
        self.assertEqual(q3.location, "Delhi")

    # ------------------------------------------------------------------
    # Test 2: Cross-Pattern Title Storage Tests (Remote Java Developer)
    # ------------------------------------------------------------------
    def test_cross_pattern_title_storage(self):
        # Change with compound role
        set_search_context(self.session_id, {"title": "Python"})
        q1, _ = SearchPlanner.build_or_refine_query(
            session_id=self.session_id,
            user_message="Change Python to Remote Java Developer",
            parsed_query_dict={},
        )
        self.assertEqual(q1.title, "Remote Java Developer")

        # Switch with compound role
        set_search_context(self.session_id, {"title": "Python Developer"})
        q2, _ = SearchPlanner.build_or_refine_query(
            session_id=self.session_id,
            user_message="switch Python Developer to Remote Java Developer",
            parsed_query_dict={},
        )
        self.assertEqual(q2.title, "Remote Java Developer")

        # Replace with developer suffix
        set_search_context(self.session_id, {"title": "Python Developer"})
        q3, _ = SearchPlanner.build_or_refine_query(
            session_id=self.session_id,
            user_message="replace Python Developer with Java Developer",
            parsed_query_dict={},
        )
        self.assertEqual(q3.title, "Java Developer")

    # ------------------------------------------------------------------
    # Test 3: Terminal role suffix cleanup vs filter fallthrough
    # ------------------------------------------------------------------
    def test_role_suffix_cleanup_and_filter_protection(self):
        set_search_context(self.session_id, {"title": "Python"})

        # Trailing 'jobs' is stripped
        q1, mod1 = SearchPlanner.build_or_refine_query(
            session_id=self.session_id,
            user_message="change Python to Java jobs",
            parsed_query_dict={},
        )
        self.assertTrue(mod1)
        self.assertEqual(q1.title, "Java")

        # Trailing 'filter' is NOT stripped, new_role becomes 'Java filter' which is not in reserved terms
        set_search_context(self.session_id, {"title": "Python"})
        q2, mod2 = SearchPlanner.build_or_refine_query(
            session_id=self.session_id,
            user_message="change Python to Java filter",
            parsed_query_dict={},
        )
        self.assertTrue(mod2)
        self.assertEqual(q2.title, "Java filter")

    # ------------------------------------------------------------------
    # Test 4: Precedence fallthrough for reserved terms
    # ------------------------------------------------------------------
    def test_precedence_fallthrough_for_reserved_terms(self):
        set_search_context(self.session_id, {"title": "Python", "salary_min": 5.0})

        # 'salary' is in RESERVED_ROLE_TERMS -> role substitution rejected -> falls through to filter removal
        q1, mod1 = SearchPlanner.build_or_refine_query(
            session_id=self.session_id,
            user_message="change Python to salary",
            parsed_query_dict={},
        )
        # Does not substitute title to 'salary'
        self.assertEqual(q1.title, "Python")

    # ------------------------------------------------------------------
    # Test 5: Compound query fallthrough test (No Partial Execution)
    # ------------------------------------------------------------------
    def test_compound_query_fallthrough_no_partial_execution(self):
        set_search_context(self.session_id, {"title": "Python", "work_mode": "Remote"})

        # Compound query should NOT match simple single-turn role substitution anchored pattern
        q1, mod1 = SearchPlanner.build_or_refine_query(
            session_id=self.session_id,
            user_message="Change Python to Java and remove remote filter",
            parsed_query_dict={},
        )
        self.assertEqual(q1.title, "Python")
        self.assertIsNone(q1.work_mode)

    # ------------------------------------------------------------------
    # Test 6: State Mutation Verification (The Critical Test)
    # ------------------------------------------------------------------
    def test_state_mutation_preserves_every_active_filter(self):
        initial_state = {
            "title": "Python Developer",
            "location": "Delhi",
            "experience_level": "2 years",
            "work_mode": "Remote",
            "salary_min": 8.0,
        }
        set_search_context(self.session_id, dict(initial_state))

        q1, mod1 = SearchPlanner.build_or_refine_query(
            session_id=self.session_id,
            user_message="Change Python to Java",
            parsed_query_dict={},
        )
        self.assertTrue(mod1)
        self.assertEqual(q1.title, "Java")
        self.assertEqual(q1.location, "Delhi")
        self.assertEqual(q1.experience_level, "2 years")
        self.assertEqual(q1.work_mode, "Remote")
        self.assertEqual(q1.salary_min, 8.0)

    # ------------------------------------------------------------------
    # Test 7: Sequential Multi-Turn Role Substitution
    # ------------------------------------------------------------------
    def test_sequential_multi_turn_role_substitution(self):
        set_search_context(self.session_id, {"title": "Python", "location": "Delhi"})

        for old_t, new_t in [("Python", "Java"), ("Java", "Go"), ("Go", "Rust")]:
            q, mod = SearchPlanner.build_or_refine_query(
                session_id=self.session_id,
                user_message=f"Change {old_t} to {new_t}",
                parsed_query_dict={},
            )
            self.assertTrue(mod)
            self.assertEqual(q.title, new_t)
            self.assertEqual(q.location, "Delhi")

    # ------------------------------------------------------------------
    # Test 8: Role Substitution + Next Page
    # ------------------------------------------------------------------
    def test_role_substitution_resets_pagination(self):
        set_search_context(self.session_id, {"title": "Python"})
        set_pagination_state(self.session_id, {"current_page": 3, "total_pages": 10})

        # Role substitution occurs -> context_modified = True -> clear_pagination_state called
        q1, mod1 = SearchPlanner.build_or_refine_query(
            session_id=self.session_id,
            user_message="Change Python to Java",
            parsed_query_dict={},
        )
        self.assertTrue(mod1)
        self.assertIsNone(get_pagination_state(self.session_id))

    # ------------------------------------------------------------------
    # Test 9: Scenario A (5-Turn Sequential Chain)
    # ------------------------------------------------------------------
    def test_scenario_a_five_turn_chain(self):
        # Turn 1: "Python jobs near me"
        q1, _ = SearchPlanner.build_or_refine_query(
            session_id=self.session_id,
            user_message="Python jobs near me",
            parsed_query_dict={"title": "Python", "location_mode": "NEAR_ME"},
            request_lat=28.6139,
            request_lon=77.2090,
        )
        self.assertEqual(q1.title, "Python")

        # Turn 2: "Only fresher jobs"
        q2, _ = SearchPlanner.build_or_refine_query(
            session_id=self.session_id,
            user_message="Only fresher jobs",
            parsed_query_dict={"experience_level": "Fresher"},
        )
        self.assertEqual(q2.experience_level, "Fresher")

        # Turn 3: "Only remote jobs"
        q3, _ = SearchPlanner.build_or_refine_query(
            session_id=self.session_id,
            user_message="Only remote jobs",
            parsed_query_dict={"work_mode": "Remote"},
        )
        self.assertEqual(q3.work_mode, "Remote")

        # Turn 4: "Remove remote filter"
        q4, _ = SearchPlanner.build_or_refine_query(
            session_id=self.session_id,
            user_message="Remove remote filter",
            parsed_query_dict={},
        )
        self.assertIsNone(q4.work_mode)

        # Turn 5: "Change Python to Java"
        q5, mod5 = SearchPlanner.build_or_refine_query(
            session_id=self.session_id,
            user_message="Change Python to Java",
            parsed_query_dict={},
        )
        self.assertTrue(mod5)
        self.assertEqual(q5.title, "Java")
        self.assertEqual(q5.experience_level, "Fresher")
        self.assertIsNone(q5.work_mode)

    # ------------------------------------------------------------------
    # Test 10: Conditional pagination reset
    # ------------------------------------------------------------------
    def test_inactive_filter_removal_no_pagination_reset(self):
        set_search_context(self.session_id, {"title": "Python"})
        set_pagination_state(self.session_id, {"current_page": 2, "total_pages": 5})

        # "Remove remote filter" when work_mode is not in context -> context_modified = False
        q1, mod1 = SearchPlanner.build_or_refine_query(
            session_id=self.session_id,
            user_message="Remove remote filter",
            parsed_query_dict={},
        )
        self.assertFalse(mod1)
        self.assertIsNotNone(get_pagination_state(self.session_id))

    # ------------------------------------------------------------------
    # Test 11: False positive rejection
    # ------------------------------------------------------------------
    def test_false_positive_rejection(self):
        set_search_context(self.session_id, {"title": "Python"})

        # Sentences that do not match explicit anchors
        q1, mod1 = SearchPlanner.build_or_refine_query(
            session_id=self.session_id,
            user_message="What is the difference between Python and Java?",
            parsed_query_dict={},
        )
        self.assertFalse(mod1)
        self.assertEqual(q1.title, "Python")

    # ------------------------------------------------------------------
    # Test 12: Role substitution with NO prior context
    # ------------------------------------------------------------------
    def test_role_substitution_with_no_prior_context(self):
        # Empty search context
        clear_pagination_state(self.session_id)

        q1, mod1 = SearchPlanner.build_or_refine_query(
            session_id=self.session_id,
            user_message="Change Python to Java",
            parsed_query_dict={},
        )
        self.assertFalse(mod1)
        self.assertIsNone(q1.title)


if __name__ == "__main__":
    unittest.main()
