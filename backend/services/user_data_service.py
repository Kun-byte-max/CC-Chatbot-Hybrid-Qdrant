"""
user_data_service.py — Orchestrator service for intent resolution, strict API deduplication, data extraction, and formatting.

Phase 4:
  - User-scoped local DB fallbacks strictly bound to authenticated cyb_user.id.
  - Verification semantics normalization (is_verified = True iff approved='1' and status='1').
  - Safe error propagation (never masks DB errors as empty 200 OK).
"""

import logging
from typing import Dict, Any, List, Set, Optional, Union
from dataclasses import dataclass, field

from backend.services import platform_api
from backend.services.user_data_intent_parser import UserIntent, parse_user_data_intents, parse_user_data_intents_async, detect_requested_level
from backend.services.user_data_extractor import extract_by_intent, is_current_role
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
    UserIntent.EMPLOYMENT_COMPANY_CLAIMED: "allEmployementNew",
    UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS: "allEmployementNew",
    UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS: "allEmployementNew",
    UserIntent.EMPLOYMENT_VERIFICATION_WHY_INCOMPLETE: "allEmployementNew",
    UserIntent.EMPLOYMENT_VERIFICATION_OWNERSHIP: "allEmployementNew",
    UserIntent.EMPLOYMENT_DOCUMENTS: "allEmployementNew",

    # Level 3 — random-widget
    UserIntent.WIDGET_CLOSING_SOON: "random-widget",
    UserIntent.WIDGET_NEARBY_ORGS: "random-widget",
    UserIntent.WIDGET_ALL: "random-widget",

    # Level 4 — user-profile/{slug} & allEmployementNew
    UserIntent.PROFILE_SKILLS: ("user-profile", "allEmployementNew"),
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
    requested_level: Optional[int] = None


def resolve_required_endpoints(intents: List[UserIntent]) -> List[str]:
    seen: Set[str] = set()
    required: List[str] = []

    for intent in intents:
        ep = INTENT_ENDPOINT_MAP.get(intent)
        if not ep:
            continue
        eps = [ep] if isinstance(ep, str) else list(ep)
        for e in eps:
            if e and e not in seen:
                seen.add(e)
                required.append(e)

    return required


def _parse_positive_int_id(val: Any) -> Optional[int]:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val if val > 0 else None
    if isinstance(val, float):
        return None
    if isinstance(val, str):
        val_str = val.strip()
        if val_str.isdigit():
            parsed = int(val_str)
            return parsed if parsed > 0 else None
    return None


def _clean_str(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s if s != "" else None


def _get_user_db_fallback(user_id: Optional[Union[str, int]] = None) -> Optional[Dict[str, Any]]:
    """
    Retrieves cyb_user row strictly by authenticated canonical integer primary key (user_id).
    Rejects boolean, None, zero, negative, or invalid user_id values.
    """
    valid_uid = _parse_positive_int_id(user_id)
    if valid_uid is None:
        return None

    conn = None
    c = None
    try:
        from backend.repositories.db import get_db
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "SELECT * FROM cyb_user WHERE id = %s AND status = 1 AND (is_deleted IS NULL OR is_deleted = 0) LIMIT 1",
            (valid_uid,)
        )
        row = c.fetchone()
        if row:
            return dict(row)
        return None
    except Exception as ex:
        log.warning("Database exception during _get_user_db_fallback: %s", ex)
        raise RuntimeError("Database error during user profile fallback query") from ex
    finally:
        if c:
            try:
                c.close()
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _get_user_skills_db_fallback(user_id: Optional[Union[str, int]]) -> List[Dict[str, str]]:
    valid_uid = _parse_positive_int_id(user_id)
    if valid_uid is None:
        return []

    conn = None
    c = None
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
            (valid_uid,)
        )
        rows = c.fetchall()
        skills = []
        for r in rows:
            skill_name = (r.get("name") if isinstance(r, dict) else (r[0] if isinstance(r, (list, tuple)) else None)) or ""
            if skill_name and str(skill_name).strip():
                skills.append({"skill": str(skill_name).strip()})
        return skills
    except Exception as ex:
        log.warning("Failed to fetch user skills from DB fallback for user_id=%s: %s", valid_uid, ex)
        raise RuntimeError("Database error during skills fallback query") from ex
    finally:
        if c:
            try:
                c.close()
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _get_employment_db_fallback(user_id: Optional[Union[str, int]]) -> Dict[str, Any]:
    """
    Queries cyb_user_experience for authenticated user_id and transforms rows
    into payload fully compatible with allEmployementNew contract.
    Includes both role-level and company-level is_verified calculations.
    Raises RuntimeError on database failures (never masks DB errors).
    """
    valid_uid = _parse_positive_int_id(user_id)
    if valid_uid is None:
        raise ValueError("Invalid positive integer user_id required for employment DB fallback.")

    conn = None
    c = None
    try:
        from backend.repositories.db import get_db
        conn = get_db()
        c = conn.cursor()
        c.execute(
            """
            SELECT e.id, e.company AS company_id, e.company_name, e.designation, e.joining_date,
                   e.worked_till_date, e.still_working, e.salary, e.salary_inhand,
                   e.salary_mode, e.approved, e.status,
                   c.name AS company_label
            FROM cyb_user_experience e
            LEFT JOIN cyb_company c ON e.company = c.id
            WHERE e.user = %s AND (e.is_deleted IS NULL OR e.is_deleted = 0)
            ORDER BY e.joining_date DESC
            """,
            (valid_uid,)
        )
        rows = c.fetchall()

        companies_map = {}
        for r in rows:
            c_name = _clean_str(r.get("company_label")) or _clean_str(r.get("company_name")) or "Company N/A"
            comp_id_val = _clean_str(r.get("company_id"))
            c_key = comp_id_val if comp_id_val else f"unassigned_{r['id']}"

            raw_approved = _clean_str(r.get("approved"))
            raw_status = _clean_str(r.get("status"))

            # Strict verification logic: verified iff approved == '1' AND status == '1'
            role_verified = (raw_approved in ("1", 1) and raw_status in ("1", 1))

            if c_key not in companies_map:
                companies_map[c_key] = {
                    "company": c_name,
                    "company_id": comp_id_val or "",
                    "is_verified": None,
                    "company_claim_status": None,
                    "lists": []
                }

            companies_map[c_key]["lists"].append({
                "id": str(r["id"]),
                "designation": _clean_str(r.get("designation")) or "Role N/A",
                "joining_date": str(r.get("joining_date") or ""),
                "worked_till_date": str(r.get("worked_till_date") or ""),
                "still_working": str(r.get("still_working") or "0"),
                "salary": r.get("salary"),
                "salary_inhand": r.get("salary_inhand"),
                "salary_mode": r.get("salary_mode"),
                "approved": raw_approved or "0",
                "status": raw_status or "0",
                "is_verified": role_verified,
                "verificationProcess": {},
                "skill": [],
                "have_salary": None,
                "have_document": None,
                "have_review": None,
                "claim_status": None,
                "employment_status": None,
                "employment_type": None,
                "department": None,
                "bonus": None,
                "document": None
            })

        return {
            "status": True,
            "message": "All Employment (DB Fallback)",
            "data": list(companies_map.values())
        }
    except Exception as ex:
        log.warning("Employment DB fallback query failed due to database infrastructure error.")
        raise RuntimeError("Database error during employment fallback execution") from ex
    finally:
        if c:
            try:
                c.close()
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _get_random_widgets_fallback() -> Dict[str, Any]:
    jobs = []
    conn = None
    c = None
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
    except Exception as ex:
        log.warning("Widget DB fallback jobs query failed: %s", ex)
        raise RuntimeError("Database error during widget fallback query") from ex
    finally:
        if c:
            try:
                c.close()
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass

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


def _is_auth_refusal(res: Any) -> bool:
    """
    Evaluates whether an API response dictionary represents an explicit platform authorization refusal.
    Precedence:
      1. Structured HTTP status code 401 or 403.
      2. Recognised authorization refusal keywords in error or message strings.
    Prevents triggering local DB fallbacks when authorization is explicitly denied.
    """
    if not isinstance(res, dict):
        return False

    status_code = res.get("status_code")
    if status_code in (401, 403):
        return True

    msg = str(res.get("message") or "").lower()
    err = str(res.get("error") or "").lower()
    combined_text = f"{msg} {err}".strip()

    if not combined_text:
        return False

    refusal_phrases = (
        "unauthorized", "forbidden", "authentication required", "no authenticated user token",
        "access denied", "invalid token", "token expired", "unauthorized access",
        "client error '401", "client error '403", "401 unauthorized", "403 forbidden"
    )
    if any(phrase in combined_text for phrase in refusal_phrases):
        return True

    return False


def fetch_endpoint_data(
    endpoint_key: str,
    token: Optional[str] = None,
    user_slug: Optional[str] = None,
    user_id: Optional[Union[str, int]] = None
) -> Any:
    """
    Fetches raw API response for a single endpoint key via platform_api.
    Falls back to user-scoped local DB queries ONLY for network/500 errors on authenticated users.
    Platform 401/403 responses return status False immediately without local DB fallback.
    """
    valid_uid = _parse_positive_int_id(user_id)

    if endpoint_key == "user-detail":
        res = platform_api._get("employee/user-detail", token=token, user_id=user_id)
        if isinstance(res, dict) and res.get("status"):
            return res
        if valid_uid is not None and not _is_auth_refusal(res):
            user_row = _get_user_db_fallback(user_id=valid_uid)
            if user_row:
                return {"status": True, "message": "User Detail (DB Local)", "data": user_row}
        return res

    elif endpoint_key == "allEmployementNew":
        res = platform_api._get("employee/allEmployementNew", token=token, user_id=user_id)
        if isinstance(res, dict) and res.get("status"):
            return res
        # Fallback to local DB employment query ONLY if user is authenticated and response is not an authorization refusal
        if valid_uid is not None and not _is_auth_refusal(res):
            try:
                return _get_employment_db_fallback(valid_uid)
            except Exception as ex:
                log.warning("Employment DB fallback failed: %s", ex)
        return {"status": False, "message": "All Employment unavailable", "data": []}

    elif endpoint_key == "random-widget":
        res = platform_api._get("random-widget", token=token, user_id=user_id)
        if not (isinstance(res, dict) and res.get("status") and res.get("data")):
            res = _get_random_widgets_fallback()
        return res

    elif endpoint_key == "user-profile":
        slug = user_slug
        user_row = None
        if not slug and valid_uid is not None:
            user_row = _get_user_db_fallback(user_id=valid_uid)
            if user_row and user_row.get("slug"):
                slug = user_row.get("slug")

        if not slug:
            detail_res = platform_api._get("employee/user-detail", token=token, user_id=user_id)
            if isinstance(detail_res, dict) and detail_res.get("status") and isinstance(detail_res.get("data"), dict):
                slug = detail_res["data"].get("slug")

        res = None
        if slug:
            res = platform_api._get(f"auth/user-profile/{slug}", token=token, user_id=user_id)
            if isinstance(res, dict) and res.get("status"):
                return res

        # Fallback to local DB user profile ONLY if user is authenticated and response is not an authorization refusal
        if valid_uid is not None and not _is_auth_refusal(res):
            if user_row is None:
                user_row = _get_user_db_fallback(user_id=valid_uid)
            if user_row:
                db_skills = _get_user_skills_db_fallback(valid_uid)
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


async def process_query_resolution_async(
    query: str,
    token: Optional[str] = None,
    user_slug: Optional[str] = None,
    user_id: Optional[Union[str, int]] = None,
    fetch_data: bool = True
) -> ResolutionResult:
    intents = await parse_user_data_intents_async(query)
    endpoints = resolve_required_endpoints(intents)
    req_level = detect_requested_level(query)

    result = ResolutionResult(
        intents=intents,
        required_endpoints=endpoints,
        api_responses={},
        api_call_count=0,
        requested_level=req_level
    )

    if not fetch_data or not endpoints:
        return result

    for ep_key in endpoints:
        resp = fetch_endpoint_data(ep_key, token=token, user_slug=user_slug, user_id=user_id)
        result.api_responses[ep_key] = resp
        result.api_call_count += 1

    return result


def process_query_resolution(
    query: str,
    token: Optional[str] = None,
    user_slug: Optional[str] = None,
    user_id: Optional[Union[str, int]] = None,
    fetch_data: bool = True
) -> ResolutionResult:
    intents = parse_user_data_intents(query)
    endpoints = resolve_required_endpoints(intents)
    req_level = detect_requested_level(query)

    result = ResolutionResult(
        intents=intents,
        required_endpoints=endpoints,
        api_responses={},
        api_call_count=0,
        requested_level=req_level
    )

    if not fetch_data or not endpoints:
        return result

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
    presentation_mode: Optional[str] = None
    intents: List[UserIntent] = field(default_factory=list)
    action_metadata: Optional[List[Dict[str, Any]]] = None
    selection_metadata: Optional[Dict[str, Any]] = None
    requested_level: Optional[int] = None


async def handle_query(
    query: str,
    token: Optional[str] = None,
    user_slug: Optional[str] = None,
    user_id: Optional[Union[str, int]] = None,
    session_id: Optional[str] = None
) -> ServiceResponse:
    resolution = await process_query_resolution_async(query, token=token, user_slug=user_slug, user_id=user_id, fetch_data=True)

    from backend.utils.session_state import (
        set_pending_requested_level,
        get_pending_requested_level,
        clear_pending_requested_level,
        get_selected_employment_context,
        get_session_option_mapping
    )

    explicit_lvl = detect_requested_level(query)
    if explicit_lvl is not None:
        effective_requested_level = explicit_lvl
        if session_id:
            set_pending_requested_level(session_id, explicit_lvl)
    elif session_id:
        effective_requested_level = get_pending_requested_level(session_id)
    else:
        effective_requested_level = None

    resolution.requested_level = effective_requested_level

    if effective_requested_level is not None:
        if UserIntent.EMPLOYMENT_VERIFICATION not in resolution.intents:
            resolution.intents.insert(0, UserIntent.EMPLOYMENT_VERIFICATION)

    if not resolution.intents:
        if session_id and (effective_requested_level is not None or get_selected_employment_context(session_id) or get_session_option_mapping(session_id)):
            resolution.intents = [UserIntent.EMPLOYMENT_VERIFICATION]
            ep_key = INTENT_ENDPOINT_MAP[UserIntent.EMPLOYMENT_VERIFICATION]
            if ep_key not in resolution.api_responses:
                resolution.api_responses[ep_key] = fetch_endpoint_data(ep_key, token=token, user_slug=user_slug, user_id=user_id)
        else:
            return ServiceResponse(handled=False)

    intents = resolution.intents

    extracted_by_intent = {}
    for intent in intents:
        ep_key = INTENT_ENDPOINT_MAP.get(intent)
        if ep_key:
            eps = [ep_key] if isinstance(ep_key, str) else list(ep_key)
            for e in eps:
                if e not in resolution.api_responses:
                    resolution.api_responses[e] = fetch_endpoint_data(e, token=token, user_slug=user_slug, user_id=user_id)

        if intent == UserIntent.PROFILE_SKILLS:
            prof_resp = resolution.api_responses.get("user-profile")
            emp_resp = resolution.api_responses.get("allEmployementNew")
            try:
                from backend.services.user_skill_aggregator import aggregate_user_skills
            except ModuleNotFoundError:
                from services.user_skill_aggregator import aggregate_user_skills  # type: ignore

            agg_res = aggregate_user_skills(
                user_profile_data=prof_resp if isinstance(prof_resp, dict) else None,
                employment_data=emp_resp if isinstance(emp_resp, dict) else None,
                profile_status="FAILED" if prof_resp and not prof_resp.get("status") else None,
                employment_status="FAILED" if emp_resp and not (isinstance(emp_resp, dict) and emp_resp.get("status")) else None,
            )
            extracted_by_intent[intent] = {
                "effective_skills": agg_res.effective_skills,
                "provenance": agg_res.provenance,
                "profile_skills": agg_res.profile_skills,
                "employment_skills": agg_res.employment_skills,
                "profile_status": agg_res.profile_status,
                "employment_status": agg_res.employment_status,
            }
        else:
            primary_ep = ep_key[0] if isinstance(ep_key, (list, tuple)) else ep_key
            raw_api_resp = resolution.api_responses.get(primary_ep, {}) if primary_ep else {}
            extracted_by_intent[intent] = extract_by_intent(intent, raw_api_resp)

    res_tuple = format_multi_intent_response(
        intents,
        extracted_by_intent,
        resolution.api_responses,
        query=query,
        session_id=session_id,
        return_metadata=True,
        requested_level=resolution.requested_level
    )
    if isinstance(res_tuple, tuple):
        reply_text, selection_metadata = res_tuple
    else:
        reply_text = res_tuple
        selection_metadata = None

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

    emp_intents = {
        UserIntent.EMPLOYMENT_HISTORY,
        UserIntent.EMPLOYMENT_VERIFICATION,
        UserIntent.EMPLOYMENT_COMPANY_CLAIMED,
        UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS,
        UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS,
        UserIntent.EMPLOYMENT_VERIFICATION_WHY_INCOMPLETE,
        UserIntent.EMPLOYMENT_VERIFICATION_OWNERSHIP,
    }
    if any(intent in intents for intent in emp_intents):
        hist_data = {}
        for intent_key in intents:
            if intent_key in extracted_by_intent and isinstance(extracted_by_intent[intent_key], dict):
                if "employment_history" in extracted_by_intent[intent_key]:
                    hist_data = extracted_by_intent[intent_key]
                    break
        history = hist_data.get("employment_history") if isinstance(hist_data, dict) else []
        if not isinstance(history, list):
            history = []

        from backend.services.user_data_formatter import resolve_employment_selection_context
        filtered_history, sel_meta, matched_comp = resolve_employment_selection_context(
            query, history, session_id=session_id, consume_key=True, requested_level=resolution.requested_level
        )
        
        # Determine company records to process for results[] payload:
        # Contract Invariant: If selection_metadata is present (selection required & unresolved), results[] MUST be empty []
        if sel_meta:
            records_to_process = []
        elif filtered_history:
            records_to_process = filtered_history
        elif matched_comp:
            records_to_process = []
        else:
            records_to_process = history


        for comp in records_to_process:
            if not isinstance(comp, dict):
                continue
            comp_name = comp.get("company") or "Company N/A"
            comp_id = comp.get("company_id")
            comp_logo = comp.get("company_logo") or None
            comp_is_verified = comp.get("is_verified")
            roles = comp.get("roles") if isinstance(comp.get("roles"), list) else []

            for r in roles:
                if not isinstance(r, dict):
                    continue
                verif_dict = r.get("verification") if isinstance(r.get("verification"), dict) else {}
                is_verif = verif_dict.get("is_verified", False)
                level_label = verif_dict.get("level_label")
                status_label = verif_dict.get("status_label") or ("Verified" if is_verif else "Not Verified")

                match_reason = f"Verified ({level_label})" if (is_verif and level_label) else ("Verified Employment Record" if is_verif else "Employment Record")
                desig_val = r.get("designation") or r.get("position") or r.get("title") or "Role / Designation"

                # Normalize employment_status ("current" vs "previous")
                if is_current_role(r):
                    normalized_emp_status = "current"
                    is_still_working = True
                else:
                    normalized_emp_status = "previous"
                    is_still_working = False

                # Extract verification levels breakdown cleanly preserving source flags
                breakdown = verif_dict.get("levels_breakdown") or {}
                vp_raw = r.get("verificationProcess") if isinstance(r.get("verificationProcess"), dict) else {}
                levels_map = {
                    "level1": bool(vp_raw.get("level1")) if "level1" in vp_raw else bool((breakdown.get("level1") or {}).get("completed")),
                    "level2": bool(vp_raw.get("level2")) if "level2" in vp_raw else bool((breakdown.get("level2") or {}).get("completed")),
                    "level3": bool(vp_raw.get("level3")) if "level3" in vp_raw else bool((breakdown.get("level3") or {}).get("completed")),
                    "level4": bool(vp_raw.get("level4")) if "level4" in vp_raw else bool((breakdown.get("level4") or {}).get("completed"))
                }

                # Normalize skills array
                raw_skills = r.get("skill") or []
                clean_skills = [
                    {"id": str(s.get("id")), "name": str(s.get("name"))}
                    for s in raw_skills if isinstance(s, dict) and s.get("name")
                ]

                # Evidence flags (salary, document, review)
                have_salary = r.get("have_salary") if r.get("have_salary") is not None else bool(r.get("salary"))
                have_doc = r.get("have_document") if r.get("have_document") is not None else bool(r.get("document"))
                have_rev = r.get("have_review") if r.get("have_review") is not None else False

                results_list.append({
                    "id": str(r.get("id")) if r.get("id") is not None else None,
                    "company": comp_name,
                    "company_name": comp_name,
                    "company_logo": comp_logo,
                    "company_id": comp_id,
                    "company_verification": {
                        "is_verified": bool(comp_is_verified) if comp_is_verified is not None else None,
                        "claim_status": comp.get("company_claim_status") if comp.get("company_claim_status") is not None else r.get("claim_status")
                    },
                    "company_is_verified": comp_is_verified,
                    "designation": desig_val,
                    "current_designation": desig_val,
                    "position": desig_val,
                    "employment_type": r.get("employment_type") or None,
                    "employment_status": normalized_emp_status,
                    "joining_date": r.get("joining_date"),
                    "worked_till_date": r.get("worked_till_date") or ("Present" if is_still_working else ""),
                    "still_working": is_still_working,
                    "verification": {
                        "status": verif_dict.get("status", "unknown"),
                        "status_label": status_label,
                        "highest_level": verif_dict.get("level"),
                        "highest_level_label": level_label,
                        "levels": levels_map,
                        "next_level": verif_dict.get("next_level"),
                        "next_action": verif_dict.get("next_action"),
                        "semantic_contract": verif_dict.get("semantic_contract")
                    },
                    "skills": clean_skills,
                    "evidence": {
                        "salary": have_salary,
                        "document": have_doc,
                        "review": have_rev
                    },
                    "department": r.get("department") or None,
                    "approved": r.get("approved"),
                    "status": r.get("status"),
                    "verificationProcess": r.get("verificationProcess") or {},
                    "url": "https://www.collarcheck.com/dashboard/user/experience",
                    "match_reason": match_reason
                })

        if any(intent in intents for intent in emp_intents):
            if res_type is None:
                if effective_requested_level is not None or any(i in intents for i in (UserIntent.EMPLOYMENT_VERIFICATION, UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS, UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS, UserIntent.EMPLOYMENT_VERIFICATION_WHY_INCOMPLETE, UserIntent.EMPLOYMENT_VERIFICATION_OWNERSHIP)):
                    res_type = "employment_verification"
                else:
                    res_type = "employment"

    # Presentation mode resolution (Phase 2.5G/K/O)
    presentation_mode = None
    if res_type in ("employment_verification", "employment"):
        if effective_requested_level is not None or resolution.requested_level or detect_requested_level(query):
            presentation_mode = "FOCUSED_EMPLOYMENT_VERIFICATION"
            res_type = "employment_verification"
        elif res_type == "employment_verification":
            presentation_mode = "GENERIC_EMPLOYMENT_VERIFICATION"


    action_metadata = []
    seen_action_ids = set()
    for intent in intents:
        if intent == UserIntent.EMPLOYMENT_ACTION_ADD:
            if "employment_add" not in seen_action_ids:
                seen_action_ids.add("employment_add")
                action_metadata.append({
                    "id": "employment_add",
                    "label": "Add Employment",
                    "type": "nav_link",
                    "destination": "experience_dashboard"
                })
        elif intent == UserIntent.EMPLOYMENT_ACTION_INVITE_EMPLOYER:
            if "employer_verification" not in seen_action_ids:
                seen_action_ids.add("employer_verification")
                action_metadata.append({
                    "id": "employer_verification",
                    "label": "Verify Employment",
                    "type": "nav_link",
                    "destination": "experience_dashboard"
                })
        elif intent == UserIntent.EMPLOYMENT_ACTION_UPLOAD_SALARY:
            if "salary_verification" not in seen_action_ids:
                seen_action_ids.add("salary_verification")
                action_metadata.append({
                    "id": "salary_verification",
                    "label": "Complete Salary Verification",
                    "type": "chat_action",
                    "message": "How do I complete Level 4 salary verification?"
                })

    if session_id and selection_metadata is None and effective_requested_level is not None and len(results_list) == 1:
        clear_pending_requested_level(session_id)

    return ServiceResponse(
        handled=True,
        reply=reply_text,
        results_payload=results_list,
        result_type_payload=res_type,
        presentation_mode=presentation_mode,
        intents=intents,
        action_metadata=action_metadata if action_metadata else None,
        selection_metadata=selection_metadata,
        requested_level=resolution.requested_level
    )


