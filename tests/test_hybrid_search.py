import unittest
import time
from typing import List, Dict, Any

from backend.schemas.search_query import (
    SearchQuery,
    SearchSnapshot,
    ChatbotIntent,
    SearchMode,
    LocationMode,
    LocationSource,
    ProfileStatus,
    SortMode,
    MAX_SNAPSHOT_CANDIDATES,
)
from backend.utils.location_resolver import LocationResolver, haversine_distance
from backend.utils.session_state import (
    get_search_context,
    set_search_context,
    get_search_snapshot,
    set_search_snapshot,
    clear_pagination_state,
)
from backend.services.search_planner import SearchPlanner
from backend.services.search_executor import SearchExecutor
from backend.recommendation.recommendation_engine import RecommendationEngine
from backend.recommendation.models import CandidateProfile, JobProfile


class TestHybridSearchEngine(unittest.TestCase):
    """
    Comprehensive Test Suite for Hybrid Dynamic Job Search + Recommendation Engine.
    """

    def setUp(self):
        clear_pagination_state("session_test_a")
        clear_pagination_state("session_test_b")

    def tearDown(self):
        clear_pagination_state("session_test_a")
        clear_pagination_state("session_test_b")

    # ------------------------------------------------------------------
    # 1. SCHEMAS & ENUMS & FINGERPRINTING TESTS
    # ------------------------------------------------------------------
    def test_search_query_enums_and_fingerprinting(self):
        q1 = SearchQuery(
            intent=ChatbotIntent.JOB_SEARCH,
            search_mode=SearchMode.HYBRID,
            title="Python Developer",
            location="Delhi",
            canonical_location="delhi",
            location_mode=LocationMode.EXPLICIT,
            location_source=LocationSource.EXPLICIT,
        )
        hash1 = q1.compute_hash()
        self.assertTrue(isinstance(hash1, str))
        self.assertGreater(len(hash1), 5)

        # Identical criteria should produce identical hash
        q2 = SearchQuery(
            intent=ChatbotIntent.JOB_SEARCH,
            search_mode=SearchMode.HYBRID,
            title="Python Developer ",
            location="delhi",
            canonical_location="delhi",
            location_mode=LocationMode.EXPLICIT,
            location_source=LocationSource.EXPLICIT,
        )
        self.assertEqual(hash1, q2.compute_hash())

        # Modified title should produce different hash
        q3 = SearchQuery(
            intent=ChatbotIntent.JOB_SEARCH,
            search_mode=SearchMode.HYBRID,
            title="Java Developer",
            location="Delhi",
            canonical_location="delhi",
        )
        self.assertNotEqual(hash1, q3.compute_hash())

    def test_search_snapshot_validity_and_cap(self):
        q = SearchQuery(title="Python Developer")
        h = q.compute_hash()

        snap = SearchSnapshot(
            snapshot_id="snap_123",
            session_id="session_test_a",
            query_hash=h,
            search_query=q,
            candidate_job_ids=list(range(1, 150)),
            ranked_job_ids=list(range(1, 150)),
            created_at=time.time(),
            expires_at=time.time() + 3600.0,
        )

        self.assertTrue(snap.is_valid(h))
        self.assertFalse(snap.is_valid("wrong_hash"))

        # Snapshot expiration test
        expired_snap = SearchSnapshot(
            snapshot_id="snap_exp",
            session_id="session_test_a",
            query_hash=h,
            search_query=q,
            expires_at=time.time() - 10.0,
        )
        self.assertFalse(expired_snap.is_valid(h))

    # ------------------------------------------------------------------
    # 2. LOCATION RESOLVER & NORMALIZATION TESTS
    # ------------------------------------------------------------------
    def test_location_normalization_and_metro_clusters(self):
        # Canonical alias mapping
        self.assertEqual(LocationResolver.normalize_location("New Delhi"), "delhi")
        self.assertEqual(LocationResolver.normalize_location("Gurugram"), "gurgaon")
        self.assertEqual(LocationResolver.normalize_location("Bengaluru"), "bangalore")

        # Metro clusters
        cluster_delhi = LocationResolver.get_metro_cluster("Delhi")
        self.assertIn("delhi", cluster_delhi)
        self.assertIn("new delhi", cluster_delhi)
        self.assertIn("gurgaon", cluster_delhi)
        self.assertIn("noida", cluster_delhi)

        # Haversine distance
        # Distance between Delhi center (28.6139, 77.2090) and Gurgaon center (28.4595, 77.0266) ~27-30 km
        dist = haversine_distance(28.6139, 77.2090, 28.4595, 77.0266)
        self.assertGreater(dist, 20.0)
        self.assertLess(dist, 40.0)

    def test_location_resolution_fallbacks(self):
        # Case A: GPS available -> LocationSource.GPS
        res_gps = LocationResolver.resolve_location(
            parsed_location=None,
            parsed_location_mode="NEAR_ME",
            parsed_radius_km=10.0,
            request_lat=28.6139,
            request_lon=77.2090,
            profile_location="Gurgaon",
        )
        self.assertEqual(res_gps["location_source"], LocationSource.GPS)
        self.assertEqual(res_gps["latitude"], 28.6139)

        # Case B: GPS unavailable + Profile location available -> LocationSource.PROFILE
        res_prof = LocationResolver.resolve_location(
            parsed_location=None,
            parsed_location_mode="NEAR_ME",
            parsed_radius_km=10.0,
            request_lat=None,
            request_lon=None,
            profile_location="Gurgaon",
        )
        self.assertEqual(res_prof["location_source"], LocationSource.PROFILE)
        self.assertEqual(res_prof["canonical_location"], "gurgaon")

        # Case C: Everything unavailable -> location_source is None
        res_none = LocationResolver.resolve_location(
            parsed_location=None,
            parsed_location_mode="NEAR_ME",
            parsed_radius_km=10.0,
            request_lat=None,
            request_lon=None,
            profile_location=None,
        )
        self.assertIsNone(res_none["location_source"])

    # ------------------------------------------------------------------
    # 3. CONVERSATIONAL SEARCH REFINEMENT CHAIN
    # ------------------------------------------------------------------
    def test_conversational_search_refinement_chain(self):
        session_id = "session_test_a"

        # Turn 1: "Python jobs near me"
        q1, _ = SearchPlanner.build_or_refine_query(
            session_id=session_id,
            user_message="Python jobs near me",
            parsed_query_dict={"title": "Python", "location_mode": "NEAR_ME"},
            request_lat=28.6139,
            request_lon=77.2090,
        )
        self.assertEqual(q1.title, "Python")
        self.assertEqual(q1.location_mode, LocationMode.NEAR_ME)

        # Turn 2: "Only fresher"
        q2, mod2 = SearchPlanner.build_or_refine_query(
            session_id=session_id,
            user_message="Only fresher",
            parsed_query_dict={"experience_level": "Fresher"},
        )
        self.assertEqual(q2.title, "Python")
        self.assertEqual(q2.experience_level, "Fresher")

        # Turn 3: "Within 5 km"
        q3, mod3 = SearchPlanner.build_or_refine_query(
            session_id=session_id,
            user_message="Within 5 km",
            parsed_query_dict={"radius_km": 5.0},
        )
        self.assertEqual(q3.radius_km, 5.0)

        # Turn 4: "Remote"
        q4, mod4 = SearchPlanner.build_or_refine_query(
            session_id=session_id,
            user_message="Remote",
            parsed_query_dict={"work_mode": "Remote"},
        )
        self.assertEqual(q4.work_mode, "Remote")

        # Turn 5: "Remove remote"
        q5, mod5 = SearchPlanner.build_or_refine_query(
            session_id=session_id,
            user_message="Remove remote",
            parsed_query_dict={},
        )
        self.assertIsNone(q5.work_mode)

        # Turn 6: "Change Python to Java"
        q6, mod6 = SearchPlanner.build_or_refine_query(
            session_id=session_id,
            user_message="Change Python to Java",
            parsed_query_dict={},
        )
        self.assertEqual(q6.title.lower(), "java")

        # Turn 7: "Change 5 km to 10 km"
        q7, mod7 = SearchPlanner.build_or_refine_query(
            session_id=session_id,
            user_message="Within 10 km",
            parsed_query_dict={"radius_km": 10.0},
        )
        self.assertEqual(q7.radius_km, 10.0)

    # ------------------------------------------------------------------
    # 4. PROFILE CONFLICT & UNAVAILABLE PROFILE TESTS
    # ------------------------------------------------------------------
    def test_profile_conflict_retrieval(self):
        # Frontend Developer Candidate Profile
        cand = CandidateProfile(
            id=101,
            name="Frontend Dev",
            skills=["Frontend Developer", "React", "CSS", "JavaScript"],
            current_title="Frontend Developer",
        )

        # Query asking specifically for "Data Scientist jobs"
        jobs = [
            JobProfile(id=1, title="Data Scientist", required_skills=["Python", "Machine Learning"]),
            JobProfile(id=2, title="Data Analyst", required_skills=["SQL", "Python"]),
        ]

        engine = RecommendationEngine()
        recs = engine.recommend_jobs(
            candidate=cand,
            jobs=jobs,
            search_mode=SearchMode.HYBRID,
            search_query_title="Data Scientist",
        )

        self.assertEqual(len(recs), 2)
        # Verify job titles in retrieved results are Data Scientist / Data Analyst
        retrieved_titles = [r.title_result.job_title for r in recs]
        self.assertIn("Data Scientist", retrieved_titles)
        self.assertNotIn("Frontend Developer", retrieved_titles)

    def test_unauthenticated_no_profile_search(self):
        # No profile available
        cand = CandidateProfile(id=0, name="Guest", skills=[])
        jobs = [
            JobProfile(id=1, title="Python Developer", required_skills=["Python", "FastAPI"]),
            JobProfile(id=2, title="Java Developer", required_skills=["Java", "Spring"]),
        ]

        engine = RecommendationEngine()
        recs = engine.recommend_jobs(
            candidate=cand,
            jobs=jobs,
            profile_status=ProfileStatus.PROFILE_UNAVAILABLE,
            search_mode=SearchMode.SEARCH_ONLY,
            search_query_title="Python Developer",
        )

        self.assertEqual(len(recs), 2)
        self.assertGreater(recs[0].overall_score, 0.0)

    # ------------------------------------------------------------------
    # 5. RANKING STABILITY & DETERMINISM TEST
    # ------------------------------------------------------------------
    def test_ranking_stability_determinism(self):
        cand = CandidateProfile(id=10, name="Test User", skills=["Python"])
        jobs = [
            JobProfile(id=101, title="Python Developer", required_skills=["Python"]),
            JobProfile(id=102, title="Python Engineer", required_skills=["Python"]),
            JobProfile(id=103, title="Python Programmer", required_skills=["Python"]),
        ]

        engine = RecommendationEngine()

        # Execute 10 consecutive times
        first_order = [r.job_id for r in engine.recommend_jobs(cand, jobs)]
        for _ in range(9):
            subsequent_order = [r.job_id for r in engine.recommend_jobs(cand, jobs)]
            self.assertEqual(first_order, subsequent_order)

    # ------------------------------------------------------------------
    # 6. DUAL EXPLANATION GENERATOR TESTS
    # ------------------------------------------------------------------
    def test_dual_explanation_generator(self):
        cand = CandidateProfile(id=10, name="Test User", skills=["Python", "FastAPI"])
        job = JobProfile(id=1, title="Python Developer", required_skills=["Python", "FastAPI"])

        engine = RecommendationEngine()
        result = engine.recommend(
            candidate=cand,
            job=job,
            search_query_title="Python Developer",
            search_query_location="Delhi",
        )

        self.assertTrue(len(result.search_reasons) > 0)
        self.assertTrue(len(result.recommendation_reasons) > 0)
        self.assertIn("Python Developer", result.search_reasons[0])

    # ------------------------------------------------------------------
    # 7. CROSS-SESSION ISOLATION TEST
    # ------------------------------------------------------------------
    def test_cross_session_isolation(self):
        session_a = "session_test_a"
        session_b = "session_test_b"

        # Session A: Python Developer
        set_search_context(session_a, {"title": "Python Developer", "location": "Delhi"})

        # Session B: Java Developer
        set_search_context(session_b, {"title": "Java Developer", "location": "Gurgaon"})

        ctx_a = get_search_context(session_a)
        ctx_b = get_search_context(session_b)

        self.assertEqual(ctx_a["title"], "Python Developer")
        self.assertEqual(ctx_b["title"], "Java Developer")
        self.assertNotEqual(ctx_a["location"], ctx_b["location"])


if __name__ == "__main__":
    unittest.main()
