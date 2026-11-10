import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import FastAPI, HTTPException, Request

from backend.main import lifespan
from backend.api.search import search_users, filter_exploring_users


# ===========================================================================
# Lifespan Lifecycle Tests
# ===========================================================================

def test_lifespan_pool_creation_failure_clean_shutdown():
    """1. Pool creation failure followed by clean lifespan shutdown without UnboundLocalError."""
    async def _test():
        app = FastAPI()
        with patch("aiomysql.create_pool", side_effect=RuntimeError("DB down")):
            async with lifespan(app):
                assert getattr(app.state, "db_pool", None) is None
        # Context manager exit should complete cleanly without UnboundLocalError or crash

    asyncio.run(_test())


def test_lifespan_successful_pool_creation_and_shutdown():
    """2. Successful pool creation and normal shutdown, including close() and awaited wait_closed()."""
    async def _test():
        app = FastAPI()
        mock_pool = MagicMock()
        mock_pool.close = MagicMock()
        mock_pool.wait_closed = AsyncMock()

        with patch("aiomysql.create_pool", new_callable=AsyncMock, return_value=mock_pool), \
             patch("backend.main._partial_sync_loop", side_effect=asyncio.CancelledError), \
             patch("backend.main._full_sync_loop", side_effect=asyncio.CancelledError):
            async with lifespan(app):
                assert getattr(app.state, "db_pool", None) is mock_pool

        mock_pool.close.assert_called_once()
        mock_pool.wait_closed.assert_awaited_once()

    asyncio.run(_test())


def test_lifespan_partial_startup_tasks():
    """3. Partial startup where only some background tasks were created."""
    async def _test():
        app = FastAPI()
        mock_pool = MagicMock()
        mock_pool.close = MagicMock()
        mock_pool.wait_closed = AsyncMock()

        async def dummy_loop():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                pass

        with patch("aiomysql.create_pool", new_callable=AsyncMock, return_value=mock_pool), \
             patch("backend.main._partial_sync_loop", side_effect=dummy_loop), \
             patch("backend.main._full_sync_loop", side_effect=RuntimeError("task create fail")):
            try:
                async with lifespan(app):
                    pass
            except RuntimeError:
                pass

        mock_pool.close.assert_called_once()
        mock_pool.wait_closed.assert_awaited_once()

    asyncio.run(_test())


def test_lifespan_created_tasks_cancelled_and_awaited():
    """4. Created background tasks are cancelled and awaited during shutdown."""
    async def _test():
        app = FastAPI()
        started_event = asyncio.Event()
        task1_cancelled = False
        task2_cancelled = False

        async def fake_partial():
            nonlocal task1_cancelled
            started_event.set()
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                task1_cancelled = True
                raise

        async def fake_full():
            nonlocal task2_cancelled
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                task2_cancelled = True
                raise

        mock_pool = MagicMock()
        mock_pool.close = MagicMock()
        mock_pool.wait_closed = AsyncMock()

        with patch("aiomysql.create_pool", new_callable=AsyncMock, return_value=mock_pool), \
             patch("backend.main._partial_sync_loop", side_effect=fake_partial), \
             patch("backend.main._full_sync_loop", side_effect=fake_full):
            async with lifespan(app):
                await started_event.wait()

        assert task1_cancelled is True
        assert task2_cancelled is True
        mock_pool.close.assert_called_once()
        mock_pool.wait_closed.assert_awaited_once()

    asyncio.run(_test())


# ===========================================================================
# Privacy Fail-Closed /search/users Tests
# ===========================================================================

def make_dummy_request(app: FastAPI) -> Request:
    scope = {
        "type": "http",
        "app": app,
        "query_string": b"",
        "headers": [],
    }
    return Request(scope)


DEFAULT_SEARCH_KWARGS = {
    "keyword": "",
    "limit": 20,
    "offset": 0,
    "verified_identity": None,
    "current_company": None,
    "facets": None,
    "country": None,
    "state": None,
    "city": None,
    "department": None,
    "designation": None,
    "skill": None,
    "salary": None,
    "company": None,
    "industry": None,
    "work_type": None,
    "employment_type": None,
    "verified_employement": None,
    "verified_salary": None,
    "on_notice": None,
    "on_immediate": None,
    "on_immidate": None,
    "starRating": None,
    "yearExperience": None,
    "course": None,
    "course_type": None,
}


def test_search_users_exploring_no_pool_returns_503():
    """5. /search/users requiring exploring filtering with no pool returns HTTP 503 and no unfiltered candidates."""
    async def _test():
        app = FastAPI()
        app.state.db_pool = None
        req = make_dummy_request(app)

        with pytest.raises(HTTPException) as exc_info:
            await search_users(
                request=req,
                on_exploring=[1],
                login_user_id=123,
                **DEFAULT_SEARCH_KWARGS,
            )

        assert exc_info.value.status_code == 503
        assert "privacy filtering is temporarily unavailable" in exc_info.value.detail

    asyncio.run(_test())


def test_search_users_exploring_filter_raises_returns_503():
    """6. /search/users requiring exploring filtering when filter_exploring_users raises returns 503 and no data."""
    async def _test():
        app = FastAPI()
        app.state.db_pool = MagicMock()
        req = make_dummy_request(app)

        fake_search_result = {
            "data": [{"id": 1, "name": "Secret Candidate"}],
            "total": 1,
            "has_more": False,
        }

        with patch("backend.api.search.search_with_fallback", return_value=fake_search_result), \
             patch("backend.api.search.filter_exploring_users", side_effect=RuntimeError("DB query failed")):
            with pytest.raises(HTTPException) as exc_info:
                await search_users(
                    request=req,
                    on_exploring=[1],
                    login_user_id=123,
                    **DEFAULT_SEARCH_KWARGS,
                )

        assert exc_info.value.status_code == 503
        assert "privacy filtering is temporarily unavailable" in exc_info.value.detail

    asyncio.run(_test())


def test_search_users_no_exploring_retains_existing_behavior():
    """7. A request that does not require exploring filtering retains its existing behavior."""
    async def _test():
        app = FastAPI()
        app.state.db_pool = None  # No pool
        req = make_dummy_request(app)

        fake_search_result = {
            "data": [{"id": 1, "name": "Public Candidate"}],
            "total": 1,
            "has_more": False,
        }

        with patch("backend.api.search.search_with_fallback", return_value=fake_search_result):
            res = await search_users(
                request=req,
                on_exploring=None,  # No exploring filter
                login_user_id=None,
                **DEFAULT_SEARCH_KWARGS,
            )

        assert res["total"] == 1
        assert res["data"] == [{"id": 1, "name": "Public Candidate"}]

    asyncio.run(_test())


def test_search_users_successful_filtering_returns_expected_data():
    """8. Successful filtering returns the expected filtered results."""
    async def _test():
        app = FastAPI()
        app.state.db_pool = MagicMock()
        req = make_dummy_request(app)

        raw_candidates = [
            {"id": 1, "name": "Visible Candidate"},
            {"id": 2, "name": "Hidden Candidate"},
        ]
        fake_search_result = {
            "data": raw_candidates,
            "total": 2,
            "has_more": False,
        }
        filtered_candidates = [{"id": 1, "name": "Visible Candidate"}]

        with patch("backend.api.search.search_with_fallback", return_value=fake_search_result), \
             patch("backend.api.search.filter_exploring_users", new_callable=AsyncMock, return_value=filtered_candidates):
            res = await search_users(
                request=req,
                on_exploring=[1],
                login_user_id=123,
                **DEFAULT_SEARCH_KWARGS,
            )

        assert res["data"] == [{"id": 1, "name": "Visible Candidate"}]
        assert len(res["data"]) == 1

    asyncio.run(_test())
