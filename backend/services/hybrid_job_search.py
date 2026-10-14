"""
hybrid_job_search.py — Hybrid Job Search Engine combining Meilisearch and Qdrant Vector Match.

Fuses keyword search (Meilisearch) and vector recommendation (JobMatchClient)
using Weighted Reciprocal Rank Fusion (RRF) with intent-based weight scaling,
post-fusion location & domain eligibility constraints, and graceful degraded fallback.
"""

import asyncio
import logging
import time
from typing import Dict, Any, List, Optional, Tuple

try:
    from backend.config.config import (
        JOB_MATCH_ENABLED,
        HYBRID_SEARCH_ENABLED,
        HYBRID_MEILISEARCH_WEIGHT,
        HYBRID_JOBMATCH_WEIGHT,
        HYBRID_RRF_K,
        HYBRID_RETRIEVAL_LIMIT,
        MAX_RESULTS,
    )
    from backend.services.job_match_client import job_match_client, JobMatchException
    from backend.services.search_client import search_jobs
    from backend.recommendation.location_matcher import LocationMatcher
    from backend.recommendation.recommendation_engine import RecommendationEngine
    from backend.recommendation.mappers import to_job_profile, to_candidate_profile
    from backend.recommendation.models import CandidateProfile
except ModuleNotFoundError:
    from config.config import (  # type: ignore
        JOB_MATCH_ENABLED,
        HYBRID_SEARCH_ENABLED,
        HYBRID_MEILISEARCH_WEIGHT,
        HYBRID_JOBMATCH_WEIGHT,
        HYBRID_RRF_K,
        HYBRID_RETRIEVAL_LIMIT,
        MAX_RESULTS,
    )
    from services.job_match_client import job_match_client, JobMatchException  # type: ignore
    from services.search_client import search_jobs  # type: ignore
    from recommendation.location_matcher import LocationMatcher  # type: ignore
    from recommendation.recommendation_engine import RecommendationEngine  # type: ignore
    from recommendation.mappers import to_job_profile, to_candidate_profile  # type: ignore
    from recommendation.models import CandidateProfile  # type: ignore

log = logging.getLogger(__name__)

# Search Intent Modes
MODE_PROFILE_RECOMMENDATION = "PROFILE_RECOMMENDATION"
MODE_EXPLICIT_KEYWORD = "EXPLICIT_KEYWORD"
MODE_EXPLICIT_LOCATION = "EXPLICIT_LOCATION"
MODE_NEAR_ME = "NEAR_ME"

# Single-instance engine helpers
_location_matcher = LocationMatcher()
_recommendation_engine = RecommendationEngine()


class NormalizedJob:
    """Internal normalized data structure for job fusion."""

    def __init__(
        self,
        job_id: int,
        title: str,
        company: str = "CollarCheck Partner",
        department: str = "General",
        location: str = "Not specified",
        job_mode: str = "Office",
        experience: str = "Not specified",
        salary: str = "Not specified",
        url: str = "",
        description: str = "",
        raw_data: Optional[Dict[str, Any]] = None,
        source: str = "meilisearch",
        meili_rank: Optional[int] = None,
        jobmatch_rank: Optional[int] = None,
        vector_score: Optional[float] = None,
        match_reason: str = "",
    ) -> None:
        self.job_id = int(job_id)
        self.title = str(title or "Job Title").strip()
        self.company = str(company or "CollarCheck Partner").strip()
        self.department = str(department or "General").strip()
        self.location = str(location or "Not specified").strip()
        self.job_mode = str(job_mode or "Office").strip()
        self.experience = str(experience or "Not specified").strip()
        self.salary = str(salary or "Not specified").strip()
        self.url = url or f"https://www.collarcheck.com/jobs-details/{self.job_id}"
        self.description = description or ""
        self.raw_data = raw_data or {}
        self.source = source  # "meilisearch", "jobmatch", or "hybrid"
        self.meili_rank = meili_rank
        self.jobmatch_rank = jobmatch_rank
        self.vector_score = vector_score
        self.rrf_score = 0.0
        self.match_reason = match_reason

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict matching existing recommendation / raw_hit format."""
        d = dict(self.raw_data)
        d.update({
            "id": self.job_id,
            "job_id": self.job_id,
            "job_title": self.title,
            "company_name": self.company,
            "department_name": self.department,
            "location": self.location,
            "job_mode_name": self.job_mode,
            "employment_type": self.raw_data.get("role_type_name") or self.raw_data.get("employment_type") or self.job_mode,
            "role_type": self.raw_data.get("role_type"),
            "role_type_name": self.raw_data.get("role_type_name"),
            "experience": self.experience,
            "salary": self.salary,
            "skill_names": self.raw_data.get("skill_names") or self.raw_data.get("skills") or self.raw_data.get("required_skills"),
            "required_skills": self.raw_data.get("required_skills") or self.raw_data.get("skill_names") or self.raw_data.get("skills"),
            "url": self.url,
            "job_description": self.description,
            "_source": self.source,
            "_rrf_score": self.rrf_score,
            "_match_reason": self.match_reason,
        })
        return d


def detect_search_mode(
    last_user_msg: str,
    keyword: Optional[str] = None,
    parsed: Optional[Dict[str, Any]] = None,
    used_profile_fallback: bool = False,
    explicit_loc: bool = False,
) -> str:
    """
    Detect search mode cleanly:
    GPS presence alone DOES NOT trigger MODE_NEAR_ME.
    MODE_NEAR_ME is triggered ONLY when explicit 'near me' / 'nearby' text intent is present.
    """
    parsed = parsed or {}
    msg_lower = (last_user_msg or "").lower().strip()

    near_me_keywords = ["near me", "nearby", "jobs near me", "closest jobs", "nearby jobs"]
    is_near_me_intent = any(kw in msg_lower for kw in near_me_keywords)

    if is_near_me_intent:
        return MODE_NEAR_ME

    if explicit_loc or parsed.get("location") or "in delhi" in msg_lower:
        return MODE_EXPLICIT_LOCATION

    rec_triggers = [
        "according to my skills",
        "based on my skills",
        "suitable for me",
        "recommend jobs for me",
        "recommend jobs to me",
        "jobs for me",
        "suggest jobs for me",
        "matching my profile",
        "based on my profile",
        "according to my profile",
    ]

    is_rec = any(tr in msg_lower for tr in rec_triggers) or parsed.get("use_profile_skills") or parsed.get("is_recommendation_intent") or used_profile_fallback

    if is_rec and not keyword:
        return MODE_PROFILE_RECOMMENDATION

    if keyword or (is_rec and keyword):
        return MODE_EXPLICIT_KEYWORD

    return MODE_PROFILE_RECOMMENDATION if is_rec else MODE_EXPLICIT_KEYWORD


def get_mode_weights(mode: str) -> Tuple[float, float]:
    """Return (meili_weight, jobmatch_weight) based on intent mode."""
    if mode == MODE_PROFILE_RECOMMENDATION:
        return (HYBRID_JOBMATCH_WEIGHT, HYBRID_MEILISEARCH_WEIGHT)  # (0.30, 0.70)
    return (HYBRID_MEILISEARCH_WEIGHT, HYBRID_JOBMATCH_WEIGHT)  # (0.70, 0.30)


def _normalize_job_mode_category(val: Any) -> Optional[str]:
    """
    Normalizes a job mode ID, name, or string into canonical category:
    'remote', 'hybrid', 'office', or None if unknown/missing.
    """
    if val is None:
        return None

    if isinstance(val, (list, tuple, set)):
        if not val:
            return None
        val = next(iter(val))

    if isinstance(val, (int, float)):
        val_int = int(val)
        if val_int == 2:
            return "remote"
        elif val_int == 3:
            return "hybrid"
        elif val_int == 1:
            return "office"
        elif val_int == 4:
            return "field"
        return None

    val_str = str(val).strip().lower()
    if not val_str or val_str in ("null", "none", "not specified", "n/a"):
        return None

    if val_str == "2":
        return "remote"
    elif val_str == "3":
        return "hybrid"
    elif val_str == "1":
        return "office"
    elif val_str == "4":
        return "field"

    if any(k in val_str for k in ("home", "wfh", "remote")):
        return "remote"
    elif "hybrid" in val_str:
        return "hybrid"
    elif any(k in val_str for k in ("office", "onsite", "site")):
        return "office"
    elif "field" in val_str:
        return "field"

    return None


def passes_job_mode_eligibility(
    job: NormalizedJob,
    target_job_mode: Optional[Any] = None,
) -> bool:
    """
    Evaluates hard work-mode (job_mode) eligibility.
    Enforces strict filtering after RRF fusion so vector/Meili hits violating
    active work_mode constraints (Remote, Hybrid, Office) cannot reach final playcards.
    """
    if not target_job_mode:
        return True

    target_norm = _normalize_job_mode_category(target_job_mode)
    if not target_norm:
        return True

    job_mode_val = (
        job.raw_data.get("job_mode_id")
        or job.raw_data.get("job_mode_name")
        or job.raw_data.get("job_mode")
        or job.job_mode
    )

    job_norm = _normalize_job_mode_category(job_mode_val)

    if not job_norm:
        # FAIL CLOSED: Job mode cannot be reliably determined while a hard constraint is active
        log.info(
            "[JOB_MODE_GATE] Job %s rejected for missing/unknown work mode (raw_mode=%r, target=%s)",
            job.job_id,
            job_mode_val,
            target_norm,
        )
        return False

    is_passed = (job_norm == target_norm)
    if not is_passed:
        log.info(
            "[JOB_MODE_GATE] Job %s (%s) rejected for target work mode %s",
            job.job_id,
            job_norm,
            target_norm,
        )

    return is_passed


def passes_location_eligibility(
    job: NormalizedJob,
    target_city_name: Optional[str] = None,
    target_state_name: Optional[str] = None,
    target_country_name: Optional[str] = None,
) -> bool:
    """
    Evaluates hard location eligibility by delegating directly to LocationMatcher.
    Preserves LocationMatcher Rule 1 (Remote jobs score 100.0 / exempt).
    Hybrid and Onsite jobs require city/state compatibility.
    """
    if not target_city_name and not target_state_name and not target_country_name:
        return True

    job_city = job.raw_data.get("city_name") or job.location
    job_state = job.raw_data.get("state_name")
    job_country = job.raw_data.get("country_name")
    work_mode = job.raw_data.get("job_mode_name") or job.job_mode

    match_res = _location_matcher._match_locations(
        candidate_city=target_city_name,
        candidate_state=target_state_name,
        candidate_country=target_country_name,
        job_city=job_city,
        job_state=job_state,
        job_country=job_country,
        work_mode=work_mode,
    )

    # Remote jobs return match_level="remote" / score 100.0.
    # Onsite / Hybrid jobs must have match_level in ("same_city", "same_state", "same_country") or score >= 70.0
    if match_res.match_level == "remote":
        return True

    if target_city_name:
        if match_res.match_level == "same_city":
            return True
        norm_target = _location_matcher._normalize_text(target_city_name)
        norm_job = _location_matcher._normalize_text(job_city)
        if norm_target and norm_job and (norm_target == norm_job or norm_target in norm_job or norm_job in norm_target):
            return True
        return False

    if match_res.match_level in ("same_city", "same_state") or match_res.score >= 70.0:
        return True

    log.info("[LOCATION_GATE] Job %s (%s) rejected for target location %s (match_level=%s score=%.1f)",
             job.job_id, job_city, target_city_name, match_res.match_level, match_res.score)
    return False


def _normalize_salary_value(val: Any) -> Optional[float]:
    """
    Normalizes a salary value into float annual INR.
    Handles numeric LPA (e.g., 10 -> 1,000,000), direct INR (1,000,000), or strings ('10 LPA', '₹50,000').
    """
    if val is None:
        return None

    if isinstance(val, (int, float)):
        val_f = float(val)
        if val_f <= 0:
            return None
        if val_f < 100:
            return val_f * 100_000.0
        return val_f

    val_str = str(val).strip().lower()
    if not val_str or val_str in ("null", "none", "not specified", "n/a"):
        return None

    val_clean = val_str.replace("₹", "").replace("$", "").replace(",", "").strip()
    is_lpa = "lpa" in val_clean or "lakh" in val_clean or "lac" in val_clean

    import re
    nums = re.findall(r"\d+(?:\.\d+)?", val_clean)
    if not nums:
        return None

    first_num = float(nums[0])
    if first_num <= 0:
        return None

    if is_lpa:
        return first_num * 100_000.0

    if first_num < 100:
        return first_num * 100_000.0

    return first_num


def _extract_job_salary_range(job: NormalizedJob) -> Tuple[Optional[float], Optional[float]]:
    """
    Extracts normalized (sal_min, sal_max) in annual INR float for a NormalizedJob.
    Checks structured fields on raw_data first, falling back to scalar salary attributes.
    """
    raw = job.raw_data or {}
    raw_min = raw.get("salary_min") or raw.get("min_salary") or raw.get("salary_from")
    raw_max = raw.get("salary_max") or raw.get("max_salary") or raw.get("salary_to")
    raw_scalar = raw.get("salary") or job.salary

    sal_min = _normalize_salary_value(raw_min)
    sal_max = _normalize_salary_value(raw_max)

    if sal_min is None and sal_max is None and raw_scalar:
        scal_str = str(raw_scalar).lower()
        import re
        if ("-" in scal_str or "to" in scal_str) and ("lpa" in scal_str or "lakh" in scal_str or "lac" in scal_str):
            nums = re.findall(r"\d+(?:\.\d+)?", scal_str)
            if len(nums) >= 2:
                sal_min = float(nums[0]) * 100_000.0
                sal_max = float(nums[1]) * 100_000.0
        else:
            scalar_norm = _normalize_salary_value(raw_scalar)
            sal_min = scalar_norm
            sal_max = scalar_norm

    return sal_min, sal_max


def passes_salary_eligibility(
    job: NormalizedJob,
    salary_min: Optional[float] = None,
    salary_max: Optional[float] = None,
) -> bool:
    """
    Evaluates hard salary eligibility.
    Enforces strict filtering after RRF fusion so vector/Meili hits violating
    active salary constraints (salary_min, salary_max) cannot reach final playcards.
    Fail-closed when salary constraint is active but job salary cannot be determined.
    """
    if salary_min is None and salary_max is None:
        return True

    job_sal_min, job_sal_max = _extract_job_salary_range(job)

    if job_sal_min is None and job_sal_max is None:
        # FAIL CLOSED: Job salary cannot be reliably determined while a hard constraint is active
        log.info(
            "[SALARY_GATE] Job %s rejected for missing/unknown salary (target min=%s, max=%s)",
            job.job_id, salary_min, salary_max
        )
        return False

    if salary_max is not None:
        check_val = job_sal_max if job_sal_max is not None else job_sal_min
        if check_val is not None and check_val > salary_max:
            log.info(
                "[SALARY_GATE] Job %s (sal_max=%.0f) rejected for target max salary %.0f",
                job.job_id, check_val, salary_max
            )
            return False

    if salary_min is not None:
        check_val = job_sal_min if job_sal_min is not None else job_sal_max
        if check_val is not None and check_val < salary_min:
            log.info(
                "[SALARY_GATE] Job %s (sal_min=%.0f) rejected for target min salary %.0f",
                job.job_id, check_val, salary_min
            )
            return False

    return True


def _extract_job_experience_bounds(job: NormalizedJob) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Extracts normalized (job_exp_min, job_exp_max, job_exp_id) for a NormalizedJob.
    Checks structured fields on raw_data first, falling back to string text parsing.
    """
    raw = job.raw_data or {}
    exp_min_raw = raw.get("experience_min") or raw.get("min_experience")
    exp_max_raw = raw.get("experience_max") or raw.get("max_experience")
    exp_id_raw = raw.get("experience_id")

    exp_min = int(exp_min_raw) if exp_min_raw is not None and str(exp_min_raw).isdigit() else None
    exp_max = int(exp_max_raw) if exp_max_raw is not None and str(exp_max_raw).isdigit() else None
    exp_id = int(exp_id_raw) if exp_id_raw is not None and str(exp_id_raw).isdigit() else None

    if exp_min is None and exp_max is None:
        exp_str = str(raw.get("experience_name") or raw.get("experience") or job.experience or "").strip().lower()
        if exp_str and exp_str not in ("null", "none", "not specified", "n/a"):
            import re
            if "fresher" in exp_str or "fresh" in exp_str or "0-1" in exp_str or "0 year" in exp_str or "0 yrs" in exp_str:
                exp_min, exp_max = 0, 1
            else:
                m = re.search(r"(\d+)\s*(?:-\s*(\d+))?\s*(?:year|yr|yrs|years|\+)?", exp_str)
                if m:
                    exp_min = int(m.group(1))
                    exp_max = int(m.group(2)) if m.group(2) else (exp_min if "+" not in exp_str else 99)

    return exp_min, exp_max, exp_id


def passes_experience_eligibility(
    job: NormalizedJob,
    experience_min: Optional[int] = None,
    experience_max: Optional[int] = None,
    experience_level: Optional[str] = None,
    experience_ids: Optional[List[int]] = None,
    message: str = "",
) -> bool:
    """
    Evaluates hard experience eligibility.
    Enforces strict filtering after RRF fusion so vector/Meili hits violating
    active experience constraints (fresher, min experience, max experience) cannot reach final playcards.
    Fail-closed when experience constraint is active but job experience requirements cannot be determined.
    """
    msg_lower = (message or "").lower()
    is_fresher_req = (
        (experience_level or "").lower() == "fresher"
        or "fresher" in msg_lower
        or "freshers" in msg_lower
        or (experience_min == 0 and experience_max == 0)
        or (experience_ids and set(experience_ids).issubset({1, 2}))
    )

    is_exp_constrained = (
        experience_min is not None
        or experience_max is not None
        or experience_level is not None
        or (experience_ids and len(experience_ids) > 0)
        or is_fresher_req
    )

    if not is_exp_constrained:
        return True

    job_exp_min, job_exp_max, job_exp_id = _extract_job_experience_bounds(job)

    if job_exp_min is None and job_exp_max is None and job_exp_id is None:
        # FAIL CLOSED: Job required experience cannot be reliably determined while a hard constraint is active
        log.info(
            "[EXPERIENCE_GATE] Job %s rejected for missing/unknown experience requirements",
            job.job_id
        )
        return False

    if is_fresher_req:
        is_fresher_job = (
            (job_exp_min is not None and job_exp_min == 0)
            or (job_exp_max is not None and job_exp_max <= 1)
            or (job_exp_id is not None and job_exp_id in (1, 2))
        )
        if not is_fresher_job:
            log.info(
                "[EXPERIENCE_GATE] Job %s (min=%s, max=%s, id=%s) rejected for fresher query",
                job.job_id, job_exp_min, job_exp_max, job_exp_id
            )
            return False
        return True

    if experience_min is not None:
        job_max_bound = job_exp_max if job_exp_max is not None else job_exp_min
        if job_max_bound is not None and job_max_bound < experience_min:
            log.info(
                "[EXPERIENCE_GATE] Job %s (max_bound=%s) rejected for target min experience %s",
                job.job_id, job_max_bound, experience_min
            )
            return False

    if experience_max is not None:
        job_min_bound = job_exp_min if job_exp_min is not None else job_exp_max
        if job_min_bound is not None and job_min_bound > experience_max:
            log.info(
                "[EXPERIENCE_GATE] Job %s (min_bound=%s) rejected for target max experience %s",
                job.job_id, job_min_bound, experience_max
            )
            return False

    return True


def passes_domain_relevance_eligibility(
    job: NormalizedJob,
    keyword: Optional[str] = None,
    candidate_profile: Optional[CandidateProfile] = None,
) -> bool:
    """
    Evaluates explicit keyword domain relevance by delegating directly to RecommendationEngine._evaluate_domain_gate.
    Ensures Meilisearch, Hybrid, and Qdrant hits enforce explicit role/title relevance.
    """
    if not keyword or not str(keyword).strip():
        return True

    job_prof = to_job_profile(job.raw_data)
    cand_prof = candidate_profile or CandidateProfile(id=0)

    domain_passed = _recommendation_engine._evaluate_domain_gate(
        candidate=cand_prof,
        job=job_prof,
        skill_res=None,
        title_res=None,
        search_query_title=keyword,
    )

    if not domain_passed:
        log.info("[DOMAIN_GATE] Job %s ('%s') rejected for explicit keyword '%s'",
                 job.job_id, job.title, keyword)

    return domain_passed


def passes_employment_type_eligibility(
    job: NormalizedJob,
    target_role_type_id: Optional[int] = None,
    target_employment_type: Optional[str] = None,
) -> bool:
    """
    Evaluates hard employment-type (role_type) eligibility.
    Enforces strict filtering after RRF fusion so vector/Meili hits violating
    active employment-type constraints (Full-time, Part-time, Internship) cannot reach final playcards.
    Fail-closed when employment-type constraint is active but job role_type is missing/unknown/0.
    """
    if target_role_type_id is None and not target_employment_type:
        return True

    if target_role_type_id is None and target_employment_type:
        try:
            from backend.services.search_parser import resolve_job_role_type_id
            target_role_type_id = resolve_job_role_type_id(target_employment_type)
        except Exception:
            pass

    if target_role_type_id is None:
        return True

    raw = job.raw_data or {}
    job_role_type = raw.get("role_type")
    
    if job_role_type is None and raw.get("role_type_name"):
        try:
            from backend.services.search_parser import resolve_job_role_type_id
            job_role_type = resolve_job_role_type_id(raw.get("role_type_name"))
        except Exception:
            pass

    if job_role_type is None or str(job_role_type).strip() in ("0", "null", "none"):
        log.info(
            "[EMPLOYMENT_TYPE_GATE] Job %s rejected for missing/unknown role_type (target_id=%s)",
            job.job_id, target_role_type_id
        )
        return False

    try:
        job_rt_int = int(job_role_type)
    except (ValueError, TypeError):
        log.info(
            "[EMPLOYMENT_TYPE_GATE] Job %s rejected for unparseable role_type=%r (target_id=%s)",
            job.job_id, job_role_type, target_role_type_id
        )
        return False

    is_passed = (job_rt_int == int(target_role_type_id))
    if not is_passed:
        log.info(
            "[EMPLOYMENT_TYPE_GATE] Job %s (role_type=%s) rejected for target role_type_id %s",
            job.job_id, job_rt_int, target_role_type_id
        )

    return is_passed


async def execute_hybrid_job_search(
    last_user_msg: str,
    candidate_id: Optional[Any] = None,
    candidate_profile: Optional[CandidateProfile] = None,
    keyword: Optional[str] = None,
    base_filters: Optional[Dict[str, Any]] = None,
    parsed: Optional[Dict[str, Any]] = None,
    used_profile_fallback: bool = False,
    profile_terms: Optional[List[str]] = None,
    explicit_loc: bool = False,
    target_city_name: Optional[str] = None,
    target_state_name: Optional[str] = None,
    limit: int = MAX_RESULTS,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Executes parallel retrieval from Meilisearch and JobMatch (Qdrant),
    applies Reciprocal Rank Fusion (RRF), enforces post-fusion LocationMatcher and RecommendationEngine
    eligibility constraints, and returns deduplicated hits with telemetry metadata.
    """
    t_start = time.time()
    base_filters = dict(base_filters or {})
    profile_terms = profile_terms or []
    parsed = parsed or {}

    target_role_type_id = (
        (parsed.get("role_type_id") if parsed else None)
        or (base_filters.get("role_type") if base_filters else None)
    )
    if isinstance(target_role_type_id, (list, tuple, set)) and target_role_type_id:
        target_role_type_id = next(iter(target_role_type_id))
    if target_role_type_id is None and parsed and parsed.get("employment_type"):
        try:
            from backend.services.search_parser import resolve_job_role_type_id
            target_role_type_id = resolve_job_role_type_id(parsed["employment_type"])
        except Exception:
            pass

    if target_role_type_id and "role_type" not in base_filters:
        base_filters["role_type"] = target_role_type_id

    mode = detect_search_mode(
        last_user_msg=last_user_msg,
        keyword=keyword,
        parsed=parsed,
        used_profile_fallback=used_profile_fallback,
        explicit_loc=explicit_loc,
    )

    valid_candidate = bool(
        candidate_id
        and str(candidate_id).isdigit()
        and int(candidate_id) > 0
    )

    can_use_jobmatch = bool(JOB_MATCH_ENABLED and valid_candidate)

    meili_weight, jobmatch_weight = get_mode_weights(mode)

    meili_attempted = True
    jobmatch_attempted = can_use_jobmatch

    # Prepare concurrent async tasks
    async def _fetch_meilisearch() -> Tuple[List[Dict[str, Any]], float]:
        t0 = time.time()
        hits = []
        try:
            _search_fn = search_jobs

            if used_profile_fallback and profile_terms and not keyword:
                from backend.services.user_skill_aggregator import select_balanced_retrieval_terms
                prov_list = getattr(candidate_profile, "skill_provenance", None) if candidate_profile else None
                clean_terms = select_balanced_retrieval_terms(profile_terms, provenance=prov_list, limit=15)
                if clean_terms:
                    tasks = [_search_fn(keyword=t, **base_filters) for t in clean_terms]
                    results_list = await asyncio.gather(*tasks, return_exceptions=True)
                    seen_ids = set()
                    for r in results_list:
                        if isinstance(r, dict) and r.get("success"):
                            for item in r.get("data", []):
                                j_id = item.get("id")
                                if j_id is not None and j_id not in seen_ids:
                                    seen_ids.add(j_id)
                                    hits.append(item)
                    if not hits and not explicit_loc:
                        res_all = await _search_fn(**base_filters)
                        hits = res_all.get("data", []) if isinstance(res_all, dict) else []
            else:
                filters = dict(base_filters)
                if keyword:
                    filters["keyword"] = keyword
                filters["limit"] = max(HYBRID_RETRIEVAL_LIMIT, limit * 2)
                res_jobs = await _search_fn(**filters)
                hits = res_jobs.get("data", []) if isinstance(res_jobs, dict) else []
        except Exception as err:
            log.warning("[HybridSearch] Meilisearch retrieval failed: %s", err)
            hits = []
        dt = (time.time() - t0) * 1000.0
        return hits, dt

    async def _fetch_jobmatch() -> Tuple[List[Dict[str, Any]], float]:
        t0 = time.time()
        results = []
        if not can_use_jobmatch:
            return [], 0.0
        try:
            jm_filters = {}
            if target_role_type_id:
                jm_filters["role_type"] = target_role_type_id
            res_data = await job_match_client.find_jobs(
                candidate_id=int(candidate_id),
                limit=max(HYBRID_RETRIEVAL_LIMIT, limit * 2),
                filters=jm_filters,
            )
            raw = res_data.get("results") or res_data.get("data") or []
            if isinstance(raw, list):
                results = raw
        except JobMatchException as jm_err:
            log.info("[HybridSearch] JobMatch unavailable or not indexed (%s). Graceful fallback to Meilisearch.", jm_err)
            results = []
        except Exception as ex:
            log.warning("[HybridSearch] Unexpected error calling JobMatch API: %s", ex)
            results = []
        dt = (time.time() - t0) * 1000.0
        return results, dt

    # Parallel Execution
    meili_res, jm_res = await asyncio.gather(_fetch_meilisearch(), _fetch_jobmatch(), return_exceptions=True)

    meili_hits, meili_ms = meili_res if isinstance(meili_res, tuple) else ([], 0.0)
    jm_hits, jm_ms = jm_res if isinstance(jm_res, tuple) else ([], 0.0)

    # Normalize Meilisearch hits
    normalized_map: Dict[int, NormalizedJob] = {}

    for rank, item in enumerate(meili_hits, start=1):
        if not isinstance(item, dict):
            continue
        try:
            j_id = int(item.get("id") or item.get("job_id"))
        except (TypeError, ValueError):
            continue

        title = item.get("job_title") or item.get("title") or "Job Title"
        comp = item.get("company_name") or "CollarCheck Partner"
        dept = item.get("department_name") or "General"
        loc = item.get("location") or item.get("city_name") or "Not specified"
        mode_str = item.get("job_mode_name") or "Office"
        exp_str = item.get("experience_name") or str(item.get("experience") or "Not specified")
        sal_str = str(item.get("salary") or "Not specified")
        desc = item.get("job_description") or ""

        nj = NormalizedJob(
            job_id=j_id,
            title=title,
            company=comp,
            department=dept,
            location=loc,
            job_mode=mode_str,
            experience=exp_str,
            salary=sal_str,
            description=desc,
            raw_data=item,
            source="meilisearch",
            meili_rank=rank,
            match_reason="Matched search filters",
        )
        normalized_map[j_id] = nj

    # Normalize JobMatch hits
    for rank, item in enumerate(jm_hits, start=1):
        if not isinstance(item, dict):
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        j_id_raw = item.get("job_id") or payload.get("job_id") or item.get("id")
        try:
            j_id = int(j_id_raw)
        except (TypeError, ValueError):
            continue

        title = payload.get("job_title") or payload.get("title") or item.get("title")
        if not title or not str(title).strip():
            continue  # Drop incomplete record missing essential title field

        raw_score = float(item.get("final_score", 0.0))
        reasons = item.get("reasons") or []
        reason_str = f"Match Score: {round(raw_score * 100.0)}% • " + " • ".join(str(r) for r in reasons) if reasons else f"Match Score: {round(raw_score * 100.0)}%"

        if j_id in normalized_map:
            # Merged hit (appears in both sources)
            existing = normalized_map[j_id]
            existing.source = "hybrid"
            existing.jobmatch_rank = rank
            existing.vector_score = raw_score
            existing.match_reason = f"Vector Match ({round(raw_score * 100.0)}%) + Keyword Match"
        else:
            comp = payload.get("company_name") or "CollarCheck Partner"
            dept = payload.get("department_name") or "General"
            loc = payload.get("location") or payload.get("city_name") or "Not specified"
            mode_str = payload.get("job_mode_name") or "Office"
            exp_str = payload.get("experience_name") or str(payload.get("experience") or "Not specified")
            sal_str = str(payload.get("salary") or "Not specified")
            desc = payload.get("job_description") or ""

            nj = NormalizedJob(
                job_id=j_id,
                title=title,
                company=comp,
                department=dept,
                location=loc,
                job_mode=mode_str,
                experience=exp_str,
                salary=sal_str,
                description=desc,
                raw_data=payload,
                source="jobmatch",
                jobmatch_rank=rank,
                vector_score=raw_score,
                match_reason=reason_str,
            )
            normalized_map[j_id] = nj

    # Calculate Reciprocal Rank Fusion (RRF) scores
    all_jobs = list(normalized_map.values())
    for job in all_jobs:
        score = 0.0
        if job.meili_rank is not None:
            score += meili_weight / (HYBRID_RRF_K + job.meili_rank)
        if job.jobmatch_rank is not None:
            score += jobmatch_weight / (HYBRID_RRF_K + job.jobmatch_rank)
        job.rrf_score = score

    # Sort candidates by RRF score descending
    all_jobs.sort(key=lambda j: j.rrf_score, reverse=True)

    # Determine active target constraints
    target_job_mode = (
        (base_filters.get("job_mode") if base_filters else None)
        or (parsed.get("job_mode") if parsed else None)
    )
    target_mode_norm = _normalize_job_mode_category(target_job_mode)

    target_sal_min = (
        (parsed.get("salary_min") if parsed else None)
        or (base_filters.get("salary_min") if base_filters else None)
    )
    if target_sal_min is None and parsed and parsed.get("salary"):
        try:
            val = float(parsed["salary"])
            if val > 0:
                target_sal_min = val * 100_000.0 if val < 100 else val
        except (ValueError, TypeError):
            pass

    target_sal_max = (
        (parsed.get("salary_max") if parsed else None)
        or (base_filters.get("salary_max") if base_filters else None)
    )

    target_exp_min = parsed.get("experience_min") if parsed else None
    target_exp_max = parsed.get("experience_max") if parsed else None
    target_exp_level = parsed.get("experience_level") if parsed else None
    raw_exp = parsed.get("experience") if parsed else None
    if target_exp_min is None and target_exp_max is None and raw_exp is not None:
        try:
            exp_n = int(raw_exp)
            if exp_n == 0:
                target_exp_min, target_exp_max = 0, 0
                target_exp_level = "fresher"
            else:
                target_exp_min = exp_n
        except (ValueError, TypeError):
            pass

    target_exp_ids = base_filters.get("experience") if base_filters else None

    job_mode_rejected_count = 0
    salary_rejected_count = 0
    experience_rejected_count = 0
    emp_type_rejected_count = 0

    # Post-Fusion Eligibility Filtering Pipeline
    eligible_jobs = []
    for job in all_jobs:
        # Step 1: Hard Work-Mode (Job Mode) Eligibility Filter
        if target_mode_norm:
            if not passes_job_mode_eligibility(job, target_job_mode=target_mode_norm):
                job_mode_rejected_count += 1
                continue

        # Step 2: Location Eligibility
        if (explicit_loc or mode == MODE_NEAR_ME) and target_city_name:
            if not passes_location_eligibility(job, target_city_name=target_city_name, target_state_name=target_state_name):
                continue

        # Step 3: Hard Salary Eligibility Filter
        if target_sal_min is not None or target_sal_max is not None:
            if not passes_salary_eligibility(job, salary_min=target_sal_min, salary_max=target_sal_max):
                salary_rejected_count += 1
                continue

        # Step 4: Hard Experience Eligibility Filter
        if (
            target_exp_min is not None
            or target_exp_max is not None
            or target_exp_level
            or target_exp_ids
            or (parsed and parsed.get("experience") is not None)
            or ("fresher" in (last_user_msg or "").lower())
        ):
            if not passes_experience_eligibility(
                job,
                experience_min=target_exp_min,
                experience_max=target_exp_max,
                experience_level=target_exp_level,
                experience_ids=target_exp_ids,
                message=last_user_msg,
            ):
                experience_rejected_count += 1
                continue

        # Step 5: Explicit Keyword Domain Relevance Eligibility
        if mode == MODE_EXPLICIT_KEYWORD and keyword:
            if not passes_domain_relevance_eligibility(job, keyword=keyword, candidate_profile=candidate_profile):
                continue

        # Step 6: Urgent Job Eligibility Filter
        if base_filters.get("urgent") == 1 or parsed.get("urgent"):
            job_urg_val = job.raw_data.get("urgent")
            is_urgent_confirmed = (
                job_urg_val is not None
                and str(job_urg_val).strip().lower() in ("1", "true")
            )
            if not is_urgent_confirmed:
                log.info("[URGENT_GATE] Job %s rejected for non-urgent status (urgent=%s)", job.job_id, job_urg_val)
                continue

        # Step 7: Hard Employment-Type (Role Type) Eligibility Filter
        if target_role_type_id:
            if not passes_employment_type_eligibility(job, target_role_type_id=target_role_type_id):
                emp_type_rejected_count += 1
                continue

        eligible_jobs.append(job)

    # Convert back to hit dicts preserving raw payload for JobCard building
    final_hits = [j.to_dict() for j in eligible_jobs[:limit]]

    t_total_ms = (time.time() - t_start) * 1000.0

    telemetry = {
        "search_mode": mode,
        "job_mode_requested": target_mode_norm,
        "job_mode_rejected_count": job_mode_rejected_count,
        "salary_rejected_count": salary_rejected_count,
        "experience_rejected_count": experience_rejected_count,
        "candidate_present": valid_candidate,
        "meilisearch_attempted": meili_attempted,
        "jobmatch_attempted": jobmatch_attempted,
        "meilisearch_count": len(meili_hits),
        "jobmatch_count": len(jm_hits),
        "fused_count": len(all_jobs),
        "eligible_count": len(eligible_jobs),
        "final_count": min(len(eligible_jobs), limit),
        "meili_ms": round(meili_ms, 1),
        "jobmatch_ms": round(jm_ms, 1),
        "total_ms": round(t_total_ms, 1),
    }

    log.info(
        "HYBRID_SEARCH mode=%s candidate=%s target_job_mode=%s rejected_mode_count=%d rejected_sal_count=%d rejected_exp_count=%d meilisearch_count=%d jobmatch_count=%d fused_count=%d eligible_count=%d final_count=%d total_ms=%.1fms",
        mode, valid_candidate, target_mode_norm, job_mode_rejected_count, salary_rejected_count, experience_rejected_count, len(meili_hits), len(jm_hits), len(all_jobs), len(eligible_jobs), telemetry["final_count"], t_total_ms
    )

    return final_hits, telemetry

