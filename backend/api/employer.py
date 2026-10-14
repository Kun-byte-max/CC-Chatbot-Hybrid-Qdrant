from typing import Optional, Dict, Any, List, Set
import logging
import math
import re

try:
    from backend.utils.utils import is_employer_hiring_query
    from backend.services.search_client import search_users
    from backend.services.search_parser import parse_search_query, resolve_state_id
    from backend.config.config import MAX_RESULTS
except ModuleNotFoundError:
    from utils.utils import is_employer_hiring_query
    from services.search_client import search_users
    from services.search_parser import parse_search_query, resolve_state_id
    from config.config import MAX_RESULTS  # type: ignore

log = logging.getLogger(__name__)

def check_employer_access(last_user_msg: str, session_id: str, is_career_query_func) -> bool:
    # Employers are blocked from the job-search flow (any career query that is not a hiring trigger)
    if is_career_query_func(last_user_msg) and not is_employer_hiring_query(last_user_msg):
        return False
    return True

try:
    from backend.utils.timing import stage_timer, stage_timer_async, set_gate_flags, set_decision_flags
    from backend.utils.gates import should_run_search_parse_safe
except ModuleNotFoundError:
    from utils.timing import stage_timer, stage_timer_async, set_gate_flags, set_decision_flags  # type: ignore
    from utils.gates import should_run_search_parse_safe  # type: ignore


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
            # Scrub matching sensitive key targets or untrusted nested session_id
            if k_lower in SENSITIVE_KEY_TARGETS or (not is_root and k_lower == "session_id"):
                continue
            cleaned[k] = scrub_sensitive_payload_keys(v, is_root=False)
        return cleaned
    elif isinstance(data, list):
        return [scrub_sensitive_payload_keys(item, is_root=False) for item in data]
    return data


class OrchestrationResult:
    """Canonical internal response model for Phase 6 orchestration."""
    def __init__(
        self,
        role: str,
        route_branch: str,
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
        search_metadata: Optional[Dict[str, Any]] = None,
        requirements: Any = None,
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
        self.search_metadata = search_metadata
        self.requirements = requirements
        self.suggestion_chips = suggestion_chips


def compose_final_response(result: OrchestrationResult) -> Dict[str, Any]:
    """Mandatory Public Response Security Boundary.
    Translates internal OrchestrationResult to standard API response payload
    or Public Error Response Schema, applying recursive secret scrubbing.
    """
    if not result.success or result.status in ("UNAVAILABLE", "FAILED"):
        msg = result.message or "I couldn't complete the request right now."
        if any(term in msg.lower() for term in ["traceback", "connection refused", "mysql", "10.", "error at", "exception"]):
            msg = "I couldn't complete the request right now."
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
        "search_metadata": result.search_metadata,
        "requirements": result.requirements,
    }
    if result.suggestion_chips is not None:
        raw_payload["suggestion_chips"] = result.suggestion_chips
    return scrub_sensitive_payload_keys(raw_payload, is_root=True)


async def handle_employer_context(
    last_user_msg: str, 
    session_id: str, 
    messages: list = None, 
    user_location: str = None, 
    compose_mode: str = "dual",
    **kwargs: Any
) -> Optional[dict]:
    """End-to-End Employer Dialogue Orchestrator.
    Routes user turn requests down modular terminal branches.
    """
    ran, reason = should_run_search_parse_safe("employer", last_user_msg, session_id=session_id)
    log.info("gate_decision parser=search_parse ran=%s reason=%s msg_len=%d role=employer", ran, reason, len(last_user_msg or ""))
    set_gate_flags(search_parse_ran=ran)

    if not ran:
        set_decision_flags(search_query_found=False, search_executed=False)
        return None

    try:
        from backend.utils.session_state import (
            get_employer_search_context,
            update_employer_session_cas,
            reset_search_context,
            handle_pagination_command,
            get_pagination_state,
            begin_search_generation,
        )
        from backend.services.employer_search_parser import parse_employer_query
        from backend.services.employer_search_executor import execute_employer_candidate_search
        from backend.schemas.employer_requirements import EmployerSearchRequirements
        from backend.recommendation.recommendation_service import recommendation_service
        from backend.recommendation.formatters import EmployerResponseFormatter
        from backend.schemas.schemas import CandidateCard
    except ModuleNotFoundError:
        from utils.session_state import (  # type: ignore
            get_employer_search_context,
            update_employer_session_cas,
            reset_search_context,
            handle_pagination_command,
            get_pagination_state,
            begin_search_generation,
        )
        from services.employer_search_parser import parse_employer_query  # type: ignore
        from services.employer_search_executor import execute_employer_candidate_search  # type: ignore
        from schemas.employer_requirements import EmployerSearchRequirements  # type: ignore
        from recommendation.recommendation_service import recommendation_service  # type: ignore
        from recommendation.formatters import EmployerResponseFormatter  # type: ignore
        from schemas.schemas import CandidateCard  # type: ignore

    msg_clean = (last_user_msg or "").lower().strip()

    # -------------------------------------------------------------------------
    # TERMINAL BRANCH 1: PAGINATION BRANCH
    # -------------------------------------------------------------------------
    is_pagination_cmd = False
    requested_page = 1
    page_num_match = re.search(r'\b(?:go\s+to\s+|show\s+)?page\s+(\d+)\b', msg_clean)
    if page_num_match:
        is_pagination_cmd = True
        requested_page = int(page_num_match.group(1))
    elif re.search(r'\b(?:next\s+page|next\s+candidates|show\s+next|next)\b', msg_clean) and not re.search(r'\b(hire|candidate|role|developer|analyst|engineer|search|find|php|react|python|java|data)\b', msg_clean):
        is_pagination_cmd = True
        saved_p_state = get_pagination_state(session_id, role="employer")
        requested_page = (saved_p_state.get("current_page", 1) + 1) if saved_p_state else 2
    elif re.search(r'\b(?:previous\s+page|prev\s+page|back\s+page|previous)\b', msg_clean) and not re.search(r'\b(hire|candidate|role|developer|analyst|engineer|search|find|php|react|python|java|data)\b', msg_clean):
        is_pagination_cmd = True
        saved_p_state = get_pagination_state(session_id, role="employer")
        requested_page = max(1, (saved_p_state.get("current_page", 1) - 1) if saved_p_state else 1)

    if is_pagination_cmd:
        with stage_timer("pagination_branch"):
            handle_pagination_command(session_id, last_user_msg, role="employer")
            existing_ctx_dict = get_employer_search_context(session_id, role="employer")
            if existing_ctx_dict:
                try:
                    reqs = EmployerSearchRequirements.parse_obj(existing_ctx_dict)
                    candidates, metadata, raw_payload = await execute_employer_candidate_search(
                        requirements=reqs,
                        session_id=session_id,
                        page=1,
                        limit=100,
                    )
                    raw_hits = raw_payload.get("raw_hits", [])
                    if not raw_hits and saved_p_state and saved_p_state.get("all_candidates"):
                        raw_hits = saved_p_state.get("all_candidates")
                    if raw_hits:
                        emp_search_gen = saved_p_state.get("search_gen", 1) if saved_p_state else 1
                        page_recs, pag_meta_dict, all_recs = recommendation_service.get_paginated_employer_candidate_recommendations(
                            employer_reqs=reqs,
                            candidates=raw_hits,
                            page=max(1, requested_page),
                            per_page=MAX_RESULTS,
                            session_id=session_id,
                            search_gen=emp_search_gen,
                        )
                        cands_by_id = {str(h.get("id") or h.get("user_id") or h.get("ccid") or i): h for i, h in enumerate(raw_hits, 1)}
                        all_formatted_card_dicts = EmployerResponseFormatter.format_candidate_cards(all_recs, cands_by_id)

                        all_candidate_cards = []
                        allowed_ids = set()
                        compact_lines = []

                        for i, card_dict in enumerate(all_formatted_card_dicts, 1):
                            c_id_str = card_dict["candidate_id"]
                            try:
                                allowed_ids.add(int(c_id_str))
                            except (ValueError, TypeError):
                                allowed_ids.add(c_id_str)

                            cand_card = CandidateCard(
                                cc_id=c_id_str,
                                name=card_dict["name"],
                                position=card_dict["title"],
                                city=card_dict["city"],
                                state=card_dict["state"],
                                experience_label=card_dict["experience_label"],
                                skills=card_dict["skills"],
                                url=card_dict["url"],
                                rating=card_dict["rating"],
                                match_reason=card_dict["match_reason"],
                            )
                            all_candidate_cards.append(cand_card)
                            if i <= MAX_RESULTS:
                                compact_lines.append(f"{i}. {card_dict['name']} - {card_dict['title']} - {card_dict['match_reason']}")

                        total = pag_meta_dict.get("total_results", len(all_candidate_cards))
                        total_pages_cnt = pag_meta_dict.get("total_pages", max(1, math.ceil(total / MAX_RESULTS)))
                        curr_page = pag_meta_dict.get("current_page", requested_page)
                        start_idx = pag_meta_dict.get("start_index", (curr_page - 1) * MAX_RESULTS + 1 if total > 0 else 0)
                        end_idx = pag_meta_dict.get("end_index", min(curr_page * MAX_RESULTS, total))

                        page_cards = all_candidate_cards[(curr_page - 1) * MAX_RESULTS : curr_page * MAX_RESULTS] if total > 0 else []

                        pagination_payload = {
                            "current_page": curr_page,
                            "per_page": MAX_RESULTS,
                            "page_size": MAX_RESULTS,
                            "total_results": total,
                            "total_pages": total_pages_cnt,
                            "start_index": start_idx,
                            "end_index": end_idx,
                            "has_next": curr_page < total_pages_cnt,
                            "has_prev": curr_page > 1,
                            "ranked_jobs": [c.model_dump() if hasattr(c, "model_dump") else c.dict() for c in all_candidate_cards]
                        }

                        db_context_str = f"Results found: {total} matching candidate(s) (Page {curr_page} of {total_pages_cnt})\n" + "\n".join(compact_lines)
                        orch_res = OrchestrationResult(
                            role="employer",
                            route_branch="PAGINATION",
                            db_context=db_context_str,
                            results=all_candidate_cards,
                            result_type="candidates",
                            allowed_ids=allowed_ids,
                            raw_hits=raw_hits,
                            pagination=pagination_payload,
                            search_metadata=metadata.dict() if metadata else None,
                            requirements=reqs,
                        )
                        return compose_final_response(orch_res)
                except Exception as e:
                    log.warning(f"Error during employer pagination branch execution: {e}")

    # -------------------------------------------------------------------------
    # TERMINAL BRANCH 2: RESET BRANCH
    # -------------------------------------------------------------------------
    if re.search(r'\b(?:reset\s+search|clear\s+search|reset\s+criteria|clear\s+criteria)\b', msg_clean):
        with stage_timer("reset_branch"):
            reset_search_context(session_id, role="employer")
            orch_res = OrchestrationResult(
                role="employer",
                route_branch="RESET",
                db_context="Search criteria reset successfully.",
                results=None,
                result_type=None,
            )
            return compose_final_response(orch_res)

    # -------------------------------------------------------------------------
    # -------------------------------------------------------------------------
    parsed_query = kwargs.get("parsed_query")
    if parsed_query and isinstance(parsed_query, dict):
        parsed_k = parsed_query
    else:
        parsed_k = await parse_search_query(last_user_msg, "employer", user_location=None)
    is_knowledge = bool(parsed_k.get("is_knowledge_intent"))
    
    # Step 3a. Recover session context
    existing_ctx_dict = get_employer_search_context(session_id)
    existing_reqs = None
    if existing_ctx_dict:
        try:
            existing_reqs = EmployerSearchRequirements.parse_obj(existing_ctx_dict)
        except Exception as e:
            log.warning(f"Failed to parse existing employer requirements: {e}")

    # Step 3b. Parse query delta into canonical EmployerSearchRequirements
    reqs = parse_employer_query(
        last_user_msg,
        existing_requirements=existing_reqs,
        user_location=user_location,
        parsed_llm=parsed_query if (parsed_query and isinstance(parsed_query, dict)) else None,
    )
    
    prof_api_data = kwargs.get("user_profile_api_data")
    if not reqs.location and prof_api_data and isinstance(prof_api_data, dict):
        prof_city = (prof_api_data.get("city") or prof_api_data.get("location") or prof_api_data.get("state") or "").strip()
        if prof_city:
            res = resolve_location_from_db(prof_city)
            is_res = getattr(res, "resolved", None) if hasattr(res, "resolved") else (res.get("resolved") if isinstance(res, dict) else False)
            if is_res:
                reqs.location = LocationRequirement(
                    text=getattr(res, "text", None) or (res.get("text") if isinstance(res, dict) else prof_city.title()),
                    type=getattr(res, "type", None) or (res.get("type") if isinstance(res, dict) else "city"),
                    city_id=getattr(res, "city_id", None) or (res.get("city_id") if isinstance(res, dict) else None),
                    state_id=getattr(res, "state_id", None) or (res.get("state_id") if isinstance(res, dict) else None),
                    country_id=getattr(res, "country_id", None) or (res.get("country_id") if isinstance(res, dict) else None),
                    source=ProvenanceSource.PROFILE,
                )

    is_search = bool(reqs.role and reqs.role.value) or bool(reqs.location) or bool(reqs.experience) or bool(reqs.skills and reqs.skills.value) or bool(reqs.fresher)

    if is_knowledge and not is_search:
        try:
            from backend.rag.retriever import KnowledgeRetriever
            from backend.rag.grounder import GroundedAnswerEngine
        except ModuleNotFoundError:
            from rag.retriever import KnowledgeRetriever  # type: ignore
            from rag.grounder import GroundedAnswerEngine  # type: ignore

        ret_result = KnowledgeRetriever.retrieve_job_knowledge(
            user_id=employer_id or "",
            session_id=session_id or "",
            query=last_user_msg,
            role="employer"
        )
        answer_text = GroundedAnswerEngine.generate_answer(ret_result)

        db_context_str = f"## KNOWLEDGE ANSWER\n{answer_text}\n\n[INSTRUCTION]\nRespond to the employer with: {answer_text}"
        orch_res = OrchestrationResult(
            role="employer",
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

    if not is_intent and not reqs.safe_failure:
        set_decision_flags(search_executed=False)
        return None

    set_decision_flags(search_executed=True)

    # Step 3c. Phase 5 Atomic CAS Session Persistence
    update_employer_session_cas(session_id, reqs, prompt_category="ADDITIVE", role="employer")

    # Step 3c-gen. Capture new search generation atomically prior to slow network/retrieval execution
    cur_employer_search_gen = begin_search_generation(session_id, role="employer") if session_id else 1

    # Step 3d. Search Execution
    try:
        candidates, metadata, raw_payload = await execute_employer_candidate_search(
            requirements=reqs,
            session_id=session_id,
            page=1,
            limit=100,
        )
    except Exception as e:
        log.exception(f"Employer candidate search execution failed: {e}")
        orch_res = OrchestrationResult(
            role="employer",
            route_branch="SEARCH",
            success=False,
            section="search",
            status="UNAVAILABLE",
            message="I couldn't complete the candidate search right now.",
        )
        return compose_final_response(orch_res)

    if reqs.safe_failure:
        orch_res = OrchestrationResult(
            role="employer",
            route_branch="SEARCH",
            db_context=f"## SEARCH STATUS\nFailed to apply requested filter: {reqs.failure_reason}",
            results=None,
            result_type=None,
            search_metadata=metadata.dict() if metadata else None,
            requirements=reqs,
        )
        return compose_final_response(orch_res)

    raw_hits = raw_payload.get("raw_hits", [])
    if not raw_hits:
        try:
            from backend.services.chip_generator import generate_suggestion_chips
        except ImportError:
            from services.chip_generator import generate_suggestion_chips  # type: ignore
        employer_fallback_chips = generate_suggestion_chips("employer", reqs, 0, user_location=user_location)

        orch_res = OrchestrationResult(
            role="employer",
            route_branch="SEARCH",
            db_context="Found 0 matches for candidates.",
            results=None,
            result_type=None,
            search_metadata=metadata.dict() if metadata else None,
            requirements=reqs,
            suggestion_chips=employer_fallback_chips,
        )
        return compose_final_response(orch_res)

    # Step 3e. Phase 2 Ranking & Presentation Formatting
    # Consumes Phase 2 authoritative ordering without re-sorting
    page_recs, pag_meta_dict, all_recs = recommendation_service.get_paginated_employer_candidate_recommendations(
        employer_reqs=reqs,
        candidates=raw_hits,
        page=max(1, requested_page),
        per_page=MAX_RESULTS,
        session_id=session_id,
        search_gen=cur_employer_search_gen,
    )

    cands_by_id = {str(h.get("id") or h.get("user_id") or h.get("ccid") or i): h for i, h in enumerate(raw_hits, 1)}
    all_formatted_card_dicts = EmployerResponseFormatter.format_candidate_cards(all_recs, cands_by_id)

    all_candidate_cards = []
    allowed_ids = set()
    compact_lines = []

    for i, card_dict in enumerate(all_formatted_card_dicts, 1):
        c_id_str = card_dict["candidate_id"]
        try:
            allowed_ids.add(int(c_id_str))
        except (ValueError, TypeError):
            allowed_ids.add(c_id_str)

        cand_card = CandidateCard(
            cc_id=c_id_str,
            name=card_dict["name"],
            position=card_dict["title"],
            city=card_dict["city"],
            state=card_dict["state"],
            experience_label=card_dict["experience_label"],
            skills=card_dict["skills"],
            url=card_dict["url"],
            rating=card_dict["rating"],
            match_reason=card_dict["match_reason"],
        )
        all_candidate_cards.append(cand_card)
        if i <= MAX_RESULTS:
            compact_lines.append(f"{i}. {card_dict['name']} - {card_dict['title']} - {card_dict['match_reason']}")

    total = pag_meta_dict.get("total_results", len(all_candidate_cards))
    total_pages_cnt = pag_meta_dict.get("total_pages", max(1, math.ceil(total / MAX_RESULTS)))
    curr_page = pag_meta_dict.get("current_page", requested_page)
    start_idx = pag_meta_dict.get("start_index", (curr_page - 1) * MAX_RESULTS + 1 if total > 0 else 0)
    end_idx = pag_meta_dict.get("end_index", min(curr_page * MAX_RESULTS, total))

    page_cards = all_candidate_cards[(curr_page - 1) * MAX_RESULTS : curr_page * MAX_RESULTS] if total > 0 else []

    pagination_payload = {
        "current_page": curr_page,
        "per_page": MAX_RESULTS,
        "page_size": MAX_RESULTS,
        "total_results": total,
        "total_pages": total_pages_cnt,
        "start_index": start_idx,
        "end_index": end_idx,
        "has_next": curr_page < total_pages_cnt,
        "has_prev": curr_page > 1,
        "ranked_jobs": [c.model_dump() if hasattr(c, "model_dump") else c.dict() for c in all_candidate_cards]
    }

    parts = [
        f"Results found: {total} matching candidate(s) (Page {curr_page} of {total_pages_cnt})",
        *compact_lines,
        "\n[MANDATORY AI INSTRUCTION — EMPLOYER CANDIDATE SEARCH]\n"
        "You are assisting an EMPLOYER searching for CANDIDATES/PROFILES to hire.\n"
        "The candidate results are ALREADY displayed as interactive candidate cards directly below your message.\n"
        "Write 1 or 2 short framing sentences introducing the candidate matches. You MUST NOT:\n"
        "- list, number, or format job listings or job roles\n"
        "- say 'To apply, visit collarcheck.com/jobs'\n"
        "- list candidate details in the text body\n"
        "Good example: 'I found several candidates matching your requirements. Here are the top matches:'"
    ]
    db_context_str = "\n".join(parts)

    try:
        from backend.services.chip_generator import generate_suggestion_chips
    except ImportError:
        from services.chip_generator import generate_suggestion_chips  # type: ignore
    employer_chips = generate_suggestion_chips("employer", reqs, total, user_location=user_location)

    orch_res = OrchestrationResult(
        role="employer",
        route_branch="SEARCH",
        db_context=db_context_str,
        results=all_candidate_cards,
        result_type="candidates",
        allowed_ids=allowed_ids,
        raw_hits=raw_hits,
        pagination=pagination_payload,
        search_metadata=metadata.dict() if metadata else None,
        requirements=reqs,
        suggestion_chips=employer_chips,
    )
    return compose_final_response(orch_res)





