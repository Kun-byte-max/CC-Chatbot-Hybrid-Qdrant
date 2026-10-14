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
    source: ProvenanceSource = ProvenanceSource.QUERY,
    user_location: Optional[Any] = None,
    parsed_llm: Optional[Dict[str, Any]] = None,
) -> EmployerSearchRequirements:
    """
    Parses an employer search query into a canonical EmployerSearchRequirements instance via LLM extraction.
    If existing_requirements is provided, performs controlled conversational accumulation and replacement.
    """
    import asyncio
    try:
        from backend.services.search_parser import parse_search_query
    except ImportError:
        from services.search_parser import parse_search_query  # type: ignore

    reqs = EmployerSearchRequirements()
    text = query_text.strip()
    text_lower = text.lower()

    # Reuse provided parsed_llm if available, otherwise call LLM search parser
    if not parsed_llm or not isinstance(parsed_llm, dict):
        parsed_llm = {}
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                parsed_llm = {}
            else:
                parsed_llm = asyncio.run(parse_search_query(text, "employer", messages=[]))
        except Exception:
            log.warning("LLM employer query extraction fallback to deterministic rules")
            parsed_llm = {}

    # Extract Role & Skills
    role_val = parsed_llm.get("keyword")
    skills_val = parsed_llm.get("skills") or []

    # Fallback role/skill extraction if LLM did not extract keyword
    if not role_val:
        if "frontend" in text_lower:
            role_val = "Frontend Developer"
        elif "backend" in text_lower:
            role_val = "Backend Developer"
        elif "full stack" in text_lower or "fullstack" in text_lower:
            role_val = "Full Stack Developer"
        elif "data analyst" in text_lower:
            role_val = "Data Analyst"
        elif "data scientist" in text_lower:
            role_val = "Data Scientist"
        elif "business analyst" in text_lower:
            role_val = "Business Analyst"
        elif "hr" in text_lower or "human resource" in text_lower:
            role_val = "HR Manager"
        elif "sales" in text_lower:
            role_val = "Sales Executive"
        elif "accountant" in text_lower or "accounting" in text_lower:
            role_val = "Accountant"
        elif "marketing" in text_lower:
            role_val = "Marketing Manager"
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

    # Skill detection fallback
    clear_skills_requested = bool(re.search(r"\b(?:clear|remove|delete)\s+(?:the\s+)?skill[s]?\b", text_lower))
    if not clear_skills_requested:
        known_techs = ["react", "reactjs", "javascript", "js", "node", "nodejs", "node.js", "python", "py", "sql", "php", "next.js", "jquery"]
        for tech in known_techs:
            if re.search(r"\b" + re.escape(tech) + r"\b", text_lower) and tech not in skills_val:
                skills_val.append(tech)

        # Apply role/skill extraction: ensure technology is populated in skills_val while preserving full role_val
        if role_val and any(t in str(role_val).lower() for t in ["react", "python", "java", "node", "nodejs", "node.js"]):
            tech_tokens = [t for t in ["react", "python", "java", "node", "nodejs", "node.js"] if t in str(role_val).lower()]
            for t in tech_tokens:
                if t not in skills_val:
                    skills_val.append(t)

    if role_val:
        norm_role = normalize_role_title(role_val)
        reqs.role = ProvenanceValue[str](value=norm_role, source=source)
    
    if skills_val and not clear_skills_requested:
        norm_sk = normalize_skills(skills_val)
        reqs.skills = ProvenanceValue[list](value=norm_sk, source=source)

    # 2. Location Parsing
    GENERIC_NEARBY = {"me", "by me", "near me", "near by", "near by me", "nearby", "my location", "here", "around me", "close to me", "current location"}
    loc_match = re.search(r"\b(?:in|at|near|location|across)\s+([a-z\s]+?)(?=\s+(?:with|under|who|for|having)|$)", text_lower)
    if loc_match:
        loc_str = loc_match.group(1).strip()
        if loc_str and loc_str.lower() in GENERIC_NEARBY:
            user_city = None
            if isinstance(user_location, dict):
                user_city = (user_location.get("city") or user_location.get("state") or "").strip()
            elif isinstance(user_location, str):
                user_city = user_location.strip()
            loc_str = user_city if user_city else None

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

    if not reqs.location:
        is_nearby_intent = any(kw in text_lower for kw in ["near me", "near by", "nearby", "around me", "close to me"])
        if is_nearby_intent and user_location:
            user_city = None
            if isinstance(user_location, dict):
                user_city = (user_location.get("city") or user_location.get("state") or "").strip()
            elif isinstance(user_location, str):
                user_city = user_location.strip()

            if user_city:
                res = resolve_location_from_db(user_city)
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
                            text=res_text or user_city.title(),
                            type=res_type,
                            city_id=res_city_id,
                            state_id=res_state_id,
                            country_id=res_country_id,
                            source=ProvenanceSource.QUERY,
                        )



    # 3. Experience Parsing (Lower Bound, Upper Bound & Range)
    range_match = re.search(r"(?:between\s+)?(\d+(?:\.\d+)?)\s*(?:to|-|and)\s*(\d+(?:\.\d+)?)\s*(?:years?|yrs?|yr)", text_lower)
    max_match = re.search(r"(?:under|less than|at most|up to|maximum|max)\s*(\d+(?:\.\d+)?)\s*(?:years?|yrs?|yr)", text_lower)
    min_match = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?|yr)", text_lower)

    if range_match:
        exp_min = float(range_match.group(1))
        exp_max = float(range_match.group(2))
        reqs.experience = ExperienceRequirement(min=exp_min, max=exp_max, source=source)
    elif max_match:
        exp_max = float(max_match.group(1))
        reqs.experience = ExperienceRequirement(min=None, max=exp_max, source=source)
    elif min_match:
        exp_min = float(min_match.group(1))
        reqs.experience = ExperienceRequirement(min=exp_min, max=None, source=source)

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
        # Detect explicit clear operations in employer query
        clear_loc = bool(re.search(r"\b(?:clear|remove|delete)\s+(?:the\s+)?(?:location|city|state|place)\b", text_lower))
        clear_exp_all = bool(re.search(r"\b(?:clear|remove|delete)\s+(?:the\s+)?experience\s*(?:filter|requirement)?\b", text_lower)) and not ("min" in text_lower or "max" in text_lower or "least" in text_lower or "most" in text_lower)
        clear_exp_min = bool(re.search(r"\b(?:clear|remove|delete)\s+(?:the\s+)?(?:minimum|min)\s+experience\b", text_lower))
        clear_exp_max = bool(re.search(r"\b(?:clear|remove|delete)\s+(?:the\s+)?(?:maximum|max)\s+experience\b", text_lower))
        clear_skills = bool(re.search(r"\b(?:clear|remove|delete)\s+(?:the\s+)?skill[s]?\b", text_lower))
        clear_role = bool(re.search(r"\b(?:clear|remove|delete)\s+(?:the\s+)?(?:role|title|designation)\b", text_lower))

        # Retain or Replace Role
        if clear_role:
            reqs.role = None
        elif existing_requirements.role and existing_requirements.role.value:
            if not reqs.role or not reqs.role.value or reqs.role.value in ("Someone", "Developer"):
                reqs.role = ProvenanceValue[str](
                    value=existing_requirements.role.value,
                    source=ProvenanceSource.EXPLICIT_FOLLOWUP,
                )

        # Retain or Replace Location
        if clear_loc:
            reqs.location = None
        elif reqs.location and existing_requirements.location:
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

        # Retain or Replace Skills
        if clear_skills:
            reqs.skills = None
        elif (not reqs.skills or not reqs.skills.value) and existing_requirements.skills:
            reqs.skills = ProvenanceValue[list](
                value=existing_requirements.skills.value,
                source=ProvenanceSource.EXPLICIT_FOLLOWUP,
            )

        # Retain or merge Experience bounds
        if clear_exp_all:
            reqs.experience = None
        elif clear_exp_min:
            prev_max = existing_requirements.experience.max if existing_requirements.experience else None
            reqs.experience = ExperienceRequirement(min=None, max=prev_max, source=source)
        elif clear_exp_max:
            prev_min = existing_requirements.experience.min if existing_requirements.experience else None
            reqs.experience = ExperienceRequirement(min=prev_min, max=None, source=source)
        elif not reqs.experience and existing_requirements.experience:
            reqs.experience = ExperienceRequirement(
                min=existing_requirements.experience.min,
                max=existing_requirements.experience.max,
                source=ProvenanceSource.EXPLICIT_FOLLOWUP,
            )
        elif reqs.experience and existing_requirements.experience:
            merged_min = reqs.experience.min if reqs.experience.min is not None else existing_requirements.experience.min
            merged_max = reqs.experience.max if reqs.experience.max is not None else existing_requirements.experience.max
            if merged_min is not None and merged_max is not None and merged_min > merged_max:
                merged_max = None
            reqs.experience = ExperienceRequirement(
                min=merged_min,
                max=merged_max,
                source=reqs.experience.source or ProvenanceSource.EXPLICIT_FOLLOWUP,
            )

    return reqs
