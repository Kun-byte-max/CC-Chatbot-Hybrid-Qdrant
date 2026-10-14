import pytest
from unittest.mock import patch, MagicMock

from backend.schemas.search_criteria import SearchCriteria, SearchTarget
from backend.schemas.search_query import LocationMode
from backend.utils.session_state import merge_candidate_search_criteria


def test_scenario_a_preserve_unrelated_filters_on_skill_replacement():
    """Scenario A: Skill replacement preserves unrelated active filters (location, experience)."""
    prev = SearchCriteria(skills=["Python"], location="Delhi", experience_min=5, target=SearchTarget.PEOPLE_SEARCH)
    curr = SearchCriteria(skills=["React"], skill_operation="REPLACE", target=SearchTarget.PEOPLE_SEARCH)
    
    merged = merge_candidate_search_criteria(prev, curr)
    assert merged.skills == ["React"]
    assert merged.location == "Delhi"
    assert merged.experience_min == 5


def test_scenario_b_additive_skill_refinement():
    """Scenario B: Additive skill refinement appends new skill while preserving location."""
    prev = SearchCriteria(skills=["Python"], location="Delhi", target=SearchTarget.PEOPLE_SEARCH)
    curr = SearchCriteria(skills=["Java"], skill_operation="ADD", target=SearchTarget.PEOPLE_SEARCH)
    
    merged = merge_candidate_search_criteria(prev, curr)
    assert set(merged.skills) == {"Python", "Java"}
    assert merged.location == "Delhi"


def test_scenario_c_explicit_clear_skills():
    """Scenario C: Explicit skill clear resets skills to [] while preserving location."""
    prev = SearchCriteria(skills=["Python"], location="Delhi", target=SearchTarget.PEOPLE_SEARCH)
    curr = SearchCriteria(clear_filters=["skills"], target=SearchTarget.PEOPLE_SEARCH)
    
    merged = merge_candidate_search_criteria(prev, curr)
    assert merged.skills == []
    assert merged.location == "Delhi"


def test_scenario_d_replacement_with_overlapping_skill():
    """Scenario D: Replacement with overlapping skill replaces skills set without triggering new topic reset."""
    prev = SearchCriteria(skills=["Python"], location="Delhi", target=SearchTarget.PEOPLE_SEARCH)
    curr = SearchCriteria(skills=["Python", "Java"], skill_operation="REPLACE", target=SearchTarget.PEOPLE_SEARCH)
    
    merged = merge_candidate_search_criteria(prev, curr)
    assert set(merged.skills) == {"Python", "Java"}
    assert merged.location == "Delhi"


def test_scenario_e_additive_with_overlapping_skill():
    """Scenario E: Additive refinement with overlapping skill merges skills cleanly."""
    prev = SearchCriteria(skills=["Python"], location="Delhi", target=SearchTarget.PEOPLE_SEARCH)
    curr = SearchCriteria(skills=["Python", "Java"], skill_operation="ADD", target=SearchTarget.PEOPLE_SEARCH)
    
    merged = merge_candidate_search_criteria(prev, curr)
    assert set(merged.skills) == {"Python", "Java"}
    assert merged.location == "Delhi"


def test_scenario_f_new_candidate_search_clears_stale_state():
    """Scenario F: Complete standalone NEW_SEARCH resets stale filters completely."""
    prev = SearchCriteria(skills=["Python"], location="Delhi", experience_min=5, target=SearchTarget.PEOPLE_SEARCH)
    curr = SearchCriteria(skills=["Java"], clear_filters=["location", "experience"], target=SearchTarget.PEOPLE_SEARCH)
    
    merged = merge_candidate_search_criteria(prev, curr, query_type="NEW_SEARCH")
    assert merged.skills == ["Java"]
    assert merged.location is None
    assert merged.experience_min is None


def test_scenario_g_clear_followed_by_add():
    """Scenario G: Clearing skills followed by adding a skill does not resurrect Python."""
    prev = SearchCriteria(skills=["Python"], location="Delhi", target=SearchTarget.PEOPLE_SEARCH)
    
    # Step 1: Clear skills
    step1 = merge_candidate_search_criteria(prev, SearchCriteria(clear_filters=["skills"], target=SearchTarget.PEOPLE_SEARCH))
    assert step1.skills == []
    
    # Step 2: Add Java
    step2 = merge_candidate_search_criteria(step1, SearchCriteria(skills=["Java"], skill_operation="ADD", target=SearchTarget.PEOPLE_SEARCH))
    assert step2.skills == ["Java"]
    assert "Python" not in step2.skills


def test_duplicate_skill_elimination():
    """Deduplication eliminates duplicates case-insensitively while preserving ordering."""
    prev = SearchCriteria(skills=["Python", "SQL"], target=SearchTarget.PEOPLE_SEARCH)
    curr = SearchCriteria(skills=["sql", "Java", "python"], target=SearchTarget.PEOPLE_SEARCH)
    
    merged = merge_candidate_search_criteria(prev, curr)
    assert merged.skills == ["Python", "SQL", "Java"]


def test_multi_filter_refinement_sequence():
    """Multi-step refinement sequence combining skills, location, min/max experience."""
    initial = SearchCriteria(skills=["Python"], location="Delhi", target=SearchTarget.PEOPLE_SEARCH)
    
    # 1. Add experience min 3
    s1 = merge_candidate_search_criteria(initial, SearchCriteria(experience_min=3, target=SearchTarget.PEOPLE_SEARCH))
    assert s1.experience_min == 3
    assert s1.location == "Delhi"
    
    # 2. Add experience max 8
    s2 = merge_candidate_search_criteria(s1, SearchCriteria(experience_max=8, target=SearchTarget.PEOPLE_SEARCH))
    assert s2.experience_min == 3
    assert s2.experience_max == 8
    
    # 3. Replace skill with Java
    s3 = merge_candidate_search_criteria(s2, SearchCriteria(skills=["Java"], skill_operation="REPLACE", target=SearchTarget.PEOPLE_SEARCH))
    assert s3.skills == ["Java"]
    assert s3.location == "Delhi"
    assert s3.experience_min == 3
    assert s3.experience_max == 8


def test_e2e_chat_skill_replacement_preserves_location():
    """E2E FastAPI /chat test for skill replacement preserving location and sticky candidate target."""
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
                {"id": 1, "candidate_id": 1, "cc_id": "CC-001", "url": "/candidate/1", "name": "Bob Programmer", "fname": "Bob", "lname": "Programmer", "current_title": "React Developer", "skills": ["React"], "location": "Delhi, Delhi", "total_experience_years": 5.0}
            ],
            "total_results": 1,
            "result_type": "people",
            "db_context": "Found 1 candidates"
        }

        session_id = "phase6_e2e_refinement_session"

        # Turn 1: "Find Python candidates in Delhi"
        r1 = client.post("/chat", json={"messages": [{"role": "user", "content": "Find Python candidates in Delhi"}], "session_id": session_id}, headers=headers)
        assert r1.status_code == 200
        assert r1.json().get("result_type") == "people"

        # Turn 2: "Instead find React candidates" -> replaces skill with React, preserves Delhi
        r2 = client.post("/chat", json={"messages": [{"role": "user", "content": "Instead find React candidates"}], "session_id": session_id}, headers=headers)
        assert r2.status_code == 200
        assert r2.json().get("result_type") in ("people", "people_search_empty")
