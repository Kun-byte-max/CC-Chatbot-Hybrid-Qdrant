"""
location_matcher.py — Location Matching Module for V1 Recommendation Engine.
"""

from functools import lru_cache
from typing import Any, Optional
from backend.recommendation.models import CandidateProfile, JobProfile, LocationMatchResult


@lru_cache(maxsize=1024)
def _normalize_location_text_cached(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip().lower()
    if not cleaned or cleaned == "unknown" or cleaned == "none":
        return None
    return cleaned


@lru_cache(maxsize=512)
def _normalize_work_mode_cached(work_mode: Optional[Any]) -> str:
    if work_mode is None:
        return "onsite"

    if isinstance(work_mode, int):
        if work_mode == 2:
            return "remote"
        elif work_mode == 3:
            return "hybrid"
        return "onsite"

    mode_str = str(work_mode).strip().lower()
    if "remote" in mode_str or "wfh" in mode_str or "home" in mode_str:
        return "remote"
    elif "hybrid" in mode_str:
        return "hybrid"
    return "onsite"


class LocationMatcher:
    """
    Evaluates geographic proximity and work mode compatibility between candidates and jobs.
    """

    def _normalize_text(self, value: Optional[Any]) -> Optional[str]:
        """
        Normalizes a location text input:
        - Converts to lowercase and trims whitespace.
        - Returns None for empty strings, None, or 'unknown'.

        :param value: Raw location string or None.
        :return: Cleaned location string or None.
        """
        if value is None:
            return None
        return _normalize_location_text_cached(str(value))

    def _normalize_work_mode(self, work_mode: Optional[Any]) -> str:
        """
        Normalizes work mode input into 'remote', 'hybrid', or 'onsite'.

        :param work_mode: Integer (1=Onsite, 2=Remote, 3=Hybrid) or String.
        :return: Standardized work mode string.
        """
        return _normalize_work_mode_cached(work_mode)

    def _match_locations(
        self,
        candidate_city: Optional[Any] = None,
        candidate_state: Optional[Any] = None,
        candidate_country: Optional[Any] = None,
        job_city: Optional[Any] = None,
        job_state: Optional[Any] = None,
        job_country: Optional[Any] = None,
        work_mode: Optional[Any] = None,
    ) -> LocationMatchResult:
        """
        Internal evaluator calculating location match score and match level between location inputs.

        :return: LocationMatchResult dataclass.
        """
        cand_city = self._normalize_text(candidate_city)
        cand_state = self._normalize_text(candidate_state)
        cand_country = self._normalize_text(candidate_country)

        j_city = self._normalize_text(job_city)
        j_state = self._normalize_text(job_state)
        j_country = self._normalize_text(job_country)

        mode = self._normalize_work_mode(work_mode)

        # Rule 1 — Remote Job
        if mode == "remote":
            return LocationMatchResult(
                score=100.0,
                candidate_city=cand_city,
                candidate_state=cand_state,
                candidate_country=cand_country,
                job_city=j_city,
                job_state=j_state,
                job_country=j_country,
                work_mode=mode,
                match_level="remote",
            )

        # Missing Location Data Rule
        cand_has_loc = any([cand_city, cand_state, cand_country])
        job_has_loc = any([j_city, j_state, j_country])

        if not cand_has_loc or not job_has_loc:
            return LocationMatchResult(
                score=50.0,
                candidate_city=cand_city,
                candidate_state=cand_state,
                candidate_country=cand_country,
                job_city=j_city,
                job_state=j_state,
                job_country=j_country,
                work_mode=mode,
                match_level="unknown",
            )

        # Rule 2 — Same City
        if cand_city and j_city and cand_city == j_city:
            return LocationMatchResult(
                score=100.0,
                candidate_city=cand_city,
                candidate_state=cand_state,
                candidate_country=cand_country,
                job_city=j_city,
                job_state=j_state,
                job_country=j_country,
                work_mode=mode,
                match_level="same_city",
            )

        # Rule 3 — Same State
        if cand_state and j_state and cand_state == j_state:
            return LocationMatchResult(
                score=70.0,
                candidate_city=cand_city,
                candidate_state=cand_state,
                candidate_country=cand_country,
                job_city=j_city,
                job_state=j_state,
                job_country=j_country,
                work_mode=mode,
                match_level="same_state",
            )

        # Rule 4 & 5 — Same Country vs Different Country
        if (cand_country and j_country and cand_country == j_country) or (
            not cand_country and not j_country
        ) or (cand_country and not j_country) or (not cand_country and j_country):
            # Same implicit or explicit domestic country
            if cand_country and j_country and cand_country != j_country:
                score = 20.0
                match_level = "different_country"
            else:
                score = 40.0
                match_level = "same_country"
        else:
            score = 20.0
            match_level = "different_country"

        return LocationMatchResult(
            score=score,
            candidate_city=cand_city,
            candidate_state=cand_state,
            candidate_country=cand_country,
            job_city=j_city,
            job_state=j_state,
            job_country=j_country,
            work_mode=mode,
            match_level=match_level,
        )

    def evaluate_profiles(
        self, candidate: Optional[CandidateProfile], job: Optional[JobProfile]
    ) -> LocationMatchResult:
        """
        Standardized public API to evaluate location match between CandidateProfile and JobProfile.

        :param candidate: CandidateProfile dataclass instance or None.
        :param job: JobProfile dataclass instance or None.
        :return: LocationMatchResult dataclass instance.
        """
        c_city = candidate.city if candidate else None
        c_state = candidate.state if candidate else None
        c_country = candidate.country if candidate else None

        j_city = job.city if job else None
        j_state = job.state if job else None
        j_country = job.country if job else None
        mode = job.job_mode if job else 1

        return self._match_locations(
            candidate_city=c_city,
            candidate_state=c_state,
            candidate_country=c_country,
            job_city=j_city,
            job_state=j_state,
            job_country=j_country,
            work_mode=mode,
        )

    def match_employer_location(
        self,
        req_location: Optional[str] = None,
        req_work_mode: Optional[str] = None,
        candidate_city: Optional[str] = None,
        candidate_state: Optional[str] = None,
        candidate_country: Optional[str] = None,
    ) -> Any:
        """
        Domain-safe location matcher entry point for Phase 2 Employer Candidate Recommendation.
        Evaluates physical geographic proximity or Remote capability using 4-state MatchStatus taxonomy.
        """
        from backend.recommendation.types import MatchStatus, MatchResult, RecommendationDimension

        norm_req_loc = self._normalize_text(req_location)
        norm_req_mode = self._normalize_text(req_work_mode)

        if not norm_req_loc and not norm_req_mode:
            return MatchResult(
                dimension=RecommendationDimension.LOCATION,
                status=MatchStatus.NOT_REQUESTED,
                score=None,
                details={"reason": "No location or work mode criteria requested."},
            )

        if norm_req_mode == "remote" and not norm_req_loc:
            return MatchResult(
                dimension=RecommendationDimension.LOCATION,
                status=MatchStatus.MATCH,
                score=100.0,
                details={"reason": "Remote work mode requested and candidate qualifies.", "match_level": "remote"},
            )

        cand_c = self._normalize_text(candidate_city)
        cand_s = self._normalize_text(candidate_state)
        cand_co = self._normalize_text(candidate_country)

        if not cand_c and not cand_s and not cand_co:
            return MatchResult(
                dimension=RecommendationDimension.LOCATION,
                status=MatchStatus.UNKNOWN,
                score=None,
                details={"reason": "Candidate profile contains no location data."},
            )

        if norm_req_loc:
            # Check 1: Exact City Match (100.0)
            if cand_c and (cand_c == norm_req_loc or norm_req_loc in cand_c or cand_c in norm_req_loc):
                return MatchResult(
                    dimension=RecommendationDimension.LOCATION,
                    status=MatchStatus.MATCH,
                    score=100.0,
                    details={"reason": "Exact city match", "match_level": "city"},
                )

            # Check 2: State-Level Match (80.0)
            if cand_s and (cand_s == norm_req_loc or norm_req_loc in cand_s or cand_s in norm_req_loc):
                return MatchResult(
                    dimension=RecommendationDimension.LOCATION,
                    status=MatchStatus.MATCH,
                    score=80.0,
                    details={"reason": "State-level match", "match_level": "state"},
                )

            # Check 3: Country-Level Match (60.0)
            if cand_co and (cand_co == norm_req_loc or norm_req_loc in cand_co or cand_co in norm_req_loc):
                return MatchResult(

                    dimension=RecommendationDimension.LOCATION,
                    status=MatchStatus.MATCH,
                    score=60.0,
                    details={"reason": "Country-level match", "match_level": "country"},
                )

            # Check 4: Mismatch (0.0)
            return MatchResult(
                dimension=RecommendationDimension.LOCATION,
                status=MatchStatus.MISMATCH,
                score=0.0,
                details={"reason": "Requested location does not match candidate location data.", "match_level": "mismatch"},
            )

        return MatchResult(
            dimension=RecommendationDimension.LOCATION,
            status=MatchStatus.MATCH,
            score=100.0,
            details={"reason": "Work mode requirements satisfied.", "match_level": "mode_match"},
        )

