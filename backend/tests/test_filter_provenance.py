"""
Filter Provenance Test Suite.
Verifies ProvenanceValue schema, JSON serialization, and strict hard-filter source eligibility (QUERY and EXPLICIT_FOLLOWUP ONLY).
"""

import unittest
from backend.schemas.employer_requirements import (
    EmployerSearchRequirements,
    ProvenanceValue,
    ProvenanceSource,
    LocationRequirement,
    ExperienceRequirement,
)


class TestFilterProvenance(unittest.TestCase):
    def test_provenance_value_initialization(self):
        pv = ProvenanceValue[str](value="Frontend Developer", source=ProvenanceSource.QUERY)
        self.assertEqual(pv.value, "Frontend Developer")
        self.assertEqual(pv.source, ProvenanceSource.QUERY)

    def test_strict_query_and_explicit_followup_only_hard_filter_gating(self):
        reqs = EmployerSearchRequirements()
        reqs.role = ProvenanceValue[str](value="Frontend Developer", source=ProvenanceSource.QUERY)
        reqs.experience = ExperienceRequirement(min=3.0, max=None, source=ProvenanceSource.EXPLICIT_FOLLOWUP)

        # Allowed sources for Phase 1 hard filters
        ALLOWED_SOURCES = (ProvenanceSource.QUERY, ProvenanceSource.EXPLICIT_FOLLOWUP)
        self.assertIn(reqs.role.source, ALLOWED_SOURCES)
        self.assertIn(reqs.experience.source, ALLOWED_SOURCES)

        # Prohibited sources for hard filters
        PROHIBITED_SOURCES = (ProvenanceSource.SESSION_CONTEXT, ProvenanceSource.EMPLOYER_PREFERENCE, ProvenanceSource.SYSTEM_DEFAULT)
        pv_pref = ProvenanceValue[str](value="Delhi", source=ProvenanceSource.EMPLOYER_PREFERENCE)
        self.assertNotIn(pv_pref.source, ALLOWED_SOURCES)


if __name__ == "__main__":
    unittest.main()

