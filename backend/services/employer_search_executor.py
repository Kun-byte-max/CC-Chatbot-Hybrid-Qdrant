"""
Search Executor for Employer Candidate Searches.
Pushes hard filters to search_client/search_users and emits structured [EMPLOYER_SEARCH] diagnostic log blocks.
"""

import uuid
import logging
from typing import Dict, Any, List, Optional, Tuple

from backend.schemas.employer_requirements import (
    EmployerSearchRequirements,
    ProvenanceSource,
)
from backend.schemas.candidate_schema import SearchMetadata, StandardizedCandidateObject
from backend.recommendation.mappers import map_doc_to_standard_candidate
from backend.services.search_client import search_users

log = logging.getLogger(__name__)


async def execute_employer_candidate_search(
    requirements: EmployerSearchRequirements,
    session_id: str,
    request_id: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
) -> Tuple[List[StandardizedCandidateObject], SearchMetadata, Dict[str, Any]]:
    """
    Executes an employer candidate search pushing Hard Deterministic Filters to search_users().
    Emits structured [EMPLOYER_SEARCH] diagnostic log blocks.
    Enforces Phase 1 Qualification Boundary: Zero rank_score / matcher scores applied to determine eligibility.
    """
    if not request_id:
        request_id = f"req_{uuid.uuid4().hex[:8]}"

    search_filters: Dict[str, Any] = {}
    parsed_log_lines = []
    filter_log_lines = []

    # 1. Meilisearch Transient Keyword Retrieval Aid
    keyword = requirements.to_meilisearch_keyword()
    if keyword:
        search_filters["keyword"] = keyword

    # 2. Hard Filter Gating: ONLY QUERY and EXPLICIT_FOLLOWUP generate Hard Deterministic Filters
    ALLOWED_HARD_FILTER_SOURCES = (ProvenanceSource.QUERY, ProvenanceSource.EXPLICIT_FOLLOWUP)

    # Location Filter
    if requirements.location:
        loc = requirements.location
        if loc.source in ALLOWED_HARD_FILTER_SOURCES:
            if loc.city_id:
                search_filters["city_id"] = loc.city_id
                filter_log_lines.append(f"city_id={loc.city_id} [{loc.source.value}]")
            elif loc.state_id:
                search_filters["state"] = [loc.state_id]
                filter_log_lines.append(f"state_id={loc.state_id} [{loc.source.value}]")
            elif loc.country_id:
                search_filters["country_id"] = loc.country_id
                filter_log_lines.append(f"country_id={loc.country_id} [{loc.source.value}]")
            parsed_log_lines.append(f"location=\"{loc.text}\" ({loc.type}) [{loc.source.value}]")

    # Role & Skills Provenance Logs
    if requirements.role and requirements.role.value:
        parsed_log_lines.append(f"role=\"{requirements.role.value}\" [{requirements.role.source.value}]")
        filter_log_lines.append(f"role={requirements.role.value} [{requirements.role.source.value}]")
    if requirements.skills and requirements.skills.value:
        parsed_log_lines.append(f"skills={requirements.skills.value} [{requirements.skills.source.value}]")

    # Experience Filter
    if requirements.experience and requirements.experience.source in ALLOWED_HARD_FILTER_SOURCES:
        exp = requirements.experience
        if exp.min is not None:
            search_filters["yearExperience"] = [exp.min]
            filter_log_lines.append(f"experience_min={exp.min} [{exp.source.value}]")
            parsed_log_lines.append(f"experience_min={exp.min} [{exp.source.value}]")

    # Fresher Filter (Categorical work_status, NEVER min_experience=0)
    if requirements.fresher and requirements.fresher.value and requirements.fresher.source in ALLOWED_HARD_FILTER_SOURCES:
        from backend.services.employer_search_parser import resolve_fresher_work_type_id
        fresher_id = resolve_fresher_work_type_id()
        if fresher_id:
            search_filters["work_status"] = fresher_id
            filter_log_lines.append(f"work_status={fresher_id} [{requirements.fresher.source.value}]")
            parsed_log_lines.append(f"fresher=True [{requirements.fresher.source.value}]")

    # Employment Type Filter
    if requirements.employment_type_id and requirements.employment_type_id.value and requirements.employment_type_id.source in ALLOWED_HARD_FILTER_SOURCES:
        search_filters["employment_type"] = requirements.employment_type_id.value
        filter_log_lines.append(f"employment_type_id={requirements.employment_type_id.value} [{requirements.employment_type_id.source.value}]")

    # Department Filter
    if requirements.department_id and requirements.department_id.value and requirements.department_id.source in ALLOWED_HARD_FILTER_SOURCES:
        search_filters["department"] = requirements.department_id.value
        filter_log_lines.append(f"department_id={requirements.department_id.value} [{requirements.department_id.source.value}]")

    # Immediate & Notice Booleans
    if requirements.on_immediate and requirements.on_immediate.value and requirements.on_immediate.source in ALLOWED_HARD_FILTER_SOURCES:
        search_filters["on_immediate"] = True
        filter_log_lines.append(f"on_immediate=True [{requirements.on_immediate.source.value}]")
    if requirements.on_notice and requirements.on_notice.value and requirements.on_notice.source in ALLOWED_HARD_FILTER_SOURCES:
        search_filters["on_notice"] = True
        filter_log_lines.append(f"on_notice=True [{requirements.on_notice.source.value}]")

    # Safe Resolver Failure Check
    if requirements.safe_failure:
        log_block = (
            f"\n[EMPLOYER_SEARCH]\n"
            f"request_id={request_id}\n"
            f"session_id={session_id}\n"
            f"actor=EMPLOYER\n\n"
            f"status=SAFE_FAILURE\n"
            f"reason={requirements.failure_reason}\n"
        )
        log.warning(log_block)
        meta = SearchMetadata(total_matches=0, returned=0, page=page, limit=limit)
        return [], meta, {"success": False, "failure_reason": requirements.failure_reason}

    # 3. Execute Search Client Retrieval
    try:
        res = await search_users(**search_filters)
    except Exception as e:
        log.error("search_users failed in employer_search_executor: %s", e)
        res = {}


    if not res or not res.get("success"):
        total_matches = 0
        raw_hits = []
    else:
        total_matches = res.get("total") or len(res.get("data", []))
        raw_hits = res.get("data", [])

    # Non-Artificial Truncation Slicing
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    page_slice = raw_hits[start_idx:end_idx]
    returned_count = len(page_slice)

    metadata = SearchMetadata(
        total_matches=total_matches,
        returned=returned_count,
        page=page,
        limit=limit,
    )
    metadata.validate_invariants()

    # Map raw documents to StandardizedCandidateObject list
    standard_candidates = [map_doc_to_standard_candidate(doc) for doc in page_slice]

    # Structured [EMPLOYER_SEARCH] & [SEARCH_FILTERS] Diagnostic Logging Block
    log_block = (
        f"\n[EMPLOYER_SEARCH]\n"
        f"request_id={request_id}\n"
        f"session_id={session_id}\n"
        f"actor=EMPLOYER\n\n"
        f"parsed:\n" + ("\n".join(parsed_log_lines) if parsed_log_lines else "None") + "\n\n"
        f"[SEARCH_FILTERS]\n" + ("\n".join(filter_log_lines) if filter_log_lines else "None") + "\n\n"
        f"results:\n"
        f"total_matches={metadata.total_matches}\n"
        f"returned={metadata.returned}\n"
        f"page={metadata.page}\n"
        f"limit={metadata.limit}\n\n"
        f"status=SUCCESS\n"
    )
    log.info(log_block)

    raw_payload = {
        "success": True,
        "raw_hits": page_slice,
        "search_metadata": metadata.dict(),
    }

    return standard_candidates, metadata, raw_payload
