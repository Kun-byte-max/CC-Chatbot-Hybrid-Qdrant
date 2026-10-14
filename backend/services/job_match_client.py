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

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
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

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
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


# Singleton instance
job_match_client = JobMatchClient()
