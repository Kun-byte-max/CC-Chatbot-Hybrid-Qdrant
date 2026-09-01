from typing import Optional
import logging

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

async def handle_employer_context(last_user_msg: str, session_id: str, messages: list = None, user_location: str = None, compose_mode: str = "dual") -> Optional[dict]:
    ran, reason = should_run_search_parse_safe("employer", last_user_msg, session_id=session_id)
    log.info("gate_decision parser=search_parse ran=%s reason=%s msg_len=%d role=employer", ran, reason, len(last_user_msg or ""))
    set_gate_flags(search_parse_ran=ran)

    if not ran:
        set_decision_flags(search_query_found=False, search_executed=False)
        return None

    try:
        from backend.utils.session_state import get_employer_search_context, set_employer_search_context
        from backend.services.employer_search_parser import parse_employer_query
        from backend.services.employer_search_executor import execute_employer_candidate_search
        from backend.schemas.employer_requirements import EmployerSearchRequirements
    except ModuleNotFoundError:
        from utils.session_state import get_employer_search_context, set_employer_search_context  # type: ignore
        from services.employer_search_parser import parse_employer_query  # type: ignore
        from services.employer_search_executor import execute_employer_candidate_search  # type: ignore
        from schemas.employer_requirements import EmployerSearchRequirements  # type: ignore

    # 1. Load existing isolated employer search context
    existing_ctx_dict = get_employer_search_context(session_id)
    existing_reqs = None
    if existing_ctx_dict:
        try:
            existing_reqs = EmployerSearchRequirements.parse_obj(existing_ctx_dict)
        except Exception as e:
            log.warning(f"Failed to parse existing employer requirements: {e}")

    # 2. Parse query into canonical EmployerSearchRequirements with provenance tracking
    reqs = parse_employer_query(last_user_msg, existing_requirements=existing_reqs)

    # Check if query contains employer search intent
    is_intent = bool(reqs.role and reqs.role.value) or bool(reqs.location) or bool(reqs.experience) or bool(reqs.skills and reqs.skills.value) or bool(reqs.fresher)
    set_decision_flags(search_query_found=is_intent)

    if not is_intent and not reqs.safe_failure:
        set_decision_flags(search_executed=False)
        return None

    set_decision_flags(search_executed=True)

    # Save updated employer search context
    set_employer_search_context(session_id, reqs.dict())

    # 3. Execute Candidate Search (Fetch up to 100 candidates to support client & server pagination)
    candidates, metadata, raw_payload = await execute_employer_candidate_search(
        requirements=reqs,
        session_id=session_id,
        page=1,
        limit=100,
    )

    if reqs.safe_failure:
        return {
            "db_context": f"## SEARCH STATUS\nFailed to apply requested filter: {reqs.failure_reason}",
            "results": None,
            "result_type": None,
            "allowed_ids": set(),
            "search_metadata": metadata.dict(),
            "requirements": reqs,
        }

    raw_hits = raw_payload.get("raw_hits", [])
    if not raw_hits:
        return {
            "db_context": f"Found 0 matches for candidates.",
            "results": None,
            "result_type": None,
            "allowed_ids": set(),
            "search_metadata": metadata.dict(),
            "requirements": reqs,
        }

    try:
        from backend.schemas.schemas import CandidateCard
    except ModuleNotFoundError:
        from schemas.schemas import CandidateCard  # type: ignore

    candidate_cards = []
    allowed_ids = set()
    compact_lines = []

    for i, h in enumerate(raw_hits, 1):
        try:
            cand_id = int(h.get("id") or h.get("user_id") or h.get("ccid") or i)
        except Exception:
            cand_id = i
        allowed_ids.add(cand_id)

        first_name = (h.get("first_name") or "").strip()
        last_name = (h.get("last_name") or "").strip()
        full_name_raw = (h.get("full_name") or "").strip()
        if first_name or last_name:
            name = f"{first_name} {last_name}".strip()
        elif full_name_raw:
            name = full_name_raw
        elif h.get("slug"):
            slug_parts = h["slug"].split("-")
            clean_parts = [p.capitalize() for p in slug_parts if p and not p.lower().startswith("cce")]
            name = " ".join(clean_parts) or f"Candidate {cand_id}"
        else:
            name = f"Candidate {cand_id}"

        pos = h.get("current_position_name") or h.get("designation") or "Candidate"
        city_raw = h.get("city_name")
        state_raw = h.get("state_name")
        city = city_raw if (city_raw and city_raw != "N/A") else ""
        state = state_raw if (state_raw and state_raw != "N/A") else ""
        loc_parts = [p for p in (city, state) if p]
        loc_str = ", ".join(loc_parts) or "Location not specified"

        exp = h.get("total_experience_years") or 0.0
        if exp == 0.0:
            exp_str = "Fresher (0 years)"
        elif exp > 0 and exp < 1.0:
            months = int(round(exp * 12))
            exp_str = f"{months} month{'s' if months != 1 else ''}"
        else:
            exp_str = f"{round(exp, 1)} year{'s' if round(exp, 1) != 1.0 else ''}"
            
        sk_list = h.get("skill_names") or h.get("skills") or []
        slug = h.get("slug") or str(cand_id)
        cand_url = f"https://www.collarcheck.com/candidate-details/{slug}"
        match_reason = f"{exp_str} | {loc_str}".strip(" | ")

        cand_card = CandidateCard(
            cc_id=str(cand_id),
            name=name,
            position=pos,
            city=city or None,
            state=state or None,
            experience_label=exp_str,
            skills=sk_list if isinstance(sk_list, list) else [str(sk_list)],
            url=cand_url,
            rating=h.get("star_rating_avg"),
            match_reason=match_reason,
        )
        candidate_cards.append(cand_card)
        if i <= MAX_RESULTS:
            compact_lines.append(f"{i}. {name} - {pos} - {match_reason}")

    total = metadata.total_matches
    import math
    total_pages_cnt = max(1, math.ceil(total / MAX_RESULTS))

    pagination_payload = {
        "current_page": 1,
        "per_page": MAX_RESULTS,
        "page_size": MAX_RESULTS,
        "total_results": total,
        "total_pages": total_pages_cnt,
        "start_index": 1 if total > 0 else 0,
        "end_index": min(MAX_RESULTS, total),
        "has_next": 1 < total_pages_cnt,
        "has_prev": False,
        "ranked_jobs": [c.model_dump() if hasattr(c, "model_dump") else c.dict() for c in candidate_cards]
    }

    if compose_mode in ("structured", "dual"):
        parts = []
        parts.append(f"Results found: {total} candidates")
        parts.extend(compact_lines)
        parts.append(
            "\n[MANDATORY AI INSTRUCTION]\n"
            "The user's results are displayed as cards below your message. "
            "Write one or two sentences of framing. Do not list the results, do not repeat titles, salaries, or links."
        )
        db_context_str = "\n".join(parts)
    else:
        parts = []
        parts.append("## COLLARCHECK CANDIDATE SEARCH RESULTS")
        parts.append(f"found {total} matches, showing top {min(len(raw_hits), MAX_RESULTS)}")
        parts.append("")
        for i, h in enumerate(raw_hits[:MAX_RESULTS], 1):
            name = h.get("full_name") or "Candidate"
            cc_id = h.get("ccid") or "N/A"
            pos = h.get("current_position_name") or "N/A"
            city = h.get("city_name") or "N/A"
            state = h.get("state_name") or "N/A"
            exp = h.get("total_experience_years") or 0.0
            if exp == 0.0:
                exp_str = "Fresher (0 years)"
            elif exp > 0 and exp < 1.0:
                months = int(round(exp * 12))
                exp_str = f"{months} month{'s' if months != 1 else ''}"
            else:
                exp_str = f"{round(exp, 1)} year{'s' if round(exp, 1) != 1.0 else ''}"
            h_skills = ", ".join(h.get("skill_names", []))
            rating = h.get("star_rating_avg") or 0.0
            parts.append(
                f"CANDIDATE {i}:\n"
                f"  Name: {name}\n"
                f"  CC ID: {cc_id}\n"
                f"  Role/Position: {pos}\n"
                f"  Location: {city}, {state}\n"
                f"  Experience: {exp_str}\n"
                f"  Skills: {h_skills}\n"
                f"  Star Rating: ⭐ {rating} / 5.0\n"
                f"  Link: [View Candidate Profile](https://www.collarcheck.com/candidate-details/{h.get('slug')})\n"
            )
        parts.append(
            "\n[MANDATORY AI INSTRUCTION]\n"
            f"The candidate search returned {total} candidate(s). You MUST:\n"
            f"1. Start with: 'I found {total} candidate(s) matching your criteria.'\n"
            "2. List complete details for ALL top candidates provided above (Name, CC ID, Role/Position, Location, Experience, Star Rating, Skills, and ALWAYS include the clickable markdown link: [View Candidate Profile](URL) for EACH candidate).\n"
            "3. Present EVERY candidate in full without cutting off or omitting candidate profile links.\n"
            "4. Present the candidates in a clear numbered or bulleted list format. DO NOT use a table format.\n"
            "5. NEVER show or guess private information (emails, phone numbers, salaries).\n"
            "6. Offer to refine the search by location, experience, or skills.\n"
        )
        db_context_str = "\n".join(parts)

    return {
        "db_context": db_context_str,
        "results": candidate_cards if compose_mode in ("dual", "structured") else None,
        "result_type": "candidates" if compose_mode in ("dual", "structured") else None,
        "allowed_ids": allowed_ids,
        "raw_hits": raw_hits,
        "pagination": pagination_payload,
        "search_metadata": metadata.dict(),
        "requirements": reqs,
    }


