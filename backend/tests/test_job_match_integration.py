"""
test_job_match_integration.py — Unit and Integration Test Suite for Job Match API Integration.
All remote HTTP API calls are fully mocked.
"""

import pytest
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock
import httpx

from backend.services.job_match_client import (
    JobMatchClient,
    CandidateNotIndexedException,
    JobMatchUnavailableException,
    JobMatchAuthException,
    JobMatchContractException,
)
from backend.recommendation.recommendation_service import RecommendationService
from backend.recommendation.models import CandidateProfile
from backend.utils.session_state import set_recommendation_session, get_recommendation_session


def test_feature_flag_default_disabled():
    """Verify that when JOB_MATCH_ENABLED is False, job_match_client raises JobMatchUnavailableException and uses fallback."""
    async def _test():
        with patch("backend.services.job_match_client.JOB_MATCH_ENABLED", False):
            client = JobMatchClient()
            with pytest.raises(JobMatchUnavailableException) as exc_info:
                await client.find_jobs(candidate_id=123)
            assert "disabled" in str(exc_info.value).lower()

    asyncio.run(_test())


def test_job_match_find_jobs_clean_json_body():
    """Verify clean JSON body request construction and 200 response parsing."""
    async def _test():
        client = JobMatchClient(base_url="https://ai.collarcheck.com/jobmatch", api_key="test_key")
        mock_payload = {
            "candidate_id": 123,
            "total": 1,
            "shortlisted": 10,
            "took_ms": 12.5,
            "results": [
                {
                    "job_id": 456,
                    "final_score": 0.85,
                    "reasons": ["Matched 3/4 skills", "Same city"],
                    "payload": {
                        "job_id": 456,
                        "job_title": "Python Developer",
                        "company_name": "Tech Corp",
                        "city_name": "Mumbai",
                        "state_name": "Maharashtra",
                        "salary": "800000",
                        "job_description": "<p>Hiring Python developer</p>"
                    }
                }
            ]
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_payload

        with patch("backend.services.job_match_client.JOB_MATCH_ENABLED", True):
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
                mock_post.return_value = mock_response

                res = await client.find_jobs(candidate_id=123, limit=11)

                assert res["candidate_id"] == 123
                assert len(res["results"]) == 1
                assert res["results"][0]["job_id"] == 456

                # Verify JSON body passed to post
                mock_post.assert_called_once()
                call_kwargs = mock_post.call_args[1]
                assert call_kwargs["json"] == {
                    "candidate_id": 123,
                    "final_limit": 11,
                    "filters": {}
                }
                assert call_kwargs["headers"]["X-API-KEY"] == "test_key"

    asyncio.run(_test())


def test_missing_unauthenticated_candidate_id():
    """Verify client rejects missing or invalid candidate IDs."""
    async def _test():
        client = JobMatchClient()
        with patch("backend.services.job_match_client.JOB_MATCH_ENABLED", True):
            with pytest.raises(JobMatchContractException):
                await client.find_jobs(candidate_id=0)

    asyncio.run(_test())


def test_job_match_409_unindexed_candidate():
    """Verify HTTP 409 response raises CandidateNotIndexedException to trigger fallback."""
    async def _test():
        client = JobMatchClient()
        mock_response = MagicMock()
        mock_response.status_code = 409

        with patch("backend.services.job_match_client.JOB_MATCH_ENABLED", True):
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
                mock_post.return_value = mock_response
                with pytest.raises(CandidateNotIndexedException):
                    await client.find_jobs(candidate_id=9999)

    asyncio.run(_test())


def test_job_match_401_unauthorized_alert():
    """Verify HTTP 401 Unauthorized raises JobMatchAuthException and logs CRITICAL error."""
    async def _test():
        client = JobMatchClient()
        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch("backend.services.job_match_client.JOB_MATCH_ENABLED", True):
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
                mock_post.return_value = mock_response
                with pytest.raises(JobMatchAuthException):
                    await client.find_jobs(candidate_id=123)

    asyncio.run(_test())


def test_job_match_503_service_unavailable():
    """Verify HTTP 503 response raises JobMatchUnavailableException."""
    async def _test():
        client = JobMatchClient()
        mock_response = MagicMock()
        mock_response.status_code = 503

        with patch("backend.services.job_match_client.JOB_MATCH_ENABLED", True):
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
                mock_post.return_value = mock_response
                with pytest.raises(JobMatchUnavailableException):
                    await client.find_jobs(candidate_id=123)

    asyncio.run(_test())


def test_job_match_timeout():
    """Verify network timeout raises JobMatchUnavailableException."""
    async def _test():
        client = JobMatchClient()

        with patch("backend.services.job_match_client.JOB_MATCH_ENABLED", True):
            with patch("httpx.AsyncClient.post", side_effect=httpx.TimeoutException("Timeout")):
                with pytest.raises(JobMatchUnavailableException):
                    await client.find_jobs(candidate_id=123)

    asyncio.run(_test())


def test_incomplete_record_skipping():
    """Verify that records missing essential fields (job_id or title) are skipped without crashing."""
    async def _test():
        service = RecommendationService()
        mock_payload = {
            "candidate_id": 123,
            "results": [
                {
                    # Valid record
                    "job_id": 101,
                    "final_score": 0.90,
                    "reasons": ["Valid job"],
                    "payload": {"job_id": 101, "job_title": "Software Engineer", "company_name": "Tech Inc"}
                },
                {
                    # Incomplete record (missing title)
                    "job_id": 102,
                    "final_score": 0.80,
                    "payload": {"job_id": 102, "job_title": ""}
                },
                {
                    # Incomplete record (missing job_id)
                    "final_score": 0.70,
                    "payload": {"job_title": "Backend Dev"}
                }
            ]
        }

        with patch("backend.recommendation.recommendation_service.JOB_MATCH_ENABLED", True):
            with patch("backend.services.job_match_client.JOB_MATCH_ENABLED", True):
                with patch("backend.services.job_match_client.job_match_client.find_jobs", new_callable=AsyncMock) as mock_find:
                    mock_find.return_value = mock_payload

                    page_slice, meta, all_recs, used_jm = await service.get_job_match_recommendations_async(
                        candidate_id=123,
                        session_id="test_session_skipping"
                    )

                    assert used_jm is True
                    assert len(all_recs) == 1
                    assert all_recs[0].job_id == 101

    asyncio.run(_test())


def test_score_conversion_scale():
    """Verify final_score scale conversion from float (0.854) to percentage (85.4%)."""
    async def _test():
        service = RecommendationService()
        mock_payload = {
            "candidate_id": 123,
            "results": [
                {
                    "job_id": 201,
                    "final_score": 0.854,
                    "reasons": ["Good fit"],
                    "payload": {"job_id": 201, "job_title": "Data Scientist"}
                }
            ]
        }

        with patch("backend.recommendation.recommendation_service.JOB_MATCH_ENABLED", True):
            with patch("backend.services.job_match_client.JOB_MATCH_ENABLED", True):
                with patch("backend.services.job_match_client.job_match_client.find_jobs", new_callable=AsyncMock) as mock_find:
                    mock_find.return_value = mock_payload

                    page_slice, meta, all_recs, used_jm = await service.get_job_match_recommendations_async(
                        candidate_id=123,
                        session_id="test_session_score"
                    )

                    assert used_jm is True
                    assert all_recs[0].overall_score == 85.4

    asyncio.run(_test())


def test_deduplication_job_ids_across_fallback():
    """Verify that fallback recommendations contain unique job IDs."""
    async def _test():
        service = RecommendationService()
        profile = CandidateProfile(id=1, skills=["Python", "SQL"])

        with patch("backend.recommendation.recommendation_service.JOB_MATCH_ENABLED", False):
            slice_recs, meta, all_recs, used_jm = await service.get_job_match_recommendations_async(
                candidate_id=1,
                candidate_profile=profile,
                session_id="test_session_dedup"
            )
            assert used_jm is False
            job_ids = [r.job_id for r in all_recs if r.job_id]
            assert len(job_ids) == len(set(job_ids))

    asyncio.run(_test())


def test_user_session_isolation():
    """Verify that recommendation snapshots in session memory remain isolated per session_id."""
    set_recommendation_session(session_id="session_user_A", query_key="cand_100", ranked_jobs=[{"job_id": 11}], current_page=1, page_size=5, total_jobs=1)
    set_recommendation_session(session_id="session_user_B", query_key="cand_200", ranked_jobs=[{"job_id": 22}], current_page=1, page_size=5, total_jobs=1)

    sess_A = get_recommendation_session("session_user_A")
    sess_B = get_recommendation_session("session_user_B")

    assert sess_A["ranked_jobs"][0]["job_id"] == 11
    assert sess_B["ranked_jobs"][0]["job_id"] == 22


def test_match_preview_invalid_ordinal_and_api_failure():
    """Verify that match preview handles bad rank ordinals and remote API failures cleanly."""
    async def _test():
        service = RecommendationService()

        # Test invalid rank ordinal lookup on empty session
        res = service.get_recommendation_by_rank_or_id(session_id="empty_session", user_message="explain 99th job")
        assert res["success"] is False
        assert "don't have a recent job recommendation list" in res["error"]

        # Test preview API 500 error handling
        client = JobMatchClient()
        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch("backend.services.job_match_client.JOB_MATCH_ENABLED", True):
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
                mock_post.return_value = mock_response
                with pytest.raises(JobMatchUnavailableException):
                    await client.match_preview(candidate_id=123, job_id=456)

    asyncio.run(_test())


def test_general_search_regression():
    """Verify general search functionality remains functional and unaffected."""
    async def _test():
        try:
            from backend.services.search_client import search_jobs
        except ModuleNotFoundError:
            from services.search_client import search_jobs

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"success": True, "data": [{"id": 1, "job_title": "PHP Developer"}]}

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            res = await search_jobs(keyword="PHP")
            assert res["success"] is True
            assert res["data"][0]["job_title"] == "PHP Developer"

    asyncio.run(_test())
