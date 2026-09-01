"""
Dedicated Employer Query Parser.
Parses natural language query into EmployerSearchRequirements contract with per-attribute provenance tracking.
"""

import re
import logging
from typing import Dict, Any, Optional

from backend.schemas.employer_requirements import (
    EmployerSearchRequirements,
    ProvenanceValue,
    ProvenanceSource,
    LocationRequirement,
    ExperienceRequirement,
    SalaryRequirement,
)
from backend.services.normalization_service import normalize_role_title, normalize_skills
from backend.services.location_service import resolve_location_from_db
from backend.services.profile_parser import (
    resolve_or_create_department_id,
    resolve_employment_type_id,
)
from backend.repositories.db import get_db

log = logging.getLogger(__name__)


def resolve_fresher_work_type_id() -> Optional[int]:
    """
    Dynamically resolves the authoritative work_status ID for Fresher from cyb_work_type table.
    """
    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("SELECT id FROM cyb_work_type WHERE LOWER(name) LIKE '%fresher%' LIMIT 1")
            row = cursor.fetchone()
            if row:
                return row["id"] if isinstance(row, dict) else row[0]
    except Exception as e:
        log.warning(f"Error resolving fresher work type ID from DB: {e}")
    return 1  # Fallback default ID if query fails


def parse_employer_query(
    query_text: str,
    existing_requirements: Optional[EmployerSearchRequirements] = None,
    source: ProvenanceSource = ProvenanceSource.QUERY
) -> EmployerSearchRequirements:
    """
    Parses an employer search query into a canonical EmployerSearchRequirements instance.
    If existing_requirements is provided, performs controlled conversational accumulation and replacement.
    """
    reqs = EmployerSearchRequirements()
    text = query_text.strip()
    text_lower = text.lower()

    # 1. Role & Skill Parsing
    role_val = None
    skills_val = []

    # Role & Skill Parsing
    role_val = None
    skills_val = []

    if "frontend" in text_lower:
        role_val = "Frontend Developer"
    elif "backend" in text_lower:
        role_val = "Backend Developer"
    elif "full stack" in text_lower or "fullstack" in text_lower:
        role_val = "Full Stack Developer"
    elif "data analyst" in text_lower:
        role_val = "Data Analyst"
    elif "developer" in text_lower or "dev" in text_lower:
        role_val = "Developer"
    else:
        role_match = re.search(
            r"(?:hire|need|want|looking for)\s+(?:a|an)?\s*([a-z0-9\-\s]+?)(?=\s+(?:in|with|who|under|for)|$)",
            text_lower,
        )
        if role_match:
            extracted = role_match.group(1).strip()
            extracted = re.sub(r"^(?:to\s+)?(?:hire|need|want|find)\s+(?:a|an)?\s*", "", extracted).strip()
            if extracted and extracted not in ("a", "an", "fresher", "full time", "part time"):
                role_val = extracted

    # Specific technology skill detection
    known_techs = ["react", "reactjs", "javascript", "js", "node", "nodejs", "node.js", "python", "py", "sql", "php", "next.js", "jquery"]
    for tech in known_techs:
        if re.search(r"\b" + re.escape(tech) + r"\b", text_lower):
            skills_val.append(tech)

    if role_val:
        norm_role = normalize_role_title(role_val)
        reqs.role = ProvenanceValue[str](value=norm_role, source=source)
    
    if skills_val:
        norm_sk = normalize_skills(skills_val)
        reqs.skills = ProvenanceValue[list](value=norm_sk, source=source)

    # 2. Location Parsing
    loc_match = re.search(r"\b(?:in|at|near|location|across)\s+([a-z\s]+?)(?=\s+(?:with|under|who|for|having)|$)", text_lower)
    if loc_match:
        loc_str = loc_match.group(1).strip()
        if loc_str and loc_str not in ("a", "an"):
            if loc_str.lower() == "india":
                reqs.location = LocationRequirement(
                    text="India",
                    type="country",
                    country_id=101,
                    city_id=None,
                    state_id=None,
                    source=source,
                )
            else:
                res = resolve_location_from_db(loc_str)
                if res:
                    res_text = getattr(res, "text", None) or (res.get("text") if isinstance(res, dict) else None)
                    res_type = getattr(res, "type", None) or (res.get("type") if isinstance(res, dict) else None)
                    res_city_id = getattr(res, "city_id", None) or (res.get("city_id") if isinstance(res, dict) else None)
                    res_state_id = getattr(res, "state_id", None) or (res.get("state_id") if isinstance(res, dict) else None)
                    res_country_id = getattr(res, "country_id", None) or (res.get("country_id") if isinstance(res, dict) else None)
                    
                    if res_city_id:
                        res_type = "city"
                        res_state_id = None
                    elif res_state_id:
                        res_type = "state"
                    elif res_country_id:
                        res_type = "country"

                    is_res = getattr(res, "resolved", None) if hasattr(res, "resolved") else (res.get("resolved") if isinstance(res, dict) else bool(res_city_id or res_state_id or res_country_id))

                    if is_res:
                        reqs.location = LocationRequirement(
                            text=res_text or loc_str.title(),
                            type=res_type,
                            city_id=res_city_id,
                            state_id=res_state_id,
                            country_id=res_country_id,
                            source=source,
                        )
                    else:
                        reqs.safe_failure = True
                        reqs.failure_reason = f"Could not resolve location: {loc_str}"


    # 3. Experience Parsing
    exp_match = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?|yr)", text_lower)
    if exp_match:
        min_e = float(exp_match.group(1))
        reqs.experience = ExperienceRequirement(min=min_e, max=None, source=source)

    # 4. Fresher Status (Categorical work_status, NEVER min_experience=0)
    if "fresher" in text_lower:
        reqs.fresher = ProvenanceValue[bool](value=True, source=source)

    # 5. Employment Type
    if "full-time" in text_lower or "full time" in text_lower:
        emp_id = resolve_employment_type_id("full-time")
        if emp_id:
            reqs.employment_type = ProvenanceValue[str](value="full-time", source=source)
            reqs.employment_type_id = ProvenanceValue[int](value=emp_id, source=source)
        else:
            reqs.safe_failure = True
            reqs.failure_reason = "Could not resolve employment type: full-time"

    # 6. Immediate Joining & Notice
    if "immediately" in text_lower or "immediate" in text_lower:
        reqs.on_immediate = ProvenanceValue[bool](value=True, source=source)
    if "notice" in text_lower:
        reqs.on_notice = ProvenanceValue[bool](value=True, source=source)

    # 7. Salary Parsing (Deferred Execution)
    sal_match = re.search(r"(?:under|below|max)\s*(\d+(?:\.\d+)?)\s*(?:lpa|lakh)", text_lower)
    if sal_match:
        lpa = float(sal_match.group(1))
        sal_max = lpa * 100000.0
        reqs.salary = SalaryRequirement(min=None, max=sal_max, source=source)
        reqs.salary_filter_deferred = True

    # 8. Multi-turn Accumulation / Replacement Logic
    if existing_requirements:
        # Retain or Replace Role
        if (not reqs.role.value or reqs.role.value in ("Someone", "Developer")) and existing_requirements.role and existing_requirements.role.value:
            if not ("developer" in text_lower or "analyst" in text_lower or "engineer" in text_lower):
                reqs.role = ProvenanceValue[str](
                    value=existing_requirements.role.value,
                    source=ProvenanceSource.EXPLICIT_FOLLOWUP,
                )

        # Retain or Replace Location
        if reqs.location and existing_requirements.location:
            # Handle Replacement (e.g. city -> country replacement purges city_id)
            if reqs.location.type == "country" or reqs.location.text != existing_requirements.location.text:
                reqs.location.city_id = None
                reqs.location.state_id = None
        elif not reqs.location and existing_requirements.location:
            reqs.location = LocationRequirement(
                text=existing_requirements.location.text,
                type=existing_requirements.location.type,
                city_id=existing_requirements.location.city_id,
                state_id=existing_requirements.location.state_id,
                country_id=existing_requirements.location.country_id,
                source=ProvenanceSource.EXPLICIT_FOLLOWUP,
            )



        # Retain Experience if not explicitly overridden
        if not reqs.experience and existing_requirements.experience:
            reqs.experience = ExperienceRequirement(
                min=existing_requirements.experience.min,
                max=existing_requirements.experience.max,
                source=ProvenanceSource.EXPLICIT_FOLLOWUP,
            )

    return reqs
