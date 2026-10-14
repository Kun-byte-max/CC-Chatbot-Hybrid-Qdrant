import pytest
from unittest.mock import patch, MagicMock

from backend.schemas.search_criteria import SearchCriteria, SearchTarget
from backend.schemas.search_query import LocationMode
from backend.utils.session_state import merge_candidate_search_criteria
from backend.services.employer_search_parser import parse_employer_query
from backend.schemas.employer_requirements import EmployerSearchRequirements, ProvenanceValue, ProvenanceSource, LocationRequirement, ExperienceRequirement


def test_session_merge_clear_location():
    prev = SearchCriteria(skills=["Python"], location="Delhi", experience_min=5, target=SearchTarget.PEOPLE_SEARCH)
    curr = SearchCriteria(clear_filters=["location"], target=SearchTarget.PEOPLE_SEARCH)
    merged = merge_candidate_search_criteria(prev, curr)

    assert merged.location is None
    assert merged.canonical_location is None
    assert merged.location_mode == LocationMode.NONE
    assert merged.skills == ["Python"]
    assert merged.experience_min == 5


def test_session_merge_clear_company():
    prev = SearchCriteria(skills=["Python"], company="Google", experience_min=5, target=SearchTarget.PEOPLE_SEARCH)
    curr = SearchCriteria(clear_filters=["company"], target=SearchTarget.PEOPLE_SEARCH)
    merged = merge_candidate_search_criteria(prev, curr)

    assert merged.company is None
    assert merged.skills == ["Python"]
    assert merged.experience_min == 5


def test_session_merge_clear_experience():
    prev = SearchCriteria(skills=["Python"], experience_min=3, experience_max=7, target=SearchTarget.PEOPLE_SEARCH)
    curr = SearchCriteria(clear_filters=["experience"], target=SearchTarget.PEOPLE_SEARCH)
    merged = merge_candidate_search_criteria(prev, curr)

    assert merged.experience_min is None
    assert merged.experience_max is None
    assert merged.experience_level is None
    assert merged.skills == ["Python"]


def test_session_merge_clear_experience_min():
    prev = SearchCriteria(skills=["Python"], experience_min=3, experience_max=7, target=SearchTarget.PEOPLE_SEARCH)
    curr = SearchCriteria(clear_filters=["experience_min"], target=SearchTarget.PEOPLE_SEARCH)
    merged = merge_candidate_search_criteria(prev, curr)

    assert merged.experience_min is None
    assert merged.experience_max == 7
    assert merged.skills == ["Python"]


def test_session_merge_clear_experience_max():
    prev = SearchCriteria(skills=["Python"], experience_min=3, experience_max=7, target=SearchTarget.PEOPLE_SEARCH)
    curr = SearchCriteria(clear_filters=["experience_max"], target=SearchTarget.PEOPLE_SEARCH)
    merged = merge_candidate_search_criteria(prev, curr)

    assert merged.experience_min == 3
    assert merged.experience_max is None
    assert merged.skills == ["Python"]


def test_session_merge_clear_skills():
    prev = SearchCriteria(skills=["Python", "Java"], location="Delhi", target=SearchTarget.PEOPLE_SEARCH)
    curr = SearchCriteria(clear_filters=["skills"], target=SearchTarget.PEOPLE_SEARCH)
    merged = merge_candidate_search_criteria(prev, curr)

    assert merged.skills == []
    assert merged.location == "Delhi"


def test_session_merge_clear_title():
    prev = SearchCriteria(title="Developer", skills=["Python"], target=SearchTarget.PEOPLE_SEARCH)
    curr = SearchCriteria(clear_filters=["title"], target=SearchTarget.PEOPLE_SEARCH)
    merged = merge_candidate_search_criteria(prev, curr)

    assert merged.title is None
    assert merged.skills == ["Python"]


def test_omitted_filter_preserves_state():
    prev = SearchCriteria(skills=["Python"], location="Delhi", company="Google", experience_min=5, target=SearchTarget.PEOPLE_SEARCH)
    curr = SearchCriteria(experience_max=10, target=SearchTarget.PEOPLE_SEARCH)
    merged = merge_candidate_search_criteria(prev, curr)

    assert merged.skills == ["Python"]
    assert merged.location == "Delhi"
    assert merged.company == "Google"
    assert merged.experience_min == 5
    assert merged.experience_max == 10


def test_multi_filter_clearing_composition():
    initial = SearchCriteria(skills=["Python"], location="Delhi", company="Google", experience_min=5, experience_max=10, target=SearchTarget.PEOPLE_SEARCH)
    
    # 1. Clear location
    step1 = merge_candidate_search_criteria(initial, SearchCriteria(clear_filters=["location"], target=SearchTarget.PEOPLE_SEARCH))
    assert step1.location is None
    assert step1.company == "Google"
    assert step1.experience_min == 5
    assert step1.experience_max == 10
    assert step1.skills == ["Python"]

    # 2. Clear maximum experience
    step2 = merge_candidate_search_criteria(step1, SearchCriteria(clear_filters=["experience_max"], target=SearchTarget.PEOPLE_SEARCH))
    assert step2.location is None
    assert step2.company == "Google"
    assert step2.experience_min == 5
    assert step2.experience_max is None
    assert step2.skills == ["Python"]

    # 3. Clear company
    step3 = merge_candidate_search_criteria(step2, SearchCriteria(clear_filters=["company"], target=SearchTarget.PEOPLE_SEARCH))
    assert step3.location is None
    assert step3.company is None
    assert step3.experience_min == 5
    assert step3.experience_max is None
    assert step3.skills == ["Python"]


def test_employer_clear_location():
    existing = EmployerSearchRequirements(
        role=ProvenanceValue(value="Developer", source=ProvenanceSource.QUERY),
        location=LocationRequirement(text="Delhi", type="city", source=ProvenanceSource.QUERY)
    )
    res = parse_employer_query("Clear location", existing_requirements=existing)
    assert res.location is None
    assert res.role.value == "Developer"


def test_employer_clear_experience():
    existing = EmployerSearchRequirements(
        experience=ExperienceRequirement(min=3, max=7, source=ProvenanceSource.QUERY)
    )
    res = parse_employer_query("Clear experience filter", existing_requirements=existing)
    assert res.experience is None

    res_min = parse_employer_query("Clear minimum experience", existing_requirements=existing)
    assert res_min.experience.min is None
    assert res_min.experience.max == 7

    res_max = parse_employer_query("Clear maximum experience", existing_requirements=existing)
    assert res_max.experience.min == 3
    assert res_max.experience.max is None


def test_employer_clear_skills_and_role():
    existing = EmployerSearchRequirements(
        role=ProvenanceValue(value="Developer", source=ProvenanceSource.QUERY),
        skills=ProvenanceValue(value=["python", "java"], source=ProvenanceSource.QUERY)
    )
    res_sk = parse_employer_query("Clear skills", existing_requirements=existing)
    assert res_sk.skills is None
    assert res_sk.role.value == "Developer"

    res_role = parse_employer_query("Clear role", existing_requirements=existing)
    assert res_role.role is None
    assert res_role.skills.value == ["python", "java"]


def test_skills_additive_vs_clear_semantics():
    prev = SearchCriteria(skills=["Python"], location="Delhi", target=SearchTarget.PEOPLE_SEARCH)
    
    # Additive turn with overlapping skill: "Python and Java" -> skills = ["Python", "Java"]
    curr_add = SearchCriteria(skills=["Python", "Java"], target=SearchTarget.PEOPLE_SEARCH)
    merged_add = merge_candidate_search_criteria(prev, curr_add)
    assert set(merged_add.skills) == {"Python", "Java"}
    assert merged_add.location == "Delhi"

    # Clear turn: "Clear skills" -> skills = []
    curr_clear = SearchCriteria(clear_filters=["skills"], target=SearchTarget.PEOPLE_SEARCH)
    merged_clear = merge_candidate_search_criteria(merged_add, curr_clear)
    assert merged_clear.skills == []
    assert merged_clear.location == "Delhi"


def test_employee_chat_e2e_clear_location_and_sticky_routing():
    from fastapi.testclient import TestClient
    from backend.main import app
    import jwt
    from backend.config.config import JWT_SECRET, JWT_ALGORITHM

    client = TestClient(app)
    synthetic_uid = 1
    auth_jwt = jwt.encode(
        {"id": synthetic_uid, "uid": synthetic_uid, "email": "employee@collarcheck.com", "role": "employee"},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM
    )
    headers = {"Authorization": f"Bearer {auth_jwt}"}

    db_user_row = {
        "id": 1,
        "uid": 1,
        "individual_id": "IND001",
        "fname": "Auth",
        "lname": "User",
        "email": "employee@collarcheck.com",
        "user_type": 1,
        "status": 1,
        "is_deleted": 0,
        "token": "synth_platform_tok_001",
        "slug": "emp1"
    }

    with patch("backend.api.auth.find_user_by_token", return_value=db_user_row), \
         patch("backend.repositories.people_repository.get_db") as mock_people_db, \
         patch("backend.api.auth.get_db") as mock_auth_db, \
         patch("backend.repositories.people_repository.PeopleRepository._get_current_experience"), \
         patch("backend.repositories.people_repository.PeopleRepository._get_user_skills"), \
         patch("backend.services.people_search_service.PeopleSearchService.search_people") as mock_search_people:

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_people_db.return_value = mock_conn
        mock_auth_db.return_value = mock_conn
        mock_cursor.fetchall.return_value = [db_user_row]
        mock_cursor.fetchone.return_value = db_user_row

        mock_search_people.return_value = {
            "success": True,
            "results": [
                {"id": 1, "candidate_id": 1, "cc_id": "CC-001", "url": "/candidate/1", "name": "Alice Smith", "fname": "Alice", "lname": "Smith", "current_title": "Python Developer", "skills": ["Python"], "location": "Delhi, Delhi", "total_experience_years": 5.0}
            ],
            "total_results": 1,
            "result_type": "people",
            "db_context": "Found 1 candidates"
        }

        session_id = "e2e_clear_loc_test_session"

        # Turn 1: "Find Python candidates in Delhi"
        r1 = client.post("/chat", json={"messages": [{"role": "user", "content": "Find Python candidates in Delhi"}], "session_id": session_id}, headers=headers)
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1.get("result_type") == "people"

        # Turn 2: "Clear location" -> sticky routing stays in people search, clears location
        r2 = client.post("/chat", json={"messages": [{"role": "user", "content": "Clear location"}], "session_id": session_id}, headers=headers)
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2.get("result_type") in ("people", "people_search_empty")

        # Turn 3: Explicit job switch: "Find Python jobs" -> routes to jobs
        r3 = client.post("/chat", json={"messages": [{"role": "user", "content": "Find Python jobs"}], "session_id": session_id}, headers=headers)
        assert r3.status_code == 200
        d3 = r3.json()
        assert d3.get("result_type") == "jobs"

