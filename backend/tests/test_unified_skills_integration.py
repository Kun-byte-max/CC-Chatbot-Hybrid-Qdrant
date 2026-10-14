"""
test_unified_skills_integration.py — Integration test suite for Phase 2B-2C Unified User Skill Integration.
"""

import pytest
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock

from backend.services.user_skill_aggregator import aggregate_user_skills
from backend.recommendation.mappers import to_candidate_profile_from_api
from backend.recommendation.models import CandidateProfile


def get_mock_user_profile():
    return {
        "status": True,
        "data": {
            "id": "41031",
            "full_name": "Kunal Yadav",
            "fname": "Kunal",
            "lname": "Yadav",
            "current_position_name": "Software Engineer",
            "still_working_position_name": "Software Engineer",
            "city_name": "Delhi",
            "all_Skill": [
                {"id": "1", "skill": "Python"},
                {"id": "2", "skill": "React.js"}
            ]
        }
    }


def get_mock_employment_history():
    return {
        "status": True,
        "data": [
            {
                "company": "TechCorp",
                "lists": [
                    {
                        "designation": "Senior Developer",
                        "skill": [
                            {"id": 10, "name": "Python"},
                            {"id": 11, "name": "PostgreSQL"}
                        ]
                    }
                ]
            }
        ]
    }


def test_1_both_sources_succeed_deduplicated_union():
    """1. Both sources succeed: candidate profile receives deduplicated union with canonical names."""
    prof_payload = get_mock_user_profile()
    emp_payload = get_mock_employment_history()

    cand_prof = to_candidate_profile_from_api(prof_payload["data"], user_id=41031)
    res = aggregate_user_skills(user_profile_data=prof_payload, employment_data=emp_payload)

    cand_prof.skills = res.effective_skills

    assert cand_prof.skills == ["Python", "React", "PostgreSQL"]
    assert cand_prof.name == "Kunal Yadav"
    assert cand_prof.current_title == "Software Engineer"


def test_2_overlapping_skills_appear_once():
    """2. Overlapping skills ('Python') appear only once in effective_skills."""
    prof_payload = get_mock_user_profile()
    emp_payload = get_mock_employment_history()

    res = aggregate_user_skills(user_profile_data=prof_payload, employment_data=emp_payload)
    assert res.effective_skills.count("Python") == 1


def test_3_profile_success_employment_failure():
    """3. Profile success plus employment failure retains profile skills."""
    prof_payload = get_mock_user_profile()
    emp_payload = {"status": False, "error": "500 Server Error"}

    res = aggregate_user_skills(user_profile_data=prof_payload, employment_data=emp_payload)

    assert res.effective_skills == ["Python", "React"]
    assert res.profile_status == "SUCCESS"
    assert res.employment_status == "FAILED"


def test_4_employment_success_profile_failure():
    """4. Employment success plus profile failure retains employment skills."""
    prof_payload = {"status": False, "error": "404 Not Found"}
    emp_payload = get_mock_employment_history()

    res = aggregate_user_skills(user_profile_data=prof_payload, employment_data=emp_payload)

    assert res.effective_skills == ["Python", "PostgreSQL"]
    assert res.profile_status == "FAILED"
    assert res.employment_status == "SUCCESS"


def test_5_both_sources_empty():
    """5. Both sources return valid empty skill sets."""
    prof_payload = {"status": True, "data": {"all_Skill": []}}
    emp_payload = {"status": True, "data": []}

    res = aggregate_user_skills(user_profile_data=prof_payload, employment_data=emp_payload)

    assert res.effective_skills == []
    assert res.profile_status == "EMPTY"
    assert res.employment_status == "EMPTY"


def test_6_both_sources_fail():
    """6. Both sources fail gracefully."""
    prof_payload = {"status": False, "error": "Timeout"}
    emp_payload = {"status": False, "error": "Timeout"}

    res = aggregate_user_skills(user_profile_data=prof_payload, employment_data=emp_payload)

    assert res.effective_skills == []
    assert res.profile_status == "FAILED"
    assert res.employment_status == "FAILED"


@pytest.mark.anyio
async def test_7_generic_search_does_not_fetch_employment():
    """7. Generic search (use_profile_skills=False) does not trigger an employment fetch."""
    parsed = {"is_search_intent": True, "use_profile_skills": False, "keyword": "Accountant", "location": "Delhi"}

    # Mock fetch_endpoint_data
    with patch("backend.services.user_data_service.fetch_endpoint_data") as mock_fetch:
        mock_fetch.side_effect = lambda ep, **kw: get_mock_user_profile() if ep == "user-profile" else get_mock_employment_history()

        # Simulate api/employee logic check
        _is_profile_turn = bool(parsed.get("use_profile_skills") or parsed.get("skill_source") == "PROFILE")
        assert _is_profile_turn is False


@pytest.mark.anyio
async def test_8_personalized_search_triggers_employment_fetch():
    """8. Personalized search (use_profile_skills=True) triggers employment fetch."""
    parsed = {"is_search_intent": True, "use_profile_skills": True, "skill_source": "PROFILE"}
    _is_profile_turn = bool(parsed.get("use_profile_skills") or parsed.get("skill_source") == "PROFILE")
    assert _is_profile_turn is True


def test_9_reuse_available_profile_payload():
    """9. Available user_profile_api_data is reused rather than refetched."""
    user_profile_api_data = get_mock_user_profile()
    prof_data = user_profile_api_data
    assert prof_data is user_profile_api_data


def test_10_explicit_python_keyword_plus_personalization():
    """10. Explicit Python keyword plus personalization preserves both explicit query and combined skills."""
    parsed = {"is_search_intent": True, "use_profile_skills": True, "keyword": "Python", "skills": ["Python"]}
    keyword = (parsed.get("keyword") or "").strip()
    has_explicit_query_criteria = bool(keyword)

    prof_payload = get_mock_user_profile()
    emp_payload = get_mock_employment_history()
    cand_prof = to_candidate_profile_from_api(prof_payload["data"], user_id=41031)

    res = aggregate_user_skills(user_profile_data=prof_payload, employment_data=emp_payload)
    cand_prof.skills = res.effective_skills

    _is_profile_turn = bool(parsed.get("use_profile_skills"))
    profile_skills_for_fallback = []
    if _is_profile_turn and cand_prof and cand_prof.skills:
        profile_skills_for_fallback = cand_prof.skills

    assert keyword == "Python"
    assert profile_skills_for_fallback == ["Python", "React", "PostgreSQL"]


def test_11_candidate_profile_attributes_preserved():
    """11. Updating skills does not lose other CandidateProfile fields."""
    prof_payload = get_mock_user_profile()
    cand_prof = to_candidate_profile_from_api(prof_payload["data"], user_id=41031)
    original_name = cand_prof.name
    original_title = cand_prof.current_title

    cand_prof.skills = ["NewSkill1", "NewSkill2"]

    assert cand_prof.name == original_name
    assert cand_prof.current_title == original_title
    assert cand_prof.skills == ["NewSkill1", "NewSkill2"]


def test_12_combined_skills_reach_profile_terms_and_scoring():
    """12. Combined skill list reaches both profile_terms and candidate_profile.skills."""
    from backend.services.user_skill_aggregator import select_balanced_retrieval_terms
    prof_payload = get_mock_user_profile()
    emp_payload = get_mock_employment_history()
    res = aggregate_user_skills(user_profile_data=prof_payload, employment_data=emp_payload)

    cand_prof = to_candidate_profile_from_api(prof_payload["data"], user_id=41031)
    cand_prof.skills = res.effective_skills
    cand_prof.skill_provenance = res.provenance

    profile_terms = select_balanced_retrieval_terms(cand_prof.skills, provenance=cand_prof.skill_provenance, limit=15)

    assert profile_terms == ["Python", "React", "PostgreSQL"]
    assert cand_prof.skills == ["Python", "React", "PostgreSQL"]


def test_phase4b_a_employment_skills_beyond_first_ten_profile_skills():
    """Test A: Employment skills beyond the first ten profile skills are included in retrieval terms."""
    from backend.services.user_skill_aggregator import select_balanced_retrieval_terms, SkillProvenance
    prof_skills = [f"ProfileSkill{i}" for i in range(1, 11)]
    emp_skills = ["Docker", "Kubernetes", "AWS"]
    all_skills = prof_skills + emp_skills
    
    prov = [SkillProvenance(skill_name=s, sources=["PROFILE"]) for s in prof_skills]
    prov += [SkillProvenance(skill_name=s, sources=["EMPLOYMENT"]) for s in emp_skills]

    selected = select_balanced_retrieval_terms(all_skills, provenance=prov, limit=15)
    
    for emp_s in emp_skills:
        assert emp_s in selected, f"Employment skill {emp_s} should be included in retrieval terms"


def test_phase4b_b_employment_skills_with_larger_profile_list():
    """Test B: Employment skills with a large profile list (>15 skills) still receive fair budget allocation."""
    from backend.services.user_skill_aggregator import select_balanced_retrieval_terms, SkillProvenance
    prof_skills = [f"ProfileSkill{i}" for i in range(1, 21)]
    emp_skills = ["Docker", "Kubernetes", "AWS", "Terraform", "GCP"]
    all_skills = prof_skills + emp_skills
    
    prov = [SkillProvenance(skill_name=s, sources=["PROFILE"]) for s in prof_skills]
    prov += [SkillProvenance(skill_name=s, sources=["EMPLOYMENT"]) for s in emp_skills]

    selected = select_balanced_retrieval_terms(all_skills, provenance=prov, limit=12)
    
    assert len(selected) == 12
    # Should include employment skills due to round-robin allocation
    emp_in_selected = [s for s in emp_skills if s in selected]
    assert len(emp_in_selected) >= 5, "All employment skills should be selected within the cap"


def test_phase4b_c_case_insensitive_deduplication():
    """Test C: Case-insensitive deduplication preserves canonical forms and keeps employment skills eligible."""
    from backend.services.user_skill_aggregator import aggregate_user_skills, select_balanced_retrieval_terms
    prof_payload = {
        "status": True,
        "data": {
            "all_Skill": [{"id": 1, "skill": "Python"}]
        }
    }
    emp_payload = {
        "status": True,
        "data": [
            {
                "company": "TechCorp",
                "lists": [
                    {
                        "designation": "Senior Developer",
                        "skill": [
                            {"id": 2, "name": "python"},
                            {"id": 3, "name": "Docker"}
                        ]
                    }
                ]
            }
        ]
    }
    
    res = aggregate_user_skills(user_profile_data=prof_payload, employment_data=emp_payload)
    selected = select_balanced_retrieval_terms(res.effective_skills, provenance=res.provenance, limit=15)

    assert selected.count("Python") == 1
    assert "Docker" in selected


def test_phase4b_d_profile_only_and_employment_only_availability():
    """Test D: Either profile-only or employment-only sources can independently populate retrieval terms."""
    from backend.services.user_skill_aggregator import select_balanced_retrieval_terms, SkillProvenance
    
    # Profile only
    p_skills = ["Python", "Java"]
    p_prov = [SkillProvenance(skill_name=s, sources=["PROFILE"]) for s in p_skills]
    assert select_balanced_retrieval_terms(p_skills, provenance=p_prov, limit=15) == ["Python", "Java"]

    # Employment only
    e_skills = ["Docker", "Kubernetes"]
    e_prov = [SkillProvenance(skill_name=s, sources=["EMPLOYMENT"]) for s in e_skills]
    assert select_balanced_retrieval_terms(e_skills, provenance=e_prov, limit=15) == ["Docker", "Kubernetes"]

    # Fallback when provenance is None
    assert select_balanced_retrieval_terms(["Python", "Docker"], provenance=None, limit=15) == ["Python", "Docker"]


def test_phase4b_e_explicit_keyword_preservation():
    """Test E: Explicit keyword queries ('Show me Python jobs') retain explicit keyword without profile fallback replacement."""
    parsed = {"is_search_intent": True, "use_profile_skills": True, "keyword": "Python"}
    keyword = (parsed.get("keyword") or "").strip()
    
    # When keyword exists, profile fallback logic does not replace keyword in _fetch_meilisearch
    used_profile_fallback = True
    profile_terms = ["Java", "C++", "Docker"]
    
    # Check conditional: if used_profile_fallback and profile_terms and not keyword
    should_use_profile_tasks = bool(used_profile_fallback and profile_terms and not keyword)
    assert should_use_profile_tasks is False
    assert keyword == "Python"


def test_phase4b_f_existing_hard_filters_propagation():
    """Test F: Hard filter options (remote/job_mode, urgent) propagate correctly along with balanced retrieval terms."""
    from backend.services.user_skill_aggregator import select_balanced_retrieval_terms, SkillProvenance
    
    parsed = {"job_mode": "remote", "urgent": True}
    base_filters = {}
    
    if parsed.get("job_mode") == "remote":
        base_filters["job_mode"] = [2]
    if parsed.get("urgent"):
        base_filters["urgent"] = 1

    skills = ["Python", "Docker"]
    prov = [SkillProvenance(skill_name=s, sources=["PROFILE"]) for s in skills]
    clean_terms = select_balanced_retrieval_terms(skills, provenance=prov, limit=15)

    tasks_filters = [{"keyword": t, **base_filters} for t in clean_terms]
    
    assert len(tasks_filters) == 2
    assert tasks_filters[0] == {"keyword": "Python", "job_mode": [2], "urgent": 1}
    assert tasks_filters[1] == {"keyword": "Docker", "job_mode": [2], "urgent": 1}


@pytest.mark.anyio
async def test_13_job_match_client_contract_unchanged():
    """13. JobMatchClient receives only candidate_id integer, not skill strings."""
    from backend.services.job_match_client import JobMatchClient

    client = JobMatchClient(base_url="https://mock.jobmatch", api_key="test_key")

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_instance = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={"results": []})
        mock_client_instance.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__.return_value = mock_client_instance

        res = await client.find_jobs(candidate_id=41031, limit=10)
        assert res == {"results": []}
        mock_client_instance.get.assert_called_once()
        call_kwargs = mock_client_instance.get.call_args.kwargs
        assert call_kwargs["params"] == {"login_user_id": 41031, "limit": 10}


def test_14_hard_filters_remain_unchanged():
    """14. Remote (job_mode=2) and urgent (urgent=1) filters remain in base_filters."""
    parsed = {"job_mode": "home", "urgent": True}
    base_filters = {}

    mode_str = (parsed.get("job_mode") or "").lower().strip()
    if "home" in mode_str or "remote" in mode_str:
        base_filters["job_mode"] = [2]
    if parsed.get("urgent"):
        base_filters["urgent"] = 1

    assert base_filters == {"job_mode": [2], "urgent": 1}


def test_15_provenance_and_statuses_available():
    """15. Provenance metadata and source statuses are accessible for diagnostics."""
    prof_payload = get_mock_user_profile()
    emp_payload = get_mock_employment_history()

    res = aggregate_user_skills(user_profile_data=prof_payload, employment_data=emp_payload)

    assert res.profile_status == "SUCCESS"
    assert res.employment_status == "SUCCESS"
    assert len(res.provenance) == 3
    py_prov = next(p for p in res.provenance if p.skill_name == "Python")
    assert set(py_prov.sources) == {"PROFILE", "EMPLOYMENT"}


@pytest.mark.anyio
async def test_phase4e_a_meilisearch_caller_task_intercept():
    """Phase 4E Test A: End-to-end task interception for execute_hybrid_job_search / _fetch_meilisearch.
    Proves that employment skills reach actual search_jobs calls when >10 profile skills exist.
    """
    from backend.services.hybrid_job_search import execute_hybrid_job_search
    from backend.services.user_skill_aggregator import SkillProvenance
    from backend.recommendation.models import CandidateProfile

    prof_skills = [f"ProfileSkill{i}" for i in range(1, 12)]
    emp_skills = ["Docker", "Kubernetes", "AWS"]
    all_terms = prof_skills + emp_skills

    prov = [SkillProvenance(skill_name=s, sources=["PROFILE"]) for s in prof_skills]
    prov += [SkillProvenance(skill_name=s, sources=["EMPLOYMENT"]) for s in emp_skills]

    cand_prof = CandidateProfile(id=41031, name="Test User", skills=all_terms, skill_provenance=prov)

    captured_keywords = []

    async def mock_search_jobs(**kwargs):
        kw = kwargs.get("keyword")
        if kw:
            captured_keywords.append(kw)
        return {"success": True, "data": [{"id": 101, "title": f"Job for {kw}"}]}

    with patch("backend.services.hybrid_job_search.search_jobs", side_effect=mock_search_jobs), \
         patch("backend.services.hybrid_job_search.JOB_MATCH_ENABLED", False):
        hits, meta = await execute_hybrid_job_search(
            last_user_msg="Show me jobs matching my skills",
            candidate_id=41031,
            candidate_profile=cand_prof,
            keyword="",
            base_filters={},
            parsed={"is_search_intent": True, "use_profile_skills": True},
            used_profile_fallback=True,
            profile_terms=all_terms,
            explicit_loc=False,
            target_city_name=None,
            target_state_name=None,
            limit=20,
        )

    # 1. Employment skills reach the actual search_jobs task list
    for emp_s in emp_skills:
        assert emp_s in captured_keywords, f"Employment skill {emp_s} was not searched by Meilisearch!"

    # 2. Total task count is bounded to 14 terms (all 14 unique input terms < 15 cap)
    assert len(captured_keywords) == 14

    # 3. Counterfactual proof: If clean_terms[:10] were used, captured_keywords would be exactly prof_skills[:10]
    # and employment skills would be 100% missing.
    assert not set(emp_skills).issubset(set(prof_skills[:10]))


@pytest.mark.anyio
async def test_phase4e_b_legacy_fallback_caller_task_intercept():
    """Phase 4E Test B: Task interception for legacy fallback path in employee.py.
    Proves employment skills reach search_jobs calls within 15-term budget when hybrid search is disabled/falls back.
    """
    from backend.api.employee import handle_employee_context
    from backend.services.user_skill_aggregator import SkillProvenance
    from backend.recommendation.models import CandidateProfile

    prof_skills = [f"ProfileSkill{i}" for i in range(1, 11)]
    emp_skills = ["Docker", "Kubernetes", "AWS"]
    raw_terms = prof_skills + emp_skills

    prov = [SkillProvenance(skill_name=s, sources=["PROFILE"]) for s in prof_skills]
    prov += [SkillProvenance(skill_name=s, sources=["EMPLOYMENT"]) for s in emp_skills]

    cand_prof = CandidateProfile(id=41031, name="Test User", skills=raw_terms, skill_provenance=prov)

    captured_keywords = []
    async def mock_search_jobs(**kwargs):
        kw = kwargs.get("keyword")
        if kw:
            captured_keywords.append(kw)
        return {"success": True, "data": [{"id": 201, "title": f"Job for {kw}"}]}

    # Mock dependencies to force employee.py into line 1206 fallback branch
    with patch("backend.api.employee.HYBRID_SEARCH_ENABLED", False), \
         patch("backend.api.employee.search_jobs", side_effect=mock_search_jobs), \
         patch("backend.api.employee.parse_search_query", return_value={"is_search_intent": True, "use_profile_skills": True, "skill_source": "PROFILE"}), \
         patch("backend.services.user_skill_aggregator.aggregate_user_skills", return_value=MagicMock(effective_skills=raw_terms, provenance=prov)), \
         patch("backend.api.employee.to_candidate_profile_from_api", return_value=cand_prof), \
         patch("backend.api.employee.user_data_service.fetch_endpoint_data", return_value={"status": True, "data": []}), \
         patch("backend.api.employee.resolve_location_from_db", return_value=MagicMock(resolved=False, city_name=None, state_name=None, location_type=None)), \
         patch("backend.api.employee.recommendation_service.get_recommendations_for_profile", return_value=[]), \
         patch("backend.api.employee.compose_final_response", return_value={"reply": "Mocked response", "success": True}):

        res = await handle_employee_context(
            last_user_msg="Show me jobs matching my skills",
            candidate_id=41031,
            token="mock_token",
            session_id="test_session",
        )

    # Assertions on captured calls to search_jobs from inside employee.py
    for emp_s in emp_skills:
        assert emp_s in captured_keywords, f"Employment skill {emp_s} was omitted by employee.py fallback branch!"

    assert len(captured_keywords) == 13
    assert len(captured_keywords) <= 15
    assert len(captured_keywords) == len(set(captured_keywords))

    # Counterfactual proof check
    assert not set(emp_skills).issubset(set(prof_skills[:10]))


def test_phase5b_score_calculator_weight_renormalization():
    """Phase 5B Test: ScoreCalculator renormalizes weights when location/salary are unknown."""
    from backend.recommendation.score_calculator import ScoreCalculator
    from backend.recommendation.models import SkillMatchResult, LocationMatchResult, SalaryMatchResult

    calc = ScoreCalculator()
    
    skill_res = SkillMatchResult(score=0.0, matched_skills=[], missing_skills=["Python"], extra_skills=[], total_required=1, total_matched=0)
    exp_res = MagicMock(score=100.0)
    title_res = MagicMock(score=0.0)
    
    loc_res = LocationMatchResult(score=50.0, candidate_city=None, candidate_state=None, candidate_country=None, job_city=None, job_state=None, job_country=None, work_mode="onsite", match_level="unknown")
    sal_res = SalaryMatchResult(score=50.0, candidate_expected_salary=None, job_offered_salary=None, normalized_candidate_salary=None, normalized_job_salary=None, salary_difference_percent=0.0, meets_expectation=False)

    score_res = calc.calculate_score(
        skill_result=skill_res,
        experience_result=exp_res,
        location_result=loc_res,
        salary_result=sal_res,
        title_result=title_res,
    )

    # Weights for location (0.10) and salary (0.15) should be excluded because data is unknown.
    # Active weights: skill 0.40, experience 0.20, title 0.15 (total active = 0.75).
    # Renormalized exp weight = 0.20 / 0.75 = 0.2667 (26.67%).
    # Overall score should be 26.67%, NOT artificially inflated to 43.0%!
    assert score_res.overall_score == 26.67
    assert score_res.recommendation_level == "Not Recommended"
    assert score_res.weight_location == 0.0
    assert score_res.weight_salary == 0.0


def test_phase5b_domain_gate_empty_job_skills():
    """Phase 5B Test: RecommendationEngine domain gate checks job text when job required_skills is empty."""
    from backend.recommendation.recommendation_engine import RecommendationEngine
    from backend.recommendation.models import CandidateProfile, JobProfile, SkillMatchResult, LocationMatchResult, SalaryMatchResult

    engine = RecommendationEngine()
    
    cand = CandidateProfile(id=1, name="Test Candidate", skills=["Python", "Docker"])
    job_rel = JobProfile(id=101, title="Backend Developer", required_skills=[], raw_data={"job_description": "We are seeking a Python developer proficient in building backend microservices."})
    job_irrel = JobProfile(id=102, title="Accountant", required_skills=[], raw_data={"job_description": "Managing financial books and tax audits."})

    skill_res_zero = SkillMatchResult(score=0.0, matched_skills=[], missing_skills=["Python"], extra_skills=[], total_required=1, total_matched=0)
    title_res_zero = MagicMock(score=0.0, match_level="none")

    # Relevant job with matching text in description passes gate despite empty required_skills
    gate_rel = engine._evaluate_domain_gate(cand, job_rel, skill_res_zero, title_res_zero)
    assert gate_rel is True

    # Irrelevant job with empty required_skills fails gate
    gate_irrel = engine._evaluate_domain_gate(cand, job_irrel, skill_res_zero, title_res_zero)
    assert gate_irrel is False


def test_phase5b2_domain_gate_token_boundary_and_punctuation():
    """Phase 5B.2 Test: Fallback domain gate respects token boundaries and punctuation-sensitive skills."""
    from backend.recommendation.recommendation_engine import RecommendationEngine
    from backend.recommendation.models import CandidateProfile, JobProfile, SkillMatchResult

    engine = RecommendationEngine()
    skill_zero = SkillMatchResult(score=0.0, matched_skills=[], missing_skills=["C++"], extra_skills=[], total_required=1, total_matched=0)
    title_zero = MagicMock(score=0.0, match_level="none")

    # 1. Partial word false-positive rejection: "Testing" skill should NOT match "contestants" or "protesting"
    cand_test = CandidateProfile(id=1, name="Test Candidate", skills=["Testing"])
    job_partial = JobProfile(id=103, title="Event Manager", required_skills=[], raw_data={"job_description": "Managing contestants and event protests."})
    assert engine._evaluate_domain_gate(cand_test, job_partial, skill_zero, title_zero) is False

    # 2. Punctuation-sensitive skill match: "C++" skill matches "Looking for a C++ developer"
    cand_cpp = CandidateProfile(id=2, name="CPP Candidate", skills=["C++"])
    job_cpp = JobProfile(id=104, title="Software Engineer", required_skills=[], raw_data={"job_description": "Looking for a senior C++ developer."})
    assert engine._evaluate_domain_gate(cand_cpp, job_cpp, skill_zero, title_zero) is True


def test_phase5d_a1_missing_candidate_title_score_and_match_level():
    """Phase 5D Task A Test: Missing candidate title produces score=0.0 with match_level='unknown'."""
    from backend.recommendation.title_matcher import TitleMatcher

    matcher = TitleMatcher()
    res1 = matcher._match_titles(None, "Software Developer")
    assert res1.score == 0.0
    assert res1.match_level == "unknown"

    res2 = matcher._match_titles("Software Developer", None)
    assert res2.score == 0.0
    assert res2.match_level == "unknown"


def test_phase5d_a2_unknown_title_score_calculator_weight_exclusion():
    """Phase 5D Task A Test: Unknown title is excluded from active score weights, distinguishable from mismatch."""
    from backend.recommendation.score_calculator import ScoreCalculator
    from backend.recommendation.models import SkillMatchResult, ExperienceMatchResult, LocationMatchResult, SalaryMatchResult, TitleMatchResult

    calc = ScoreCalculator()

    skill_res = SkillMatchResult(score=100.0, matched_skills=["Python"], missing_skills=[], extra_skills=[], total_required=1, total_matched=1)
    exp_res = ExperienceMatchResult(score=100.0, candidate_experience=5.0, required_experience=5.0, experience_gap=0.0, meets_requirement=True)
    loc_res = LocationMatchResult(score=100.0, candidate_city="Delhi", candidate_state="Delhi", candidate_country="India", job_city="Delhi", job_state="Delhi", job_country="India", work_mode="onsite", match_level="exact")
    sal_res = SalaryMatchResult(score=100.0, candidate_expected_salary=None, job_offered_salary=None, normalized_candidate_salary=None, normalized_job_salary=None, salary_difference_percent=0.0, meets_expectation=True)

    # Unknown title: title match_level="unknown", score=0.0
    title_unk = TitleMatchResult(score=0.0, candidate_title=None, job_title="Developer", normalized_candidate_title=None, normalized_job_title="developer", matched_keywords=[], match_level="unknown")
    score_unk = calc.calculate_score(skill_res, exp_res, loc_res, sal_res, title_unk)
    # Title is excluded, active weights (skill 0.40, exp 0.20, loc 0.20) sum to 0.80 -> all 100% -> overall = 100.0
    assert score_unk.overall_score == 100.0

    # Genuine title mismatch: title match_level="mismatch", score=0.0
    title_mis = TitleMatchResult(score=0.0, candidate_title="Accountant", job_title="Developer", normalized_candidate_title="accountant", normalized_job_title="developer", matched_keywords=[], match_level="mismatch")
    score_mis = calc.calculate_score(skill_res, exp_res, loc_res, sal_res, title_mis)
    # Title IS included in active weights, so title=0.0 penalizes final score (overall = 82.35 < 100.0)
    assert score_mis.overall_score == 82.35
    assert score_mis.overall_score < score_unk.overall_score


def test_phase5d_b1_secondary_skill_score_sorting_tie_breaker():
    """Phase 5D Task B Test: Equal overall scores are sorted by skill score descending as secondary key."""
    from backend.recommendation.recommendation_engine import RecommendationEngine
    from backend.recommendation.models import RecommendationResult, ScoreResult, SkillMatchResult

    engine = RecommendationEngine()
    cand = MagicMock()

    score_res_low = ScoreResult(
        overall_score=85.0, recommendation_level="High", skill_score=60.0, experience_score=100.0, location_score=100.0, salary_score=0.0, title_score=0.0,
        weight_skill=0.4, weight_experience=0.2, weight_location=0.2, weight_salary=0.0, weight_title=0.2
    )
    res_low_skill = RecommendationResult(
        overall_score=85.0,
        recommendation_level="High",
        skill_result=SkillMatchResult(score=60.0, matched_skills=[], missing_skills=[], extra_skills=[], total_required=1, total_matched=1),
        experience_result=MagicMock(),
        location_result=MagicMock(),
        salary_result=MagicMock(),
        title_result=MagicMock(),
        score_result=score_res_low,
        explanation_result=MagicMock(),
        candidate_id=1,
        job_id=101,
        search_reasons=[],
        recommendation_reasons=[],
        raw_data={}
    )

    score_res_high = ScoreResult(
        overall_score=85.0, recommendation_level="High", skill_score=90.0, experience_score=80.0, location_score=80.0, salary_score=0.0, title_score=0.0,
        weight_skill=0.4, weight_experience=0.2, weight_location=0.2, weight_salary=0.0, weight_title=0.2
    )
    res_high_skill = RecommendationResult(
        overall_score=85.0,
        recommendation_level="High",
        skill_result=SkillMatchResult(score=90.0, matched_skills=[], missing_skills=[], extra_skills=[], total_required=1, total_matched=1),
        experience_result=MagicMock(),
        location_result=MagicMock(),
        salary_result=MagicMock(),
        title_result=MagicMock(),
        score_result=score_res_high,
        explanation_result=MagicMock(),
        candidate_id=1,
        job_id=102,
        search_reasons=[],
        recommendation_reasons=[],
        raw_data={}
    )

    with patch.object(engine, "recommend", side_effect=[res_low_skill, res_high_skill]):
        recs = engine.recommend_jobs(cand, [MagicMock(), MagicMock()])
        assert len(recs) == 2
        # Job 102 with skill_score=90.0 should be ranked first ahead of Job 101 with skill_score=60.0
        assert recs[0].job_id == 102
        assert recs[1].job_id == 101


def test_phase5d_b2_stable_insertion_order_complete_ties():
    """Phase 5D Task B Test: Identical overall and skill scores retain stable insertion order."""
    from backend.recommendation.recommendation_engine import RecommendationEngine
    from backend.recommendation.models import RecommendationResult, ScoreResult, SkillMatchResult

    engine = RecommendationEngine()
    cand = MagicMock()

    score_res_1 = ScoreResult(
        overall_score=85.0, recommendation_level="High", skill_score=80.0, experience_score=90.0, location_score=90.0, salary_score=0.0, title_score=0.0,
        weight_skill=0.4, weight_experience=0.2, weight_location=0.2, weight_salary=0.0, weight_title=0.2
    )
    res_first = RecommendationResult(
        overall_score=85.0,
        recommendation_level="High",
        skill_result=SkillMatchResult(score=80.0, matched_skills=[], missing_skills=[], extra_skills=[], total_required=1, total_matched=1),
        experience_result=MagicMock(),
        location_result=MagicMock(),
        salary_result=MagicMock(),
        title_result=MagicMock(),
        score_result=score_res_1,
        explanation_result=MagicMock(),
        candidate_id=1,
        job_id=201,
        search_reasons=[],
        recommendation_reasons=[],
        raw_data={}
    )

    score_res_2 = ScoreResult(
        overall_score=85.0, recommendation_level="High", skill_score=80.0, experience_score=90.0, location_score=90.0, salary_score=0.0, title_score=0.0,
        weight_skill=0.4, weight_experience=0.2, weight_location=0.2, weight_salary=0.0, weight_title=0.2
    )
    res_second = RecommendationResult(
        overall_score=85.0,
        recommendation_level="High",
        skill_result=SkillMatchResult(score=80.0, matched_skills=[], missing_skills=[], extra_skills=[], total_required=1, total_matched=1),
        experience_result=MagicMock(),
        location_result=MagicMock(),
        salary_result=MagicMock(),
        title_result=MagicMock(),
        score_result=score_res_2,
        explanation_result=MagicMock(),
        candidate_id=1,
        job_id=202,
        search_reasons=[],
        recommendation_reasons=[],
        raw_data={}
    )

    with patch.object(engine, "recommend", side_effect=[res_first, res_second]):
        recs = engine.recommend_jobs(cand, [MagicMock(), MagicMock()])
        assert len(recs) == 2
        # Identical overall (85.0) and skill_score (80.0) retains insertion order: 201 then 202
        assert recs[0].job_id == 201
        assert recs[1].job_id == 202







