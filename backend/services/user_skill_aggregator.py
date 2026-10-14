"""
user_skill_aggregator.py — Aggregation foundation for unified user skills.

Aggregates profile skills (user-profile/slug) and employment skills (allEmployementNew)
into a unified, deduplicated effective skill set with rich source provenance tracking.
"""

from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
import logging

try:
    from backend.services.user_data_extractor import (
        extract_profile_skills,
        extract_employment_skills,
    )
except ModuleNotFoundError:
    from services.user_data_extractor import (  # type: ignore
        extract_profile_skills,
        extract_employment_skills,
    )

log = logging.getLogger(__name__)

# High-confidence, manually reviewed canonical alias map for skills
HIGH_CONFIDENCE_ALIAS_MAP: Dict[str, str] = {
    "react.js": "React",
    "reactjs": "React",
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "js": "JavaScript",
    "javascript": "JavaScript",
    "node.js": "Node.js",
    "nodejs": "Node.js",
    "vue.js": "Vue.js",
    "vuejs": "Vue.js",
    "angular.js": "Angular",
    "angularjs": "Angular",
    "py": "Python",
    "golang": "Go",
}

INVALID_SKILL_NAMES = {"none", "null", "undefined", "n/a", "na", "-", ""}


@dataclass
class SkillProvenance:
    """
    Tracks provenance details for a single canonical skill.
    """
    skill_name: str
    raw_names: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    profile_details: List[Dict[str, Any]] = field(default_factory=list)
    employment_details: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class AggregatedSkillsResult:
    """
    Result container for aggregated skills and provenance metadata.
    """
    effective_skills: List[str]
    provenance: List[SkillProvenance]
    profile_skills: List[Dict[str, Any]]
    employment_skills: List[Dict[str, Any]]
    profile_status: str
    employment_status: str


def _determine_source_status(
    api_data: Optional[Dict[str, Any]],
    extracted_items: List[Dict[str, Any]],
    explicit_status: Optional[str] = None
) -> str:
    """
    Determines the explicit status of a skill source.
    Distinguishes SUCCESS, EMPTY, FAILED, and NOT_FETCHED.
    """
    if explicit_status:
        return explicit_status
    if api_data is None:
        return "NOT_FETCHED"
    if not isinstance(api_data, dict):
        return "FAILED"
    if api_data.get("status") is False or "error" in api_data:
        return "FAILED"
    if not extracted_items:
        return "EMPTY"
    return "SUCCESS"


def aggregate_user_skills(
    user_profile_data: Optional[Dict[str, Any]] = None,
    employment_data: Optional[Dict[str, Any]] = None,
    profile_status: Optional[str] = None,
    employment_status: Optional[str] = None,
    alias_map: Optional[Dict[str, str]] = None,
) -> AggregatedSkillsResult:
    """
    Aggregates skills from user profile and employment history payloads into a unified
    effective skill set for downstream job search and recommendation engines.

    Parameters:
      - user_profile_data: Raw JSON payload from auth/user-profile/{slug} endpoint (or None)
      - employment_data: Raw JSON payload from employee/allEmployementNew endpoint (or None)
      - profile_status: Optional explicit status flag ("SUCCESS", "EMPTY", "FAILED", "NOT_FETCHED")
      - employment_status: Optional explicit status flag ("SUCCESS", "EMPTY", "FAILED", "NOT_FETCHED")
      - alias_map: Custom alias dictionary for canonical name mapping (defaults to HIGH_CONFIDENCE_ALIAS_MAP)

    Returns:
      AggregatedSkillsResult with deduplicated effective_skills list and rich provenance tracking.
    """
    effective_alias_map = HIGH_CONFIDENCE_ALIAS_MAP if alias_map is None else alias_map

    # Step 1: Extract skills using existing extractor functions (non-mutating)
    raw_profile_skills = extract_profile_skills(user_profile_data) if user_profile_data else []
    raw_employment_skills = extract_employment_skills(employment_data) if employment_data else []

    # Step 2: Determine source status independently
    final_prof_status = _determine_source_status(user_profile_data, raw_profile_skills, profile_status)
    final_emp_status = _determine_source_status(employment_data, raw_employment_skills, employment_status)

    # Step 3: Deduplicate and normalize skills while preserving source provenance
    provenance_by_key: Dict[str, SkillProvenance] = {}
    ordered_canonical_keys: List[str] = []

    # Process Profile Skills
    for item in raw_profile_skills:
        if not isinstance(item, dict):
            continue
        skill_val = item.get("skill")
        if not skill_val or not isinstance(skill_val, str):
            continue
        clean_name = skill_val.strip()
        if not clean_name or clean_name.lower() in INVALID_SKILL_NAMES:
            continue

        norm_key = clean_name.lower()
        canonical_name = effective_alias_map.get(norm_key, clean_name)
        canonical_key = canonical_name.lower()

        if canonical_key not in provenance_by_key:
            provenance_by_key[canonical_key] = SkillProvenance(
                skill_name=canonical_name,
                raw_names=[clean_name],
                sources=["PROFILE"],
                profile_details=[dict(item)],
                employment_details=[],
            )
            ordered_canonical_keys.append(canonical_key)
        else:
            prov = provenance_by_key[canonical_key]
            if "PROFILE" not in prov.sources:
                prov.sources.append("PROFILE")
            if clean_name not in prov.raw_names:
                prov.raw_names.append(clean_name)
            prov.profile_details.append(dict(item))

    # Process Employment Skills
    for item in raw_employment_skills:
        if not isinstance(item, dict):
            continue
        name_val = item.get("name")
        if not name_val or not isinstance(name_val, str):
            continue
        clean_name = name_val.strip()
        if not clean_name or clean_name.lower() in INVALID_SKILL_NAMES:
            continue

        norm_key = clean_name.lower()
        canonical_name = effective_alias_map.get(norm_key, clean_name)
        canonical_key = canonical_name.lower()

        if canonical_key not in provenance_by_key:
            provenance_by_key[canonical_key] = SkillProvenance(
                skill_name=canonical_name,
                raw_names=[clean_name],
                sources=["EMPLOYMENT"],
                profile_details=[],
                employment_details=[dict(item)],
            )
            ordered_canonical_keys.append(canonical_key)
        else:
            prov = provenance_by_key[canonical_key]
            if "EMPLOYMENT" not in prov.sources:
                prov.sources.append("EMPLOYMENT")
            if clean_name not in prov.raw_names:
                prov.raw_names.append(clean_name)
            prov.employment_details.append(dict(item))

    effective_skills = [provenance_by_key[k].skill_name for k in ordered_canonical_keys]
    provenance_list = [provenance_by_key[k] for k in ordered_canonical_keys]

    return AggregatedSkillsResult(
        effective_skills=effective_skills,
        provenance=provenance_list,
        profile_skills=raw_profile_skills,
        employment_skills=raw_employment_skills,
        profile_status=final_prof_status,
        employment_status=final_emp_status,
    )


def select_balanced_retrieval_terms(
    profile_terms: List[str],
    provenance: Optional[List[SkillProvenance]] = None,
    limit: int = 15,
) -> List[str]:
    """
    Selects up to `limit` retrieval terms fairly across profile and employment skill sources.
    Preserves case-insensitive deduplication, deterministic order, and bounds concurrent retrieval.
    """
    if not profile_terms:
        return []

    seen_lower: Set[str] = set()
    clean_terms: List[str] = []

    for t in profile_terms:
        if not t or not isinstance(t, str):
            continue
        t_clean = t.strip()
        t_low = t_clean.lower()
        if t_clean and t_low not in seen_lower:
            seen_lower.add(t_low)
            clean_terms.append(t_clean)

    if len(clean_terms) <= limit:
        return clean_terms

    if provenance:
        shared_terms: List[str] = []
        profile_only: List[str] = []
        employment_only: List[str] = []
        prov_map: Dict[str, SkillProvenance] = {}

        for p in provenance:
            if hasattr(p, "skill_name") and hasattr(p, "sources"):
                prov_map[p.skill_name.lower()] = p

        for t in clean_terms:
            t_low = t.lower()
            p = prov_map.get(t_low)
            if p:
                srcs = set(p.sources or [])
                if ("PROFILE" in srcs or "user-profile" in srcs) and ("EMPLOYMENT" in srcs or "allEmployementNew" in srcs):
                    shared_terms.append(t)
                elif "EMPLOYMENT" in srcs or "allEmployementNew" in srcs:
                    employment_only.append(t)
                else:
                    profile_only.append(t)
            else:
                profile_only.append(t)

        selected: List[str] = list(shared_terms)
        p_idx = 0
        e_idx = 0
        while len(selected) < limit and (p_idx < len(profile_only) or e_idx < len(employment_only)):
            if e_idx < len(employment_only) and len(selected) < limit:
                selected.append(employment_only[e_idx])
                e_idx += 1
            if p_idx < len(profile_only) and len(selected) < limit:
                selected.append(profile_only[p_idx])
                p_idx += 1
        return selected

    return clean_terms[:limit]
