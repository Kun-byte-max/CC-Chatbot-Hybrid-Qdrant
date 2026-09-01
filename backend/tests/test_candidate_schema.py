"""
Candidate Schema and SearchMetadata Invariant Test Suite.
"""

import unittest
from backend.schemas.candidate_schema import SearchMetadata, StandardizedCandidateObject, MatchStatus
from backend.recommendation.mappers import map_doc_to_standard_candidate


class TestCandidateSchema(unittest.TestCase):
    def test_search_metadata_invariants(self):
        meta = SearchMetadata(total_matches=100, returned=20, page=1, limit=20)
        self.assertTrue(meta.validate_invariants())
        # Test Non-artificial truncation assertion
        self.assertEqual(meta.returned, min(meta.total_matches, meta.limit))

    def test_standardized_candidate_mapper_preserves_none_for_unknown(self):
        raw_doc = {
            "id": 101,
            "first_name": "John",
            "last_name": "Doe",
            "designation": "Frontend Developer",
            "total_experience_years": 4.5,
            "city_name": "Delhi",
            # Notice skills, experience, rating are omitted / None
        }
        cand = map_doc_to_standard_candidate(raw_doc)
        self.assertEqual(cand.candidate_id, 101)
        self.assertEqual(cand.title, "Frontend Developer")
        self.assertIsNone(cand.skills)  # Preserves None for MatchStatus.UNKNOWN evaluation
        self.assertIsNone(cand.on_immediate)
        self.assertIsNone(cand.rating)

    def test_zero_rank_score_filtering_in_phase1(self):
        # Candidate raw doc containing rank_score
        raw_doc = {
            "id": 202,
            "first_name": "Alice",
            "last_name": "Smith",
            "rank_score": 98.5,
        }
        cand = map_doc_to_standard_candidate(raw_doc)
        # Mapper maps document cleanly without applying rank_score cutoffs
        self.assertEqual(cand.candidate_id, 202)


if __name__ == "__main__":
    unittest.main()

