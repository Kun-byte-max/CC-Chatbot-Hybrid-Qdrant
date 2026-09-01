"""
Cross-Context Isolation Test Suite.
Verifies that employer candidate search never reuses or inherits employee job-search state, candidate profile skills (e.g. PHP), or employee location context (e.g. Delhi).
"""

import sys
import os
import unittest

from backend.utils.session_state import (
    set_session_state,
    get_session_state,
    set_employer_search_context,
    get_employer_search_context,
)
from backend.services.employer_search_parser import parse_employer_query


class TestContextIsolation(unittest.TestCase):
    def test_cross_context_isolation_firewall(self):
        session_id = "test_firewall_session_99"

        # Step 1: Simulate Candidate/Employee Job Search State (Skills: PHP, Location: Delhi)
        employee_state = {
            "search_context": {
                "keyword": "PHP",
                "location": "Delhi",
                "skills": ["PHP"],
            },
            "pagination": {"page": 3},
        }
        set_session_state(session_id, employee_state)

        # Step 2: Employer issues query "I want to hire a frontend developer"
        employer_reqs = parse_employer_query("I want to hire a frontend developer")

        # Step 3: Firewall Assertions
        self.assertEqual(employer_reqs.role.value, "Frontend Developer")
        self.assertIsNone(employer_reqs.location)  # MUST NOT inherit employee location "Delhi"
        self.assertEqual(employer_reqs.skills.value, [])  # MUST NOT inherit employee skill "PHP"

        # Step 4: Verify Employer Session Context is saved in isolated key namespace
        set_employer_search_context(session_id, employer_reqs.dict())
        retrieved_emp_ctx = get_employer_search_context(session_id)
        self.assertIsNotNone(retrieved_emp_ctx)
        self.assertEqual(retrieved_emp_ctx["role"]["value"], "Frontend Developer")

        # Verify employee state remains distinct and unpolluted
        retrieved_employee_state = get_session_state(session_id)
        self.assertEqual(retrieved_employee_state["search_context"]["skills"], ["PHP"])


if __name__ == "__main__":
    unittest.main()

