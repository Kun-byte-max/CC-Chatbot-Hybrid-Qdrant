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
    candidate_name: Optional[str] = None
    experience_level: Optional[str] = None
    experience_min: Optional[int] = None
    experience_max: Optional[int] = None
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    work_mode: Optional[str] = None
    employment_type: Optional[str] = None
    role_type_id: Optional[int] = None
    remote: Optional[bool] = None
    urgent: Optional[bool] = None
    clear_filters: Optional[List[str]] = None
    skill_operation: Optional[str] = None

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
        if "search_target" in parsed_dict or "target" in parsed_dict:
            raw_target = parsed_dict.get("search_target") or parsed_dict.get("target")
            if raw_target in (SearchTarget.JOBS, SearchTarget.PEOPLE_SEARCH, SearchTarget.CANDIDATE_SEARCH, SearchTarget.UNKNOWN):
                target_enum = SearchTarget(raw_target)
            elif isinstance(raw_target, str) and raw_target in SearchTarget.__members__:
                target_enum = SearchTarget[raw_target]
            else:
                target_enum = SearchTarget.UNKNOWN

        exp_raw_val = parsed_dict.get("experience")
        exp_max_val = parsed_dict.get("experience_max")
        exp_min_val = parsed_dict.get("experience_min")

        # Disambiguate general experience number if upper-bound keywords are present
        if exp_raw_val is not None and exp_max_val is None:
            raw_msg = (parsed_dict.get("raw_message") or "").lower() if isinstance(parsed_dict, dict) else ""
            if any(k in raw_msg for k in ["under", "at most", "less than", "up to", "maximum"]):
                exp_max_val = exp_raw_val
                exp_raw_val = None

        exp_val = exp_raw_val if exp_raw_val is not None else exp_min_val
        exp_level_str = str(exp_val).strip() if exp_val is not None else None
        exp_min = None
        if exp_val is not None:
            if isinstance(exp_val, (int, float)):
                exp_min = int(exp_val)
            elif isinstance(exp_val, str):
                import re
                m = re.search(r"(\d+)", exp_val)
                if m:
                    exp_min = int(m.group(1))

        exp_max_val = parsed_dict.get("experience_max")
        exp_max = None
        if exp_max_val is not None:
            if isinstance(exp_max_val, (int, float)):
                exp_max = int(exp_max_val)
            elif isinstance(exp_max_val, str):
                import re
                m = re.search(r"(\d+)", exp_max_val)
                if m:
                    exp_max = int(m.group(1))

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

        cand_name = parsed_dict.get("candidate_name") or parsed_dict.get("person_name") or parsed_dict.get("name")
        cand_name_str = str(cand_name).strip() if cand_name and str(cand_name).strip() else None

        is_new_search = (parsed_dict.get("query_type") == "NEW_SEARCH")
        clear_f = [str(f).lower() for f in (parsed_dict.get("clear_filters") or [])]
        loc_val = parsed_dict.get("location")
        if (is_new_search or "location" in clear_f) and parsed_dict.get("location_source") != "QUERY":
            loc_val = None
            loc_mode = LocationMode.NONE

        if (is_new_search or "experience" in clear_f) and parsed_dict.get("experience") is None and parsed_dict.get("experience_min") is None:
            exp_min = None
            exp_level_str = None
        if (is_new_search or "experience" in clear_f) and parsed_dict.get("experience_max") is None:
            exp_max = None

        emp_type_val = parsed_dict.get("employment_type")
        r_type_id = parsed_dict.get("role_type_id")

        return cls(
            target=target_enum,
            title=parsed_dict.get("keyword") or parsed_dict.get("title"),
            skills=skills_list,
            location=loc_val,
            location_mode=loc_mode,
            seniority_level=parsed_dict.get("seniority_level"),
            company=parsed_dict.get("company"),
            candidate_name=cand_name_str,
            experience_level=exp_level_str,
            experience_min=exp_min,
            experience_max=exp_max,
            salary_min=sal_min,
            work_mode=job_mode_val,
            employment_type=emp_type_val,
            role_type_id=r_type_id,
            remote=is_remote,
            urgent=bool(parsed_dict.get("urgent")) if parsed_dict.get("urgent") is not None else None,
            clear_filters=clear_f if clear_f else None,
            skill_operation=parsed_dict.get("skill_operation"),
        )

