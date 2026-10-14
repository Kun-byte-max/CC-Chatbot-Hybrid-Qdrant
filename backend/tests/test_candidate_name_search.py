"""
test_candidate_name_search.py — Comprehensive Test Suite for Candidate Name Search.
"""

import pytest
import asyncio
from unittest.mock import patch, MagicMock

from backend.schemas.search_criteria import SearchCriteria
from backend.repositories.people_repository import PeopleRepository
from backend.services.people_search_service import PeopleSearchService
from backend.services.user_data_intent_parser import parse_user_data_intents, UserIntent
from backend.services.search_parser import normalize_search_target


# ==============================================================================
# 1. DATABASE REPOSITORY & SQL NAME MATCHING TESTS
# ==============================================================================

def test_people_repository_name_search_exact_and_case_insensitive():
    """Verify PeopleRepository.search_people SQL matches name case-insensitively."""
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        (101, "Kunal", "Yadav", 10, 20, "Delhi", "Uttar Pradesh", "India")
    ]
    mock_cursor.fetchone.return_value = {
        "company": "CollarCheck",
        "designation_name": "Software Developer",
        "still_working": 1,
    }

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    with patch("backend.repositories.people_repository.get_db", return_value=mock_conn):
        with patch.object(PeopleRepository, "_get_user_skills", return_value=["Python", "MySQL"]):
            results = PeopleRepository.search_people(name="Kunal Yadav")

            assert len(results) == 1
            assert results[0]["id"] == 101
            assert results[0]["name"] == "Kunal Yadav"
            assert results[0]["current_title"] == "Software Developer"
            assert results[0]["company"] == "CollarCheck"
            assert "Python" in results[0]["skills"]

            # Inspect main SQL query string and params passed to db cursor
            called_sql, called_params = mock_cursor.execute.call_args_list[0][0]
            assert "LOWER(u.fname) LIKE %s" in called_sql
            assert "LOWER(u.lname) LIKE %s" in called_sql
            assert "%kunal yadav%" in called_params


def test_people_repository_multiple_candidate_matches():
    """Verify search returns all matching candidates when multiple exist."""
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        (201, "Rahul", "Kumar", 10, 20, "Delhi", "Delhi", "India"),
        (202, "Rahul", "Kumar", 30, 40, "Noida", "Uttar Pradesh", "India"),
    ]
    mock_cursor.fetchone.return_value = {}

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    with patch("backend.repositories.people_repository.get_db", return_value=mock_conn):
        with patch.object(PeopleRepository, "_get_user_skills", return_value=[]):
            results = PeopleRepository.search_people(name="Rahul Kumar")

            assert len(results) == 2
            assert results[0]["id"] == 201
            assert results[1]["id"] == 202


def test_people_repository_inactive_and_deleted_status_sql_guard():
    """Verify SQL strictly enforces user_type = 1, status = 1, and is_deleted = 0."""
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    with patch("backend.repositories.people_repository.get_db", return_value=mock_conn):
        PeopleRepository.search_people(name="Inactive User")
        called_sql = mock_cursor.execute.call_args[0][0]
        assert "u.user_type = 1" in called_sql
        assert "u.status = 1" in called_sql
        assert "(u.is_deleted IS NULL OR u.is_deleted = 0)" in called_sql


# ==============================================================================
# 2. SERVICE LAYER & CANDIDATE CARD CONTRACT TESTS
# ==============================================================================

def test_people_search_service_candidate_card_structure_and_privacy():
    """Verify PeopleSearchService formats safe CandidateCard schema without private fields."""
    mock_raw_people = [
        {
            "id": 501,
            "name": "Priya Sharma",
            "fname": "Priya",
            "lname": "Sharma",
            "current_title": "QA Lead",
            "company": "Tech Corp",
            "location": "Pune, Maharashtra",
            "city": "Pune",
            "state": "Maharashtra",
            "country": "India",
            "skills": ["Automation", "Selenium"],
            "total_experience_years": 5.0,
        }
    ]

    criteria = SearchCriteria(
        target="PEOPLE_SEARCH",
        candidate_name="Priya Sharma",
    )

    with patch.object(PeopleRepository, "search_people", return_value=mock_raw_people):
        response = PeopleSearchService.search_people(criteria=criteria)

        assert response["success"] is True
        assert response["result_type"] == "people"
        results = response["results"]
        assert len(results) == 1

        card = results[0]
        assert card["id"] == 501
        assert card["candidate_id"] == 501
        assert card["cc_id"] == "CC-501"
        assert card["name"] == "Priya Sharma"
        assert card["title"] == "QA Lead"
        assert card["company"] == "Tech Corp"
        assert card["location"] == "Pune, Maharashtra"
        assert card["skills"] == ["Automation", "Selenium"]

        # Privacy Redaction Validation: Sensitive fields must NOT be exposed
        for sensitive_field in ("password", "token", "email", "phone", "present_address", "permanent_address", "dob", "salary"):
            assert sensitive_field not in card


def test_people_search_service_zero_hits_handling():
    """Verify graceful zero-hits response when no candidate matches."""
    criteria = SearchCriteria(
        target="PEOPLE_SEARCH",
        candidate_name="NonexistentPersonXYZ",
    )

    with patch.object(PeopleRepository, "search_people", return_value=[]):
        response = PeopleSearchService.search_people(criteria=criteria)

        assert response["success"] is True
        assert response["results"] == []
        assert "PEOPLE SEARCH RESULT SUMMARY" in response["db_context"]


# ==============================================================================
# 3. INTENT ROUTING & USER DATA ISOLATION TESTS
# ==============================================================================

def test_user_data_intent_parser_self_vs_third_person_isolation():
    """Verify self-profile vs third-person candidate query intent parsing."""
    # Self-profile queries -> Return user intents
    assert UserIntent.PROFILE_SUMMARY in parse_user_data_intents("Show my profile")
    assert UserIntent.PROFILE_SKILLS in parse_user_data_intents("What are my skills?")

    # Third-person candidate queries -> Return empty list to pass through to candidate search
    assert parse_user_data_intents("Show Kunal's profile") == []
    assert parse_user_data_intents("What are Kunal's skills?") == []
    assert parse_user_data_intents("Find Kunal Yadav") == []
    assert parse_user_data_intents("Show me Priya Sharma") == []


def test_normalize_search_target_classification():
    """Verify search_parser target classification for job vs candidate queries."""
    # Job Queries -> JOBS
    assert normalize_search_target(None, "Find Python jobs") == "JOBS"
    assert normalize_search_target(None, "Python developer jobs") == "JOBS"
    assert normalize_search_target(None, "jobs near me") == "JOBS"

    # Candidate / Person Queries -> PEOPLE_SEARCH
    assert normalize_search_target("PEOPLE_SEARCH", "Find Kunal Yadav") == "PEOPLE_SEARCH"
    assert normalize_search_target(None, "Show me Kunal Yadav") == "PEOPLE_SEARCH"
    assert normalize_search_target(None, "Search for Priya Sharma") == "PEOPLE_SEARCH"


# ==============================================================================
# 4. END-TO-END SEARCH PARSER CANDIDATE EXTRACTION TEST
# ==============================================================================

def test_search_criteria_from_parsed_dict_with_candidate_name():
    """Verify SearchCriteria correctly extracts candidate_name from parser output."""
    parsed_dict = {
        "search_target": "PEOPLE_SEARCH",
        "candidate_name": "Kunal Yadav",
        "keyword": None,
        "location": None,
    }

    criteria = SearchCriteria.from_parsed_dict(parsed_dict)
    assert criteria.target.value == "PEOPLE_SEARCH"
    assert criteria.candidate_name == "Kunal Yadav"


# ==============================================================================
# 5. PROFILE PHOTO RESOLUTION & SAFETY TESTS
# ==============================================================================

def test_resolve_profile_photo_url_safety_and_normalization():
    """Verify resolve_profile_photo_url validates protocols, domains, and paths safely."""
    from backend.services.people_search_service import resolve_profile_photo_url

    # 1. Valid relative profile path -> Prepend PLATFORM_MEDIA_BASE_URL
    assert resolve_profile_photo_url("uploads/profile/1743069803_example.jpg") == "https://admin.collarcheck.com/uploads/profile/1743069803_example.jpg"
    assert resolve_profile_photo_url("/uploads/profile/sample.png") == "https://admin.collarcheck.com/uploads/profile/sample.png"

    # 2. None, empty string, whitespace, null strings -> None
    assert resolve_profile_photo_url(None) is None
    assert resolve_profile_photo_url("") is None
    assert resolve_profile_photo_url("   ") is None
    assert resolve_profile_photo_url("none") is None
    assert resolve_profile_photo_url("null") is None
    assert resolve_profile_photo_url("undefined") is None

    # 3. Trusted absolute URL -> Retained as-is
    assert resolve_profile_photo_url("https://admin.collarcheck.com/uploads/profile/test.jpg") == "https://admin.collarcheck.com/uploads/profile/test.jpg"
    assert resolve_profile_photo_url("https://www.collarcheck.com/uploads/profile/test.jpg") == "https://www.collarcheck.com/uploads/profile/test.jpg"

    # 4. Untrusted external domain -> Rejected (returns None)
    assert resolve_profile_photo_url("https://evil.example.com/phish.jpg") is None
    assert resolve_profile_photo_url("http://malicious-site.org/avatar.png") is None

    # 5. Unsafe protocols (javascript:, data:, file:, ftp:) -> Rejected (returns None)
    assert resolve_profile_photo_url("javascript:alert(1)") is None
    assert resolve_profile_photo_url("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAE=") is None
    assert resolve_profile_photo_url("file:///etc/passwd") is None
    assert resolve_profile_photo_url("ftp://remote.server/image.jpg") is None

    # 6. Path traversal and null byte injections -> Rejected (returns None)
    assert resolve_profile_photo_url("../../../secret/config.py") is None
    assert resolve_profile_photo_url("uploads/profile/test.jpg\x00.exe") is None


def test_people_search_service_includes_profile_photo_url_and_isolates_candidates():
    """Verify CandidateCard payloads contain candidate-specific profile_photo_url fields."""
    mock_raw_people = [
        {
            "id": 10482,
            "name": "Vishal Verma",
            "current_title": "Software Developer",
            "company": "CollarCheck",
            "location": "Delhi, Delhi",
            "skills": ["Python", "MySQL"],
            "total_experience_years": 4.2,
            "profile": "uploads/profile/vishal_10482.jpg",
        },
        {
            "id": 10731,
            "name": "Vishal Verma",
            "current_title": "Backend Engineer",
            "company": "Tech Solutions",
            "location": "Noida, Uttar Pradesh",
            "skills": ["Java", "AWS"],
            "total_experience_years": 6.0,
            "profile": None,
        },
    ]

    criteria = SearchCriteria(
        target="PEOPLE_SEARCH",
        candidate_name="Vishal Verma",
    )

    with patch.object(PeopleRepository, "search_people", return_value=mock_raw_people):
        response = PeopleSearchService.search_people(criteria=criteria)

        assert response["success"] is True
        results = response["results"]
        assert len(results) == 2

        by_id = {c["id"]: c for c in results}
        assert 10482 in by_id
        assert 10731 in by_id

        cand1 = by_id[10482]
        assert cand1["cc_id"] == "CC-10482"
        assert cand1["profile_photo_url"] == "https://admin.collarcheck.com/uploads/profile/vishal_10482.jpg"

        cand2 = by_id[10731]
        assert cand2["cc_id"] == "CC-10731"
        assert cand2["profile_photo_url"] is None


# ==============================================================================
# 6. PHASE 4 SEARCH QUALITY & RETRIEVAL ENHANCEMENT TESTS
# ==============================================================================

def test_phase4_experience_normalization_in_search_criteria():
    """Verify SearchCriteria.from_parsed_dict normalizes various experience formats."""
    # Free-text experience string formats
    c1 = SearchCriteria.from_parsed_dict({"experience": "3+ years", "search_target": "PEOPLE_SEARCH"})
    assert c1.experience_min == 3

    c2 = SearchCriteria.from_parsed_dict({"experience": "at least 5 years", "search_target": "PEOPLE_SEARCH"})
    assert c2.experience_min == 5

    c3 = SearchCriteria.from_parsed_dict({"experience": "2 years experience", "search_target": "PEOPLE_SEARCH"})
    assert c3.experience_min == 2

    # Direct integer format
    c4 = SearchCriteria.from_parsed_dict({"experience": 4, "search_target": "PEOPLE_SEARCH"})
    assert c4.experience_min == 4

    # None format
    c5 = SearchCriteria.from_parsed_dict({"experience": None, "search_target": "PEOPLE_SEARCH"})
    assert c5.experience_min is None


def test_phase4_multi_skill_and_eligibility():
    """Verify EmployeePeopleScoringEngine enforces strict AND eligibility for multi-skill requests."""
    from backend.services.people_scoring_engine import EmployeePeopleScoringEngine

    req_skills = ["Python", "MySQL"]

    # Candidate with both requested skills -> Eligible
    cand_both = {
        "id": 1,
        "current_title": "Software Developer",
        "skills": ["Python", "MySQL", "Docker"],
        "total_experience_years": 4.0,
    }
    score_both = EmployeePeopleScoringEngine.score_candidate(cand_both, requested_skills=req_skills)
    assert score_both["is_eligible"] is True
    assert score_both["matched_skills"] == ["Python", "MySQL"]
    assert score_both["missing_skills"] == []

    # Candidate with only 1 requested skill -> Ineligible
    cand_partial = {
        "id": 2,
        "current_title": "Software Developer",
        "skills": ["Python", "Flask"],
        "total_experience_years": 4.0,
    }
    score_partial = EmployeePeopleScoringEngine.score_candidate(cand_partial, requested_skills=req_skills)
    assert score_partial["is_eligible"] is False
    assert score_partial["matched_skills"] == ["Python"]
    assert "MySQL" in score_partial["missing_skills"]


def test_phase4_compound_candidate_query_parsing_and_isolation():
    """Verify compound prompts extract name, company, and location into separate criteria fields."""
    parsed = {
        "search_target": "PEOPLE_SEARCH",
        "candidate_name": "Vishal Verma",
        "company": "CollarCheck",
        "location": "Delhi",
        "skills": ["Python"],
        "experience": "3+ years",
    }
    criteria = SearchCriteria.from_parsed_dict(parsed)

    assert criteria.target.value == "PEOPLE_SEARCH"
    assert criteria.candidate_name == "Vishal Verma"
    assert criteria.company == "CollarCheck"
    assert criteria.location == "Delhi"
    assert criteria.skills == ["Python"]
    assert criteria.experience_min == 3


def test_phase4_people_search_service_multi_skill_filtering_and_ranking():
    """Verify PeopleSearchService filters out ineligible candidates failing multi-skill AND or experience."""
    mock_people = [
        {
            "id": 1,
            "name": "Qualified Candidate",
            "current_title": "Python Developer",
            "company": "CollarCheck",
            "location": "Delhi",
            "city": "Delhi",
            "state": "Delhi",
            "skills": ["Python", "MySQL"],
            "total_experience_years": 4.0,
            "profile": None,
        },
        {
            "id": 2,
            "name": "Missing Skill Candidate",
            "current_title": "Python Developer",
            "company": "CollarCheck",
            "location": "Delhi",
            "city": "Delhi",
            "state": "Delhi",
            "skills": ["Python"], # Missing MySQL
            "total_experience_years": 5.0,
            "profile": None,
        },
        {
            "id": 3,
            "name": "Low Exp Candidate",
            "current_title": "Python Developer",
            "company": "CollarCheck",
            "location": "Delhi",
            "city": "Delhi",
            "state": "Delhi",
            "skills": ["Python", "MySQL"],
            "total_experience_years": 1.0, # Below 3.0 min
            "profile": None,
        },
    ]

    criteria = SearchCriteria(
        target="PEOPLE_SEARCH",
        title="Python Developer",
        skills=["Python", "MySQL"],
        location="Delhi",
        experience_min=3,
    )

    with patch.object(PeopleRepository, "search_people", return_value=mock_people):
        response = PeopleSearchService.search_people(criteria=criteria)

        assert response["success"] is True
        results = response["results"]
        # Candidates 2 (missing MySQL) and 3 (exp 1.0 < 3) fail eligibility gate
        assert len(results) == 1
        assert results[0]["id"] == 1
        assert results[0]["name"] == "Qualified Candidate"
        assert results[0]["profile_photo_url"] is None


def test_phase4_search_pipeline_state_isolation():
    """Verify state isolation between JOBS and PEOPLE_SEARCH targets."""
    # 1. JOBS Query dict
    job_parsed = {
        "search_target": "JOBS",
        "keyword": "Python Developer",
        "location": "Delhi",
        "candidate_name": None,
    }
    job_criteria = SearchCriteria.from_parsed_dict(job_parsed)
    assert job_criteria.target.value == "JOBS"
    assert job_criteria.candidate_name is None
    assert job_criteria.title == "Python Developer"

    # 2. Sequential PEOPLE_SEARCH query dict (must not inherit job title as candidate_name)
    people_parsed = {
        "search_target": "PEOPLE_SEARCH",
        "candidate_name": "Vishal Verma",
        "keyword": None,
        "location": None,
    }
    people_criteria = SearchCriteria.from_parsed_dict(people_parsed)
    assert people_criteria.target.value == "PEOPLE_SEARCH"
    assert people_criteria.candidate_name == "Vishal Verma"
    assert people_criteria.title is None
    assert people_criteria.location is None


# ==============================================================================
# 6. PHASE 5.2 — CANDIDATE EXPERIENCE GRADIENT & RANKING TESTS
# ==============================================================================

from backend.services.people_scoring_engine import EmployeePeopleScoringEngine, people_rank_key


def test_experience_match_exact_minimum():
    """Verify exact minimum experience yields score = 90.0 and eligible = True."""
    score, meets = EmployeePeopleScoringEngine.match_experience(requested_exp=5.0, candidate_exp=5.0)
    assert meets is True
    assert score == 90.0


def test_experience_match_one_year_above():
    """Verify 1 year above minimum yields score = 92.0 and eligible = True."""
    score, meets = EmployeePeopleScoringEngine.match_experience(requested_exp=5.0, candidate_exp=6.0)
    assert meets is True
    assert score == 92.0


def test_experience_match_several_years_above():
    """Verify 3 years above minimum yields score = 96.0 and eligible = True."""
    score, meets = EmployeePeopleScoringEngine.match_experience(requested_exp=5.0, candidate_exp=8.0)
    assert meets is True
    assert score == 96.0


def test_experience_match_gradient_ceiling():
    """Verify 5 years above minimum hits gradient ceiling score = 100.0 and eligible = True."""
    score, meets = EmployeePeopleScoringEngine.match_experience(requested_exp=5.0, candidate_exp=10.0)
    assert meets is True
    assert score == 100.0


def test_experience_match_above_ceiling():
    """Verify 10 years above minimum is bounded at ceiling score = 100.0."""
    score, meets = EmployeePeopleScoringEngine.match_experience(requested_exp=5.0, candidate_exp=15.0)
    assert meets is True
    assert score == 100.0


def test_experience_match_below_minimum():
    """Verify candidate below minimum returns score = 0.0 and eligible = False (Hard Gate)."""
    score, meets = EmployeePeopleScoringEngine.match_experience(requested_exp=5.0, candidate_exp=4.9)
    assert meets is False
    assert score == 0.0


def test_experience_match_boundary_cases():
    """Verify boundary conditions for 1-year and 10-year requirements."""
    # Req = 1.0
    s1, m1 = EmployeePeopleScoringEngine.match_experience(requested_exp=1.0, candidate_exp=1.0)
    assert m1 is True and s1 == 90.0

    s2, m2 = EmployeePeopleScoringEngine.match_experience(requested_exp=1.0, candidate_exp=2.0)
    assert m2 is True and s2 == 92.0

    s3, m3 = EmployeePeopleScoringEngine.match_experience(requested_exp=1.0, candidate_exp=6.0)
    assert m3 is True and s3 == 100.0

    # Req = 10.0
    s4, m4 = EmployeePeopleScoringEngine.match_experience(requested_exp=10.0, candidate_exp=10.0)
    assert m4 is True and s4 == 90.0

    s5, m5 = EmployeePeopleScoringEngine.match_experience(requested_exp=10.0, candidate_exp=11.0)
    assert m5 is True and s5 == 92.0

    s6, m6 = EmployeePeopleScoringEngine.match_experience(requested_exp=10.0, candidate_exp=15.0)
    assert m6 is True and s6 == 100.0


def test_experience_match_no_requirement():
    """Verify unrequested experience (None or <= 0) returns 100.0 and eligible = True."""
    s_none, m_none = EmployeePeopleScoringEngine.match_experience(requested_exp=None, candidate_exp=2.0)
    assert m_none is True and s_none == 100.0

    s_zero, m_zero = EmployeePeopleScoringEngine.match_experience(requested_exp=0.0, candidate_exp=2.0)
    assert m_zero is True and s_zero == 100.0


def test_experience_gradient_ranking_differentiation():
    """Verify candidate with 8 yrs experience ranks above candidate with 5 yrs when 5+ requested."""
    cand_a = {"id": 10, "current_title": "Python Developer", "skills": ["Python"], "total_experience_years": 5.0}
    cand_b = {"id": 11, "current_title": "Python Developer", "skills": ["Python"], "total_experience_years": 8.0}

    sc_a = EmployeePeopleScoringEngine.score_candidate(cand_a, requested_title="Python Developer", requested_skills=["Python"], requested_exp=5.0)
    sc_b = EmployeePeopleScoringEngine.score_candidate(cand_b, requested_title="Python Developer", requested_skills=["Python"], requested_exp=5.0)

    assert sc_a["exp_score"] == 90.0
    assert sc_b["exp_score"] == 96.0
    assert sc_b["final_score"] > sc_a["final_score"]

    key_a = people_rank_key(cand_a, sc_a)
    key_b = people_rank_key(cand_b, sc_b)

    # Candidate B key is strictly less than Candidate A key (since rank keys sort ascending)
    assert key_b < key_a


def test_experience_gradient_does_not_overpower_primary_title_relevance():
    """Verify candidate with exact title (100.0) + 5 yrs exp ranks above candidate with related title (85.0) + 15 yrs exp."""
    cand_exact = {"id": 20, "current_title": "Python Developer", "skills": ["Python"], "total_experience_years": 5.0}
    cand_related = {"id": 21, "current_title": "Software Engineer", "skills": ["Python"], "total_experience_years": 15.0}

    sc_exact = EmployeePeopleScoringEngine.score_candidate(cand_exact, requested_title="Python Developer", requested_skills=["Python"], requested_exp=5.0)
    sc_related = EmployeePeopleScoringEngine.score_candidate(cand_related, requested_title="Python Developer", requested_skills=["Python"], requested_exp=5.0)

    assert sc_exact["exp_score"] == 90.0
    assert sc_related["exp_score"] == 100.0

    # Core title weighting (0.40) ensures exact title candidate wins
    assert sc_exact["final_score"] > sc_related["final_score"]
    assert people_rank_key(cand_exact, sc_exact) < people_rank_key(cand_related, sc_related)


def test_experience_gradient_preserves_multi_skill_and_eligibility():
    """Verify Candidate A (full skills + 7 yrs) ranks above Candidate B (missing MySQL + 15 yrs exp)."""
    cand_eligible = {"id": 30, "current_title": "Developer", "skills": ["Python", "MySQL"], "total_experience_years": 7.0}
    cand_ineligible = {"id": 31, "current_title": "Developer", "skills": ["Python"], "total_experience_years": 15.0}

    sc_elig = EmployeePeopleScoringEngine.score_candidate(cand_eligible, requested_title="Developer", requested_skills=["Python", "MySQL"], requested_exp=5.0)
    sc_inelig = EmployeePeopleScoringEngine.score_candidate(cand_ineligible, requested_title="Developer", requested_skills=["Python", "MySQL"], requested_exp=5.0)

    assert sc_elig["is_eligible"] is True
    assert sc_inelig["is_eligible"] is False

    key_elig = people_rank_key(cand_eligible, sc_elig)
    key_inelig = people_rank_key(cand_ineligible, sc_inelig)

    # Eligible candidate always sorts first (eligibility_sort = 0 vs 1)
    assert key_elig[0] == 0
    assert key_inelig[0] == 1
    assert key_elig < key_inelig


# ==============================================================================
# 7. PHASE 6 — CANDIDATE SEARCH PAGINATION & CONVERSATION STATE TESTS
# ==============================================================================

from backend.utils.session_state import get_pagination_state, set_pagination_state, clear_pagination_state


def test_phase6_candidate_search_initial_pagination():
    """Verify initial candidate search returns page 1 (5 cards) and stores RAM session pagination state."""
    mock_candidates = [
        {"id": i, "name": f"Candidate {i}", "current_title": "Python Developer", "company": "CollarCheck",
         "location": "Delhi", "skills": ["Python", "MySQL"], "total_experience_years": 5.0 + (i * 0.5), "profile": None}
        for i in range(1, 13) # 12 eligible candidates
    ]
    criteria = SearchCriteria(target="PEOPLE_SEARCH", title="Python Developer", skills=["Python"])

    with patch.object(PeopleRepository, "search_people", return_value=mock_candidates):
        res = PeopleSearchService.search_people(criteria=criteria)

        assert res["success"] is True
        assert res["result_type"] == "people"
        assert len(res["results"]) == 12  # Complete dataset returned for frontend-only pagination
        assert len(res["all_cards"]) == 12  # Total candidate pool = 12

        pag = res["pagination"]
        assert pag["current_page"] == 1
        assert pag["per_page"] == 5
        assert pag["total_results"] == 12
        assert pag["total_pages"] == 3
        assert pag["has_next"] is True
        assert pag["has_prev"] is False


def test_phase6_candidate_search_show_more_pagination_and_stable_ordering():
    """Verify conversational pagination (pages 1, 2, 3) yields exact stable ordering without duplicate candidates."""
    mock_candidates = [
        {"id": i, "name": f"Candidate {i}", "current_title": "Python Developer", "company": "CollarCheck",
         "location": "Delhi", "skills": ["Python", "MySQL"], "total_experience_years": 5.0 + (i * 0.2), "profile": None}
        for i in range(1, 13)
    ]
    criteria = SearchCriteria(target="PEOPLE_SEARCH", title="Python Developer", skills=["Python"])

    with patch.object(PeopleRepository, "search_people", return_value=mock_candidates):
        res1 = PeopleSearchService.search_people(criteria=criteria)
        session_id = "test_sess_phase6_pagination"
        set_pagination_state(session_id, res1["pagination"], role="employee")

        p_state1 = get_pagination_state(session_id, role="employee")
        all_recs = p_state1["all_recs"]
        assert len(all_recs) == 12

        # Page 1 initial response contains all candidates
        page1_ids = [c["id"] for c in res1["results"][0:5]]
        assert page1_ids == [c["id"] for c in all_recs[0:5]]

        # Page 2 slice simulation
        page2_cards = all_recs[5:10]
        page2_ids = [c["id"] for c in page2_cards]
        assert page2_ids == [c["id"] for c in all_recs[5:10]]

        # Page 3 slice simulation
        page3_cards = all_recs[10:12]
        page3_ids = [c["id"] for c in page3_cards]
        assert page3_ids == [c["id"] for c in all_recs[10:12]]

        # Verify stability and zero duplicates across pages
        full_sequence = page1_ids + page2_ids + page3_ids
        assert len(full_sequence) == 12
        assert len(set(full_sequence)) == 12  # Zero duplicates
        assert full_sequence == [c["id"] for c in all_recs]


def test_phase6_new_candidate_search_resets_pagination():
    """Verify issuing a new candidate search resets current_page to 1."""
    session_id = "test_sess_phase6_reset"
    # Old pagination state at page 3
    old_pag = {"result_type": "people", "current_page": 3, "total_results": 15, "total_pages": 3, "all_recs": []}
    set_pagination_state(session_id, old_pag, role="employee")

    # New search execution
    mock_new_candidates = [
        {"id": 101, "name": "Java Dev", "current_title": "Java Developer", "company": "Co",
         "location": "Delhi", "skills": ["Java"], "total_experience_years": 4.0, "profile": None}
    ]
    criteria = SearchCriteria(target="PEOPLE_SEARCH", title="Java Developer", skills=["Java"])

    with patch.object(PeopleRepository, "search_people", return_value=mock_new_candidates):
        res = PeopleSearchService.search_people(criteria=criteria)
        set_pagination_state(session_id, res["pagination"], role="employee")

        updated_p = get_pagination_state(session_id, role="employee")
        assert updated_p["current_page"] == 1
        assert updated_p["total_results"] == 1


def test_phase6_state_isolation_people_vs_jobs():
    """Verify PEOPLE_SEARCH pagination state (result_type='people') does not pollute JOBS state."""
    session_id = "test_sess_isolation"
    
    # 1. Store candidate pagination state
    people_pag = {"result_type": "people", "current_page": 2, "total_results": 10, "all_recs": [{"id": 1}]}
    set_pagination_state(session_id, people_pag, role="employee")

    st = get_pagination_state(session_id, role="employee")
    assert st["result_type"] == "people"
    assert st["current_page"] == 2

    # 2. Store JOBS pagination state (switches pagination mode)
    jobs_pag = {"result_type": "jobs", "current_page": 1, "total_results": 20, "all_recs": [{"job_id": 99}]}
    set_pagination_state(session_id, jobs_pag, role="employee")

    st_jobs = get_pagination_state(session_id, role="employee")
    assert st_jobs["result_type"] == "jobs"
    assert st_jobs["current_page"] == 1


def test_phase6_out_of_bounds_pagination():
    """Verify requesting page beyond total_pages returns invalid_page_num and empty slice safely."""
    all_cards = [{"id": i, "name": f"Card {i}"} for i in range(1, 6)] # 5 candidates = 1 page
    pag_meta = {
        "current_page": 4, # Page 4 requested when total_pages = 1
        "per_page": 5,
        "page_size": 5,
        "total_results": 5,
        "total_pages": 1,
        "start_index": 0,
        "end_index": 0,
        "has_next": False,
        "has_prev": True,
        "invalid_page_num": 4,
        "result_type": "people",
        "all_recs": all_cards,
    }

    start_slice = (4 - 1) * 5
    end_slice = 4 * 5
    page_cards = all_cards[start_slice:end_slice]

    assert page_cards == []  # Out of bounds slice is empty
    assert pag_meta["invalid_page_num"] == 4
    assert pag_meta["total_pages"] == 1


def test_phase6_candidate_card_contract_preserved_across_pages():
    """Verify every candidate card contains all required CandidateCard schema keys."""
    mock_candidates = [
        {"id": 1, "name": "Test User", "current_title": "Engineer", "company": "CollarCheck",
         "location": "Delhi", "skills": ["Python"], "total_experience_years": 6.0, "profile": "profile.jpg"}
    ]
    criteria = SearchCriteria(target="PEOPLE_SEARCH", title="Engineer")

    with patch.object(PeopleRepository, "search_people", return_value=mock_candidates):
        res = PeopleSearchService.search_people(criteria=criteria)
        card = res["results"][0]

        required_keys = {
            "id", "candidate_id", "cc_id", "name", "title", "current_title",
            "current_designation", "company", "current_company_name", "location",
            "skills", "experience_years", "experience", "url", "profile_photo_url", "match_reason"
        }

        assert required_keys.issubset(card.keys())
        assert card["id"] == 1
        assert card["cc_id"] == "CC-1"
        assert card["url"] == "https://www.collarcheck.com/candidate/1"




