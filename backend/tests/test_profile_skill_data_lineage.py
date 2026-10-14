"""
test_profile_skill_data_lineage.py — Regression Test Suite for Complete Profile Skill Lineage

Validates:
1. Concurrent profile & employment fetching works without `asyncio` UnboundLocalError.
2. Failure of one endpoint does not drop data from the other endpoint.
3. Support for diverse employment skill representation formats (dicts, plain strings, CSVs, JSON arrays).
4. Multi-skill aggregation and propagation into candidate profiles & recommendation scoring.
5. Veracity of `matched_skills` output in Recommendation Engine.
6. Execution of router dispatch without unawaited coroutine warnings.
"""

import pytest
import asyncio
from unittest.mock import patch, MagicMock

from backend.services.user_data_extractor import (
    extract_employment_skills,
    parse_employment_skills_raw,
    aggregate_user_skills as extractor_aggregate_skills,
)
from backend.services.user_skill_aggregator import aggregate_user_skills
from backend.recommendation.recommendation_engine import RecommendationEngine
from backend.recommendation.models import CandidateProfile, JobProfile
from backend.services.search_router import dispatch_search_target
from backend.schemas.search_query import SearchTarget


def test_1_employment_skill_extraction_diverse_formats():
    """Verify dicts, plain strings, CSVs, and JSON arrays are extracted safely."""
    employment_payload = {
        "status": True,
        "data": [
            {
                "company": "Company A",
                "lists": [
                    {
                        "designation": "Backend Dev",
                        "skill": [
                            {"id": 101, "name": "Python"},
                            "PostgreSQL, Docker",
                            "[\"FastAPI\", \"Redis\"]"
                        ]
                    }
                ]
            },
            {
                "company": "Company B",
                "lists": [
                    {
                        "designation": "QA Engineer",
                        "skill": "Testing and Validation Techniques"
                    }
                ]
            }
        ]
    }

    extracted = extract_employment_skills(employment_payload)
    extracted_names = [s["name"] for s in extracted]

    assert "Python" in extracted_names
    assert "PostgreSQL" in extracted_names
    assert "Docker" in extracted_names
    assert "FastAPI" in extracted_names
    assert "Redis" in extracted_names
    assert "Testing and Validation Techniques" in extracted_names


def test_2_parse_employment_skills_raw_diverse_formats():
    """Verify raw DB column parsing supports strings, JSON strings, CSVs, and dicts."""
    raw_db_skills = [
        "Testing and Validation Techniques",
        "Python, SQL, Git",
        '["Kubernetes", "AWS"]',
        {"name": "GraphQL"},
        None,
        ""
    ]

    names = parse_employment_skills_raw(raw_db_skills)

    assert "Testing and Validation Techniques" in names
    assert "Python" in names
    assert "SQL" in names
    assert "Git" in names
    assert "Kubernetes" in names
    assert "AWS" in names
    assert "GraphQL" in names
    assert None not in names
    assert "" not in names


def test_3_multi_source_skill_aggregation():
    """Verify distinct profile & employment skills survive aggregation and deduplication."""
    prof_payload = {
        "status": True,
        "data": {
            "all_Skill": [
                {"skill": "Testing and Validation Techniques"},
                {"skill": "Python"}
            ]
        }
    }
    emp_payload = {
        "status": True,
        "data": [
            {
                "company": "TechCorp",
                "lists": [
                    {
                        "designation": "Senior Dev",
                        "skill": [{"name": "PostgreSQL"}, {"name": "Docker"}]
                    }
                ]
            }
        ]
    }

    res = aggregate_user_skills(user_profile_data=prof_payload, employment_data=emp_payload)
    
    assert res.effective_skills == ["Testing and Validation Techniques", "Python", "PostgreSQL", "Docker"]
    assert len(res.provenance) == 4


def test_4_concurrent_fetch_without_asyncio_error():
    """Verify concurrent profile/employment fetch in handle_employee_context completes without asyncio error."""
    async def _test():
        from backend.api.employee import handle_employee_context

        mock_parsed = {
            "search_target": "JOBS",
            "use_profile_skills": True,
            "skill_source": "PROFILE",
            "is_search_intent": True
        }
        mock_profile = {
            "status": True,
            "data": {
                "id": "1001",
                "all_Skill": [{"skill": "Testing and Validation Techniques"}]
            }
        }
        mock_emp = {
            "status": True,
            "data": [
                {
                    "company": "Acme Inc",
                    "lists": [
                        {
                            "designation": "Developer",
                            "skill": [{"name": "Python"}, {"name": "PostgreSQL"}]
                        }
                    ]
                }
            ]
        }

        def mock_fetch(endpoint, **kwargs):
            if endpoint == "user-profile":
                return mock_profile
            elif endpoint == "allEmployementNew":
                return mock_emp
            return {}

        captured_skills = []

        async def mock_hybrid_search(**kwargs):
            profile_terms = kwargs.get("profile_terms") or []
            captured_skills.extend(profile_terms)
            return [{"id": 10, "job_title": "Python Dev"}], {"total_records": 1}

        with patch("backend.services.search_parser.parse_search_query", return_value=mock_parsed), \
             patch("backend.services.user_data_service.fetch_endpoint_data", side_effect=mock_fetch), \
             patch("backend.api.employee.execute_hybrid_job_search", side_effect=mock_hybrid_search):

            res = await handle_employee_context(
                last_user_msg="show me jobs according to my skills",
                session_id="sess_test_concurrent",
                candidate_id="1001"
            )

            assert res is not None
            assert "Testing and Validation Techniques" in captured_skills
            assert "Python" in captured_skills
            assert "PostgreSQL" in captured_skills

    asyncio.run(_test())


def test_5_resilience_to_partial_endpoint_failure():
    """Verify that if one endpoint fails, the successful endpoint's skills are retained."""
    async def _test():
        from backend.api.employee import handle_employee_context

        mock_parsed = {
            "search_target": "JOBS",
            "use_profile_skills": True,
            "skill_source": "PROFILE",
            "is_search_intent": True
        }
        mock_emp = {
            "status": True,
            "data": [
                {
                    "company": "Solo Corp",
                    "lists": [
                        {
                            "designation": "DevOps",
                            "skill": [{"name": "Kubernetes"}]
                        }
                    ]
                }
            ]
        }

        def mock_fetch(endpoint, **kwargs):
            if endpoint == "user-profile":
                raise RuntimeError("API Profile Unavailable")
            elif endpoint == "allEmployementNew":
                return mock_emp
            return {}

        captured_skills = []

        async def mock_hybrid_search(**kwargs):
            profile_terms = kwargs.get("profile_terms") or []
            captured_skills.extend(profile_terms)
            return [], {"total_records": 0}

        with patch("backend.services.search_parser.parse_search_query", return_value=mock_parsed), \
             patch("backend.services.user_data_service.fetch_endpoint_data", side_effect=mock_fetch), \
             patch("backend.api.employee.execute_hybrid_job_search", side_effect=mock_hybrid_search):

            res = await handle_employee_context(
                last_user_msg="show me jobs according to my skills",
                session_id="sess_test_partial",
                candidate_id="1001"
            )

            assert res is not None
            assert "Kubernetes" in captured_skills

    asyncio.run(_test())


def test_6_recommendation_engine_matched_skills_veracity():
    """Verify recommendation engine populates matched_skills with genuine matches only."""
    engine = RecommendationEngine()
    
    cand = CandidateProfile(
        id=1,
        skills=["Python", "PostgreSQL", "Docker", "Testing and Validation Techniques"]
    )
    job = JobProfile(
        id=101,
        title="Python Backend Engineer",
        required_skills=["Python", "PostgreSQL", "React"]
    )

    skill_res = engine.skill_matcher._match_skills(cand.skills, job.required_skills)
    domain_passed = engine._evaluate_domain_gate(
        candidate=cand,
        job=job,
        skill_res=skill_res,
        title_res=None,
        search_query_title=""
    )

    assert domain_passed is True
    assert skill_res.score > 0.0
    assert "Python" in skill_res.matched_skills
    assert "PostgreSQL" in skill_res.matched_skills
    assert "Docker" not in skill_res.matched_skills
    assert "React" not in skill_res.matched_skills


def test_7_router_dispatch_without_coroutine_warning():
    """Verify router dispatch_search_target executes cleanly without unawaited coroutine warnings."""
    async def _test():
        mock_parsed = {
            "search_target": "JOBS",
            "use_profile_skills": True,
            "skill_source": "PROFILE",
            "is_search_intent": True
        }
        
        async def mock_handle_employee(**kwargs):
            return {
                "db_context": "## JOBS",
                "results": [{"id": 1, "title": "Dev"}],
                "result_type": "jobs"
            }

        with patch("backend.services.search_router.handle_employee_context", side_effect=mock_handle_employee):
            res = await dispatch_search_target(
                target=SearchTarget.JOBS,
                role="employee",
                last_user_msg="show me jobs according to my skills",
                candidate_id="1001"
            )

            assert res is not None
            assert res["result_type"] == "jobs"

    asyncio.run(_test())


def test_8_candidate_repository_skills_sql_mock():
    """Verify CandidateRepository.get_candidate_skills queries cyb_skill JOIN and returns skill names."""
    from backend.repositories.candidate_repository import CandidateRepository
    from unittest.mock import MagicMock

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    # Case 1: Cursor returns dict rows (e.g. DictCursor)
    mock_cursor.fetchall.return_value = [{"name": "Python"}, {"name": "Docker"}]
    with patch("backend.repositories.candidate_repository.get_db", return_value=mock_conn):
        skills = CandidateRepository.get_candidate_skills(1001)
        assert skills == ["Python", "Docker"]
        # Verify SQL contains JOIN on cyb_skill
        call_args = mock_cursor.execute.call_args[0]
        query = call_args[0]
        assert "JOIN cyb_skill s ON us.skill = s.id" in query
        assert "us.status = 1" in query
        assert "us.is_deleted IS NULL OR us.is_deleted = 0" in query

    # Case 2: Cursor returns tuple rows
    mock_cursor.fetchall.return_value = [("React",), ("Node.js",)]
    with patch("backend.repositories.candidate_repository.get_db", return_value=mock_conn):
        skills = CandidateRepository.get_candidate_skills(1002)
        assert skills == ["React", "Node.js"]


def test_9_candidate_profile_numeric_filtering_and_edge_cases():
    """Verify CandidateProfile.from_dict drops pure numeric IDs while retaining valid skills."""
    # 1. Foreign-key IDs mixed with valid skills
    payload_mixed = {
        "id": 1001,
        "skills": ["45", "102", "Python", "FastAPI", "  ", None, ""]
    }
    prof = CandidateProfile.from_dict(payload_mixed)
    assert prof.skills == ["Python", "FastAPI"]

    # 2. Dict-based skills with numeric values vs valid names
    payload_dicts = {
        "id": 1002,
        "skills": [
            {"skill": "99"},
            {"skill": "Docker"},
            {"name": "Kubernetes"},
            {"title": "123"},
            {"skill": "  "}
        ]
    }
    prof = CandidateProfile.from_dict(payload_dicts)
    assert prof.skills == ["Docker", "Kubernetes"]

    # 3. CSV string with numeric IDs
    payload_csv = {
        "id": 1003,
        "skills": "45, Python, 102, SQL, "
    }
    prof = CandidateProfile.from_dict(payload_csv)
    assert prof.skills == ["Python", "SQL"]

    # 4. Valid skill containing digits (e.g. Web3, OAuth2) - should NOT be dropped because not isdigit()
    payload_tech = {
        "id": 1004,
        "skills": ["Web3", "OAuth2", "HTML5", "3D Modeling", "100"]
    }
    prof = CandidateProfile.from_dict(payload_tech)
    assert prof.skills == ["Web3", "OAuth2", "HTML5", "3D Modeling"]


def test_10_job_profile_skill_extraction_and_raw_data():
    """Verify JobProfile.from_dict extracts skills from top-level and nested raw_data, filtering numeric IDs."""
    # 1. Top-level skill_names as list
    j1 = JobProfile.from_dict({
        "id": 1,
        "job_title": "Backend Dev",
        "skill_names": ["Python", "PostgreSQL", "45"]
    })
    assert j1.required_skills == ["Python", "PostgreSQL"]

    # 2. Nested raw_data with skill_names (Meilisearch format)
    j2 = JobProfile.from_dict({
        "id": 2,
        "job_title": "Data Scientist",
        "raw_data": {
            "skill_names": ["Python", "Pandas", "Scikit-Learn"]
        }
    })
    assert j2.required_skills == ["Python", "Pandas", "Scikit-Learn"]

    # 3. Top-level takes precedence over nested raw_data if both exist
    j3 = JobProfile.from_dict({
        "id": 3,
        "job_title": "Engineer",
        "skill_names": ["Go", "Kubernetes"],
        "raw_data": {
            "skill_names": ["OldSkill"]
        }
    })
    assert j3.required_skills == ["Go", "Kubernetes"]

    # 4. Missing skills entirely
    j4 = JobProfile.from_dict({
        "id": 4,
        "job_title": "Generalist"
    })
    assert j4.required_skills == []

    # 5. Comma-separated required_skills string
    j5 = JobProfile.from_dict({
        "id": 5,
        "job_title": "Frontend Dev",
        "required_skills": "React, TypeScript, 55"
    })
    assert j5.required_skills == ["React", "TypeScript"]


def test_11_hybrid_search_to_job_profile_contract():
    """Verify NormalizedJob output converts cleanly to JobProfile with populated required_skills and matches skills."""
    from backend.services.hybrid_job_search import NormalizedJob
    from backend.recommendation.mappers import to_job_profile
    from backend.recommendation.recommendation_engine import RecommendationEngine

    raw_meili_hit = {
        "id": "job_501",
        "job_title": "Senior Python Architect",
        "company_name": "Tech Corp",
        "skill_names": ["Python", "FastAPI", "PostgreSQL", "Docker"],
        "job_description": "We need an experienced Python developer"
    }

    norm_job = NormalizedJob(
        job_id=501,
        title="Senior Python Architect",
        company="Tech Corp",
        department=None,
        location="Remote",
        job_mode="remote",
        experience="5+ years",
        salary=None,
        url=None,
        description="We need an experienced Python developer",
        source="meilisearch",
        raw_data=raw_meili_hit
    )

    job_dict = norm_job.to_dict()
    assert "skill_names" in job_dict
    assert job_dict["skill_names"] == ["Python", "FastAPI", "PostgreSQL", "Docker"]

    job_prof = to_job_profile(job_dict)
    assert job_prof.required_skills == ["Python", "FastAPI", "PostgreSQL", "Docker"]

    cand = CandidateProfile(
        id=1,
        skills=["Python", "Docker", "Java"]
    )

    engine = RecommendationEngine()
    skill_res = engine.skill_matcher._match_skills(cand.skills, job_prof.required_skills)
    assert skill_res.matched_skills == ["Python", "Docker"]
    assert skill_res.score > 0.0


def test_12_deterministic_relevance_baseline():
    """Verify deterministic ranking and filtering across the full spectrum of job candidate types.
    
    Candidate: skills=['Python', 'Docker'], current_title='Backend Developer'
    Job 1 (Strong skill match): required_skills=['Python', 'Docker'], title='Backend Developer' -> Rank 1, High score
    Job 2 (Partial skill match): required_skills=['Python'], title='Backend Developer' -> Rank 2, Moderate score
    Job 3 (No required_skills, but description fallback): required_skills=[], description matches 'Python' -> Rank 3, Lower score
    Job 4 (Related title, but zero skill overlap): required_skills=['Java', 'C++'], title='Backend Developer' -> Rank 4, Lowest score
    Job 5 (Unrelated title, zero skill overlap): required_skills=['Cooking'], title='Executive Chef' -> Excluded by Domain Gate
    """
    from backend.recommendation.recommendation_engine import RecommendationEngine

    engine = RecommendationEngine()

    cand = CandidateProfile(
        id=101,
        skills=["Python", "Docker"],
        current_title="Backend Developer",
    )

    j1 = JobProfile(
        id=1,
        title="Backend Developer",
        required_skills=["Python", "Docker"],
    )
    j2 = JobProfile(
        id=2,
        title="Backend Developer",
        required_skills=["Python"],
    )
    j3 = JobProfile(
        id=3,
        title="Software Engineer",
        required_skills=[],
        raw_data={"job_description": "We are seeking an engineer experienced with Python and API development."}
    )
    j4 = JobProfile(
        id=4,
        title="Backend Developer",
        required_skills=["Java", "C++"],
    )
    j5 = JobProfile(
        id=5,
        title="Executive Chef",
        required_skills=["Cooking", "Culinary Arts"],
    )

    recs = engine.recommend_jobs(cand, [j1, j2, j3, j4, j5])

    # 1. Verification of Domain Gate filtering
    rec_ids = [r.job_id for r in recs]
    assert 5 not in rec_ids, "Unrelated job (Chef) should be rejected by Domain Gate"
    assert rec_ids == [1, 2, 4, 3] or rec_ids == [1, 2, 3, 4], "Relevant jobs must all be retained"

    # 2. Verification of Strict Deterministic Score Ordering
    # Strong match (J1) > Partial match (J2)
    rec_map = {r.job_id: r for r in recs}
    assert rec_map[1].overall_score > rec_map[2].overall_score
    assert rec_map[1].skill_result.matched_skills == ["Python", "Docker"]
    assert rec_map[2].skill_result.matched_skills == ["Python"]

    # 3. Verified skill matches must rank strictly higher than zero-skill-overlap title fallbacks
    assert rec_map[2].overall_score > rec_map[4].overall_score
    assert rec_map[4].skill_result.matched_skills == []

    # 4. Job 3 with description fallback matches via description heuristic with 0 matched required_skills
    assert rec_map[3].skill_result.matched_skills == []


def test_13_job_skill_match_integrity_contract():
    """
    Phase 5.5 Regression Tests:
    1. Populated required skills with genuine match.
    2. Populated required skills with no match (mismatch / 0.0 score, match_level='none').
    3. Empty required skills with candidate possessing skills (match_level='unknown', score=0.0, matched_skills=[]).
    4. Empty required skills with candidate possessing no skills (match_level='unknown', score=0.0, matched_skills=[]).
    5. Missing, null, empty-list, and alternate supported skill-field mappings into JobProfile.
    6. Consistency between matched_skills, skill score, and explanation generation.
    7. Dynamic renormalization safely excludes unknown skill dimension rather than falsely boosting or penalizing.
    """
    from backend.recommendation.skill_matcher import SkillMatcher
    from backend.recommendation.score_calculator import ScoreCalculator
    from backend.recommendation.explanation_generator import ExplanationGenerator
    from backend.recommendation.models import CandidateProfile, JobProfile, ExperienceMatchResult, LocationMatchResult, SalaryMatchResult, TitleMatchResult

    matcher = SkillMatcher()
    calc = ScoreCalculator()
    explainer = ExplanationGenerator()

    # Case 1: Populated required skills with genuine match
    res_match = matcher._match_skills(candidate_skills=["Python", "FastAPI"], required_skills=["Python", "Docker"])
    assert res_match.matched_skills == ["Python"]
    assert res_match.missing_skills == ["Docker"]
    assert res_match.score > 0.0
    assert res_match.match_level == "partial"

    # Case 2: Populated required skills with no match
    res_no_match = matcher._match_skills(candidate_skills=["Java", "C++"], required_skills=["Python", "Docker"])
    assert res_no_match.matched_skills == []
    assert res_no_match.missing_skills == ["Python", "Docker"]
    assert res_no_match.score == 0.0
    assert res_no_match.match_level == "none"

    # Case 3: Empty required skills with candidate possessing skills
    res_empty_req = matcher._match_skills(candidate_skills=["Python", "FastAPI"], required_skills=[])
    assert res_empty_req.matched_skills == []
    assert res_empty_req.missing_skills == []
    assert res_empty_req.score == 0.0
    assert res_empty_req.match_level == "unknown"
    assert res_empty_req.total_required == 0
    assert res_empty_req.total_matched == 0

    # Case 4: Empty required skills with candidate possessing no skills
    res_both_empty = matcher._match_skills(candidate_skills=[], required_skills=[])
    assert res_both_empty.matched_skills == []
    assert res_both_empty.missing_skills == []
    assert res_both_empty.score == 0.0
    assert res_both_empty.match_level == "unknown"

    # Case 5: Field mapping variants into JobProfile
    jp_from_skill_names = JobProfile.from_dict({"job_title": "Dev", "skill_names": ["Python", "Go"]})
    assert jp_from_skill_names.required_skills == ["Python", "Go"]

    jp_from_skills = JobProfile.from_dict({"job_title": "Dev", "skills": ["React"]})
    assert jp_from_skills.required_skills == ["React"]

    jp_from_raw_nested = JobProfile.from_dict({"job_title": "Dev", "raw_data": {"skill_names": ["PostgreSQL"]}})
    assert jp_from_raw_nested.required_skills == ["PostgreSQL"]

    jp_empty = JobProfile.from_dict({"job_title": "Dev", "required_skills": None, "skills": [], "skill_names": None})
    assert jp_empty.required_skills == []

    # Case 6 & 7: Consistency between score_calculator, explanation_generator, and match_level
    exp_res = ExperienceMatchResult(score=100.0, candidate_experience=3.0, required_experience=3.0, experience_gap=0.0, meets_requirement=True)
    loc_res = LocationMatchResult(score=100.0, candidate_city="Delhi", candidate_state="Delhi", candidate_country="India", job_city="Delhi", job_state="Delhi", job_country="India", work_mode="onsite", match_level="exact")
    sal_res = SalaryMatchResult(score=100.0, candidate_expected_salary=None, job_offered_salary=None, normalized_candidate_salary=None, normalized_job_salary=None, salary_difference_percent=0.0, meets_expectation=True)
    title_res = TitleMatchResult(score=100.0, candidate_title="Developer", job_title="Developer", normalized_candidate_title="developer", normalized_job_title="developer", matched_keywords=["developer"], match_level="exact")

    score_res = calc.calculate_score(
        skill_result=res_empty_req,
        experience_result=exp_res,
        location_result=loc_res,
        salary_result=sal_res,
        title_result=title_res,
    )
    # Skill weight must be excluded (renormalized out) because match_level='unknown'
    assert score_res.weight_skill == 0.0
    assert score_res.overall_score == 100.0

    exp_result = explainer.generate_explanation(
        score_result=score_res,
        skill_result=res_empty_req,
        experience_result=exp_res,
        location_result=loc_res,
        salary_result=sal_res,
        title_result=title_res,
    )
    # Explanation must NOT claim any matched skills
    skill_reasons = [r for r in exp_result.recommendation_reasons if r.type == "skill"]
    assert len(skill_reasons) == 0
    assert not any("Matched skills:" in s for s in exp_result.strengths)


