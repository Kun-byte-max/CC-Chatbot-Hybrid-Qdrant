from typing import Optional, Any, List, Dict, Set
import logging
import asyncio
import re
import math

from starlette.concurrency import run_in_threadpool

try:
    from backend.utils.utils import is_employer_hiring_query
    from backend.services.ranking_service import get_employer_session
    from backend.services.search_client import search_jobs
    from backend.services.search_parser import parse_search_query, resolve_state_id, is_self_profile_advice_query
    from backend.services.location_service import resolve_location_from_db, LocationResult
    from backend.services.platform_api import get_closing_soon_jobs, get_nearby_organizations
    from backend.config.config import MAX_RESULTS, JOB_MATCH_ENABLED, HYBRID_SEARCH_ENABLED
    from backend.services import user_data_service
    from backend.services.job_match_client import job_match_client
    from backend.services.hybrid_job_search import execute_hybrid_job_search
    from backend.recommendation.recommendation_service import recommendation_service
    from backend.recommendation.mappers import to_candidate_profile_from_api
    from backend.utils.session_state import (
        get_pagination_state,
        set_pagination_state,
        clear_pagination_state,
        set_recommendation_session,
        reset_search_context,
        handle_pagination_command,
        claim_next_pagination_page,
        get_search_context,
        set_search_context,
        update_employee_session_cas,
        begin_search_generation,
    )
    from backend.services.search_executor import SearchExecutor
    from backend.services.search_planner import SearchPlanner
    from backend.schemas.search_query import SearchQuery
except ModuleNotFoundError:
    from utils.utils import is_employer_hiring_query
    from services.ranking_service import get_employer_session
    from services.search_client import search_jobs
    from services.search_parser import parse_search_query, resolve_state_id, is_self_profile_advice_query  # type: ignore
    from services.location_service import resolve_location_from_db, LocationResult  # type: ignore
    from services.platform_api import get_closing_soon_jobs, get_nearby_organizations
    from config.config import MAX_RESULTS
    from services import user_data_service  # type: ignore
    from recommendation.recommendation_service import recommendation_service  # type: ignore
    from recommendation.mappers import to_candidate_profile_from_api  # type: ignore
    from utils.session_state import (  # type: ignore
        get_pagination_state,
        set_pagination_state,
        clear_pagination_state,
        set_recommendation_session,
        reset_search_context,
        handle_pagination_command,
    )
    from services.search_executor import SearchExecutor  # type: ignore
    from services.search_planner import SearchPlanner  # type: ignore
    from schemas.search_query import SearchQuery  # type: ignore



log = logging.getLogger(__name__)

JOB_MODE_FALLBACK = "Work from Office"

SENSITIVE_KEY_TARGETS = {
    "token", "jwt", "password", "access_token", 
    "auth_token", "cookie", "secret", "authorization", "api_key"
}


def scrub_sensitive_payload_keys(data: Any, is_root: bool = True) -> Any:
    """Recursively scrub secret key targets across dictionaries and lists."""
    if isinstance(data, dict):
        cleaned = {}
        for k, v in data.items():
            k_clean = str(k).strip()
            k_lower = k_clean.lower()
            if k_lower in SENSITIVE_KEY_TARGETS or (not is_root and k_lower == "session_id"):
                continue
            cleaned[k] = scrub_sensitive_payload_keys(v, is_root=False)
        return cleaned
    elif isinstance(data, list):
        return [scrub_sensitive_payload_keys(item, is_root=False) for item in data]
    return data


class OrchestrationResult:
    """Canonical internal response model for Phase 6 Employee orchestration."""
    def __init__(
        self,
        role: str = "employee",
        route_branch: str = "SEARCH",
        success: bool = True,
        section: str = "search",
        status: str = "SUCCESS",
        message: str = "",
        db_context: Optional[str] = None,
        results: Optional[List[Any]] = None,
        result_type: Optional[str] = None,
        allowed_ids: Optional[Set[Any]] = None,
        raw_hits: Optional[List[Dict[str, Any]]] = None,
        pagination: Optional[Dict[str, Any]] = None,
        explanation: Optional[str] = None,
        global_rank: Optional[int] = None,
        job_id: Optional[Any] = None,
        suggestion_chips: Optional[List[Any]] = None,
    ):
        self.role = role
        self.route_branch = route_branch
        self.success = success
        self.section = section
        self.status = status
        self.message = message
        self.db_context = db_context
        self.results = results
        self.result_type = result_type
        self.allowed_ids = allowed_ids or set()
        self.raw_hits = raw_hits or []
        self.pagination = pagination
        self.explanation = explanation
        self.global_rank = global_rank
        self.job_id = job_id
        self.suggestion_chips = suggestion_chips


def compose_final_response(result: OrchestrationResult) -> Dict[str, Any]:
    """Mandatory Public Response Security Boundary.
    Translates internal OrchestrationResult to standard API response payload
    or Public Error Response Schema, applying recursive secret scrubbing.
    """
    if not result.success or result.status in ("UNAVAILABLE", "FAILED"):
        msg = result.message or "I couldn't complete the search right now."
        if any(term in msg.lower() for term in ["traceback", "connection refused", "mysql", "10.", "error at", "exception"]):
            msg = "I couldn't complete the search right now."
        raw_err = {
            "success": False,
            "section": result.section or "search",
            "status": "unavailable",
            "message": msg,
        }
        return scrub_sensitive_payload_keys(raw_err, is_root=True)

    raw_payload = {
        "db_context": result.db_context,
        "results": result.results,
        "result_type": result.result_type,
        "allowed_ids": result.allowed_ids,
        "raw_hits": result.raw_hits,
        "pagination": result.pagination,
    }
    if result.explanation is not None:
        raw_payload["explanation"] = result.explanation
    if result.global_rank is not None:
        raw_payload["global_rank"] = result.global_rank
    if result.job_id is not None:
        raw_payload["job_id"] = result.job_id
    if result.suggestion_chips is not None:
        raw_payload["suggestion_chips"] = result.suggestion_chips

    return scrub_sensitive_payload_keys(raw_payload, is_root=True)



def check_employee_access(last_user_msg: str, session_id: str) -> bool:
    session = get_employer_session(session_id, role="employee")
    # Employees are blocked from the employer flow (candidate search)
    if is_employer_hiring_query(last_user_msg) or session["current_step"] != "idle":
        return False
    return True


try:
    from backend.utils.timing import stage_timer, stage_timer_async, set_gate_flags, set_decision_flags
    from backend.utils.gates import should_run_search_parse_safe
except ModuleNotFoundError:
    from utils.timing import stage_timer, stage_timer_async, set_gate_flags, set_decision_flags  # type: ignore
    from utils.gates import should_run_search_parse_safe  # type: ignore


def _empty_result(db_context: str) -> dict:
    return {
        "db_context": db_context,
        "results": None,
        "result_type": None,
        "allowed_ids": set(),
        "raw_hits": [],
    }


def _term_pattern(term: str) -> Optional[re.Pattern]:
    """Word-boundary matcher so 'r' and 'go' don't match every description."""
    term = (term or "").strip()
    if len(term) < 2:
        return None
    return re.compile(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", re.IGNORECASE)


def _format_salary(raw: Any) -> Optional[str]:
    """Indian-grouped currency string or LPA representation."""
    if raw in (None, "", 0):
        return None
    try:
        amount = float(raw)
    except (TypeError, ValueError):
        text = str(raw).strip()
        return text or None
    if amount <= 0:
        return None
    if amount < 100:
        val = int(amount) if amount.is_integer() else amount
        return f"₹{val} LPA"
    amount_int = int(amount)
    s = str(amount_int)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        s = f"{head},{tail}"
    return f"₹{s}"


def _extract_location(raw_job: Any) -> str:
    """Extracts location string from job dictionary, resolving location, city_name, and state_name."""
    if not isinstance(raw_job, dict):
        return "Location not specified"
    loc = raw_job.get("location")
    if loc and not str(loc).isdigit() and str(loc).strip().lower() not in ("n/a", "none", "null"):
        return str(loc).strip()

    city = raw_job.get("city_name") or raw_job.get("city")
    state = raw_job.get("state_name") or raw_job.get("state")

    city_str = str(city).strip() if city and not str(city).isdigit() and str(city).strip().lower() not in ("n/a", "none", "null") else ""
    state_str = str(state).strip() if state and not str(state).isdigit() and str(state).strip().lower() not in ("n/a", "none", "null") else ""

    loc_parts = [p for p in (city_str, state_str) if p]
    if loc_parts:
        return ", ".join(loc_parts)
    return "Location not specified"


def resolve_experience_ids(parsed_exp: Any, message: str = "") -> Optional[List[int]]:
    """Resolves extracted experience years or keywords (fresher, 0-1 yrs) to database experience IDs."""
    msg_lower = (message or "").lower()
    if "fresher" in msg_lower or "freshers" in msg_lower or "fresh" in msg_lower or "0 year" in msg_lower or "0 yrs" in msg_lower:
        return [1, 2]

    exp_val = parsed_exp
    if exp_val is None:
        m = re.search(r"(\d+)\s*(?:-\s*\d+)?\s*(?:year|yr|yrs|years)", msg_lower)
        if m:
            exp_val = int(m.group(1))

    if exp_val is None:
        return None

    try:
        exp_num = int(exp_val)
    except (ValueError, TypeError):
        return None

    if exp_num <= 0:
        return [1, 2]
    elif exp_num == 1:
        return [2, 3]
    elif exp_num == 2:
        return [3]
    elif exp_num in (3, 4):
        return [4, 5]
    elif exp_num in (5, 6, 7):
        return [6, 7]
    elif exp_num in (8, 9, 10):
        return [7, 8]
    elif exp_num >= 11:
        return [9, 10]
    return None


import html

def _clean_description(desc: Any) -> Optional[str]:
    text = re.sub(r"<[^>]+>", " ", str(desc or ""))
    text = re.sub(r"&nbsp;", " ", text, flags=re.IGNORECASE)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if text else None


def _clean_preview(desc: Any, limit: int = 180) -> Optional[str]:
    text = _clean_description(desc)
    if not text:
        return None
    return (text[:limit].rstrip() + "…") if len(text) > limit else text


async def handle_employee_context(
    last_user_msg: str,
    messages: list = None,
    user_location: Optional[Any] = None,
    user_skills: list = None,
    user_roles: list = None,
    compose_mode: str = "structured",
    candidate_id: Optional[Any] = None,
    token: Optional[str] = None,
    user_slug: Optional[str] = None,
    user_profile_api_data: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = "default_session",
    **kwargs: Any,
) -> Optional[dict]:
    """End-to-End Employee Dialogue Orchestrator.
    Routes user turn requests down modular terminal branches.
    """
    msg_lower = (last_user_msg or "").lower().strip()

    # ------------------------------------------------------------------
    # WIDGET BRANCH 1: Position Closing Soon (Urgent Jobs)
    # ------------------------------------------------------------------
    urgent_keywords = ["closing soon", "urgent job", "urgent jobs", "position closing", "deadline approaching", "expiring job"]
    if any(kw in msg_lower for kw in urgent_keywords):
        set_gate_flags(search_parse_ran=True)
        set_decision_flags(search_query_found=True, search_executed=True)

        raw_jobs = await run_in_threadpool(get_closing_soon_jobs, keyword=None, limit=MAX_RESULTS)
        try:
            from backend.schemas.schemas import JobCard
        except ModuleNotFoundError:
            from schemas.schemas import JobCard  # type: ignore

        job_cards = []
        allowed_ids = set()
        for item in raw_jobs:
            try:
                j_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            allowed_ids.add(j_id)
            title = item.get("job_title") or "Job Title"
            company = item.get("company_name") or "CollarCheck Partner"
            dept = item.get("department_name") or "General"
            location = _extract_location(item)
            exp = f"{item.get('experience')} yrs" if item.get("experience") is not None else "Not specified"
            mode = item.get("job_mode_name") or "Office"
            salary = _format_salary(item.get("salary"))
            raw_desc = item.get("job_description")
            preview = _clean_preview(raw_desc)
            full_desc = _clean_description(raw_desc)
            url = f"https://www.collarcheck.com/jobs-details/{j_id}"
            match_reason = "⏰ Position Closing Soon"

            job_cards.append(JobCard(
                job_id=j_id,
                title=title,
                company=company,
                department=dept,
                location=location,
                job_mode=mode,
                experience=exp,
                salary=salary,
                preview=preview,
                description=full_desc,
                url=url,
                match_reason=match_reason,
            ))

        db_context_str = (
            f"## POSITION CLOSING SOON (URGENT JOBS)\n"
            f"Retrieved {len(job_cards)} time-sensitive jobs closing soon.\n"
            f"The top {len(job_cards)} are ALREADY displayed to the user as interactive cards directly below your message.\n\n"
            "[INSTRUCTION]\n"
            "Write ONE short framing sentence informing the user about these positions closing soon. Do NOT list the jobs."
        )
        orch_res = OrchestrationResult(
            role="employee",
            route_branch="WIDGET_URGENT",
            success=True,
            db_context=db_context_str,
            results=job_cards,
            result_type="urgent_jobs",
            allowed_ids=allowed_ids,
            raw_hits=raw_jobs,
        )
        return compose_final_response(orch_res)

    # ------------------------------------------------------------------
    # WIDGET BRANCH 2: Organizations Near You
    # ------------------------------------------------------------------
    nearby_keywords = ["organizations near", "organization near", "companies near", "company near", "nearby company", "nearby companies", "employers near", "local employer", "local employers"]
    if any(kw in msg_lower for kw in nearby_keywords):
        set_gate_flags(search_parse_ran=True)
        set_decision_flags(search_query_found=True, search_executed=True)

        raw_orgs = await run_in_threadpool(get_nearby_organizations, location=user_location, limit=MAX_RESULTS)
        org_cards = []
        for item in raw_orgs:
            name = item.get("name") or "Organization"
            city = item.get("city_name") or ""
            state = item.get("state_name") or ""
            loc = ", ".join([p for p in (city, state) if p]) or "Location not specified"
            dist = item.get("distance")
            dist_str = f"📍 {dist} km away" if dist is not None else "📍 Nearby Organization"
            slug = item.get("slug") or ""
            url = f"https://www.collarcheck.com/company/{slug}" if slug else "https://www.collarcheck.com/organizations"

            org_cards.append({
                "org_id": str(item.get("id")),
                "name": name,
                "location": loc,
                "city": city,
                "state": state,
                "country": item.get("country_name") or "",
                "industry": item.get("industry_name") or "General",
                "company_size": item.get("company_size_name") or "",
                "turnover": item.get("turnover_name") or "",
                "distance": dist,
                "distance_label": dist_str,
                "profile": item.get("profile"),
                "url": url,
                "match_reason": dist_str,
            })

        db_context_str = (
            f"## ORGANIZATIONS NEAR YOU\n"
            f"Retrieved {len(org_cards)} organizations near the user's location.\n"
            f"The top {len(org_cards)} are ALREADY displayed to the user as interactive organization cards directly below your message.\n\n"
            "[INSTRUCTION]\n"
            "Write ONE short framing sentence informing the user about organizations near their location. Do NOT list the organizations."
        )
        orch_res = OrchestrationResult(
            role="employee",
            route_branch="WIDGET_NEARBY",
            success=True,
            db_context=db_context_str,
            results=org_cards,
            result_type="nearby_organizations",
            allowed_ids=set(),
            raw_hits=raw_orgs,
        )
        return compose_final_response(orch_res)

    # ------------------------------------------------------------------
    # WIDGET BRANCH 3: Follow-up Explanation Handler
    # ------------------------------------------------------------------
    explanation_keywords = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"]
    if re.search(r'\bwhy\b', msg_lower) and ("job" in msg_lower or "recommend" in msg_lower or "this" in msg_lower or "that" in msg_lower or "rank" in msg_lower or "#" in msg_lower or any(word in msg_lower for word in explanation_keywords)):
        set_gate_flags(search_parse_ran=True)
        set_decision_flags(search_query_found=True, search_executed=True)

        res_exp = recommendation_service.get_recommendation_by_rank_or_id(
            session_id=session_id,
            user_message=last_user_msg
        )
        if res_exp.get("success"):
            summary_txt = res_exp.get("recommendation_explanation", "")
            g_rank = res_exp.get("global_rank", 1)
            j_id = res_exp.get("job_id")

            if JOB_MATCH_ENABLED and candidate_id and j_id and str(candidate_id).isdigit() and str(j_id).isdigit():
                try:
                    prev_data = await job_match_client.match_preview(candidate_id=int(candidate_id), job_id=int(j_id))
                    if isinstance(prev_data, dict):
                        score_val = round(float(prev_data.get("final_score", 0.0)) * 100.0)
                        reasons_list = prev_data.get("reasons", [])
                        if reasons_list:
                            summary_txt = f"Match Score: {score_val}% • " + " • ".join(str(r) for r in reasons_list)
                        else:
                            summary_txt = f"Match Score: {score_val}%"
                except Exception as prev_ex:
                    log.info("[JobMatch] match_preview unavailable (%s), using session explanation fallback.", prev_ex)

            db_context_str = (
                f"## JOB RECOMMENDATION EXPLANATION\n"
                f"Explanation for global rank #{g_rank} (Job ID {j_id}): {summary_txt}\n\n"
                "[INSTRUCTION]\n"
                f"Explain clearly to the user why job #{g_rank} was recommended: {summary_txt}"
            )
            orch_res = OrchestrationResult(
                role="employee",
                route_branch="EXPLANATION",
                success=True,
                db_context=db_context_str,
                result_type="recommendation_explanation",
                explanation=summary_txt,
                global_rank=g_rank,
                job_id=j_id,
            )
            return compose_final_response(orch_res)
        else:
            err_msg = res_exp.get("error", "I couldn't find a matching recommendation snapshot in your current session.")
            db_context_str = f"## RECOMMENDATION EXPLANATION ERROR\n{err_msg}\n\n[INSTRUCTION]\nRespond with: {err_msg}"
            orch_res = OrchestrationResult(
                role="employee",
                route_branch="EXPLANATION_ERROR",
                success=True,
                db_context=db_context_str,
                result_type="recommendation_explanation_error",
            )
            return compose_final_response(orch_res)

    # ------------------------------------------------------------------
    # TERMINAL BRANCH 1: PAGINATION
    # ------------------------------------------------------------------
    is_pagination_cmd = False
    requested_page = 1
    saved_p_state = get_pagination_state(session_id, role="employee")
    page_num_match = re.search(r'\b(?:go\s+to\s+|show\s+)?page\s+(\d+)\b', msg_lower)
    if page_num_match:
        is_pagination_cmd = True
        requested_page = int(page_num_match.group(1))
    elif re.search(r'\b(?:next\s+page|next\s+jobs|next\s+candidates|show\s+next|show\s+more|more\s+candidates|show\s+more\s+candidates|show\s+me\s+more|more|continue)\b', msg_lower) and not re.search(r'\b(job|role|developer|analyst|engineer|search|find|php|react|python|java|data)\b', msg_lower):
        is_pagination_cmd = True
        if saved_p_state:
            requested_page = saved_p_state.get("current_page", 1) + 1
        else:
            requested_page = 2
    elif re.search(r'\b(?:previous\s+page|prev\s+page|back\s+page|previous)\b', msg_lower) and not re.search(r'\b(job|role|developer|analyst|engineer|search|find|php|react|python|java|data)\b', msg_lower):
        is_pagination_cmd = True
        if saved_p_state:
            requested_page = max(1, saved_p_state.get("current_page", 1) - 1)
        else:
            requested_page = 1

    if is_pagination_cmd and saved_p_state:
        set_gate_flags(search_parse_ran=True)
        set_decision_flags(search_query_found=True, search_executed=True)

        cmd_str = f"page {requested_page}" if page_num_match else ("next" if requested_page > saved_p_state.get("current_page", 1) else "prev")

        # Atomically claim the next page under session lock to prevent concurrent duplicate page claims
        claim_res = claim_next_pagination_page(
            session_id=session_id,
            command=cmd_str,
            role="employee"
        )
        if claim_res.get("success"):
            requested_page = claim_res["claimed_page"]
            saved_p_state = claim_res["saved_p_state"]
        else:
            handle_pagination_command(
                session_id=session_id,
                command=cmd_str,
                role="employee"
            )

        res_type = saved_p_state.get("result_type", "jobs")

        if res_type == "people":
            all_recs = saved_p_state.get("all_recs", [])
            total_results = len(all_recs) if all_recs else saved_p_state.get("total_results", 0)
            per_page = 5
            total_pages = max(1, math.ceil(total_results / per_page))
            is_oob = requested_page > total_pages

            start_slice = (requested_page - 1) * per_page
            end_slice = requested_page * per_page
            people_cards = all_recs[start_slice:end_slice] if not is_oob else []

            pag_meta = {
                "current_page": requested_page,
                "per_page": per_page,
                "page_size": per_page,
                "total_results": total_results,
                "total_pages": total_pages,
                "start_index": start_slice + 1 if not is_oob and total_results > 0 else 0,
                "end_index": min(end_slice, total_results) if not is_oob else 0,
                "has_next": requested_page < total_pages,
                "has_prev": requested_page > 1,
                "invalid_page_num": requested_page if is_oob else None,
                "result_type": "people",
                "all_recs": all_recs,
                "search_gen": saved_p_state.get("search_gen", 1),
            }
            set_pagination_state(session_id, pag_meta, role="employee")

            if is_oob:
                db_context_str = f"## CANDIDATE PAGINATION\nRequested page {requested_page} is out of bounds (Total pages: {total_pages}).\n\n[INSTRUCTION]\nInform the user page {requested_page} is not available (valid pages 1–{total_pages})."
            else:
                db_context_str = (
                    f"## CANDIDATE PAGINATION RESULT\n"
                    f"Showing page {requested_page} of {total_pages} ({len(people_cards)} professionals).\n"
                    f"The top {len(people_cards)} are ALREADY displayed to the user as interactive cards directly below your message.\n\n"
                    "[INSTRUCTION]\n"
                    f"Write ONE short sentence of framing: 'Showing professionals {pag_meta['start_index']}–{pag_meta['end_index']} of {total_results} matching your criteria (Page {requested_page} of {total_pages}).' Do NOT list the candidates."
                )

            orch_res = OrchestrationResult(
                role="employee",
                route_branch="PEOPLE_PAGINATION",
                success=True,
                db_context=db_context_str,
                results=people_cards,
                result_type="people",
                pagination=pag_meta,
            )
            return compose_final_response(orch_res)
        else:
            all_recs = saved_p_state.get("all_recs", [])
            total_results = len(all_recs) if all_recs else saved_p_state.get("total_results", 0)
            total_pages = max(1, math.ceil(total_results / MAX_RESULTS))
            is_oob = requested_page > total_pages

            start_slice = (requested_page - 1) * MAX_RESULTS
            end_slice = requested_page * MAX_RESULTS
            page_recs = all_recs[start_slice:end_slice] if not is_oob else []

            try:
                from backend.schemas.schemas import JobCard
            except ModuleNotFoundError:
                from schemas.schemas import JobCard  # type: ignore

            job_cards = []
            allowed_ids = set()
            for r in page_recs:
                j_id = getattr(r, "job_id", None) if hasattr(r, "job_id") else (r.get("job_id") if isinstance(r, dict) else None)
                if not j_id:
                    continue
                allowed_ids.add(j_id)
                raw_job = getattr(r, "raw_data", {}) if hasattr(r, "raw_data") else (r.get("raw_data", {}) if isinstance(r, dict) else {})
                title = raw_job.get("job_title") or (getattr(r.title_result, "job_title", "Job Title") if hasattr(r, "title_result") else "Job Title")
                company = raw_job.get("company_name") or "CollarCheck Partner"
                location = _extract_location(raw_job)
                exp = raw_job.get("experience_name") or raw_job.get("experience") or "Not specified"
                mode = raw_job.get("job_mode_name") or "Office"
                salary = _format_salary(raw_job.get("salary"))
                raw_desc = raw_job.get("job_description")
                url = f"https://www.collarcheck.com/jobs-details/{j_id}"
                match_reason = getattr(r.explanation_result, "summary", "Matched candidate profile") if hasattr(r, "explanation_result") else "Matched candidate profile"

                job_cards.append(JobCard(
                    job_id=j_id,
                    title=title,
                    company=company,
                    department=raw_job.get("department_name") or "General",
                    location=location,
                    job_mode=mode,
                    experience=exp,
                    salary=salary,
                    preview=_clean_preview(raw_desc),
                    description=_clean_description(raw_desc),
                    url=url,
                    match_reason=match_reason,
                ))

            pag_meta = {
                "current_page": requested_page,
                "per_page": MAX_RESULTS,
                "total_results": total_results,
                "total_pages": total_pages,
                "start_index": start_slice + 1 if not is_oob and total_results > 0 else 0,
                "end_index": min(end_slice, total_results) if not is_oob else 0,
                "has_next": requested_page < total_pages,
                "has_prev": requested_page > 1,
                "invalid_page_num": requested_page if is_oob else None,
                "result_type": "jobs",
                "all_recs": all_recs,
                "search_gen": saved_p_state.get("search_gen", 1),
            }
            set_pagination_state(session_id, pag_meta, role="employee")

            if is_oob:
                db_context_str = f"## PAGINATION\nRequested page {requested_page} is out of bounds (Total pages: {total_pages}).\n\n[INSTRUCTION]\nInform the user page {requested_page} is not available (valid pages 1–{total_pages})."
            else:
                db_context_str = (
                    f"## PAGINATION RESULT\n"
                    f"Showing page {requested_page} of {total_pages} ({len(job_cards)} jobs).\n"
                    f"The top {len(job_cards)} are ALREADY displayed to the user as interactive cards directly below your message.\n\n"
                    "[INSTRUCTION]\n"
                    f"Write ONE short sentence of framing: 'Showing jobs {pag_meta['start_index']}–{pag_meta['end_index']} of {total_results} matching your skills (Page {requested_page} of {total_pages}).' Do NOT list the jobs."
                )

            orch_res = OrchestrationResult(
                role="employee",
                route_branch="PAGINATION",
                success=True,
                db_context=db_context_str,
                results=job_cards,
                result_type="jobs",
                allowed_ids=allowed_ids,
                pagination=pag_meta,
            )
            return compose_final_response(orch_res)

    # ------------------------------------------------------------------
    # TERMINAL BRANCH 2: RESET
    # ------------------------------------------------------------------
    if re.search(r'\b(?:reset\s+search|clear\s+search|start\s+over)\b', msg_lower):
        set_gate_flags(search_parse_ran=True)
        set_decision_flags(search_query_found=True, search_executed=True)

        reset_search_context(session_id, role="employee")
        db_context_str = "## SEARCH CONTEXT RESET\nYour job search context has been cleared.\n\n[INSTRUCTION]\nInform the user in one sentence that their search context has been reset and ask what role or location they want to search for."
        orch_res = OrchestrationResult(
            role="employee",
            route_branch="RESET",
            success=True,
            db_context=db_context_str,
            results=[],
            result_type="reset",
        )
        return compose_final_response(orch_res)

    # ------------------------------------------------------------------
    # TERMINAL BRANCH 3 & 4: SEARCH / REFINEMENT & PROFILE RECOMMENDATION
    # ------------------------------------------------------------------
    ran, reason = should_run_search_parse_safe("employee", last_user_msg)
    log.info(
        "gate_decision parser=search_parse ran=%s reason=%s msg_len=%d role=employee",
        ran, reason, len(last_user_msg or ""),
    )
    set_gate_flags(search_parse_ran=ran)

    if not ran:
        set_decision_flags(search_query_found=False, search_executed=False)
    parsed_query = kwargs.get("parsed_query")
    if parsed_query and isinstance(parsed_query, dict):
        parsed = parsed_query
    else:
        async with stage_timer_async("search_parse"):
            parsed = await parse_search_query(last_user_msg, "employee", messages, user_location=user_location)

    # ------------------------------------------------------------------
    # PHASE 10: RAG / KNOWLEDGE RETRIEVAL & GROUNDING BRANCH
    # Rule 0: Structured Search Intent strictly takes precedence over Knowledge Intent.
    # ------------------------------------------------------------------
    try:
        from backend.utils.session_state import get_candidate_search_criteria
    except ModuleNotFoundError:
        from utils.session_state import get_candidate_search_criteria  # type: ignore

    has_cand_ctx = bool(get_candidate_search_criteria(session_id, role="employee")) if session_id else False
    has_active_ctx = (bool(get_search_context(session_id, role="employee")) or has_cand_ctx) if session_id else False
    is_search = bool(parsed.get("is_search_intent")) or bool(parsed.get("is_recommendation_intent")) or bool(parsed.get("use_profile_skills")) or bool(parsed.get("candidate_name")) or bool(parsed.get("job_mode")) or bool(parsed.get("location")) or bool(parsed.get("salary")) or (parsed.get("experience_min") is not None) or (parsed.get("experience_max") is not None) or (parsed.get("search_target") in ("JOBS", "PEOPLE_SEARCH", "CANDIDATE_SEARCH")) or (has_active_ctx and bool(parsed.get("clear_filters")))
    
    is_knowledge = bool(parsed.get("is_knowledge_intent"))
    
    if is_knowledge and not is_search:
        try:
            from backend.rag.retriever import KnowledgeRetriever
            from backend.rag.grounder import GroundedAnswerEngine
        except ModuleNotFoundError:
            from rag.retriever import KnowledgeRetriever  # type: ignore
            from rag.grounder import GroundedAnswerEngine  # type: ignore
            
        ret_result = KnowledgeRetriever.retrieve_job_knowledge(
            user_id=candidate_id or "",
            session_id=session_id or "",
            query=last_user_msg,
            role="employee"
        )
        answer_text = GroundedAnswerEngine.generate_answer(ret_result)
        
        db_context_str = f"## KNOWLEDGE ANSWER\n{answer_text}\n\n[INSTRUCTION]\nRespond to the user with: {answer_text}"
        orch_res = OrchestrationResult(
            role="employee",
            route_branch="KNOWLEDGE_RAG",
            success=True,
            db_context=db_context_str,
            result_type="knowledge_answer",
            message=answer_text,
            results=[],
            raw_hits=[]
        )
        return compose_final_response(orch_res)

    is_intent = is_search
    set_decision_flags(search_query_found=is_intent)

    if not is_intent and not has_active_ctx:
        set_decision_flags(search_executed=False)
        return None

    set_decision_flags(search_executed=True)

    # ------------------------------------------------------------------
    # PHASE 3: PEOPLE SEARCH DELEGATION
    # ------------------------------------------------------------------
    try:
        from backend.utils.session_state import get_candidate_search_criteria
    except ModuleNotFoundError:
        from utils.session_state import get_candidate_search_criteria  # type: ignore

    target_val = parsed.get("search_target")
    has_cand_ctx = bool(get_candidate_search_criteria(session_id, role="employee")) if session_id else False

    # Detect explicit job topic switch (e.g. "Find Python jobs", "developer vacancies", "openings", "jobs with urgent hiring")
    msg_lower = (last_user_msg or "").lower()
    job_switch_patterns = [r"\bjobs?\b", r"\bvacanc(?:y|ies)\b", r"\bopenings?\b", r"\bgigs?\b", r"\bpositions?\b"]
    cand_noun_patterns = [r"\bcandidates?\b", r"\bresumes?\b", r"\bprofiles?\b", r"\bpeople\b", r"\bprofessionals?\b"]
    cand_switch_patterns = [r"\bcandidates?\b", r"\bresumes?\b", r"\bhire\b", r"\bhiring\b", r"\bprofiles?\b"]
    has_job_term = any(re.search(p, msg_lower) for p in job_switch_patterns)
    has_cand_noun = any(re.search(p, msg_lower) for p in cand_noun_patterns)
    has_cand_term = any(re.search(p, msg_lower) for p in cand_switch_patterns)
    is_explicit_job_switch = (has_job_term and not has_cand_noun) or (target_val == "JOBS" and not has_cand_noun)

    is_advice = is_self_profile_advice_query(last_user_msg)

    is_people_search = False
    if not is_advice:
        if target_val in ("PEOPLE_SEARCH", getattr(SearchQuery, "SearchTarget", None) and getattr(SearchQuery, "SearchTarget").PEOPLE_SEARCH) and not is_explicit_job_switch:
            is_people_search = True
        elif has_cand_ctx and not is_explicit_job_switch:
            is_people_search = True

    if is_people_search:
        # Candidate search multi-turn state classification:
        # NEW_SEARCH if new non-overlapping candidate skills are requested with candidate terms.
        # REFINEMENT for all follow-up refinements when candidate context is active and no job switch requested.
        prev_cand_ctx = get_candidate_search_criteria(session_id, role="employee") if session_id else None
        if prev_cand_ctx and has_cand_term and parsed.get("skills"):
            prev_skills_l = [str(s).lower() for s in (prev_cand_ctx.skills or []) if s]
            curr_skills_l = [str(s).lower() for s in (parsed.get("skills") or []) if s]
            if curr_skills_l and prev_skills_l and not any(s in prev_skills_l for s in curr_skills_l):
                parsed["query_type"] = "NEW_SEARCH"
            else:
                parsed["query_type"] = "REFINEMENT"
        elif has_cand_ctx and not is_explicit_job_switch:
            parsed["query_type"] = "REFINEMENT"

    if is_people_search:
        parsed["raw_message"] = last_user_msg
        try:
            from backend.schemas.search_criteria import SearchCriteria
            from backend.services.people_search_service import PeopleSearchService
            from backend.utils.session_state import (
                set_candidate_search_criteria,
                merge_candidate_search_criteria,
            )
        except ModuleNotFoundError:
            from schemas.search_criteria import SearchCriteria  # type: ignore
            from services.people_search_service import PeopleSearchService  # type: ignore
            from utils.session_state import (  # type: ignore
                set_candidate_search_criteria,
                merge_candidate_search_criteria,
            )

        current_criteria = SearchCriteria.from_parsed_dict(parsed)
        query_type = parsed.get("query_type", "NEW_SEARCH")

        # Multi-turn candidate search context retrieval & refinement merge
        prev_criteria = get_candidate_search_criteria(session_id, role="employee") if session_id else None
        if query_type == "REFINEMENT" and prev_criteria:
            log.info(f"[SESSION_TRACE] candidate_search_state=refined session_id={session_id}")
            criteria = merge_candidate_search_criteria(prev_criteria, current_criteria, query_type=query_type, parsed_dict=parsed)
        else:
            log.info(f"[SESSION_TRACE] candidate_search_state=created session_id={session_id}")
            criteria = current_criteria

        if session_id:
            set_candidate_search_criteria(session_id, criteria, role="employee")

        cur_search_gen = begin_search_generation(session_id, role="employee") if session_id else 1
        cur_u_id = int(candidate_id) if candidate_id and str(candidate_id).isdigit() else None
        res_people = PeopleSearchService.search_people(
            criteria=criteria,
            current_user_id=cur_u_id,
            user_location=user_location,
        )

        pag_info = res_people.get("pagination")
        if pag_info and session_id:
            pag_info["search_gen"] = cur_search_gen
            set_pagination_state(session_id, pag_info, role="employee")

        orch_res = OrchestrationResult(
            role="employee",
            route_branch="PEOPLE_SEARCH",
            success=res_people.get("success", True),
            db_context=res_people.get("db_context"),
            results=res_people.get("results"),
            result_type=res_people.get("result_type", "people"),
            pagination=pag_info,
            raw_hits=res_people.get("raw_hits", []),
            suggestion_chips=res_people.get("suggestion_chips"),
        )
        return compose_final_response(orch_res)


    # Multi-Turn Session State Integration
    turn_sq = SearchQuery(
        title=parsed.get("keyword"),
        skills=parsed.get("skills"),
        location=parsed.get("location"),
        canonical_location=parsed.get("location"),
        salary_min=parsed.get("salary_min") or (float(parsed.get("salary")) if parsed.get("salary") else None),
        salary_max=parsed.get("salary_max"),
        work_mode=parsed.get("job_mode"),
        company=parsed.get("company"),
        urgent=parsed.get("urgent"),
        clear_filters=parsed.get("clear_filters"),
    )

    msg_l = last_user_msg.lower()
    new_kw = (parsed.get("keyword") or "").lower()
    active_ctx = get_search_context(session_id, role="employee") if session_id else None
    existing_title = (active_ctx.get("title") or "").lower() if active_ctx else ""

    # Determine whether new_kw is a title refinement (e.g. 'python' -> 'senior python developer') vs completely new domain search ('accountant')
    is_related_title = False
    if existing_title and new_kw:
        is_related_title = any(w in new_kw for w in existing_title.split()) or any(w in existing_title for w in new_kw.split())

    # OVERRIDE replaces single-valued title while preserving location/work_mode/salary; RESET wipes all criteria for an unrelated domain search or NEW_SEARCH
    q_type = parsed.get("query_type")
    _is_profile_turn = bool(parsed.get("use_profile_skills") or parsed.get("skill_source") == "PROFILE")
    if _is_profile_turn and not new_kw and not parsed.get("job_mode") and not parsed.get("location") and not parsed.get("urgent"):
        prompt_cat = "RESET"
    elif _is_profile_turn and not new_kw:
        prompt_cat = "ADDITIVE"
    elif q_type == "NEW_SEARCH" or (new_kw and not (is_related_title or "in " in msg_l or "only " in msg_l or "with " in msg_l or "mein" in msg_l or "wale" in msg_l)):
        prompt_cat = "RESET"
    elif new_kw and is_related_title:
        prompt_cat = "OVERRIDE"
    else:
        prompt_cat = "ADDITIVE"
    cur_job_search_gen = begin_search_generation(session_id, role="employee") if session_id else 1
    if session_id:
        try:
            active_state = update_employee_session_cas(session_id, turn_sq, prompt_category=prompt_cat, role="employee")
            raw_merged_ctx = active_state.get("search_context") if isinstance(active_state, dict) else None
            ctx_dict = {}
            if raw_merged_ctx:
                if isinstance(raw_merged_ctx, dict):
                    ctx_dict = raw_merged_ctx
                elif hasattr(raw_merged_ctx, "model_dump"):
                    ctx_dict = raw_merged_ctx.model_dump()
                elif hasattr(raw_merged_ctx, "dict"):
                    ctx_dict = raw_merged_ctx.dict()

            if ctx_dict:
                _near_me_turn = (
                    parsed.get("location_source") == "CURRENT"
                    or parsed.get("location_mode") == "NEAR_ME"
                    or "near me" in msg_lower
                    or "near by" in msg_lower
                    or "nearby" in msg_lower
                    or "around me" in msg_lower
                    or "close to me" in msg_lower
                )
                _is_profile_skill_turn = bool(parsed.get("use_profile_skills") or parsed.get("skill_source") == "PROFILE")

                if not _is_profile_skill_turn:
                    if ctx_dict.get("title") and not parsed.get("keyword"):
                        parsed["keyword"] = ctx_dict.get("title")
                    if ctx_dict.get("skills") and not parsed.get("skills"):
                        parsed["skills"] = ctx_dict.get("skills")

                if ctx_dict.get("location") and not parsed.get("location") and not _near_me_turn and not _is_profile_skill_turn:
                    parsed["location"] = ctx_dict.get("location")
                if ctx_dict.get("work_mode") and not parsed.get("job_mode"):
                    parsed["job_mode"] = ctx_dict.get("work_mode")
                if ctx_dict.get("salary_min") and not parsed.get("salary_min"):
                    parsed["salary_min"] = ctx_dict.get("salary_min")
                if ctx_dict.get("salary_max") and not parsed.get("salary_max"):
                    parsed["salary_max"] = ctx_dict.get("salary_max")
                if ctx_dict.get("urgent") is not None and parsed.get("urgent") is None:
                    parsed["urgent"] = ctx_dict.get("urgent")
        except Exception as st_ex:
            log.warning("Session state merge error in employee context: %s", st_ex)

    try:
        # EXPLICIT INTENT PRECEDENCE ENGINE
        # User explicitly supplied keywords/skills take strict precedence over profile attributes.
        keyword = (parsed.get("keyword") or "").strip()
        skills = parsed.get("skills") or []
        if skills:
            skill_str = " ".join(skills).strip()
            if not keyword:
                keyword = skill_str
            elif skill_str and skill_str.lower() not in keyword.lower() and keyword.lower() not in skill_str.lower():
                keyword = f"{keyword} {skill_str}".strip()

        has_explicit_query_criteria = bool(keyword or skills)

        # Profile & Employment Skill Fetching
        _is_profile_turn = bool(parsed.get("use_profile_skills") or parsed.get("skill_source") == "PROFILE")
        prof_data = user_profile_api_data
        emp_data = None

        if _is_profile_turn:
            if not prof_data:
                try:
                    prof_task = run_in_threadpool(
                        user_data_service.fetch_endpoint_data,
                        "user-profile",
                        token=token,
                        user_slug=user_slug,
                        user_id=str(candidate_id or "")
                    )
                    emp_task = run_in_threadpool(
                        user_data_service.fetch_endpoint_data,
                        "allEmployementNew",
                        token=token,
                        user_id=str(candidate_id or "")
                    )
                    p_res, e_res = await asyncio.gather(prof_task, emp_task, return_exceptions=True)
                    prof_data = p_res if (isinstance(p_res, dict) and not isinstance(p_res, Exception)) else None
                    emp_data = e_res if (isinstance(e_res, dict) and not isinstance(e_res, Exception)) else None
                except Exception as gather_ex:
                    log.warning("Failed concurrent profile/employment fetch: %s", gather_ex)

                if not prof_data:
                    try:
                        prof_data = await run_in_threadpool(
                            user_data_service.fetch_endpoint_data,
                            "user-profile",
                            token=token,
                            user_slug=user_slug,
                            user_id=str(candidate_id or "")
                        )
                    except Exception as p_ex:
                        log.warning("Failed fallback fetch for user-profile: %s", p_ex)

                if not emp_data:
                    try:
                        emp_data = await run_in_threadpool(
                            user_data_service.fetch_endpoint_data,
                            "allEmployementNew",
                            token=token,
                            user_id=str(candidate_id or "")
                        )
                    except Exception as e_ex:
                        log.warning("Failed fallback fetch for allEmployementNew: %s", e_ex)
            else:
                try:
                    emp_data = await run_in_threadpool(
                        user_data_service.fetch_endpoint_data,
                        "allEmployementNew",
                        token=token,
                        user_id=str(candidate_id or "")
                    )
                except Exception as e_ex:
                    log.warning("Failed to fetch employment history for skills: %s", e_ex)
        else:
            if not prof_data:
                try:
                    prof_data = await run_in_threadpool(
                        user_data_service.fetch_endpoint_data,
                        "user-profile",
                        token=token,
                        user_slug=user_slug,
                        user_id=str(candidate_id or "")
                    )
                except Exception as p_ex:
                    log.warning("Failed to fetch API profile: %s", p_ex)

        if not prof_data and kwargs.get("user_detail"):
            prof_data = kwargs.get("user_detail")

        prof_data_dict = prof_data if isinstance(prof_data, dict) else {}
        candidate_prof = to_candidate_profile_from_api(
            prof_data_dict,
            user_id=int(candidate_id) if candidate_id and str(candidate_id).isdigit() else None,
            fallback_skills=user_skills
        )

        if _is_profile_turn:
            try:
                from backend.services.user_skill_aggregator import aggregate_user_skills
            except ModuleNotFoundError:
                from services.user_skill_aggregator import aggregate_user_skills  # type: ignore

            aggregated_res = aggregate_user_skills(
                user_profile_data=prof_data_dict if prof_data_dict else None,
                employment_data=emp_data if isinstance(emp_data, dict) else None,
                profile_status="FAILED" if prof_data and not prof_data_dict.get("status") else None,
                employment_status="FAILED" if emp_data and not (isinstance(emp_data, dict) and emp_data.get("status")) else None,
            )
            if aggregated_res.effective_skills and candidate_prof:
                candidate_prof.skills = aggregated_res.effective_skills
                candidate_prof.skill_provenance = aggregated_res.provenance

        has_valid_profile = bool(prof_data_dict.get("status") and candidate_prof and (candidate_prof.skills or candidate_prof.current_title or candidate_prof.name))
        set_decision_flags(profile_retrieved=has_valid_profile)
        log.info(
            "[PROFILE] retrieval_completed authentication_available=%s profile_retrieved=%s profile_skills_count=%d",
            bool(token),
            has_valid_profile,
            len(candidate_prof.skills) if candidate_prof else 0
        )

        # Profile attributes supplement under-specified queries ONLY when user gave no explicit query terms
        used_profile_fallback = False
        profile_skills_for_fallback = []
        profile_roles_for_fallback = []
        if not has_explicit_query_criteria and candidate_prof:
            used_profile_fallback = True
            if candidate_prof.skills:
                profile_skills_for_fallback = candidate_prof.skills
            if candidate_prof.current_title:
                profile_roles_for_fallback = [candidate_prof.current_title]
        elif _is_profile_turn and candidate_prof and candidate_prof.skills:
            profile_skills_for_fallback = candidate_prof.skills
            if candidate_prof.current_title:
                profile_roles_for_fallback = [candidate_prof.current_title]

        # Location Resolution & SearchSpec
        raw_query_location = parsed.get("location")
        is_near_me = (
            parsed.get("location_mode") == "NEAR_ME"
            or parsed.get("location_source") == "CURRENT"
            or "near me" in msg_lower
            or "near by" in msg_lower
            or "nearby" in msg_lower
            or "around me" in msg_lower
            or "close to me" in msg_lower
        )
        loc_source = "QUERY" if (raw_query_location and not is_near_me) else ("CURRENT" if is_near_me else ("QUERY" if raw_query_location else "NONE"))
        explicit_loc = loc_source in ("QUERY", "CURRENT")

        resolved_raw_loc = None
        current_loc_missing = False
        resolution_method = "none"
        gps_city_hint = None
        gps_state_hint = None
        gps_coords_present = False

        if loc_source == "QUERY":
            resolved_raw_loc = raw_query_location
            resolution_method = "EXPLICIT_QUERY"
        elif loc_source == "CURRENT":
            if user_location and isinstance(user_location, dict):
                gps_coords_present = (
                    user_location.get("latitude") is not None
                    and user_location.get("longitude") is not None
                )
                gps_city_hint = (user_location.get("city") or "").strip() or None
                gps_state_hint = (user_location.get("state") or "").strip() or None
                # Prefer city over state so GPS does not collapse to a statewide nationwide-like search.
                resolved_raw_loc = gps_city_hint or gps_state_hint
                if resolved_raw_loc:
                    resolution_method = "GPS_REVERSE_GEOCODE"
            elif user_location and isinstance(user_location, str) and user_location.strip():
                resolved_raw_loc = user_location.strip()
                resolution_method = "GPS_REVERSE_GEOCODE"

            if not resolved_raw_loc and not gps_coords_present and prof_data and isinstance(prof_data, dict):
                resolved_raw_loc = prof_data.get("city") or prof_data.get("state") or prof_data.get("location")
                resolution_method = "PROFILE_DB"

            if not resolved_raw_loc:
                current_loc_missing = True
                log.warning("CURRENT location requested ('near me') but no location available.")

        loc_res = await run_in_threadpool(resolve_location_from_db, resolved_raw_loc) if (resolved_raw_loc and not current_loc_missing) else LocationResult(resolved=True, location_type="none")
        if (
            loc_source == "CURRENT"
            and gps_state_hint
            and gps_state_hint != resolved_raw_loc
            and not (loc_res.resolved and (loc_res.city_id or loc_res.state_id))
        ):
            state_res = await run_in_threadpool(resolve_location_from_db, gps_state_hint)
            if state_res.resolved and (state_res.city_id or state_res.state_id):
                loc_res = state_res
                resolved_raw_loc = gps_state_hint
        loc_res.resolution_method = resolution_method
        if loc_source == "CURRENT" and gps_coords_present and not (loc_res.city_id or loc_res.state_id):
            current_loc_missing = True
            log.warning(
                "[NearbyDebug] GPS coordinates present but city/state could not be resolved for filtering. loc=%r",
                resolved_raw_loc,
            )

        log.info(
            "[NearbyDebug] is_near_me=%s loc_source=%s resolved_raw_loc=%r city_id=%s state_id=%s gps_coords=%s user_location=%s",
            is_near_me,
            loc_source,
            resolved_raw_loc,
            loc_res.city_id,
            loc_res.state_id,
            gps_coords_present,
            user_location,
        )

        has_explicit_unresolved_location = (loc_source == "QUERY" and resolved_raw_loc and not loc_res.resolved)

        base_filters = {}
        if loc_res.resolved and explicit_loc:
            if loc_res.location_type == "city" and loc_res.city_id:
                base_filters["city"] = [loc_res.city_id]
            elif loc_res.location_type == "state" and loc_res.state_id:
                base_filters["state"] = [loc_res.state_id]

        mode_str = (parsed.get("job_mode") or "").lower().strip()
        job_mode_id = None
        if "home" in mode_str or "wfh" in mode_str or "remote" in mode_str:
            job_mode_id = 2
        elif "office" in mode_str or "site" in mode_str:
            job_mode_id = 1
        elif "hybrid" in mode_str:
            job_mode_id = 3
        elif "field" in mode_str:
            job_mode_id = 4

        if job_mode_id:
            base_filters["job_mode"] = [job_mode_id]
        if parsed.get("urgent"):
            base_filters["urgent"] = 1

        exp_ids = resolve_experience_ids(parsed.get("experience"), last_user_msg)
        if exp_ids:
            base_filters["experience"] = exp_ids

        # Hybrid Search Layer Integration Branch
        hybrid_executed = False
        used_job_match_flag = False
        if HYBRID_SEARCH_ENABLED and not has_explicit_unresolved_location and not current_loc_missing:
            try:
                hits, hybrid_meta = await execute_hybrid_job_search(
                    last_user_msg=last_user_msg,
                    candidate_id=candidate_id,
                    candidate_profile=candidate_prof if candidate_prof else None,
                    keyword=keyword,
                    base_filters=base_filters,
                    parsed=parsed,
                    used_profile_fallback=used_profile_fallback,
                    profile_terms=(profile_skills_for_fallback + profile_roles_for_fallback),
                    explicit_loc=explicit_loc,
                    target_city_name=loc_res.city_name if loc_res.resolved else resolved_raw_loc,
                    target_state_name=loc_res.state_name if loc_res.resolved else None,
                    limit=MAX_RESULTS * 2,
                )
                hybrid_executed = True
                used_job_match_flag = False
            except Exception as h_err:
                log.warning("[HybridSearch] Hybrid search execution failed (%s). Falling back to legacy pipeline.", h_err)
                hybrid_executed = False

        if not hybrid_executed and not used_job_match_flag:
            # Retrieval Phase
            hits = []
            if has_explicit_unresolved_location:
                hits = []
            elif current_loc_missing:
                orch_res = OrchestrationResult(
                    role="employee",
                    route_branch="LOCATION_UNAVAILABLE",
                    success=True,
                    db_context=(
                        "## LOCATION UNAVAILABLE\n"
                        "Your location details are not available in your profile or browser.\n\n"
                        "[INSTRUCTION]\n"
                        "Inform the user in one sentence that their location could not be determined and ask them to specify a city."
                    )
                )
                return compose_final_response(orch_res)
            elif used_profile_fallback and not keyword:
                try:
                    from backend.services.user_skill_aggregator import select_balanced_retrieval_terms
                except ModuleNotFoundError:
                    from services.user_skill_aggregator import select_balanced_retrieval_terms  # type: ignore

                raw_terms = profile_skills_for_fallback + profile_roles_for_fallback
                prov_list = getattr(candidate_prof, "skill_provenance", None) if candidate_prof else None
                clean_terms = select_balanced_retrieval_terms(raw_terms, provenance=prov_list, limit=15)
                if clean_terms:
                    tasks = [search_jobs(keyword=t, **base_filters) for t in clean_terms]
                    results_list = await asyncio.gather(*tasks, return_exceptions=True)
                    merged_hits = []
                    seen_ids = set()
                    for r in results_list:
                        if isinstance(r, dict) and r.get("success"):
                            for item in r.get("data", []):
                                j_id = item.get("id")
                                if j_id is not None and j_id not in seen_ids:
                                    seen_ids.add(j_id)
                                    merged_hits.append(item)
                    hits = merged_hits
                if not hits and not explicit_loc:
                    res_all = await search_jobs(**base_filters)
                    hits = res_all.get("data", []) if isinstance(res_all, dict) else []
            else:
                filters = dict(base_filters)
                if keyword:
                    filters["keyword"] = keyword
                res_jobs = await search_jobs(**filters)
                hits = res_jobs.get("data", []) if isinstance(res_jobs, dict) else []

            # MySQL SearchExecutor fallback if 0 hits
            if not hits and not explicit_loc and not base_filters.get("city") and not base_filters.get("state"):
                sq = SearchQuery(title=keyword, work_mode=parsed.get("job_mode"))
                executor_hits = SearchExecutor.execute_search(sq)
                if executor_hits:
                    hits = executor_hits

            # Handle 0 hits case
            if not hits:
                try:
                    from backend.services.chip_generator import generate_suggestion_chips
                except ImportError:
                    from services.chip_generator import generate_suggestion_chips  # type: ignore
                
                fallback_chips = generate_suggestion_chips("employee", parsed, 0, user_location=user_location)
                if used_profile_fallback and not clean_terms:
                    no_hits_msg = (
                        "## PROFILE SKILLS MISSING\n"
                        "I couldn't find any skills listed in your profile yet.\n\n"
                        "[INSTRUCTION]\n"
                        "Inform the user in one polite sentence that no skills were found in their profile yet, and ask them to add skills to their profile or tell you which skills or role they are looking for."
                    )
                else:
                    searched_item = keyword or parsed.get("location") or "your requested skills"
                    no_hits_msg = (
                        f"## JOB SEARCH RESULT\n"
                        f"No active job listings were found matching {searched_item}.\n\n"
                        "[INSTRUCTION]\n"
                        "Inform the user in one sentence that no jobs were found matching their request and suggest broadening their search."
                    )

                orch_res = OrchestrationResult(
                    role="employee",
                    route_branch="NO_HITS",
                    success=True,
                    db_context=no_hits_msg,
                    suggestion_chips=fallback_chips,
                )
                return compose_final_response(orch_res)

        # Deduplicate hits without re-sorting (Authoritative Phase 2 Ranking Ownership)
        valid_hits = []
        seen_ids = set()
        for h in hits:
            try:
                j_id = int(h.get("id"))
            except (TypeError, ValueError):
                continue
            if j_id not in seen_ids:
                seen_ids.add(j_id)
                h["_job_id"] = j_id
                valid_hits.append(h)

        search_query_term = keyword.strip() if keyword else None
        page_recs, pagination_meta, all_recs = recommendation_service.get_paginated_recommendations_for_profile(
            candidate_prof if candidate_prof else None,
            jobs=valid_hits,
            page=1,
            per_page=MAX_RESULTS,
            session_id=session_id,
            job_mode=job_mode_id,
            city_id=loc_res.city_id if loc_res.resolved else None,
            search_query_title=search_query_term,
            search_query_location=(loc_res.city_name or loc_res.state_name or resolved_raw_loc) if explicit_loc else None,
        )

        pagination_meta["all_recs"] = all_recs
        pagination_meta["search_gen"] = cur_job_search_gen
        pagination_meta["_is_fresh_init"] = True
        set_pagination_state(session_id, pagination_meta, role="employee")

        try:
            from backend.schemas.schemas import JobCard
        except ModuleNotFoundError:
            from schemas.schemas import JobCard  # type: ignore

        # Build Job Cards preserving Phase 2 recommendation order exactly
        job_cards = []
        allowed_ids = set()
        compact_lines = []

        target_recs = all_recs if all_recs else page_recs
        for i, r in enumerate(target_recs, start=1):
            j_id = getattr(r, "job_id", None) if hasattr(r, "job_id") else (r.get("job_id") if isinstance(r, dict) else None)
            if not j_id:
                continue
            allowed_ids.add(j_id)

            raw_job = getattr(r, "raw_data", {}) if hasattr(r, "raw_data") else (r.get("raw_data", {}) if isinstance(r, dict) else {})
            title = raw_job.get("job_title") or (getattr(r.title_result, "job_title", "Job Title") if hasattr(r, "title_result") else "Job Title")
            company = raw_job.get("company_name") or "CollarCheck Partner"
            location = _extract_location(raw_job)
            exp = raw_job.get("experience_name") or raw_job.get("experience") or "Not specified"
            mode = raw_job.get("job_mode_name") or JOB_MODE_FALLBACK
            salary = _format_salary(raw_job.get("salary"))
            raw_desc = raw_job.get("job_description")
            url = f"https://www.collarcheck.com/jobs-details/{j_id}"

            summary_reason = getattr(r.explanation_result, "summary", None) if hasattr(r, "explanation_result") else None
            score_val = getattr(r, "overall_score", 0.0) if hasattr(r, "overall_score") else 0.0
            match_reason = f"Match Score: {round(score_val)}% • {summary_reason}" if summary_reason else "Matched candidate profile"

            job_card = JobCard(
                job_id=j_id,
                title=title,
                company=company,
                department=raw_job.get("department_name") or "General",
                location=location,
                job_mode=mode,
                experience=exp,
                salary=salary,
                preview=_clean_preview(raw_desc),
                description=_clean_description(raw_desc),
                url=url,
                match_reason=match_reason,
                global_rank=i,
                match_score=score_val,
                urgent=raw_job.get("urgent"),
            )
            job_cards.append(job_card)
            if i <= MAX_RESULTS:
                compact_lines.append(f"{i}. {title} — {company}")

        # Store all job cards in pagination_meta for instant client-side pagination
        pagination_meta["ranked_jobs"] = [card.model_dump() if hasattr(card, "model_dump") else card.dict() for card in job_cards]

        # Set recommendation session snapshot in Phase 5 store
        set_recommendation_session(
            session_id=session_id,
            query_key=f"cand_{candidate_prof.id if (candidate_prof and hasattr(candidate_prof, 'id')) else 'default'}",
            ranked_jobs=target_recs,
            current_page=1,
            page_size=MAX_RESULTS,
            total_jobs=len(all_recs)
        )

        display_total = pagination_meta.get("total_results", len(job_cards))
        loc_name = resolved_raw_loc
        location_label = f" in {loc_name}" if loc_name else ""

        parts = [
            "## JOB SEARCH RESULT SUMMARY",
            f"The search{location_label} returned {display_total} matching job(s). "
            f"The top {min(MAX_RESULTS, len(job_cards))} are ALREADY displayed to the user as interactive cards directly below your message.",
        ]
        example_sentence = f"I found {display_total} roles matching your criteria — here are the top matches."
        parts.append(
            "\n[MANDATORY AI INSTRUCTION]\n"
            "Write ONE short sentence of framing, then stop. You MUST NOT:\n"
            "- list, number, bullet, or name any job\n"
            f"- state any count other than the exact total count of {display_total} roles (do NOT state any other count)\n"
            f"Good example: '{example_sentence}'"
        )
        if used_profile_fallback:
            parts.append("Note: search keywords were derived from saved candidate profile.")

        db_context_str = "\n".join(parts)

        try:
            from backend.services.chip_generator import generate_suggestion_chips
        except ImportError:
            from services.chip_generator import generate_suggestion_chips  # type: ignore

        generated_chips = generate_suggestion_chips("employee", parsed, display_total, user_location=user_location)

        orch_res = OrchestrationResult(
            role="employee",
            route_branch="SEARCH",
            success=True,
            db_context=db_context_str,
            results=job_cards,
            result_type="jobs",
            allowed_ids=allowed_ids,
            raw_hits=valid_hits[:MAX_RESULTS],
            pagination=pagination_meta,
            suggestion_chips=generated_chips,
        )
        return compose_final_response(orch_res)

    except Exception as ex:
        log.exception("Employee Orchestration execution failed: %s", ex)
        orch_res = OrchestrationResult(
            role="employee",
            route_branch="SEARCH_ERROR",
            success=False,
            section="search",
            status="UNAVAILABLE",
            message="I couldn't complete the search right now.",
        )
        return compose_final_response(orch_res)