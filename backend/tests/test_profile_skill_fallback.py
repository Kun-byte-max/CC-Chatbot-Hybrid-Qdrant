import pytest
import asyncio
from unittest.mock import patch, MagicMock

from backend.services.user_data_service import fetch_endpoint_data, _get_user_skills_db_fallback
from backend.recommendation.mappers import to_candidate_profile_from_api


def test_1_api_success():
    """Test 1: External profile API succeeds and returns skills."""
    mock_api_resp = {
        "status": True,
        "data": {
            "id": 100,
            "full_name": "Test Candidate",
            "all_Skill": [{"skill": "Python"}, {"skill": "FastAPI"}]
        }
    }
    with patch("backend.services.platform_api._get", return_value=mock_api_resp):
        res = fetch_endpoint_data("user-profile", token="test_token", user_slug="test-slug")
        assert res.get("status") is True
        prof = to_candidate_profile_from_api(res)
        assert prof.skills == ["Python", "FastAPI"]


def test_2_api_failure_with_db_skills():
    """Test 2: External API fails (returns status False), DB fallback retrieves active skills from cyb_user_skill."""
    mock_user_row = {"id": 41031, "slug": "test-user", "fname": "Test", "lname": "User"}
    mock_db_skills = [{"skill": "Python"}, {"skill": "FastAPI"}]

    with patch("backend.services.platform_api._get", return_value={"status": False}), \
         patch("backend.services.user_data_service._get_user_db_fallback", return_value=mock_user_row), \
         patch("backend.services.user_data_service._get_user_skills_db_fallback", return_value=mock_db_skills):

        res = fetch_endpoint_data("user-profile", token="test_token", user_id="41031")
        assert res.get("status") is True
        assert res.get("message") == "User Profile (DB Fallback)"
        prof = to_candidate_profile_from_api(res)
        assert prof.skills == ["Python", "FastAPI"]


def test_3_api_failure_with_no_db_skills():
    """Test 3: External API fails and DB user has no active skills."""
    mock_user_row = {"id": 99999, "slug": "noskill-user", "fname": "No", "lname": "Skill"}

    with patch("backend.services.platform_api._get", return_value={"status": False}), \
         patch("backend.services.user_data_service._get_user_db_fallback", return_value=mock_user_row), \
         patch("backend.services.user_data_service._get_user_skills_db_fallback", return_value=[]):

        res = fetch_endpoint_data("user-profile", token="test_token", user_id="99999")
        assert res.get("status") is True
        prof = to_candidate_profile_from_api(res)
        assert prof.skills == []


def test_4_deleted_inactive_skills_filtered_out():
    """Test 4: Verify SQL filter ignores inactive (status != 1) or deleted (is_deleted = 1) skills."""
    mock_db_rows = [
        {"name": "ActiveSkill"},
    ]
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = mock_db_rows
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    with patch("backend.repositories.db.get_db", return_value=mock_conn):
        skills = _get_user_skills_db_fallback("41031")
        assert skills == [{"skill": "ActiveSkill"}]
        query_sql = mock_cursor.execute.call_args[0][0]
        assert "us.status = 1" in query_sql
        assert "us.is_deleted IS NULL OR us.is_deleted = 0" in query_sql


def test_5_profile_based_job_search():
    """Test 5: 'Show me jobs according to my skills' uses DB fallback skills when API is unavailable."""
    async def run_test():
        from backend.api.employee import handle_employee_context

        mock_parsed = {
            "search_target": "JOBS",
            "use_profile_skills": True,
            "skill_source": "PROFILE",
            "keyword": None,
            "is_search_intent": True,
        }
        mock_user_profile = {
            "status": True,
            "message": "User Profile (DB Fallback)",
            "data": {
                "id": "41031",
                "full_name": "Test User",
                "skill": [{"skill": "Python"}, {"skill": "FastAPI"}],
                "skills": ["Python", "FastAPI"],
                "all_Skill": [{"skill": "Python"}, {"skill": "FastAPI"}]
            }
        }

        searched_keywords = []

        async def mock_search_jobs(**kwargs):
            searched_keywords.append(kwargs.get("keyword"))
            return {"success": True, "data": [{"id": 1, "job_title": "Python Dev", "company_name": "Test Co"}]}

        with patch("backend.services.search_parser.parse_search_query", return_value=mock_parsed), \
             patch("backend.services.user_data_service.fetch_endpoint_data", return_value=mock_user_profile), \
             patch("backend.api.employee.search_jobs", side_effect=mock_search_jobs), \
             patch("backend.services.hybrid_job_search.search_jobs", side_effect=mock_search_jobs):

            res = await handle_employee_context(
                last_user_msg="Show me jobs according to my skills",
                session_id="test_sess",
                candidate_id="41031"
            )
            assert res is not None
            assert res.get("db_context") is not None
            assert "Python" in searched_keywords or "FastAPI" in searched_keywords

    asyncio.run(run_test())


def test_6_near_me_profile_skills_bounded_by_gps_city():
    """Test 6: 'Give me the jobs near me according to my skills' bounds skill search by resolved GPS city ID."""
    async def run_test():
        from backend.api.employee import handle_employee_context

        mock_parsed = {
            "search_target": "JOBS",
            "use_profile_skills": True,
            "skill_source": "PROFILE",
            "location_source": "CURRENT",
            "location_mode": "NEAR_ME",
            "keyword": None,
            "is_search_intent": True,
        }
        mock_user_profile = {
            "status": True,
            "message": "User Profile (DB Fallback)",
            "data": {
                "id": "41031",
                "full_name": "Test User",
                "skill": [{"skill": "Python"}],
                "skills": ["Python"],
                "all_Skill": [{"skill": "Python"}]
            }
        }

        captured_filters = []

        async def mock_search_jobs(**kwargs):
            captured_filters.append(kwargs)
            return {"success": True, "data": [{"id": 1, "job_title": "Python Dev in Pune", "company_name": "Test Co"}]}

        with patch("backend.services.search_parser.parse_search_query", return_value=mock_parsed), \
             patch("backend.services.user_data_service.fetch_endpoint_data", return_value=mock_user_profile), \
             patch("backend.api.employee.search_jobs", side_effect=mock_search_jobs), \
             patch("backend.services.hybrid_job_search.search_jobs", side_effect=mock_search_jobs):

            res = await handle_employee_context(
                last_user_msg="Give me the jobs near me according to my skills",
                session_id="test_sess",
                user_location={"source": "CURRENT", "city": "Pune", "latitude": 18.5204, "longitude": 73.8567},
                candidate_id="41031"
            )
            assert res is not None
            assert res.get("db_context") is not None
            assert len(captured_filters) > 0
            assert isinstance(captured_filters[0].get("city"), list)
            assert len(captured_filters[0].get("city")) > 0

    asyncio.run(run_test())


def test_7_telemetry_profile_update_intent_vs_profile_retrieved():
    """Test 7: Job search query sets profile_update_intent_found=False and profile_retrieved=True."""
    async def run_test():
        from backend.api.employee import handle_employee_context
        from backend.utils.timing import start_request_timing

        ctx = start_request_timing("test_req_7")

        mock_parsed = {
            "search_target": "JOBS",
            "use_profile_skills": True,
            "skill_source": "PROFILE",
            "keyword": None,
            "is_search_intent": True,
        }
        mock_user_profile = {
            "status": True,
            "data": {
                "id": "41031",
                "full_name": "Test User",
                "skill": [{"skill": "Python"}]
            }
        }

        with patch("backend.services.search_parser.parse_search_query", return_value=mock_parsed), \
             patch("backend.services.user_data_service.fetch_endpoint_data", return_value=mock_user_profile), \
             patch("backend.api.employee.search_jobs", return_value={"success": True, "data": []}):

            res = await handle_employee_context(
                last_user_msg="Show me jobs according to my skills",
                session_id="test_sess_7",
                candidate_id="41031"
            )
            assert res is not None
            assert ctx.profile_update_intent_found is False
            assert ctx.profile_retrieved is True

    asyncio.run(run_test())


def test_8_profile_retrieval_failure_telemetry():
    """Test 8: Failed profile retrieval sets profile_retrieved=False."""
    async def run_test():
        from backend.api.employee import handle_employee_context
        from backend.utils.timing import start_request_timing

        ctx = start_request_timing("test_req_8")

        mock_parsed = {
            "search_target": "JOBS",
            "use_profile_skills": True,
            "skill_source": "PROFILE",
            "keyword": None,
            "is_search_intent": True,
        }

        with patch("backend.services.search_parser.parse_search_query", return_value=mock_parsed), \
             patch("backend.services.user_data_service.fetch_endpoint_data", return_value={"status": False, "data": None}), \
             patch("backend.api.employee.search_jobs", return_value={"success": True, "data": []}):

            res = await handle_employee_context(
                last_user_msg="Show me jobs according to my skills",
                session_id="test_sess_8",
                candidate_id="000"
            )
            assert ctx.profile_update_intent_found is False
            assert ctx.profile_retrieved is False

    asyncio.run(run_test())


def test_9_db_slug_resolution_prevents_unnecessary_401():
    """Test 9: When slug is resolved via local DB, platform_api._get is NOT called for user-detail."""
    mock_user_row = {"id": 41031, "slug": "rakesh-maurya-cce130000", "fname": "Rakesh", "lname": "Maurya"}
    mock_profile_resp = {"status": True, "data": {"id": 41031, "full_name": "Rakesh Maurya"}}

    called_urls = []

    def mock_get(path, **kwargs):
        called_urls.append(path)
        if "auth/user-profile" in path:
            return mock_profile_resp
        return {"status": False}

    with patch("backend.services.user_data_service._get_user_db_fallback", return_value=mock_user_row), \
         patch("backend.services.platform_api._get", side_effect=mock_get):

        res = fetch_endpoint_data("user-profile", token=None, user_id="41031")
        assert res.get("status") is True
        assert "employee/user-detail" not in called_urls
        assert "auth/user-profile/rakesh-maurya-cce130000" in called_urls


def test_10_valid_auth_token_calls_user_detail_when_no_db_slug():
    """Test 10: Valid authentication calls user-detail when local DB has no slug."""
    mock_detail_resp = {"status": True, "data": {"slug": "api-slug-123"}}
    mock_profile_resp = {"status": True, "data": {"id": 101, "full_name": "API User"}}

    called_urls = []

    def mock_get(path, **kwargs):
        called_urls.append(path)
        if path == "employee/user-detail":
            return mock_detail_resp
        if "auth/user-profile" in path:
            return mock_profile_resp
        return {"status": False}

    with patch("backend.services.user_data_service._get_user_db_fallback", return_value=None), \
         patch("backend.services.platform_api._get", side_effect=mock_get):

        res = fetch_endpoint_data("user-profile", token="valid_user_token", user_id="101")
        assert res.get("status") is True
        assert "employee/user-detail" in called_urls
        assert "auth/user-profile/api-slug-123" in called_urls


def test_11_multi_turn_profile_turn_clears_session_explicit_terms():
    """Test 11 (Critical): Turn 1 explicit Node.js backend search -> Turn 2 profile skills search does NOT inherit Node.js backend."""
    async def run_test():
        from backend.api.employee import handle_employee_context
        from backend.utils.timing import start_request_timing

        ctx = start_request_timing("test_req_11")

        turn1_parsed = {
            "search_target": "JOBS",
            "keyword": "backend developer",
            "skills": ["Node.js"],
            "is_search_intent": True,
        }
        turn2_parsed = {
            "search_target": "JOBS",
            "use_profile_skills": True,
            "skill_source": "PROFILE",
            "keyword": None,
            "skills": [],
            "is_search_intent": True,
        }
        mock_user_profile = {
            "status": True,
            "data": {
                "id": "41031",
                "full_name": "Test User",
                "all_Skill": [{"skill": "Python"}, {"skill": "FastAPI"}]
            }
        }

        searched_keywords = []

        async def mock_search_jobs(**kwargs):
            searched_keywords.append(kwargs.get("keyword"))
            return {"success": True, "data": [{"id": 1, "job_title": "Python Dev", "company_name": "Test Co"}]}

        mock_rec_return = (
            [{"id": 1, "job_title": "Python Dev"}],
            {"current_page": 1, "total_pages": 1, "total_records": 1},
            [{"id": 1, "job_title": "Python Dev"}]
        )

        with patch("backend.services.search_parser.parse_search_query", return_value=turn1_parsed), \
             patch("backend.services.hybrid_job_search.search_jobs", side_effect=mock_search_jobs), \
             patch("backend.api.employee.recommendation_service.get_paginated_recommendations_for_profile", return_value=mock_rec_return):
            await handle_employee_context(
                last_user_msg="show me Node.js backend developer jobs",
                session_id="sess_multi_11",
                candidate_id="41031"
            )

        searched_keywords.clear()

        with patch("backend.services.search_parser.parse_search_query", return_value=turn2_parsed), \
             patch("backend.services.user_data_service.fetch_endpoint_data", return_value=mock_user_profile), \
             patch("backend.services.hybrid_job_search.search_jobs", side_effect=mock_search_jobs), \
             patch("backend.api.employee.recommendation_service.get_paginated_recommendations_for_profile", return_value=mock_rec_return):

            res = await handle_employee_context(
                last_user_msg="give me jobs near me according to my skills",
                session_id="sess_multi_11",
                candidate_id="41031"
            )
            assert res is not None
            assert not any("backend developer" in (k or "") for k in searched_keywords)
            assert not any("Node.js" in (k or "") for k in searched_keywords)

    asyncio.run(run_test())


def test_12_profile_skills_override_previous_explicit_skills():
    """Test 12: Turn 1 Python jobs in Delhi -> Turn 2 jobs according to my skills (Python not inherited)."""
    async def run_test():
        from backend.api.employee import handle_employee_context

        turn1_parsed = {"search_target": "JOBS", "keyword": "Python", "location": "Delhi", "is_search_intent": True}
        turn2_parsed = {"search_target": "JOBS", "use_profile_skills": True, "skill_source": "PROFILE", "is_search_intent": True}

        mock_user_profile = {"status": True, "data": {"id": "41031", "all_Skill": [{"skill": "Java"}]}}

        searched_keywords = []

        async def mock_search_jobs(**kwargs):
            searched_keywords.append(kwargs.get("keyword"))
            return {"success": True, "data": [{"id": 1, "job_title": "Java Dev"}]}

        mock_rec_return = (
            [{"id": 1, "job_title": "Java Dev"}],
            {"current_page": 1, "total_pages": 1, "total_records": 1},
            [{"id": 1, "job_title": "Java Dev"}]
        )

        with patch("backend.services.search_parser.parse_search_query", return_value=turn1_parsed), \
             patch("backend.services.hybrid_job_search.search_jobs", side_effect=mock_search_jobs), \
             patch("backend.api.employee.recommendation_service.get_paginated_recommendations_for_profile", return_value=mock_rec_return):
            await handle_employee_context(last_user_msg="Python jobs in Delhi", session_id="sess_12", candidate_id="41031")

        searched_keywords.clear()

        with patch("backend.services.search_parser.parse_search_query", return_value=turn2_parsed), \
             patch("backend.services.user_data_service.fetch_endpoint_data", return_value=mock_user_profile), \
             patch("backend.services.hybrid_job_search.search_jobs", side_effect=mock_search_jobs), \
             patch("backend.api.employee.recommendation_service.get_paginated_recommendations_for_profile", return_value=mock_rec_return):
            await handle_employee_context(last_user_msg="give me jobs according to my skills", session_id="sess_12", candidate_id="41031")
            assert "Python" not in searched_keywords
            assert "Java" in searched_keywords

    asyncio.run(run_test())


def test_13_profile_skills_plus_near_me_preserves_current_location():
    """Test 13: Turn 1 Delhi -> Turn 2 jobs near me according to my skills (GPS Pune used, Delhi not inherited)."""
    async def run_test():
        from backend.api.employee import handle_employee_context

        turn1_parsed = {"search_target": "JOBS", "keyword": "Python", "location": "Delhi", "is_search_intent": True}
        turn2_parsed = {
            "search_target": "JOBS",
            "use_profile_skills": True,
            "skill_source": "PROFILE",
            "location_source": "CURRENT",
            "location_mode": "NEAR_ME",
            "is_search_intent": True
        }
        mock_user_profile = {"status": True, "data": {"id": "41031", "all_Skill": [{"skill": "React"}]}}

        captured_filters = []

        async def mock_search_jobs(**kwargs):
            captured_filters.append(kwargs)
            return {"success": True, "data": [{"id": 1, "job_title": "React Dev"}]}

        mock_rec_return = (
            [{"id": 1, "job_title": "React Dev"}],
            {"current_page": 1, "total_pages": 1, "total_records": 1},
            [{"id": 1, "job_title": "React Dev"}]
        )

        with patch("backend.services.search_parser.parse_search_query", return_value=turn1_parsed), \
             patch("backend.services.user_data_service._get_user_db_fallback", return_value={"id": 41031, "city": 154375, "city_name": "Pune"}), \
             patch("backend.services.hybrid_job_search.search_jobs", side_effect=mock_search_jobs), \
             patch("backend.api.employee.recommendation_service.get_paginated_recommendations_for_profile", return_value=mock_rec_return):
            await handle_employee_context(last_user_msg="Python jobs in Delhi", session_id="sess_13", candidate_id="41031")

        captured_filters.clear()

        with patch("backend.services.search_parser.parse_search_query", return_value=turn2_parsed), \
             patch("backend.services.user_data_service.fetch_endpoint_data", return_value=mock_user_profile), \
             patch("backend.services.user_data_service._get_user_db_fallback", return_value={"id": 41031, "city": 154375, "city_name": "Pune"}), \
             patch("backend.services.hybrid_job_search.search_jobs", side_effect=mock_search_jobs), \
             patch("backend.api.employee.recommendation_service.get_paginated_recommendations_for_profile", return_value=mock_rec_return):
            await handle_employee_context(
                last_user_msg="give me jobs near me according to my skills",
                session_id="sess_13",
                user_location={"source": "CURRENT", "city": "Pune", "latitude": 18.5204, "longitude": 73.8567},
                candidate_id="41031"
            )
            assert len(captured_filters) > 0
            assert captured_filters[0].get("keyword") == "React"
            assert captured_filters[0].get("city") in ([154375], [133504])

    asyncio.run(run_test())


def test_14_normal_multi_turn_refinement_remains_working():
    """Test 14: Non-profile turn refinement (Turn 1 Python in Delhi -> Turn 2 What about remote) preserves Python & Delhi."""
    async def run_test():
        from backend.api.employee import handle_employee_context

        turn1_parsed = {"search_target": "JOBS", "keyword": "Python", "location": "Delhi", "is_search_intent": True}
        turn2_parsed = {"search_target": "JOBS", "job_mode": "home", "is_search_intent": True}

        captured_filters = []

        async def mock_search_jobs(**kwargs):
            captured_filters.append(kwargs)
            return {"success": True, "data": [{"id": 1, "job_title": "Python WFH"}]}

        mock_rec_return = (
            [{"id": 1, "job_title": "Python WFH"}],
            {"current_page": 1, "total_pages": 1, "total_records": 1},
            [{"id": 1, "job_title": "Python WFH"}]
        )

        with patch("backend.services.search_parser.parse_search_query", return_value=turn1_parsed), \
             patch("backend.services.hybrid_job_search.search_jobs", side_effect=mock_search_jobs), \
             patch("backend.api.employee.recommendation_service.get_paginated_recommendations_for_profile", return_value=mock_rec_return):
            await handle_employee_context(last_user_msg="Python jobs in Delhi", session_id="sess_14", candidate_id="41031")

        captured_filters.clear()

        with patch("backend.services.search_parser.parse_search_query", return_value=turn2_parsed), \
             patch("backend.services.hybrid_job_search.search_jobs", side_effect=mock_search_jobs), \
             patch("backend.api.employee.recommendation_service.get_paginated_recommendations_for_profile", return_value=mock_rec_return):
            await handle_employee_context(last_user_msg="What about remote jobs?", session_id="sess_14", candidate_id="41031")
            assert len(captured_filters) > 0
            assert captured_filters[0].get("job_mode") == 2 or captured_filters[0].get("job_mode") == [2] or captured_filters[0].get("work_mode") == "remote"

    asyncio.run(run_test())


def test_15_explicit_replacement_remains_working():
    """Test 15: Turn 1 Python -> Turn 2 Node.js replaces Python with Node.js."""
    async def run_test():
        from backend.api.employee import handle_employee_context

        turn1_parsed = {"search_target": "JOBS", "keyword": "Python", "is_search_intent": True}
        turn2_parsed = {"search_target": "JOBS", "keyword": "Node.js", "is_search_intent": True}

        captured_filters = []

        async def mock_search_jobs(**kwargs):
            captured_filters.append(kwargs)
            return {"success": True, "data": [{"id": 1, "job_title": "Node Dev"}]}

        mock_rec_return = (
            [{"id": 1, "job_title": "Node Dev"}],
            {"current_page": 1, "total_pages": 1, "total_records": 1},
            [{"id": 1, "job_title": "Node Dev"}]
        )

        with patch("backend.services.search_parser.parse_search_query", return_value=turn1_parsed), \
             patch("backend.services.hybrid_job_search.search_jobs", side_effect=mock_search_jobs), \
             patch("backend.api.employee.recommendation_service.get_paginated_recommendations_for_profile", return_value=mock_rec_return):
            await handle_employee_context(last_user_msg="Show me Python jobs", session_id="sess_15", candidate_id="41031")

        captured_filters.clear()

        with patch("backend.services.search_parser.parse_search_query", return_value=turn2_parsed), \
             patch("backend.services.hybrid_job_search.search_jobs", side_effect=mock_search_jobs), \
             patch("backend.api.employee.recommendation_service.get_paginated_recommendations_for_profile", return_value=mock_rec_return):
            await handle_employee_context(last_user_msg="Show me Node.js ones", session_id="sess_15", candidate_id="41031")
            assert len(captured_filters) > 0
            assert captured_filters[0].get("keyword") == "Node.js"

    asyncio.run(run_test())


def test_16_clean_profile_request_without_session():
    """Test 16: Clean request 'jobs near me according to my skills' without session has keyword=None and skill_source=PROFILE."""
    async def run_test():
        from backend.api.employee import handle_employee_context

        mock_parsed = {
            "search_target": "JOBS",
            "use_profile_skills": True,
            "skill_source": "PROFILE",
            "location_source": "CURRENT",
            "location_mode": "NEAR_ME",
            "is_search_intent": True
        }
        mock_user_profile = {"status": True, "data": {"id": "41031", "all_Skill": [{"skill": "Flutter"}]}}

        searched_keywords = []

        async def mock_search_jobs(**kwargs):
            searched_keywords.append(kwargs.get("keyword"))
            return {"success": True, "data": [{"id": 1, "job_title": "Flutter Dev"}]}

        mock_rec_return = (
            [{"id": 1, "job_title": "Flutter Dev"}],
            {"current_page": 1, "total_pages": 1, "total_records": 1},
            [{"id": 1, "job_title": "Flutter Dev"}]
        )

        with patch("backend.services.search_parser.parse_search_query", return_value=mock_parsed), \
             patch("backend.services.user_data_service.fetch_endpoint_data", return_value=mock_user_profile), \
             patch("backend.services.hybrid_job_search.search_jobs", side_effect=mock_search_jobs), \
             patch("backend.api.employee.recommendation_service.get_paginated_recommendations_for_profile", return_value=mock_rec_return):

            res = await handle_employee_context(
                last_user_msg="jobs near me according to my skills",
                session_id="sess_clean_16",
                user_location={"source": "CURRENT", "city": "Pune", "latitude": 18.5204, "longitude": 73.8567},
                candidate_id="41031"
            )
            assert res is not None
            assert searched_keywords == ["Flutter"]

    asyncio.run(run_test())


def test_17_explicit_query_still_classified_as_query():
    """Test 17: 'Python jobs near me' keeps explicit keyword Python and QUERY mode."""
    async def run_test():
        from backend.api.employee import handle_employee_context
        from backend.services.location_service import LocationResult

        mock_parsed = {
            "search_target": "JOBS",
            "keyword": "Python",
            "skill_source": "QUERY",
            "location_source": "CURRENT",
            "location_mode": "NEAR_ME",
            "is_search_intent": True
        }

        searched_keywords = []

        async def mock_search_jobs(**kwargs):
            searched_keywords.append(kwargs.get("keyword"))
            return {"success": True, "data": [{"id": 1, "job_title": "Python Dev"}]}

        mock_rec_return = (
            [{"id": 1, "job_title": "Python Dev"}],
            {"current_page": 1, "total_pages": 1, "total_records": 1},
            [{"id": 1, "job_title": "Python Dev"}]
        )
        mock_loc_res = LocationResult(resolved=True, city_id=154375, city_name="Pune", location_type="city")

        with patch("backend.services.search_parser.parse_search_query", return_value=mock_parsed), \
             patch("backend.api.employee.resolve_location_from_db", return_value=mock_loc_res), \
             patch("backend.services.hybrid_job_search.search_jobs", side_effect=mock_search_jobs), \
             patch("backend.api.employee.search_jobs", side_effect=mock_search_jobs), \
             patch("backend.api.employee.recommendation_service.get_paginated_recommendations_for_profile", return_value=mock_rec_return):

            res = await handle_employee_context(
                last_user_msg="Python jobs near me",
                session_id="sess_17",
                user_location={"source": "CURRENT", "city": "Pune", "latitude": 18.5204, "longitude": 73.8567},
                candidate_id="41031"
            )
            assert res is not None
            assert searched_keywords == ["Python"]

    asyncio.run(run_test())


def test_18_explicit_nodejs_backend_developer_search():
    """Test 18: 'Node.js backend developer jobs near me' keeps explicit keyword 'backend developer Node.js'."""
    async def run_test():
        from backend.api.employee import handle_employee_context
        from backend.services.location_service import LocationResult

        mock_parsed = {
            "search_target": "JOBS",
            "keyword": "backend developer",
            "skills": ["Node.js"],
            "skill_source": "QUERY",
            "location_source": "CURRENT",
            "location_mode": "NEAR_ME",
            "is_search_intent": True
        }

        searched_keywords = []

        async def mock_search_jobs(**kwargs):
            searched_keywords.append(kwargs.get("keyword"))
            return {"success": True, "data": [{"id": 1, "job_title": "Node.js Backend Dev"}]}

        mock_rec_return = (
            [{"id": 1, "job_title": "Node.js Backend Dev"}],
            {"current_page": 1, "total_pages": 1, "total_records": 1},
            [{"id": 1, "job_title": "Node.js Backend Dev"}]
        )
        mock_loc_res = LocationResult(resolved=True, city_id=154375, city_name="Pune", location_type="city")

        with patch("backend.services.search_parser.parse_search_query", return_value=mock_parsed), \
             patch("backend.api.employee.resolve_location_from_db", return_value=mock_loc_res), \
             patch("backend.services.hybrid_job_search.search_jobs", side_effect=mock_search_jobs), \
             patch("backend.api.employee.search_jobs", side_effect=mock_search_jobs), \
             patch("backend.api.employee.recommendation_service.get_paginated_recommendations_for_profile", return_value=mock_rec_return):

            res = await handle_employee_context(
                last_user_msg="Node.js backend developer jobs near me",
                session_id="sess_18",
                user_location={"source": "CURRENT", "city": "Pune", "latitude": 18.5204, "longitude": 73.8567},
                candidate_id="41031"
            )
            assert res is not None
            assert len(searched_keywords) > 0
            assert "Node.js" in searched_keywords[0] and "backend developer" in searched_keywords[0]

    asyncio.run(run_test())



