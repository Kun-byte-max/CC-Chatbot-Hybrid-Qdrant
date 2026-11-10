"""
job_match_client.py — Async HTTP client for deployed Job Match API.
Handles find-jobs and match-preview requests with X-API-KEY header, timeouts,
sanitizing credentials, and classifying error responses cleanly.
"""

import logging
from typing import Dict, Any, Optional
import httpx

try:
    from backend.config.config import (
        JOB_MATCH_API_BASE_URL,
        JOB_MATCH_API_KEY,
        JOB_MATCH_TIMEOUT_SECONDS,
        JOB_MATCH_ENABLED,
        JOB_MATCH_LIMIT,
    )
except ModuleNotFoundError:
    from config.config import (  # type: ignore
        JOB_MATCH_API_BASE_URL,
        JOB_MATCH_API_KEY,
        JOB_MATCH_TIMEOUT_SECONDS,
        JOB_MATCH_ENABLED,
        JOB_MATCH_LIMIT,
    )

log = logging.getLogger(__name__)


class JobMatchException(Exception):
    """Base exception for Job Match API interactions."""
    pass


class CandidateNotIndexedException(JobMatchException):
    """Raised when Job Match returns 409 (candidate vector missing / not indexed)."""
    pass


class JobMatchUnavailableException(JobMatchException):
    """Raised when Job Match is unreachable, times out, or returns 503/5xx."""
    pass


class JobMatchAuthException(JobMatchException):
    """Raised when Job Match returns 401 or 403 (invalid API key or auth failure)."""
    pass


class JobMatchContractException(JobMatchException):
    """Raised when Job Match returns 400 or 422 (contract/parameter validation error)."""
    pass


# Application-managed JobMatch HTTP client state (loop-aware)
_app_job_match_client: Optional[httpx.AsyncClient] = None
_app_job_match_loop: Optional[Any] = None
_test_injected_job_match_client: bool = False


def init_job_match_client(
    timeout: Optional[float] = None,
    max_connections: int = 30,
    max_keepalive_connections: int = 15,
    keepalive_expiry: float = 30.0,
) -> httpx.AsyncClient:
    """
    Initializes a shared, connection-pooled httpx.AsyncClient bound to the current running event loop.
    Intended to be called during application lifespan startup.
    """
    import asyncio
    global _app_job_match_client, _app_job_match_loop, _test_injected_job_match_client
    loop = asyncio.get_running_loop()
    effective_timeout = timeout if timeout is not None else JOB_MATCH_TIMEOUT_SECONDS
    limits = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive_connections,
        keepalive_expiry=keepalive_expiry,
    )
    _app_job_match_client = httpx.AsyncClient(
        timeout=effective_timeout,
        limits=limits,
    )
    _app_job_match_loop = loop
    _test_injected_job_match_client = False
    return _app_job_match_client


async def close_job_match_client() -> None:
    """
    Closes the application-managed JobMatch client and drains connections.
    Intended to be called during application lifespan shutdown.
    Idempotent and safe to call repeatedly.
    """
    global _app_job_match_client, _app_job_match_loop, _test_injected_job_match_client
    client = _app_job_match_client
    is_injected = _test_injected_job_match_client
    _app_job_match_client = None
    _app_job_match_loop = None
    _test_injected_job_match_client = False
    if client is not None and not client.is_closed and not is_injected:
        await client.aclose()


def set_job_match_client(client: Optional[httpx.AsyncClient], is_injected: bool = True) -> None:
    """
    Sets or resets the application client (useful for testing and dependency injection).
    """
    import asyncio
    global _app_job_match_client, _app_job_match_loop, _test_injected_job_match_client
    _app_job_match_client = client
    _test_injected_job_match_client = bool(client is not None and is_injected)
    if client is not None:
        try:
            _app_job_match_loop = asyncio.get_running_loop()
        except RuntimeError:
            _app_job_match_loop = None
    else:
        _app_job_match_loop = None


def get_job_match_client() -> Optional[httpx.AsyncClient]:
    """Returns the current application-managed JobMatch client, or None."""
    return _app_job_match_client


def get_job_match_client_loop() -> Optional[Any]:
    """Returns the event loop that owns the current application-managed JobMatch client."""
    return _app_job_match_loop


def _get_active_job_match_client() -> tuple[Optional[httpx.AsyncClient], bool]:
    """
    Returns (client, is_pooled).
    If a valid pooled client exists for the current running event loop and is open,
    returns (pooled_client, True). Otherwise returns (None, False).
    """
    import asyncio
    current_loop = None
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if (
        _app_job_match_client is not None
        and not _app_job_match_client.is_closed
        and _app_job_match_loop is not None
        and current_loop is not None
        and _app_job_match_loop == current_loop
        and not current_loop.is_closed()
    ):
        return _app_job_match_client, True
    return None, False


class JobMatchClient:
    """Async HTTP client for interacting with the deployed Job Match service."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.base_url = (base_url or JOB_MATCH_API_BASE_URL).rstrip("/")
        self.api_key = api_key or JOB_MATCH_API_KEY
        self.timeout = timeout if timeout is not None else JOB_MATCH_TIMEOUT_SECONDS

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-KEY"] = self.api_key
        return headers

    async def find_jobs(
        self,
        candidate_id: int,
        limit: Optional[int] = None,
        filters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Calls GET/POST /v1/find-jobs for personalized vector job recommendations.
        Supports both GET query param (login_user_id) and POST JSON body.
        """
        if not JOB_MATCH_ENABLED:
            log.info("Job Match API integration is disabled (JOB_MATCH_ENABLED=false).")
            raise JobMatchUnavailableException("Job Match integration is disabled via feature flag.")

        if not candidate_id or not str(candidate_id).isdigit():
            raise JobMatchContractException("Invalid or missing candidate_id.")

        final_limit = limit if limit is not None else JOB_MATCH_LIMIT
        url = f"{self.base_url}/v1/find-jobs"
        params = {"login_user_id": int(candidate_id), "limit": int(final_limit)}

        log.info("[JobMatch] GET /v1/find-jobs for login_user_id=%s limit=%d", candidate_id, final_limit)

        client, is_pooled = _get_active_job_match_client()
        transient_client = None
        if not is_pooled:
            transient_client = httpx.AsyncClient(timeout=self.timeout)
            client = transient_client

        try:
            response = await client.get(url, params=params, headers=self._headers())
            if response.status_code != 200:
                payload = {
                    "candidate_id": int(candidate_id),
                    "final_limit": int(final_limit),
                    "filters": filters or {},
                }
                response = await client.post(url, json=payload, headers=self._headers())

            if response.status_code == 200:
                data = response.json()
                if not isinstance(data, dict) or ("data" not in data and "results" not in data):
                    log.warning("[JobMatch] Malformed 200 response structure from find-jobs.")
                    raise JobMatchUnavailableException("Malformed response payload from Job Match API.")
                return data

            elif response.status_code == 409:
                log.info("[JobMatch] Candidate %s is not indexed (HTTP 409). Triggering fallback.", candidate_id)
                raise CandidateNotIndexedException(f"Candidate {candidate_id} is not indexed yet.")

            elif response.status_code in (401, 403):
                log.critical("[JobMatch] CRITICAL: Authentication failure (HTTP %d). Check JOB_MATCH_API_KEY config.", response.status_code)
                raise JobMatchAuthException(f"Authentication failed with status {response.status_code}.")

            elif response.status_code in (400, 422):
                log.error("[JobMatch] Contract validation failure (HTTP %d). Request payload rejected.", response.status_code)
                raise JobMatchContractException(f"Request rejected with status {response.status_code}.")

            else:
                log.warning("[JobMatch] Service error HTTP %d from find-jobs.", response.status_code)
                raise JobMatchUnavailableException(f"Service returned HTTP {response.status_code}.")

        except (httpx.TimeoutException, httpx.ConnectError, httpx.RequestError) as net_err:
            log.warning("[JobMatch] Network/Timeout error contacting Job Match API: %s", net_err)
            raise JobMatchUnavailableException(f"Network error contacting Job Match API: {net_err}") from net_err
        finally:
            if transient_client is not None and not transient_client.is_closed:
                await transient_client.aclose()

    async def match_preview(self, candidate_id: int, job_id: int) -> Dict[str, Any]:
        """
        Calls GET/POST /v1/match/preview for detailed job-candidate match breakdown.
        Gated by JOB_MATCH_ENABLED flag.
        """
        if not JOB_MATCH_ENABLED:
            log.info("Job Match API integration is disabled (JOB_MATCH_ENABLED=false).")
            raise JobMatchUnavailableException("Job Match integration is disabled via feature flag.")

        if not candidate_id or not job_id:
            raise JobMatchContractException("Invalid candidate_id or job_id for match_preview.")

        url = f"{self.base_url}/v1/match/preview"
        params = {"candidate_id": int(candidate_id), "job_id": int(job_id)}

        log.info("[JobMatch] GET /v1/match/preview for candidate_id=%s job_id=%s", candidate_id, job_id)

        client, is_pooled = _get_active_job_match_client()
        transient_client = None
        if not is_pooled:
            transient_client = httpx.AsyncClient(timeout=self.timeout)
            client = transient_client

        try:
            response = await client.get(url, params=params, headers=self._headers())
            if response.status_code != 200:
                payload = {"candidate_id": int(candidate_id), "job_id": int(job_id), "weights": {}}
                response = await client.post(url, json=payload, headers=self._headers())

            if response.status_code == 200:
                data = response.json()
                if not isinstance(data, dict):
                    raise JobMatchUnavailableException("Malformed response payload from match/preview.")
                return data

            elif response.status_code == 409:
                log.info("[JobMatch] Candidate %s or Job %s not indexed for preview (HTTP 409).", candidate_id, job_id)
                raise CandidateNotIndexedException(f"Candidate {candidate_id} or Job {job_id} not indexed.")

            elif response.status_code in (401, 403):
                log.critical("[JobMatch] CRITICAL: Auth failure (HTTP %d) on match_preview.", response.status_code)
                raise JobMatchAuthException(f"Auth failed with status {response.status_code}.")

            else:
                log.warning("[JobMatch] Service error HTTP %d from match_preview.", response.status_code)
                raise JobMatchUnavailableException(f"Service returned HTTP {response.status_code}.")

        except (httpx.TimeoutException, httpx.ConnectError, httpx.RequestError) as net_err:
            log.warning("[JobMatch] Network/Timeout error on match_preview: %s", net_err)
            raise JobMatchUnavailableException(f"Network error on match_preview: {net_err}") from net_err
        finally:
            if transient_client is not None and not transient_client.is_closed:
                await transient_client.aclose()


# Singleton instance
job_match_client = JobMatchClient()
