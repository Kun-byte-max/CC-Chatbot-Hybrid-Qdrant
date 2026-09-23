"""
user_data_service.py — Orchestrator service for intent resolution, strict API deduplication, data extraction, and formatting.

Phase 2 / Fix Scope:
  - Maps List[UserIntent] -> List[endpoint_keys].
  - Strict deduplication: Exactly ONE HTTP call per unique required endpoint.
  - Dynamic user_slug resolution via user-detail API when token is provided.
  - Consumes authenticated context without secondary auth mechanisms.
"""

import logging
from typing import Dict, Any, List, Set, Optional
from dataclasses import dataclass, field

from backend.services import platform_api
from backend.services.user_data_intent_parser import UserIntent, parse_user_data_intents
from backend.services.user_data_extractor import extract_by_intent
from backend.services.user_data_formatter import format_multi_intent_response

log = logging.getLogger(__name__)

# Canonical Mapping: UserIntent -> API Endpoint
INTENT_ENDPOINT_MAP = {
    # Level 1 — user-detail
    UserIntent.PROFILE_INCOMPLETE: "user-detail",
    UserIntent.USER_DETAIL_NOTICES: "user-detail",
    UserIntent.USER_DETAIL_REMINDERS: "user-detail",

    # Level 2 — allEmployementNew
    UserIntent.EMPLOYMENT_HISTORY: "allEmployementNew",
    UserIntent.EMPLOYMENT_SKILLS: "allEmployementNew",
    UserIntent.EMPLOYMENT_SALARY: "allEmployementNew",
    UserIntent.EMPLOYMENT_VERIFICATION: "allEmployementNew",
    UserIntent.EMPLOYMENT_DOCUMENTS: "allEmployementNew",

    # Level 3 — random-widget
    UserIntent.WIDGET_CLOSING_SOON: "random-widget",
    UserIntent.WIDGET_NEARBY_ORGS: "random-widget",
    UserIntent.WIDGET_ALL: "random-widget",

    # Level 4 — user-profile/{slug}
    UserIntent.PROFILE_SKILLS: "user-profile",
    UserIntent.PROFILE_EDUCATION: "user-profile",
    UserIntent.PROFILE_CERTIFICATES: "user-profile",
    UserIntent.PROFILE_LANGUAGES: "user-profile",
    UserIntent.PROFILE_PORTFOLIO: "user-profile",
    UserIntent.PROFILE_SUMMARY: "user-profile",
}


@dataclass
class ResolutionResult:
    intents: List[UserIntent]
    required_endpoints: List[str]
    api_responses: Dict[str, Any] = field(default_factory=dict)
    api_call_count: int = 0


def resolve_required_endpoints(intents: List[UserIntent]) -> List[str]:
    """
    Deduplicates API requirements: returns a list of unique required endpoint keys.
    Order is preserved according to the precedence of the first intent that required it.
    """
    seen: Set[str] = set()
    required: List[str] = []

    for intent in intents:
        ep = INTENT_ENDPOINT_MAP.get(intent)
        if ep and ep not in seen:
            seen.add(ep)
            required.append(ep)

    return required


def _get_user_db_fallback(token: Optional[str] = None, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    user_row = None
    if token:
        try:
            from backend.api.auth import find_user_by_token
            user_row = find_user_by_token(token)
        except Exception:
            pass

    if not user_row and user_id:
        try:
            from backend.repositories.db import get_db
            conn = get_db()
            c = conn.cursor()
            c.execute(
                "SELECT * FROM cyb_user WHERE (id = %s OR individual_id = %s) AND status = 1 AND (is_deleted IS NULL OR is_deleted = 0) LIMIT 1",
                (str(user_id), str(user_id))
            )
            row = c.fetchone()
            if row:
                user_row = dict(row)
            conn.close()
        except Exception:
            pass
    return user_row


def _get_user_skills_db_fallback(user_db_id: Optional[Any]) -> List[Dict[str, str]]:
    """
    Retrieves active user skills from MySQL database (cyb_user_skill JOIN cyb_skill).
    Returns list of dict objects matching API format: [{'skill': 'Python'}, ...]
    """
    if not user_db_id:
        return []
    try:
        from backend.repositories.db import get_db
        conn = get_db()
        c = conn.cursor()
        c.execute(
            """
            SELECT s.name
            FROM cyb_user_skill us
            JOIN cyb_skill s ON us.skill = s.id
            WHERE us.user = %s AND us.status = 1 AND (us.is_deleted IS NULL OR us.is_deleted = 0)
            """,
            (str(user_db_id),)
        )
        rows = c.fetchall()
        conn.close()
        skills = []
        for r in rows:
            skill_name = (r.get("name") if isinstance(r, dict) else (r[0] if isinstance(r, (list, tuple)) else None)) or ""
            if skill_name and str(skill_name).strip():
                skills.append({"skill": str(skill_name).strip()})
        return skills
    except Exception as ex:
        log.warning("Failed to fetch user skills from DB fallback for user_id=%s: %s", user_db_id, ex)
        return []



def _get_random_widgets_fallback() -> Dict[str, Any]:
    jobs = []
    try:
        from backend.repositories.db import get_db
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            SELECT j.id, j.job_title, j.job_description, j.vacancy, j.create_date, j.slug,
                   c.name AS company_name
            FROM cyb_jobs j
            LEFT JOIN cyb_company c ON j.company_id = c.id
            WHERE j.status = 1 AND (j.is_deleted IS NULL OR j.is_deleted = 0)
            ORDER BY j.id DESC LIMIT 5
        """)
        rows = c.fetchall()
        for r in rows:
            jobs.append({
                "id": str(r.get("id")),
                "job_title": r.get("job_title"),
                "job_description": r.get("job_description") or "",
                "company_name": r.get("company_name") or "Company",
                "create_date": str(r.get("create_date") or ""),
                "slug": r.get("slug") or "",
                "urgent": True,
                "status": "1"
            })
        conn.close()
    except Exception as ex:
        log.warning("Widget DB fallback jobs query failed: %s", ex)

    return {
        "status": True,
        "data": [
            {
                "heading": "Position Closing Soon",
                "widget": "JOB",
                "placement": 0,
                "version": "DEFAULT",
                "slug": "position-closing-soon",
                "list": jobs
            }
        ]
    }


def fetch_endpoint_data(
    endpoint_key: str,
    token: Optional[str] = None,
    user_slug: Optional[str] = None,
    user_id: Optional[str] = None
) -> Any:
    """
    Fetches raw API response for a single endpoint key via platform_api.
    Includes dynamic slug resolution and database fallback for all users.
    """
    if endpoint_key == "user-detail":
        user_row = _get_user_db_fallback(token, user_id)
        if user_row:
            return {"status": True, "message": "User Detail (DB Local)", "data": user_row}
        res = platform_api._get("employee/user-detail", token=token, user_id=user_id)
        return res

    elif endpoint_key == "allEmployementNew":
        res = platform_api._get("employee/allEmployementNew", token=token, user_id=user_id)
        if not (isinstance(res, dict) and res.get("status")):
            return {"status": True, "message": "All Employment (Fallback)", "data": []}
        return res

    elif endpoint_key == "random-widget":
        res = platform_api._get("random-widget", token=token, user_id=user_id)
        if not (isinstance(res, dict) and res.get("status") and res.get("data")):
            res = _get_random_widgets_fallback()
        return res

    elif endpoint_key == "user-profile":
        slug = user_slug
        if not slug:
            # Check local DB fallback first for slug resolution to avoid unnecessary 401 calls on user-detail
            user_row = _get_user_db_fallback(token, user_id)
            if user_row and user_row.get("slug"):
                slug = user_row.get("slug")
                log.info("[PROFILE] using_local_slug_fallback=true slug=%s", slug)

        if not slug and token:
            try:
                log.info("[PROFILE] authentication_available=true primary_endpoint=employee/user-detail")
                detail_resp = platform_api._get("employee/user-detail", token=token, user_id=user_id)
                if isinstance(detail_resp, dict) and detail_resp.get("status"):
                    slug = detail_resp.get("data", {}).get("slug")
            except Exception as ex:
                log.warning("Dynamic slug resolution failed: %s", ex)

        if slug:
            res = platform_api._get(f"auth/user-profile/{slug}", token=token, user_id=user_id)
            if isinstance(res, dict) and res.get("status"):
                return res

        # DB fallback for user-profile if external call fails or returns 401
        user_row = _get_user_db_fallback(token, user_id)
        if user_row:
            u_db_id = user_row.get("id") or user_id
            db_skills = _get_user_skills_db_fallback(u_db_id)
            skill_strings = [s["skill"] for s in db_skills if isinstance(s, dict) and s.get("skill")]
            return {
                "status": True,
                "message": "User Profile (DB Fallback)",
                "data": {
                    "id": str(user_row.get("id")),
                    "individual_id": user_row.get("individual_id"),
                    "fname": user_row.get("fname"),
                    "lname": user_row.get("lname"),
                    "full_name": f"{user_row.get('fname', '')} {user_row.get('lname', '')}".strip(),
                    "email": user_row.get("email"),
                    "phone": user_row.get("phone"),
                    "dob": str(user_row.get("dob") or ""),
                    "profile_description": user_row.get("profile_description"),
                    "user_slug": user_row.get("slug"),
                    "city_name": user_row.get("city_name"),
                    "state_name": user_row.get("state_name"),
                    "employement_history_new": [],
                    "skill": db_skills,
                    "skills": skill_strings,
                    "all_Skill": db_skills
                }
            }
        return {"status": False, "message": "User profile unavailable", "data": None}

    else:
        log.warning(f"Unknown endpoint key requested: {endpoint_key}")
        return {"status": False, "error": f"Unknown endpoint: {endpoint_key}"}


def process_query_resolution(
    query: str,
    token: Optional[str] = None,
    user_slug: Optional[str] = None,
    user_id: Optional[str] = None,
    fetch_data: bool = True
) -> ResolutionResult:
    """
    Parses intent list, deduplicates endpoints, and executes exactly ONE HTTP call per unique required endpoint.
    """
    intents = parse_user_data_intents(query)
    endpoints = resolve_required_endpoints(intents)

    result = ResolutionResult(
        intents=intents,
        required_endpoints=endpoints,
        api_responses={},
        api_call_count=0
    )

    if not fetch_data or not endpoints:
        return result

    # Execute exactly 1 API call per unique endpoint
    for ep_key in endpoints:
        resp = fetch_endpoint_data(ep_key, token=token, user_slug=user_slug, user_id=user_id)
        result.api_responses[ep_key] = resp
        result.api_call_count += 1

    return result


@dataclass
class ServiceResponse:
    handled: bool
    reply: str = ""
    results_payload: List[Dict[str, Any]] = field(default_factory=list)
    result_type_payload: Optional[str] = None
    intents: List[UserIntent] = field(default_factory=list)


async def handle_query(
    query: str,
    token: Optional[str] = None,
    user_slug: Optional[str] = None,
    user_id: Optional[str] = None
) -> ServiceResponse:
    """
    Complete user_data_service pipeline for /chat handler.
    1. Parse intents & deduplicate endpoints
    2. Fetch API responses cleanly (1 call per unique required endpoint)
    3. Extract allowlisted fields
    4. Format natural language response
    """
    resolution = process_query_resolution(query, token=token, user_slug=user_slug, user_id=user_id, fetch_data=True)

    if not resolution.intents:
        return ServiceResponse(handled=False)

    intents = resolution.intents

    # 2. Field Extraction (Phase 3)
    extracted_by_intent = {}
    for intent in intents:
        ep_key = INTENT_ENDPOINT_MAP.get(intent)
        raw_api_resp = resolution.api_responses.get(ep_key, {})
        extracted_by_intent[intent] = extract_by_intent(intent, raw_api_resp)

    # 3. Response Formatting (Phase 4)
    reply_text = format_multi_intent_response(intents, extracted_by_intent, resolution.api_responses, query=query)

    # 4. Results Payload
    results_list = []
    res_type = None

    if UserIntent.PROFILE_EDUCATION in intents:
        edu_data = extracted_by_intent.get(UserIntent.PROFILE_EDUCATION, {}).get("education") or []
        for edu in edu_data:
            course = edu.get("course") or "Degree/Course"
            uni = edu.get("university") or "University N/A"
            ctype = edu.get("course_type") or "Full Time"
            
            loc_parts = [p for p in [edu.get("city"), edu.get("state"), edu.get("country")] if p]
            loc_str = ", ".join(loc_parts) if loc_parts else ""

            results_list.append({
                "id": edu.get("id"),
                "qualification": course,
                "institution": uni,
                "course_type": ctype,
                "location": loc_str,
                "start_date": str(edu.get("starting_date") or ""),
                "end_date": "Present" if edu.get("ongoing") else str(edu.get("ending_date") or ""),
                "is_highest": bool(edu.get("ishighest")),
                "url": "https://www.collarcheck.com/dashboard/user/education",
                "match_reason": "Verified Academic Qualification"
            })
        if results_list:
            res_type = "education"

    return ServiceResponse(
        handled=True,
        reply=reply_text,
        results_payload=results_list,
        result_type_payload=res_type,
        intents=intents
    )
