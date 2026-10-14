"""
test_explicit_title_relevance_guard.py — Targeted test suite for explicit job title relevance guard.
Validates that explicit role queries (e.g. 'Data Analyst') reject unrelated primary titles
(e.g. 'Software Engineer', 'Data Engineer') while preserving legitimate title variants and profile-based flexibility.
"""

import pytest
from backend.recommendation.models import CandidateProfile, JobProfile
from backend.recommendation.recommendation_engine import RecommendationEngine
from backend.services.hybrid_job_search import passes_domain_relevance_eligibility, NormalizedJob

@pytest.fixture
def engine():
    return RecommendationEngine()


def test_1_explicit_data_analyst_vs_software_engineer_rejected(engine):
    """Test 1: Explicit 'Data Analyst' query vs 'Software Engineer' with 'Data Analyst' skill -> REJECT."""
    cand = CandidateProfile(id=1)
    job = JobProfile(id=101, title="Software Engineer", required_skills=["Data Analyst", "Python"])
    
    res = engine.recommend(
        candidate=cand,
        job=job,
        search_query_title="Data Analyst",
    )
    assert res is None, "Software Engineer job should be rejected for explicit Data Analyst query"


def test_2_explicit_data_analyst_vs_data_engineer_rejected(engine):
    """Test 2: Explicit 'Data Analyst' query vs 'Data Engineer' -> REJECT."""
    cand = CandidateProfile(id=2)
    job = JobProfile(id=102, title="Data Engineer", required_skills=["Data Analyst", "Python"])
    
    res = engine.recommend(
        candidate=cand,
        job=job,
        search_query_title="Data Analyst",
    )
    assert res is None, "Data Engineer job should be rejected for explicit Data Analyst query"


def test_3_senior_data_analyst_remains_valid(engine):
    """Test 3: Explicit 'Data Analyst' query vs 'Senior Data Analyst' -> PASS."""
    cand = CandidateProfile(id=3)
    job = JobProfile(id=103, title="Senior Data Analyst", required_skills=["SQL", "Python"])
    
    res = engine.recommend(
        candidate=cand,
        job=job,
        search_query_title="Data Analyst",
    )
    assert res is not None, "Senior Data Analyst must pass domain gate for Data Analyst query"


def test_4_product_data_analyst_remains_valid(engine):
    """Test 4: Explicit 'Data Analyst' query vs 'Product Data Analyst' -> PASS."""
    cand = CandidateProfile(id=4)
    job = JobProfile(id=104, title="Product Data Analyst", required_skills=["SQL", "Product Analytics"])
    
    res = engine.recommend(
        candidate=cand,
        job=job,
        search_query_title="Data Analyst",
    )
    assert res is not None, "Product Data Analyst must pass domain gate for Data Analyst query"


def test_5_business_data_analyst_remains_valid(engine):
    """Test 5: Explicit 'Data Analyst' query vs 'Business Data Analyst' -> PASS."""
    cand = CandidateProfile(id=5)
    job = JobProfile(id=105, title="Business Data Analyst", required_skills=["Excel", "SQL"])
    
    res = engine.recommend(
        candidate=cand,
        job=job,
        search_query_title="Data Analyst",
    )
    assert res is not None, "Business Data Analyst must pass domain gate for Data Analyst query"


def test_6_profile_skill_search_remains_flexible(engine):
    """Test 6: Profile-based search (no search_query_title) -> Software Engineer with matching profile skills PASSES."""
    cand = CandidateProfile(id=6, skills=["Python", "SQL", "Data Analyst"])
    job = JobProfile(id=106, title="Software Engineer", required_skills=["Python", "SQL"])
    
    # Recommendation without explicit search_query_title (profile-driven)
    res = engine.recommend(
        candidate=cand,
        job=job,
    )
    assert res is not None, "Profile skill search should remain flexible and recommend Software Engineer for Python/SQL candidate"


def test_7_meilisearch_and_hybrid_bypass_regression():
    """Test 7: MeiliSearch and Hybrid hits with mismatched titles must be rejected by passes_domain_relevance_eligibility."""
    norm_job_meili = NormalizedJob(
        job_id=201,
        title="Software Engineer",
        raw_data={"id": 201, "title": "Software Engineer", "required_skills": ["Data Analyst", "Python"]},
        source="meilisearch",
    )
    norm_job_hybrid = NormalizedJob(
        job_id=202,
        title="Software Engineer",
        raw_data={"id": 202, "title": "Software Engineer", "required_skills": ["Data Analyst", "Python"]},
        source="hybrid",
    )
    
    assert passes_domain_relevance_eligibility(norm_job_meili, keyword="Data Analyst") is False
    assert passes_domain_relevance_eligibility(norm_job_hybrid, keyword="Data Analyst") is False


def test_8_explicit_data_analyst_vs_data_entry_and_scientist_rejected(engine):
    """Test 8: Explicit 'Data Analyst' query vs 'Data Entry' and 'Data Scientist' -> REJECT."""
    cand = CandidateProfile(id=8)
    
    job_entry = JobProfile(id=108, title="Data Entry", required_skills=["Excel", "Typing"])
    res_entry = engine.recommend(candidate=cand, job=job_entry, search_query_title="Data Analyst")
    assert res_entry is None, "Data Entry job must be rejected for explicit Data Analyst query"
    
    job_scientist = JobProfile(id=109, title="Data Scientist", required_skills=["Python", "Machine Learning"])
    res_scientist = engine.recommend(candidate=cand, job=job_scientist, search_query_title="Data Analyst")
    assert res_scientist is None, "Data Scientist job must be rejected for explicit Data Analyst query"
    
    job_lead = JobProfile(id=110, title="Lead Data Analyst", required_skills=["SQL", "Leadership"])
    res_lead = engine.recommend(candidate=cand, job=job_lead, search_query_title="Data Analyst")
    assert res_lead is not None, "Lead Data Analyst job must pass for explicit Data Analyst query"

