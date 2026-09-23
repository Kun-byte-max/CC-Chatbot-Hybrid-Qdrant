from enum import Enum
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

try:
    from backend.schemas.search_query import SearchTarget, LocationMode
except ModuleNotFoundError:
    from schemas.search_query import SearchTarget, LocationMode  # type: ignore


class SearchCriteria(BaseModel):
    """
    Canonical Common Search Filter Model for multi-target searching.
    Serves as the common representation of explicit user-requested search filters
    across JOBS, PEOPLE_SEARCH, and CANDIDATE_SEARCH targets.
    Does NOT execute searches itself.
    """
    target: SearchTarget = SearchTarget.JOBS
    title: Optional[str] = None
    skills: List[str] = Field(default_factory=list)
    location: Optional[str] = None
    canonical_location: Optional[str] = None
    location_mode: LocationMode = LocationMode.NONE
    seniority_level: Optional[str] = None
    company: Optional[str] = None
    experience_level: Optional[str] = None
    experience_min: Optional[int] = None
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    work_mode: Optional[str] = None
    remote: Optional[bool] = None

    @classmethod
    def from_parsed_dict(cls, parsed_dict: Dict[str, Any]) -> "SearchCriteria":
        """
        Pure, additive converter from search_parser output dictionary to SearchCriteria.
        - Preserves explicitly parsed UNKNOWN target; defaults to JOBS only if search_target key is absent.
        - Aligns field mappings strictly with search_parser and SearchQuery schema without artificial coercions.
        """
        if not parsed_dict:
            return cls()

        target_enum = SearchTarget.JOBS
        if "search_target" in parsed_dict:
            raw_target = parsed_dict.get("search_target")
            if raw_target in (SearchTarget.JOBS, SearchTarget.PEOPLE_SEARCH, SearchTarget.CANDIDATE_SEARCH, SearchTarget.UNKNOWN):
                target_enum = SearchTarget(raw_target)
            elif isinstance(raw_target, str) and raw_target in SearchTarget.__members__:
                target_enum = SearchTarget[raw_target]
            else:
                target_enum = SearchTarget.UNKNOWN

        exp_val = parsed_dict.get("experience")
        exp_level_str = str(exp_val).strip() if exp_val is not None else None
        exp_min = int(exp_val) if exp_val is not None and str(exp_val).isdigit() else None

        sal_val = parsed_dict.get("salary")
        sal_min = float(sal_val) if sal_val is not None and str(sal_val).replace('.', '', 1).isdigit() else None

        job_mode_val = parsed_dict.get("job_mode")
        is_remote = True if job_mode_val and "home" in str(job_mode_val).lower() else None

        raw_skills = parsed_dict.get("skills") or []
        skills_list = [str(s).strip() for s in raw_skills if str(s).strip()]

        # Reuses existing LocationMode enum semantics from search_query.py
        loc_mode = LocationMode.NONE
        loc_src = parsed_dict.get("location_source")
        if loc_src == "QUERY" or parsed_dict.get("location"):
            loc_mode = LocationMode.EXPLICIT
        elif loc_src == "CURRENT":
            loc_mode = LocationMode.NEAR_ME

        return cls(
            target=target_enum,
            title=parsed_dict.get("keyword") or parsed_dict.get("title"),
            skills=skills_list,
            location=parsed_dict.get("location"),
            location_mode=loc_mode,
            seniority_level=parsed_dict.get("seniority_level"),
            company=parsed_dict.get("company"),
            experience_level=exp_level_str,
            experience_min=exp_min,
            salary_min=sal_min,
            work_mode=job_mode_val,
            remote=is_remote,
        )

