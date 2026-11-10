import re
import logging
from typing import Dict, Any, Optional, List, Union, Literal
from pydantic import BaseModel, Field

try:
    from backend.schemas.search_query import SearchTarget
    from backend.schemas.search_criteria import SearchCriteria
    from backend.schemas.conversation_context import ConversationContext
    from backend.utils.session_state import (
        get_conversation_context,
        get_conversation_context_async,
        get_recommendation_session,
        get_session_state,
        get_session_state_async,
        make_composite_key,
    )
except ModuleNotFoundError:
    from schemas.search_query import SearchTarget  # type: ignore
    from schemas.search_criteria import SearchCriteria  # type: ignore
    from schemas.conversation_context import ConversationContext  # type: ignore
    from utils.session_state import (  # type: ignore
        get_conversation_context,
        get_conversation_context_async,
        get_recommendation_session,
        get_session_state,
        get_session_state_async,
        make_composite_key,
    )

log = logging.getLogger(__name__)

ORDINAL_MAP = {
    "first": 1, "1st": 1,
    "second": 2, "2nd": 2,
    "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4,
    "fifth": 5, "5th": 5,
    "sixth": 6, "6th": 6,
    "seventh": 7, "7th": 7,
    "eighth": 8, "8th": 8,
    "ninth": 9, "9th": 9,
    "tenth": 10, "10th": 10,
}

ORDINAL_REGEX = re.compile(
    r"\b(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|sixth|6th|seventh|7th|eighth|8th|ninth|9th|tenth|10th)\b",
    re.IGNORECASE,
)

NUMERIC_RANK_REGEX = re.compile(
    r"\b(?:job|company|position|result|option|item|#)\s*#?\s*(\d+)\b",
    re.IGNORECASE,
)

PRONOUN_REGEX = re.compile(
    r"\b(?:that\s+one|this\s+one|the\s+job|that\s+job|this\s+job|the\s+company|that\s+company|this\s+company|the\s+role|it|its|their)\b",
    re.IGNORECASE,
)

SALARY_QUERY_REGEX = re.compile(
    r"\b(?:salary|pay|compensation|ctc|package|stipend|how\s+much\s+does\s+it\s+pay|what\s+does\s+it\s+pay)\b",
    re.IGNORECASE,
)

COMPANY_INFO_REGEX = re.compile(
    r"\b(?:tell\s+me\s+about\s+(?:the\s+)?(?:second\s+|first\s+|third\s+|fourth\s+|fifth\s+|that\s+|this\s+)?company|about\s+the\s+company|company\s+details|who\s+is\s+the\s+employer|what\s+is\s+(?:the\s+)?company)\b",
    re.IGNORECASE,
)

JOBS_AT_COMPANY_REGEX = re.compile(
    r"\b(?:jobs?\s+(?:at|in|with|for)|openings?\s+(?:at|in|with|for)|find\s+jobs?\s+(?:at|in|with|for)|roles?\s+(?:at|in|with|for))\b",
    re.IGNORECASE,
)

DETAIL_REGEX = re.compile(
    r"\b(?:tell\s+me\s+more|more\s+about|details?\s+about|requirements?\s+for|what\s+about|how\s+about|description|overview)\b",
    re.IGNORECASE,
)

COMPARE_REGEX = re.compile(
    r"\b(?:compare|comparison|difference|versus|vs)\b",
    re.IGNORECASE,
)

REMOTE_FILTER_REGEX = re.compile(
    r"\b(?:only\s+)?(?:remote|wfh|work\s+from\s+home|home\s+based)\b",
    re.IGNORECASE,
)

OFFICE_FILTER_REGEX = re.compile(
    r"\b(?:only\s+)?(?:office|onsite|on-site|work\s+from\s+office)\b",
    re.IGNORECASE,
)

HYBRID_FILTER_REGEX = re.compile(
    r"\b(?:only\s+)?(?:hybrid)\b",
    re.IGNORECASE,
)

SENIORITY_FILTER_REGEX = re.compile(
    r"\b(?:only\s+show\s+|only\s+)?(senior|junior|lead|principal|fresher|entry[\s-]*level)\b",
    re.IGNORECASE,
)

EXPERIENCE_FILTER_REGEX = re.compile(
    r"\b(?:with\s+)?(\d+)\s*(?:\+|\s*to\s*(\d+))?\s*(?:years?|yrs?)(?:\s+experience)?\b",
    re.IGNORECASE,
)


def _extract_job_attrs(job_item: Any) -> tuple[Optional[Union[str, int]], str, str]:
    """Helper to cleanly extract job_id, title, and company from a dict or object."""
    if not job_item:
        return None, "Job", "Company"

    j_id = None
    if isinstance(job_item, dict):
        j_id = job_item.get("id") or job_item.get("job_id")
        raw = job_item.get("raw_data") if isinstance(job_item.get("raw_data"), dict) else job_item
        title = raw.get("job_title") or job_item.get("title") or "Job"
        company = raw.get("company_name") or raw.get("company") or job_item.get("company") or "Company"
        return j_id, str(title), str(company)

    # Object / RecommendationResult
    j_id = getattr(job_item, "job_id", None) or getattr(job_item, "id", None)
    raw = getattr(job_item, "raw_data", {}) if hasattr(job_item, "raw_data") else {}
    title = (
        raw.get("job_title")
        or (getattr(job_item.title_result, "job_title", None) if hasattr(job_item, "title_result") else None)
        or getattr(job_item, "title", "Job")
    )
    company = raw.get("company_name") or raw.get("company") or "Company"
    return j_id, str(title), str(company)


class EntityRef(BaseModel):
    """Represents a validated or attempted entity reference."""
    entity_type: Literal["job", "company", "candidate", "employment_record", "unknown"] = "unknown"
    target_ordinal: Optional[int] = None
    resolved_id: Optional[Union[str, int]] = None
    entity_name: Optional[str] = None
    raw_reference: Optional[str] = None
    is_valid: bool = False
    validation_error: Optional[str] = None


class QueryRewriterResult(BaseModel):
    """
    Structured, typed output of the QueryRewriterService.
    Preserves original user message while supplying contextual resolution and routing metadata.
    """
    original_query: str
    resolved_query: str
    target_intent: Literal[
        "NEW_SEARCH",
        "FILTER_REFINEMENT",
        "JOB_DETAIL",
        "JOB_COMPARE",
        "COMPANY_INFO",
        "KNOWLEDGE_RAG",
        "EMPLOYMENT_DISAMBIGUATION",
        "PROFILE_UPDATE",
        "SELF_CONTAINED",
        "UNKNOWN"
    ]
    is_followup: bool = False
    entity_refs: List[EntityRef] = Field(default_factory=list)
    filter_delta: Dict[str, Any] = Field(default_factory=dict)
    active_filters_preserved: Dict[str, Any] = Field(default_factory=dict)
    requires_search: bool = False
    needs_clarification: bool = False
    clarification_message: Optional[str] = None
    fallback_to_original: bool = False
    search_generation: int = 1


class QueryRewriterService:
    """
    Dedicated Context-Aware Query Reformulation Service.
    Resolves conversational follow-ups deterministically using structured session state.
    Keeps LLM optional, strictly bounded, and side-effect free.
    """

    @classmethod
    def parse_ordinal(cls, text: str) -> Optional[int]:
        """Extracts 1-indexed ordinal from text if present."""
        if not text:
            return None
        m_word = ORDINAL_REGEX.search(text)
        if m_word:
            w = m_word.group(1).lower()
            if w in ORDINAL_MAP:
                return ORDINAL_MAP[w]
        m_num = NUMERIC_RANK_REGEX.search(text)
        if m_num:
            try:
                val = int(m_num.group(1))
                if val > 0:
                    return val
            except ValueError:
                pass
        return None

    @classmethod
    async def rewrite_query(
        cls,
        user_message: str,
        messages_history: Optional[List[Dict[str, Any]]] = None,
        session_id: Optional[str] = None,
        role: str = "employee",
        user_id: Optional[Union[str, int]] = None,
        conversation_context: Optional[ConversationContext] = None,
        recommendation_session: Optional[Dict[str, Any]] = None,
    ) -> QueryRewriterResult:
        """
        Asynchronous variant of resolve_query for async request paths.
        Awaits non-blocking get_conversation_context_async and get_session_state_async.
        """
        raw_msg = (user_message or "").strip()

        # 1. Load canonical ConversationContext snapshot asynchronously if not provided
        conv_ctx = conversation_context
        if conv_ctx is None and session_id:
            try:
                conv_ctx = await get_conversation_context_async(session_id, role=role, user_id=user_id)
            except Exception as e:
                log.warning("QueryRewriterService failed to load conversation context: %s", e)

        # 2. Check pending employment selection precedence
        pending_opt_map = conv_ctx.pending_option_map if conv_ctx else None
        if not pending_opt_map and session_id:
            try:
                k = make_composite_key(session_id, role, user_id=user_id)
                st = await get_session_state_async(k, user_id=user_id)
                raw_pm = st.get("pending_option_mapping")
                if isinstance(raw_pm, dict):
                    pending_opt_map = raw_pm.get("mapping") if isinstance(raw_pm.get("mapping"), dict) else raw_pm
            except Exception:
                pass

        # Delegate resolution logic with loaded conv_ctx
        return cls.resolve_query(
            user_message=user_message,
            messages_history=messages_history,
            session_id=session_id,
            role=role,
            user_id=user_id,
            conversation_context=conv_ctx,
            recommendation_session=recommendation_session,
        )

    @classmethod
    def resolve_query(
        cls,
        user_message: str,
        messages_history: Optional[List[Dict[str, Any]]] = None,
        session_id: Optional[str] = None,
        role: str = "employee",
        user_id: Optional[Union[str, int]] = None,
        conversation_context: Optional[ConversationContext] = None,
        recommendation_session: Optional[Dict[str, Any]] = None,
    ) -> QueryRewriterResult:
        """
        Pure, deterministic resolver for conversational queries.
        Inspects session state, verifies entity references against active snapshots,
        detects filter refinements, and preserves canonical original intent.
        """
        raw_msg = (user_message or "").strip()
        msg_l = raw_msg.lower()

        # 1. Load canonical ConversationContext snapshot if not provided
        conv_ctx = conversation_context
        if conv_ctx is None and session_id:
            try:
                conv_ctx = get_conversation_context(session_id, role=role, user_id=user_id)
            except Exception as e:
                log.warning("QueryRewriterService failed to load conversation context: %s", e)

        current_gen = conv_ctx.search_generation if conv_ctx else 1

        # 2. Check pending employment selection precedence (Step 5A / User Data Service)
        # Pending employment-selection options must never be confused with job results
        pending_opt_map = conv_ctx.pending_option_map if conv_ctx else None
        if not pending_opt_map and session_id:
            # Fall back to raw state check for pending_option_mapping
            try:
                k = make_composite_key(session_id, role, user_id=user_id)
                st = get_session_state(k, user_id=user_id)
                raw_pm = st.get("pending_option_mapping")
                if isinstance(raw_pm, dict):
                    pending_opt_map = raw_pm.get("mapping") if isinstance(raw_pm.get("mapping"), dict) else raw_pm
            except Exception:
                pass

        if pending_opt_map:
            is_emp_rec = bool(re.search(r"\bemp_rec_\d+\b", msg_l))
            ord_idx = cls.parse_ordinal(msg_l)
            is_pure_digit = msg_l.isdigit() and 1 <= int(msg_l) <= len(pending_opt_map)
            is_company_ref = "company" in msg_l or "employer" in msg_l or "verification" in msg_l

            if is_emp_rec or is_pure_digit or (ord_idx and (is_company_ref or ord_idx <= len(pending_opt_map))):
                chosen_idx = int(msg_l) if is_pure_digit else ord_idx
                matched_target = f"emp_rec_{chosen_idx}" if chosen_idx else (re.search(r"\bemp_rec_\d+\b", msg_l).group(0) if is_emp_rec else None)
                ref = EntityRef(
                    entity_type="employment_record",
                    target_ordinal=chosen_idx,
                    resolved_id=matched_target,
                    is_valid=True if ((chosen_idx and chosen_idx <= len(pending_opt_map)) or is_emp_rec) else False,
                )
                return QueryRewriterResult(
                    original_query=raw_msg,
                    resolved_query=raw_msg,
                    target_intent="EMPLOYMENT_DISAMBIGUATION",
                    is_followup=True,
                    entity_refs=[ref],
                    search_generation=current_gen,
                    requires_search=False,
                    fallback_to_original=False,
                )

        # 3. Load active Recommendation Session
        rec_session = recommendation_session
        if rec_session is None and session_id:
            try:
                rec_session = get_recommendation_session(session_id, role=role, user_id=user_id)
            except Exception as e:
                log.warning("QueryRewriterService failed to load recommendation session: %s", e)

        ranked_jobs = rec_session.get("ranked_jobs", []) if (rec_session and isinstance(rec_session, dict)) else []
        total_active_jobs = len(ranked_jobs)
        last_ref_job_id = (
            rec_session.get("last_referenced_job_id") if (rec_session and isinstance(rec_session, dict))
            else (conv_ctx.last_referenced_job_id if conv_ctx else None)
        )

        # 4. Check for Comparison Intent across multiple entities
        if COMPARE_REGEX.search(msg_l):
            ordinals_found = []
            for w, val in ORDINAL_MAP.items():
                if re.search(r"\b" + re.escape(w) + r"\b", msg_l):
                    if val not in ordinals_found:
                        ordinals_found.append(val)
            for m in NUMERIC_RANK_REGEX.finditer(msg_l):
                try:
                    num_val = int(m.group(1))
                    if num_val not in ordinals_found:
                        ordinals_found.append(num_val)
                except ValueError:
                    pass
            ordinals_found.sort()

            if len(ordinals_found) >= 2:
                refs = []
                for o in ordinals_found:
                    is_valid = (1 <= o <= total_active_jobs)
                    resolved_id = None
                    e_name = None
                    if is_valid:
                        job_item = ranked_jobs[o - 1]
                        resolved_id, j_title, j_comp = _extract_job_attrs(job_item)
                        e_name = f"{j_title} at {j_comp}"
                    refs.append(EntityRef(
                        entity_type="job",
                        target_ordinal=o,
                        resolved_id=resolved_id,
                        entity_name=e_name,
                        is_valid=is_valid,
                        validation_error=None if is_valid else f"Requested rank #{o} is out of bounds (Available: 1–{total_active_jobs})"
                    ))
                
                has_oob = any(not r.is_valid for r in refs)
                needs_clarification = (total_active_jobs == 0) or has_oob
                clarification_msg = None
                if total_active_jobs == 0:
                    clarification_msg = "I don't have a recent job recommendation list to refer to."
                elif has_oob:
                    clarification_msg = f"Cannot compare jobs because one or more requested ranks are out of bounds (Available: 1–{total_active_jobs})."

                return QueryRewriterResult(
                    original_query=raw_msg,
                    resolved_query=raw_msg,
                    target_intent="JOB_COMPARE",
                    is_followup=True,
                    entity_refs=refs,
                    search_generation=current_gen,
                    requires_search=False,
                    needs_clarification=needs_clarification,
                    clarification_message=clarification_msg,
                )

        # 5. Check for Ordinal or Pronoun Job/Company Reference
        ordinal = cls.parse_ordinal(msg_l)
        has_pronoun = bool(PRONOUN_REGEX.search(msg_l))
        has_salary_intent = bool(SALARY_QUERY_REGEX.search(msg_l))
        is_company_query = bool(COMPANY_INFO_REGEX.search(msg_l)) or ("company" in msg_l and not JOBS_AT_COMPANY_REGEX.search(msg_l) and (ordinal is not None or has_pronoun))
        is_jobs_at_company = bool(JOBS_AT_COMPANY_REGEX.search(msg_l)) and (ordinal is not None or has_pronoun)

        # If user refers to an ordinal or pronoun
        if ordinal is not None or has_pronoun:
            # Case A: Ordinal specified (e.g. "What is the salary of the third job?", "Tell me about the second company")
            if ordinal is not None:
                if total_active_jobs == 0:
                    return QueryRewriterResult(
                        original_query=raw_msg,
                        resolved_query=raw_msg,
                        target_intent="UNKNOWN",
                        is_followup=True,
                        entity_refs=[EntityRef(entity_type="job", target_ordinal=ordinal, is_valid=False, validation_error="No active results")],
                        search_generation=current_gen,
                        requires_search=False,
                        needs_clarification=True,
                        clarification_message="I don't have a recent job recommendation list to refer to. Please perform a job search first.",
                    )

                if ordinal < 1 or ordinal > total_active_jobs:
                    # Invariant: Out-of-range ordinal must never silently resolve to another entity
                    return QueryRewriterResult(
                        original_query=raw_msg,
                        resolved_query=raw_msg,
                        target_intent="UNKNOWN",
                        is_followup=True,
                        entity_refs=[EntityRef(entity_type="job", target_ordinal=ordinal, is_valid=False, validation_error=f"Ordinal {ordinal} out of range (1–{total_active_jobs})")],
                        search_generation=current_gen,
                        requires_search=False,
                        needs_clarification=True,
                        clarification_message=f"I don't have a position #{ordinal} in the current results (Available: 1–{total_active_jobs}).",
                    )

                # Valid ordinal
                target_job = ranked_jobs[ordinal - 1]
                target_job_id, target_title, target_company = _extract_job_attrs(target_job)

                # Branch A1: Explicit "Jobs at this company"
                if is_jobs_at_company:
                    resolved_q = f"jobs at {target_company}"
                    return QueryRewriterResult(
                        original_query=raw_msg,
                        resolved_query=resolved_q,
                        target_intent="NEW_SEARCH",
                        is_followup=True,
                        entity_refs=[EntityRef(entity_type="company", target_ordinal=ordinal, resolved_id=target_job_id, entity_name=target_company, is_valid=True)],
                        filter_delta={"company": target_company},
                        search_generation=current_gen,
                        requires_search=True,
                    )

                # Branch A2: Company Info request
                if is_company_query:
                    resolved_q = f"information about company {target_company}"
                    return QueryRewriterResult(
                        original_query=raw_msg,
                        resolved_query=resolved_q,
                        target_intent="COMPANY_INFO",
                        is_followup=True,
                        entity_refs=[EntityRef(entity_type="company", target_ordinal=ordinal, resolved_id=target_job_id, entity_name=target_company, is_valid=True)],
                        search_generation=current_gen,
                        requires_search=False,
                    )

                # Branch A3: Job Detail / Salary
                resolved_q = f"{target_title} at {target_company}"
                if has_salary_intent:
                    resolved_q = f"salary for {target_title} at {target_company}"
                return QueryRewriterResult(
                    original_query=raw_msg,
                    resolved_query=resolved_q,
                    target_intent="JOB_DETAIL",
                    is_followup=True,
                    entity_refs=[EntityRef(entity_type="job", target_ordinal=ordinal, resolved_id=target_job_id, entity_name=f"{target_title} at {target_company}", is_valid=True)],
                    search_generation=current_gen,
                    requires_search=False,
                )

            # Case B: Pronoun without explicit ordinal ("What is its salary?", "Tell me more about that job")
            if has_pronoun:
                if total_active_jobs == 0:
                    return QueryRewriterResult(
                        original_query=raw_msg,
                        resolved_query=raw_msg,
                        target_intent="UNKNOWN",
                        is_followup=True,
                        search_generation=current_gen,
                        requires_search=False,
                        needs_clarification=True,
                        clarification_message="I don't have a recent job recommendation list to refer to.",
                    )

                # If there is a uniquely identified last_referenced_job_id, use it
                resolved_ref_job = None
                if last_ref_job_id is not None:
                    for idx, j in enumerate(ranked_jobs):
                        j_id, _, _ = _extract_job_attrs(j)
                        if str(j_id) == str(last_ref_job_id):
                            resolved_ref_job = (idx + 1, j)
                            break

                # If only 1 job in result pool, uniquely unambiguous
                if resolved_ref_job is None and total_active_jobs == 1:
                    resolved_ref_job = (1, ranked_jobs[0])

                if resolved_ref_job is not None:
                    matched_ord, matched_j = resolved_ref_job
                    t_id, t_title, t_comp = _extract_job_attrs(matched_j)

                    if is_jobs_at_company:
                        resolved_q = f"jobs at {t_comp}"
                        return QueryRewriterResult(
                            original_query=raw_msg,
                            resolved_query=resolved_q,
                            target_intent="NEW_SEARCH",
                            is_followup=True,
                            entity_refs=[EntityRef(entity_type="company", target_ordinal=matched_ord, resolved_id=t_id, entity_name=t_comp, is_valid=True)],
                            filter_delta={"company": t_comp},
                            search_generation=current_gen,
                            requires_search=True,
                        )

                    if is_company_query:
                        resolved_q = f"information about company {t_comp}"
                        return QueryRewriterResult(
                            original_query=raw_msg,
                            resolved_query=resolved_q,
                            target_intent="COMPANY_INFO",
                            is_followup=True,
                            entity_refs=[EntityRef(entity_type="company", target_ordinal=matched_ord, resolved_id=t_id, entity_name=t_comp, is_valid=True)],
                            search_generation=current_gen,
                            requires_search=False,
                        )

                    resolved_q = f"salary for {t_title} at {t_comp}" if has_salary_intent else f"{t_title} at {t_comp}"
                    return QueryRewriterResult(
                        original_query=raw_msg,
                        resolved_query=resolved_q,
                        target_intent="JOB_DETAIL",
                        is_followup=True,
                        entity_refs=[EntityRef(entity_type="job", target_ordinal=matched_ord, resolved_id=t_id, entity_name=f"{t_title} at {t_comp}", is_valid=True)],
                        search_generation=current_gen,
                        requires_search=False,
                    )

                # Ambiguous pronoun when multiple jobs exist and no recent single job was referenced
                return QueryRewriterResult(
                    original_query=raw_msg,
                    resolved_query=raw_msg,
                    target_intent="UNKNOWN",
                    is_followup=True,
                    search_generation=current_gen,
                    requires_search=False,
                    needs_clarification=True,
                    clarification_message="Which job from your search results are you referring to? Please specify the position or rank number.",
                )

        # 6. Check for Filter Refinements (e.g. "Only show remote roles", "In Mumbai", "Only senior roles")
        active_criteria = conv_ctx.active_search_criteria if conv_ctx else None
        if not active_criteria and session_id:
            try:
                # Try composite user key
                k_user = make_composite_key(session_id, role, user_id=user_id)
                st_user = get_session_state(k_user, user_id=user_id)
                active_criteria = st_user.get("search_context") or st_user.get("employer_search_context")
            except Exception:
                pass

        has_active_search = bool(conv_ctx and conv_ctx.has_active_search_context) or bool(active_criteria)

        # Detect delta filter candidates
        is_remote_refine = bool(REMOTE_FILTER_REGEX.search(msg_l))
        is_office_refine = bool(OFFICE_FILTER_REGEX.search(msg_l))
        is_hybrid_refine = bool(HYBRID_FILTER_REGEX.search(msg_l))
        seniority_match = SENIORITY_FILTER_REGEX.search(msg_l)
        exp_match = EXPERIENCE_FILTER_REGEX.search(msg_l)

        # Explicit new search trigger guard
        is_explicit_brand_new_search = bool(
            re.search(r"\b(?:search\s+for|find\s+jobs?|look\s+for\s+jobs?|new\s+search)\b", msg_l)
            and re.search(r"\b(?:developer|engineer|manager|accountant|analyst|designer|sales|nurse|intern|internship)\b", msg_l)
        )

        if has_active_search and not is_explicit_brand_new_search and (is_remote_refine or is_office_refine or is_hybrid_refine or seniority_match or exp_match or msg_l.startswith("in ") or msg_l.startswith("only ")):
            filter_delta = {}
            active_filters = dict(active_criteria) if isinstance(active_criteria, dict) else {}

            if is_remote_refine:
                filter_delta["job_mode"] = "home"
                filter_delta["work_mode"] = "home"
            elif is_office_refine:
                filter_delta["job_mode"] = "office"
                filter_delta["work_mode"] = "office"
            elif is_hybrid_refine:
                filter_delta["job_mode"] = "hybrid"
                filter_delta["work_mode"] = "hybrid"

            if seniority_match:
                filter_delta["seniority_level"] = seniority_match.group(1).lower()

            if exp_match:
                try:
                    filter_delta["experience_min"] = int(exp_match.group(1))
                    if exp_match.group(2):
                        filter_delta["experience_max"] = int(exp_match.group(2))
                except (ValueError, TypeError):
                    pass

            # Construct contextual resolved query
            base_kw = active_filters.get("keyword") or active_filters.get("title") or ""
            base_loc = active_filters.get("location") or ""
            parts = [base_kw]
            if base_loc:
                parts.append(f"in {base_loc}")
            if "job_mode" in filter_delta:
                mode_str = "remote" if filter_delta["job_mode"] == "home" else filter_delta["job_mode"]
                parts.append(f"({mode_str})")
            if "seniority_level" in filter_delta:
                parts.insert(0, filter_delta["seniority_level"])

            resolved_query_str = " ".join(p for p in parts if p).strip() or raw_msg

            return QueryRewriterResult(
                original_query=raw_msg,
                resolved_query=resolved_query_str,
                target_intent="FILTER_REFINEMENT",
                is_followup=True,
                filter_delta=filter_delta,
                active_filters_preserved=active_filters,
                search_generation=current_gen,
                requires_search=True,
            )

        # 7. Default Self-Contained Query
        return QueryRewriterResult(
            original_query=raw_msg,
            resolved_query=raw_msg,
            target_intent="SELF_CONTAINED",
            is_followup=False,
            search_generation=current_gen,
            requires_search=False,
            fallback_to_original=True,
        )
