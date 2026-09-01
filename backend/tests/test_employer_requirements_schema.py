"""
Employer Requirements Schema Test Suite.
Verifies to_meilisearch_keyword() helper and structured role/skill immutability.
"""

import unittest
from backend.schemas.employer_requirements import (
    EmployerSearchRequirements,
    ProvenanceValue,
    ProvenanceSource,
)


class TestEmployerRequirementsSchema(unittest.TestCase):
    def test_meilisearch_keyword_retrieval_aid_immutability(self):
        reqs = EmployerSearchRequirements()
        reqs.role = ProvenanceValue[str](value="Frontend Developer", source=ProvenanceSource.QUERY)
        reqs.skills = ProvenanceValue[list](value=["React", "JavaScript"], source=ProvenanceSource.QUERY)

        # 1. Build transient keyword string
        keyword = reqs.to_meilisearch_keyword()
        self.assertEqual(keyword, "Frontend Developer React JavaScript")

        # 2. Assert canonical contract immutability
        self.assertEqual(reqs.role.value, "Frontend Developer")
        self.assertEqual(reqs.skills.value, ["React", "JavaScript"])


if __name__ == "__main__":
    unittest.main()

