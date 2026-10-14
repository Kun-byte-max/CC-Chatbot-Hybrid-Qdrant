"""
user_data_formatter.py — Natural language response formatter for user-data and widget queries.

Phase 4 / Fix Scope:
  - Formats extracted JSON into clean, user-facing natural language text.
  - Complete support for LANGUAGES, PORTFOLIO, SALARY, CLOSING_SOON, NEARBY_ORGS, WIDGET_ALL.
  - Company-specific filtering for employment queries.
  - "Previous company" semantics resolution.
  - Zero raw JSON or credential leakage.
"""

import re
import logging
from typing import Dict, Any, List, Optional, Tuple, Union
from backend.services.user_data_intent_parser import UserIntent
from backend.services.user_data_extractor import FORBIDDEN_KEYS, scrub_sensitive_keys, is_truthy, is_current_role, is_previous_role

log = logging.getLogger(__name__)


def _is_safe_public_url(url: Optional[str]) -> bool:
    """
    Validates whether a URL is a safe, user-facing public resource URL.
    Filters out internal API endpoints, localhost/IP addresses, admin panels, and microservice paths.
    """
    if not url or not isinstance(url, str):
        return False

    u_lower = url.strip().lower()

    # Must start with http:// or https://
    if not (u_lower.startswith("http://") or u_lower.startswith("https://")):
        return False

    # Block internal IP addresses, localhost, and internal microservice domains
    internal_hosts = ("localhost", "127.0.0.1", "0.0.0.0", "10.", "192.168.", "172.16.")
    if any(host in u_lower for host in internal_hosts):
        return False

    # Block internal API/admin path patterns
    forbidden_patterns = ("/wapi/", "/api/", "/v1/", "/v2/", "/admin/", "/internal/", "admin.collarcheck.com", ":8000", ":8001", ":8080", ":9000")
    if any(pat in u_lower for pat in forbidden_patterns):
        return False

    return True


def _parse_year_from_date(date_val: Any) -> Optional[int]:
    """Extracts a 4-digit year integer from various date format representations."""
    if not date_val:
        return None
    s = str(date_val).strip()
    match = re.search(r"\b(19\d\d|20\d\d)\b", s)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass
    return None


def _extract_company_name_from_query(
    query: str,
    available_companies: List[str],
    records: Optional[List[Dict[str, Any]]] = None
) -> Optional[str]:
    """
    Matches company names mentioned in prompt using deterministic matching hierarchy:
    1. Exact full containment match (e.g. prompt contains "Google LLC")
    2. Unambiguous partial match (single company matching token from prompt e.g. "Google" -> "Google LLC")
    3. Multiple partial matches -> Ambiguous; do not arbitrarily select one (returns None)
    4. Relative reference resolution ("previous", "last", "recent" company using verified dates)
    5. Explicit unlisted company extraction
    """
    if not query:
        return None

    q_lower = query.lower()

    # 1. Exact full containment match (e.g. prompt explicitly contains "Google LLC")
    if available_companies:
        exact_matches = [comp for comp in available_companies if comp and comp.lower() in q_lower]
        if len(exact_matches) == 1:
            return exact_matches[0]
        elif len(exact_matches) > 1:
            exact_matches.sort(key=len, reverse=True)
            return exact_matches[0]

    # 2. Extract explicit company token from query (e.g. "Google" from "salary at Google" or "is Google claimed")
    match = re.search(r"\b(?:at|for|about|with|company|is|whether)\s+([A-Za-z0-9_-]+)\s*(?:claimed|unclaimed|company)?\b", query, re.IGNORECASE)
    extracted_token = match.group(1) if match else None

    # Fallback token extraction from non-stopword query terms if pattern not found
    if not extracted_token:
        stopwords = {"what", "were", "used", "tell", "show", "that", "this", "from", "with", "about", "your", "have", "skills", "salary", "level", "status", "verification", "check", "claimed", "unclaimed", "is", "a", "company", "whether", "if", "not", "pending", "next", "steps", "how", "do", "complete", "get", "view"}
        words = [w.strip() for w in re.findall(r"\b[A-Za-z0-9_-]{3,}\b", query) if w.lower() not in stopwords]
        for word in words:
            if available_companies:
                matching = [comp for comp in available_companies if comp and word.lower() in comp.lower()]
                if len(matching) == 1:
                    return matching[0]
                elif len(matching) > 1:
                    # Ambiguous: multiple companies match token -> do not arbitrarily pick one
                    return None

    if extracted_token and extracted_token.lower() not in {"my", "the", "a", "an", "this", "that", "your", "our", "his", "her", "their", "any", "all", "previous", "last", "recent", "latest", "level", "status", "verification", "check", "info", "details", "pending", "next", "steps", "how", "do", "complete", "get", "view"}:
        if available_companies:
            partial_matches = [comp for comp in available_companies if comp and extracted_token.lower() in comp.lower()]
            if len(partial_matches) == 1:
                return partial_matches[0]
            elif len(partial_matches) > 1:
                # Ambiguous: multiple companies match token -> do not arbitrarily pick one
                return None

        # Unlisted company explicitly requested in query
        return extracted_token

    # 3. Relative query resolution ("previous", "last", "prior", "recent", "latest" company)
    if re.search(r"\b(previous|last|prior|recent|latest)\s+company\b", q_lower):
        if records:
            prior_companies = []
            for idx, item in enumerate(records):
                if not isinstance(item, dict):
                    continue

                cname = item.get("company")
                if not cname:
                    continue

                roles = item.get("roles") if isinstance(item.get("roles"), list) else [item]

                is_active = False
                latest_year = 0

                for r in roles:
                    if not isinstance(r, dict):
                        continue

                    if is_current_role(r):
                        is_active = True

                    end_year = _parse_year_from_date(r.get("worked_till_date"))
                    start_year = _parse_year_from_date(r.get("joining_date"))
                    year_val = end_year or start_year or 0
                    if year_val > latest_year:
                        latest_year = year_val

                if not is_active:
                    prior_companies.append({
                        "company": cname,
                        "year": latest_year,
                        "original_index": idx
                    })

            if prior_companies:
                prior_companies.sort(key=lambda x: (x["year"], -x["original_index"]), reverse=True)
                return prior_companies[0]["company"]

            for item in reversed(records):
                if isinstance(item, dict) and item.get("company"):
                    return item.get("company")

        if available_companies:
            return available_companies[-1] if len(available_companies) > 1 else available_companies[0]

    return None


def _generate_record_composite_key(rec: Dict[str, Any], role_idx: int = 0) -> str:
    """Generates a stable composite identity for a specific employment role record."""
    if not isinstance(rec, dict):
        return ""
    cname = str(rec.get("company") or "").strip().lower()
    roles = rec.get("roles") if isinstance(rec.get("roles"), list) else [rec]
    r = roles[role_idx] if (roles and 0 <= role_idx < len(roles) and isinstance(roles[role_idx], dict)) else (roles[0] if roles and isinstance(roles[0], dict) else {})
    desig = str(r.get("designation") or r.get("position") or "").strip().lower()
    join_d = str(r.get("joining_date") or "").strip().lower()
    till_d = str(r.get("worked_till_date") or "").strip().lower()
    rid = str(r.get("id") if r.get("id") is not None else rec.get("id") or "").strip().lower()
    return f"{cname}::{desig}::{join_d}::{till_d}::{rid}"


def build_selection_metadata(
    history: List[Dict[str, Any]],
    prompt_text: str = "Which employment record would you like to check?",
    session_id: Optional[str] = None
) -> Dict[str, Any]:
    """Builds structured employment selection metadata for role-level disambiguation and saves session option mapping."""
    options = []
    option_mapping = {}
    sel_idx = 1

    for c in history:
        if not isinstance(c, dict):
            continue
        cname = c.get("company") or f"Company {sel_idx}"
        roles = c.get("roles") if isinstance(c.get("roles"), list) else [c]

        for r_idx, r in enumerate(roles):
            if not isinstance(r, dict):
                continue
            desig = r.get("designation") or r.get("position") or ""
            is_curr = is_current_role(r)
            status_str = "Current" if is_curr else "Previous"

            label = f"{cname} — {desig}" if (desig and len(roles) > 1) else (f"{cname} ({desig})" if desig else cname)
            desc = status_str

            sel_key = f"emp_rec_{sel_idx}"
            sel_idx += 1
            composite_key = _generate_record_composite_key(c, role_idx=r_idx)
            option_mapping[sel_key] = composite_key

            options.append({
                "selection_key": sel_key,
                "company_name": cname,
                "label": label,
                "description": desc,
                "record_id": str(r.get("id")) if r.get("id") is not None else None
            })

    if session_id and option_mapping:
        try:
            from backend.utils.session_state import set_session_option_mapping
            set_session_option_mapping(session_id, option_mapping)
        except Exception as e:
            log.warning("Failed to save session option mapping: %s", e)

    return {
        "type": "employment_selection",
        "prompt": prompt_text,
        "options": options
    }



def _classify_employment_records(history: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Classifies employment records into (current_records, previous_records, unknown_records)."""
    current_recs = []
    previous_recs = []
    unknown_recs = []
    from datetime import datetime
    current_year = datetime.now().year

    for r in history:
        if not isinstance(r, dict) or not r.get("company"):
            continue
        roles = r.get("roles") if isinstance(r.get("roles"), list) else [r]

        is_curr = any(isinstance(role, dict) and is_current_role(role) for role in roles)

        if is_curr:
            current_recs.append(r)
            continue

        # Check for positive proof of previous employment
        is_prev = any(isinstance(role, dict) and is_previous_role(role) for role in roles)

        if is_prev:
            previous_recs.append(r)
        else:
            unknown_recs.append(r)

    return (current_recs, previous_recs, unknown_recs)


_ACTIVE_TURN_CONSUMED_KEYS: Dict[str, str] = {}


def resolve_employment_selection_context(
    query: str,
    history: List[Dict[str, Any]],
    session_id: Optional[str] = None,
    consume_key: bool = True,
    requested_level: Optional[int] = None,
    is_generic_summary: bool = False
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], Optional[str]]:
    """
    Resolves employment selection context:
      Returns (filtered_history, selection_metadata, matched_company_name)
    """
    if not history or not isinstance(history, list):
        return ([], None, None)

    available_companies = []
    for comp in history:
        if isinstance(comp, dict) and comp.get("company"):
            c_name = str(comp.get("company")).strip()
            if c_name and c_name not in available_companies:
                available_companies.append(c_name)

    if not available_companies:
        return (history, None, None)

    q_lower = (query or "").lower()
    is_previous_query = bool(re.search(r"\b(previous|prior|past|former)\b", q_lower))
    is_other_query = bool(re.search(r"\b(other|another|different)\s+(company|employer|record|employment)\b|\bwhat\s+about\s+the\s+other\b", q_lower))
    key_match = re.search(r"\bemp_rec_(\d+)\b", q_lower)
    matched_company = _extract_company_name_from_query(query, available_companies, history) if query else None

    stored_company, stored_key = (None, None)
    if session_id:
        try:
            from backend.utils.session_state import get_selected_employment_context, get_selected_employment_record_key
            stored_company = get_selected_employment_context(session_id)
            stored_key = get_selected_employment_record_key(session_id)
        except Exception as e:
            log.warning("Failed to fetch stored employment context: %s", e)

    # Check if this is an overall generic summary query (no explicit company, role, level, or previous/other target)
    has_explicit_target = bool(
        requested_level is not None or
        key_match or
        matched_company or
        is_previous_query or
        is_other_query or
        stored_key or
        stored_company or
        len(available_companies) == 1
    )

    if is_generic_summary or not has_explicit_target:
        return (history, None, None)

    current_recs, previous_recs, unknown_recs = _classify_employment_records(history)


    # Handle direct selection key in query (e.g. emp_rec_1, emp_rec_2)
    key_match = re.search(r"\bemp_rec_(\d+)\b", q_lower)
    if key_match:
        submitted_key = key_match.group(0).lower()
        target_composite_key = None
        has_session_mapping = False

        if session_id:
            try:
                from backend.utils.session_state import get_session_option_mapping
                opt_map = get_session_option_mapping(session_id)
                target_composite_key = opt_map.get(submitted_key)
                has_session_mapping = True
            except Exception as e:
                log.warning("Failed to fetch session option mapping: %s", e)


        if target_composite_key:
            role_matches = []
            for c in history:
                if not isinstance(c, dict):
                    continue
                roles = c.get("roles") if isinstance(c.get("roles"), list) else [c]
                for r_idx, r in enumerate(roles):
                    comp_key = _generate_record_composite_key(c, role_idx=r_idx)
                    r_id_str = str(r.get("id")).strip().lower() if r.get("id") is not None else ""
                    if comp_key == target_composite_key or (r_id_str and target_composite_key.endswith(f"::{r_id_str}")):
                        role_matches.append((c, r, r_idx))

            if len(role_matches) == 1:
                target_comp, target_role, target_r_idx = role_matches[0]
                target_name = str(target_comp.get("company")).strip()
                target_key = _generate_record_composite_key(target_comp, role_idx=target_r_idx)
                single_role_entry = {**target_comp, "roles": [target_role]}
                if session_id:
                    try:
                        from backend.utils.session_state import set_selected_employment_context, consume_session_option_mapping
                        if consume_key:
                            consume_session_option_mapping(session_id, submitted_key)
                        set_selected_employment_context(session_id, target_name, record_key=target_key)
                    except Exception as e:
                        log.warning("Failed to save selected record key: %s", e)
                return ([single_role_entry], None, target_name)

            else:
                if session_id:
                    try:
                        from backend.utils.session_state import clear_selected_employment_context
                        clear_selected_employment_context(session_id)
                    except Exception as e:
                        log.warning("Failed to clear invalid selection context: %s", e)
                meta = build_selection_metadata(history, prompt_text="Your previous selection could not be verified against current records. Please select an option below:", session_id=session_id)
                return ([], meta, None)
        else:
            # Key mapping missing/consumed/expired or a replay attempt -> Clear selection & reject
            if session_id:
                try:
                    from backend.utils.session_state import clear_selected_employment_context
                    clear_selected_employment_context(session_id)
                except Exception as e:
                    log.warning("Failed to clear selection context: %s", e)
            meta = build_selection_metadata(history, prompt_text="That selection option is no longer valid. Please choose from the current options:", session_id=session_id)
            return ([], meta, None)

    # If "other company" or relative reference with multiple candidates, generate selection metadata
    if is_other_query and len(history) > 1:
        meta = build_selection_metadata(history, prompt_text="Which employment record would you like to check?", session_id=session_id)
        return ([], meta, None)

    # 1. Previous employment query specific handling
    if is_previous_query:
        if stored_key:
            role_matches = []
            for c in history:
                if not isinstance(c, dict):
                    continue
                roles = c.get("roles") if isinstance(c.get("roles"), list) else [c]
                for r_idx, r in enumerate(roles):
                    if not isinstance(r, dict):
                        continue
                    comp_key = _generate_record_composite_key(c, role_idx=r_idx)
                    r_id_str = str(r.get("id")).strip().lower() if r.get("id") is not None else ""
                    if comp_key == stored_key or (r_id_str and stored_key.endswith(f"::{r_id_str}")):
                        role_matches.append((c, r, r_idx))

            if len(role_matches) == 1:
                target_comp, target_role, target_r_idx = role_matches[0]
                target_name = str(target_comp.get("company")).strip()
                single_role_entry = {**target_comp, "roles": [target_role]}
                return ([single_role_entry], None, target_name)
            elif len(role_matches) > 1:
                meta = build_selection_metadata(history, prompt_text="Multiple matching records found. Which would you like to check?", session_id=session_id)
                return ([], meta, None)

        if stored_company:
            stored_matches = [
                c for c in history
                if isinstance(c, dict) and c.get("company") and str(c.get("company")).strip().lower() == str(stored_company).strip().lower()
            ]
            if len(stored_matches) == 1:
                target_name = str(stored_matches[0].get("company")).strip()
                return (stored_matches, None, target_name)
            elif len(stored_matches) > 1:
                return (stored_matches, None, str(stored_company))

        matched_company = _extract_company_name_from_query(query, available_companies, history) if query else None
        if matched_company and matched_company not in ("PREVIOUS_UNAMBIGUOUS", None):
            exact_comp_matches = [
                c for c in history
                if isinstance(c, dict) and c.get("company") and str(c.get("company")).strip().lower() == matched_company.lower()
            ]
            if len(exact_comp_matches) == 1:
                target_name = str(exact_comp_matches[0].get("company")).strip()
                target_key = _generate_record_composite_key(exact_comp_matches[0])
                if session_id:
                    try:
                        from backend.utils.session_state import set_selected_employment_context
                        set_selected_employment_context(session_id, target_name, record_key=target_key)
                    except Exception as e:
                        log.warning("Failed to save selected employment context: %s", e)
                return (exact_comp_matches, None, target_name)
            elif len(exact_comp_matches) > 1:
                meta = build_selection_metadata(exact_comp_matches, prompt_text=f"Which '{matched_company}' employment record would you like to check?", session_id=session_id)
                return ([], meta, None)

        if len(previous_recs) == 0:
            return ([], None, "NO_PREVIOUS_RECORDS")

        if len(previous_recs) == 1:
            target_name = str(previous_recs[0].get("company")).strip()
            target_key = _generate_record_composite_key(previous_recs[0])
            if session_id:
                try:
                    from backend.utils.session_state import set_selected_employment_context
                    set_selected_employment_context(session_id, target_name, record_key=target_key)
                except Exception as e:
                    log.warning("Failed to save selected employment context: %s", e)
            return ([previous_recs[0]], None, target_name)

        # Multiple previous employment records (> 1)
        if re.search(r"\b(most\s+recent|latest|last)\b", q_lower):
            ordered_prev = []
            for item in previous_recs:
                roles = item.get("roles") if isinstance(item.get("roles"), list) else [item]
                max_year = 0
                for r in roles:
                    if isinstance(r, dict):
                        end_y = _parse_year_from_date(r.get("worked_till_date"))
                        start_y = _parse_year_from_date(r.get("joining_date"))
                        y_val = end_y or start_y or 0
                        if y_val > max_year:
                            max_year = y_val
                ordered_prev.append((item, max_year))
            ordered_prev.sort(key=lambda x: x[1], reverse=True)
            if len(ordered_prev) >= 2 and ordered_prev[0][1] > ordered_prev[1][1] and ordered_prev[0][1] > 0:
                target_rec = ordered_prev[0][0]
                target_name = str(target_rec.get("company")).strip()
                target_key = _generate_record_composite_key(target_rec)
                if session_id:
                    try:
                        from backend.utils.session_state import set_selected_employment_context
                        set_selected_employment_context(session_id, target_name, record_key=target_key)
                    except Exception as e:
                        log.warning("Failed to save selected employment context: %s", e)
                return ([target_rec], None, target_name)
            else:
                meta = build_selection_metadata(previous_recs, prompt_text="Which previous employment record would you like to check?", session_id=session_id)
                return ([], meta, None)

        if re.search(r"\b(oldest|first)\b", q_lower):
            ordered_prev = []
            for item in previous_recs:
                roles = item.get("roles") if isinstance(item.get("roles"), list) else [item]
                min_year = 9999
                for r in roles:
                    if isinstance(r, dict):
                        start_y = _parse_year_from_date(r.get("joining_date"))
                        if start_y and start_y < min_year:
                            min_year = start_y
                ordered_prev.append((item, min_year))
            ordered_prev.sort(key=lambda x: x[1])
            if len(ordered_prev) >= 2 and ordered_prev[0][1] < ordered_prev[1][1] and ordered_prev[0][1] < 9999:
                target_rec = ordered_prev[0][0]
                target_name = str(target_rec.get("company")).strip()
                target_key = _generate_record_composite_key(target_rec)
                if session_id:
                    try:
                        from backend.utils.session_state import set_selected_employment_context
                        set_selected_employment_context(session_id, target_name, record_key=target_key)
                    except Exception as e:
                        log.warning("Failed to save selected employment context: %s", e)
                return ([target_rec], None, target_name)
            else:
                meta = build_selection_metadata(previous_recs, prompt_text="Which previous employment record would you like to check?", session_id=session_id)
                return ([], meta, None)

        meta = build_selection_metadata(previous_recs, prompt_text="Which previous employment record would you like to check?", session_id=session_id)
        return ([], meta, None)

    # 2. Check if query matches stored session context first for generic follow-up queries
    stored_company = None
    stored_key = None
    if session_id:
        try:
            from backend.utils.session_state import get_selected_employment_context, get_selected_employment_record_key
            stored_company = get_selected_employment_context(session_id)
            stored_key = get_selected_employment_record_key(session_id)
        except Exception as e:
            log.warning("Failed to fetch selected employment context: %s", e)

    # If query contains explicit company match, respect explicit match over stored context
    explicit_company_mentioned = bool(re.search(r"\b(" + "|".join(re.escape(c) for c in available_companies) + r")\b", q_lower)) if available_companies else False

    if not explicit_company_mentioned and stored_key:
        role_matches = []
        for c in history:
            if not isinstance(c, dict):
                continue
            roles = c.get("roles") if isinstance(c.get("roles"), list) else [c]
            for r_idx, r in enumerate(roles):
                comp_key = _generate_record_composite_key(c, role_idx=r_idx)
                r_id_str = str(r.get("id")).strip().lower() if r.get("id") is not None else ""
                if comp_key == stored_key or (r_id_str and stored_key.endswith(f"::{r_id_str}")):
                    role_matches.append((c, r, r_idx))

        if len(role_matches) == 1:
            target_comp, target_role, target_r_idx = role_matches[0]
            target_name = str(target_comp.get("company")).strip()
            single_role_entry = [{**target_comp, "roles": [target_role]}]
            return (single_role_entry, None, target_name)
        elif len(role_matches) > 1:
            meta = build_selection_metadata(history, prompt_text="Multiple matching records found. Which would you like to check?", session_id=session_id)
            return ([], meta, None)

    matched_company = _extract_company_name_from_query(query, available_companies, history) if query else None

    if matched_company and matched_company in available_companies:
        exact_comp_matches = [
            c for c in history
            if isinstance(c, dict) and c.get("company") and str(c.get("company")).strip().lower() == matched_company.lower()
        ]
        if len(exact_comp_matches) == 1:
            target_comp = exact_comp_matches[0]
            target_name = str(target_comp.get("company")).strip()
            roles = target_comp.get("roles") if isinstance(target_comp.get("roles"), list) else [target_comp]

            if len(roles) == 1:
                target_key = _generate_record_composite_key(target_comp, role_idx=0)
                if session_id:
                    try:
                        from backend.utils.session_state import set_selected_employment_context
                        set_selected_employment_context(session_id, target_name, record_key=target_key)
                    except Exception as e:
                        log.warning("Failed to save selected employment context: %s", e)
                return (exact_comp_matches, None, target_name)
            else:
                # Check for single active role under matched company
                active_roles = [
                    (r_i, r) for r_i, r in enumerate(roles)
                    if isinstance(r, dict) and is_current_role(r)
                ]
                if len(active_roles) == 1:
                    a_idx, a_role = active_roles[0]
                    target_key = _generate_record_composite_key(target_comp, role_idx=a_idx)
                    single_active_entry = [{**target_comp, "roles": [a_role]}]
                    if session_id:
                        try:
                            from backend.utils.session_state import set_selected_employment_context
                            set_selected_employment_context(session_id, target_name, record_key=target_key)
                        except Exception as e:
                            log.warning("Failed to save selected active role context: %s", e)
                    return (single_active_entry, None, target_name)
                else:
                    meta = build_selection_metadata(exact_comp_matches, prompt_text=f"Which '{target_name}' employment record would you like to check?", session_id=session_id)
                    return ([], meta, target_name)
        elif len(exact_comp_matches) > 1:
            meta = build_selection_metadata(exact_comp_matches, prompt_text=f"Which '{matched_company}' employment record would you like to check?", session_id=session_id)
            return ([], meta, matched_company)

    # If no explicit matching company in query, use stored session context if available
    if stored_key:
        role_matches = []
        for c in history:
            if not isinstance(c, dict):
                continue
            roles = c.get("roles") if isinstance(c.get("roles"), list) else [c]
            for r_idx, r in enumerate(roles):
                comp_key = _generate_record_composite_key(c, role_idx=r_idx)
                r_id_str = str(r.get("id")).strip().lower() if r.get("id") is not None else ""
                if comp_key == stored_key or (r_id_str and stored_key.endswith(f"::{r_id_str}")):
                    role_matches.append((c, r, r_idx))

        if len(role_matches) == 1:
            target_comp, target_role, target_r_idx = role_matches[0]
            target_name = str(target_comp.get("company")).strip()
            single_role_entry = [{**target_comp, "roles": [target_role]}]
            return (single_role_entry, None, target_name)
        elif len(role_matches) > 1:
            meta = build_selection_metadata(history, prompt_text="Multiple matching records found. Which would you like to check?", session_id=session_id)
            return ([], meta, None)

    if stored_company:
        stored_matches = [
            c for c in history
            if isinstance(c, dict) and c.get("company") and str(c.get("company")).strip().lower() == str(stored_company).strip().lower()
        ]
        if len(stored_matches) == 1:
            target_comp = stored_matches[0]
            target_name = str(target_comp.get("company")).strip()
            roles = target_comp.get("roles") if isinstance(target_comp.get("roles"), list) else [target_comp]

            desig_matches = [
                (r_i, r) for r_i, r in enumerate(roles)
                if isinstance(r, dict) and (
                    str(r.get("designation") or "").strip().lower() in q_lower or
                    q_lower in str(r.get("designation") or "").strip().lower()
                )
            ]
            if len(desig_matches) == 1:
                d_idx, d_role = desig_matches[0]
                target_key = _generate_record_composite_key(target_comp, role_idx=d_idx)
                single_role_entry = [{**target_comp, "roles": [d_role]}]
                if session_id:
                    try:
                        from backend.utils.session_state import set_selected_employment_context
                        set_selected_employment_context(session_id, target_name, record_key=target_key)
                    except Exception as e:
                        log.warning("Failed to save selected role context: %s", e)
                return (single_role_entry, None, target_name)
            elif len(roles) == 1:
                return (stored_matches, None, target_name)
            else:
                return (stored_matches, None, target_name)
        elif len(stored_matches) > 1:
            meta = build_selection_metadata(stored_matches, prompt_text=f"Which '{stored_company}' employment record would you like to check?", session_id=session_id)
            return ([], meta, None)


    # Handle unlisted company explicit request if no stored context applied
    if matched_company:
        meta = build_selection_metadata(history, prompt_text=f"I couldn't find an employment record for '{matched_company}'. Which employment record would you like to check?", session_id=session_id)
        return ([], meta, matched_company)

    # Bypassing single-record auto-selection shortcut when an explicit verification level request is active AND no stored selection context exists
    if requested_level is not None and not (stored_key or stored_company):
        meta = build_selection_metadata(history, prompt_text="Which employment record would you like to check?", session_id=session_id)
        return ([], meta, None)

    # 4. Single company or single active employment auto-selection
    q_extracted = _extract_company_name_from_query(query, available_companies, history) if query else None
    if len(available_companies) == 1 and not q_extracted:
        target_name = available_companies[0]
        single_matches = [
            c for c in history
            if isinstance(c, dict) and c.get("company") and str(c.get("company")).strip().lower() == target_name.lower()
        ]
        if single_matches:
            target_comp = single_matches[0]
            roles = target_comp.get("roles") if isinstance(target_comp.get("roles"), list) else [target_comp]
            if len(roles) > 1:
                active_roles = [
                    (r_i, r) for r_i, r in enumerate(roles)
                    if isinstance(r, dict) and is_current_role(r)
                ]
                if len(active_roles) == 1:
                    a_idx, a_role = active_roles[0]
                    target_key = _generate_record_composite_key(target_comp, role_idx=a_idx)
                    single_active_entry = [{**target_comp, "roles": [a_role]}]
                    if session_id:
                        try:
                            from backend.utils.session_state import set_selected_employment_context
                            set_selected_employment_context(session_id, target_name, record_key=target_key)
                        except Exception as e:
                            log.warning("Failed to save auto-selected active role context: %s", e)
                    return (single_active_entry, None, target_name)
                else:
                    meta = build_selection_metadata(single_matches, prompt_text=f"Which '{target_name}' employment record would you like to check?", session_id=session_id)
                    return ([], meta, target_name)
            else:
                target_key = _generate_record_composite_key(target_comp, role_idx=0)
                if session_id:
                    try:
                        from backend.utils.session_state import set_selected_employment_context
                        set_selected_employment_context(session_id, target_name, record_key=target_key)
                    except Exception as e:
                        log.warning("Failed to save auto-selected employment context: %s", e)
                return (single_matches, None, target_name)
        return (history, None, target_name)

    if len(current_recs) == 1 and not is_previous_query and not q_extracted:
        target_rec = current_recs[0]
        target_name = str(target_rec.get("company")).strip()
        target_key = _generate_record_composite_key(target_rec)
        if session_id:
            try:
                from backend.utils.session_state import set_selected_employment_context
                set_selected_employment_context(session_id, target_name, record_key=target_key)
            except Exception as e:
                log.warning("Failed to save auto-selected active employment context: %s", e)
        return ([target_rec], None, target_name)

    if not query or not str(query).strip():
        return (history, None, None)

    meta = build_selection_metadata(history, session_id=session_id)
    return ([], meta, None)



def _format_single_intent_impl(
    intent: UserIntent,
    extracted_data: Optional[Dict[str, Any]],
    api_response: Optional[Dict[str, Any]],
    query: str = "",
    session_id: Optional[str] = None,
    return_metadata: bool = False,
    *args,
    **kwargs
) -> Union[str, Tuple[str, Optional[Dict[str, Any]]]]:
    if not isinstance(extracted_data, dict):
        extracted_data = {}

    # Recursively scrub Phase 3 FORBIDDEN_KEYS from input payload before any formatting or field interpolation
    extracted_data = scrub_sensitive_keys(extracted_data)
    if isinstance(api_response, dict):
        api_response = scrub_sensitive_keys(api_response)

    # Check for API / HTTP / Auth Failure
    if isinstance(api_response, dict) and intent != UserIntent.PROFILE_SKILLS:
        status_code = api_response.get("status_code")
        error_msg = str(api_response.get("error") or "").lower()

        if status_code == 401 or api_response.get("error") == "401" or "unauthorized" in error_msg:
            return "Please log in to view your profile details."
        if status_code == 403 or "forbidden" in error_msg:
            return "You do not have permission to view these profile details."
        if status_code == 404 or "not found" in error_msg:
            return "The requested profile information could not be found."
        if status_code == 500 or "internal" in error_msg:
            return "We experienced a temporary issue loading your profile details."
        if "timeout" in error_msg:
            return "The request timed out. Please try again in a moment."
        if api_response.get("status") is False and (api_response.get("error") or status_code):
            return "I couldn't retrieve your information right now due to a service connection issue."

    try:
        # 1. PROFILE_INCOMPLETE
        if intent == UserIntent.PROFILE_INCOMPLETE:
            percentage = extracted_data.get("profile_percentage")
            uncomplete = extracted_data.get("uncomplete") or []
            incomplete = extracted_data.get("incomplete") or []

            if percentage is None and not uncomplete and not incomplete:
                return "I couldn't find any profile completeness data recorded on your account."

            lines = []
            if percentage is not None:
                lines.append(f"Your profile is **{percentage}% complete**.")

            if uncomplete:
                lines.append("**Missing Sections:**")
                for item in uncomplete:
                    lines.append(f"• {item}")
            elif incomplete:
                lines.append("**Incomplete Details:**")
                for item in incomplete:
                    if isinstance(item, dict):
                        key = item.get("key")
                        val = item.get("value")
                        lines.append(f"• {key} ({val} impact)")
                    else:
                        lines.append(f"• {item}")
            else:
                lines.append("Great job! Your profile details appear to be fully complete.")

            return "\n".join(lines)

        # 2. USER_DETAIL_NOTICES
        elif intent == UserIntent.USER_DETAIL_NOTICES:
            notices = extracted_data.get("noticeEmployments") or []
            notice_period = extracted_data.get("notice_period_name")

            if not notices:
                return "You currently have no active employment notice records on file."

            count = len(notices)
            period_str = f" ({notice_period})" if notice_period else ""
            return f"You are currently on notice for **{count} company record(s)**{period_str} (Company IDs: {', '.join([str(n) for n in notices])})."

        # 3. USER_DETAIL_REMINDERS
        elif intent == UserIntent.USER_DETAIL_REMINDERS:
            has_reminder = extracted_data.get("reminderExperience")
            reminders = extracted_data.get("reminderExperienceList") or []

            if not has_reminder and not reminders:
                return "You have no pending experience reminders at this time."

            lines = ["**Pending Experience Reminders:**"]
            for r in reminders:
                if isinstance(r, dict):
                    comp = r.get("company") or "Company"
                    desig = r.get("designation") or "Position"
                    lines.append(f"• {desig} at Company #{comp}")
                else:
                    lines.append(f"• {r}")

            return "\n".join(lines)

        # 4. EMPLOYMENT_HISTORY
        elif intent == UserIntent.EMPLOYMENT_HISTORY:
            history = extracted_data.get("employment_history") or []
            if not history:
                return "I couldn't find any employment history recorded on your account."

            filtered_records, sel_meta, target_company = resolve_employment_selection_context(query, history, session_id=session_id, consume_key=False)

            if not filtered_records and sel_meta:
                prompt = sel_meta.get("prompt", "Which employment record would you like to check?")
                res_str = prompt
                return (res_str, sel_meta) if return_metadata else res_str

            records_to_render = filtered_records if filtered_records else history

            lines = []
            if target_company:
                lines.append(f"Here is your employment history for **{target_company}**:")
            else:
                lines.append("Here is your employment history:")

            for rec in records_to_render:
                if isinstance(rec, dict) and rec.get("company"):
                    c_name = rec.get("company")
                    roles = rec.get("roles") if isinstance(rec.get("roles"), list) else [rec]
                    role_strs = []
                    for r in roles:
                        if isinstance(r, dict):
                            desig = r.get("designation") or r.get("position") or "Role"
                            j_date = r.get("joining_date") or ""
                            w_date = r.get("worked_till_date") or ""
                            date_str = f" ({j_date} – {w_date})" if j_date or w_date else ""
                            role_strs.append(f"{desig}{date_str}")
                    desc_str = ", ".join(role_strs) if role_strs else "Employment Record"
                    lines.append(f"• **{c_name}**: {desc_str}")

            return "\n".join(lines)

        # 5. EMPLOYMENT_SKILLS
        elif intent == UserIntent.EMPLOYMENT_SKILLS:
            emp_skills = extracted_data.get("employment_skills") or []
            if not emp_skills:
                return "I couldn't find any work skills recorded under your employment history."

            history = extracted_data.get("employment_history") or []
            records = history if history else emp_skills

            filtered_records, sel_meta, target_company = resolve_employment_selection_context(query, records, session_id=session_id, consume_key=False)

            if not filtered_records and sel_meta:
                prompt = sel_meta.get("prompt", "Which employment record would you like to check?")
                res_str = prompt
                return (res_str, sel_meta)

            if target_company:
                emp_skills = [s for s in emp_skills if isinstance(s, dict) and s.get("company") and str(s.get("company")).strip().lower() == target_company.lower()]

            if not emp_skills:
                target_name = target_company or "the specified company"
                return f"I couldn't find any work skills recorded for **{target_name}**."

            skills_by_comp = {}
            for item in emp_skills:
                if not isinstance(item, dict):
                    continue
                comp = item.get("company") or "Previous Employment"
                sname = item.get("name")
                if sname:
                    skills_by_comp.setdefault(comp, []).append(sname)

            lines = ["**Skills Used in Previous Employment:**"]
            for comp, sk_list in skills_by_comp.items():
                lines.append(f"• **{comp}**: {', '.join(sk_list)}")

            return "\n".join(lines)

        # 6. EMPLOYMENT_SALARY
        elif intent == UserIntent.EMPLOYMENT_SALARY:
            salaries = extracted_data.get("employment_salary") or []
            if not salaries:
                return "I couldn't find any salary details recorded under your employment history."

            history = extracted_data.get("employment_history") or []
            records = history if history else salaries

            filtered_records, sel_meta, target_company = resolve_employment_selection_context(query, records, session_id=session_id, consume_key=False)

            if not filtered_records and sel_meta:
                prompt = sel_meta.get("prompt", "Which employment record would you like to check?")
                res_str = prompt
                return (res_str, sel_meta)

            if target_company:
                salaries = [s for s in salaries if isinstance(s, dict) and s.get("company") and str(s.get("company")).strip().lower() == target_company.lower()]

            if not salaries:
                target_name = target_company or "the specified company"
                return f"I couldn't find any salary records listed for **{target_name}**."

            lines = ["**Employment Salary Details:**"]
            for item in salaries:
                if not isinstance(item, dict):
                    continue
                comp = item.get("company") or "Company"
                desig = item.get("designation") or "Role"
                sal = item.get("salary")
                mode = item.get("salary_mode") or "Per Annum"
                inhand = item.get("salary_inhand") or ""
                bonus = item.get("bonus")
                inhand_str = f" ({inhand})" if inhand else ""
                bonus_str = f" [Bonus: ₹{bonus}]" if bonus is not None and str(bonus).strip() != "" else ""
                lines.append(f"• **{desig}** at **{comp}**: ₹{sal} {mode}{inhand_str}{bonus_str}")

            return "\n".join(lines)

        # VERIFICATION_LEVEL_DEFINITION
        elif intent == UserIntent.VERIFICATION_LEVEL_DEFINITION:
            q_lower = (query or "").lower()
            if "level 1" in q_lower or "work identity" in q_lower:
                return "**Level 1 — Work Identity**\nYou have added your employment details, including your company and role information. This establishes your Work Identity."
            elif "level 2" in q_lower or "company domain" in q_lower:
                return "**Level 2 — Company Domain Verification**\nYour company has joined CollarCheck and verified its company domain."
            elif "level 3" in q_lower or "employment verification" in q_lower:
                return "**Level 3 — Employment Verification**\nYour employer has confirmed your employment and role."
            elif "level 4" in q_lower or "salary verification" in q_lower:
                return "**Level 4 — Salary Verification**\nYour salary information has been verified, providing additional credibility to your employment profile."
            else:
                return (
                    "Here are CollarCheck's four employment verification levels:\n\n"
                    "• **Level 1 — Work Identity**: You have added your employment details, including your company and role information. This establishes your Work Identity.\n"
                    "• **Level 2 — Company Domain Verification**: Your company has joined CollarCheck and verified its company domain.\n"
                    "• **Level 3 — Employment Verification**: Your employer has confirmed your employment and role.\n"
                    "• **Level 4 — Salary Verification**: Your salary information has been verified, providing additional credibility to your employment profile."
                )

        # EMPLOYMENT_COMPANY_CLAIMED
        elif intent == UserIntent.EMPLOYMENT_COMPANY_CLAIMED:
            history = extracted_data.get("employment_history") or []
            if not history or not isinstance(history, list):
                res_str = "I couldn't find any employment records to check company claimed status."
                return (res_str, None) if return_metadata else res_str

            filtered_history, sel_meta, matched_company = resolve_employment_selection_context(query, history, session_id=session_id, consume_key=False)

            q_lower = (query or "").lower().strip()
            asking_not_claimed = bool(re.search(r"\b(not\s+claimed|unclaimed)\b", q_lower))
            asking_claimed_only = bool(re.search(r"\bwhich\b.*?\b(claimed)\b", q_lower)) and not asking_not_claimed

            # Filter for multiple company questions: "which of my companies are claimed/not claimed"
            if asking_claimed_only and not matched_company:
                claimed_comps = [c for c in history if isinstance(c, dict) and is_truthy(c.get("is_verified"))]
                if not claimed_comps:
                    res_str = "None of your listed companies are currently claimed."
                    return (res_str, None) if return_metadata else res_str
                c_names = [c.get("company") for c in claimed_comps if c.get("company")]
                res_str = f"The following listed company is claimed: **{', '.join(c_names)}**." if len(c_names) == 1 else f"The following listed companies are claimed: **{', '.join(c_names)}**."
                return (res_str, None) if return_metadata else res_str

            if asking_not_claimed and not matched_company:
                unclaimed_comps = [c for c in history if isinstance(c, dict) and not is_truthy(c.get("is_verified"))]
                if not unclaimed_comps:
                    res_str = "All of your listed companies are currently claimed."
                    return (res_str, None) if return_metadata else res_str
                c_names = [c.get("company") for c in unclaimed_comps if c.get("company")]
                res_str = f"The following listed company is not claimed: **{', '.join(c_names)}**." if len(c_names) == 1 else f"The following listed companies are not claimed: **{', '.join(c_names)}**."
                return (res_str, None) if return_metadata else res_str

            if not filtered_history:
                if matched_company:
                    res_str = f"I couldn't find an employment record for '{matched_company}'."
                    return (res_str, None) if return_metadata else res_str
                elif sel_meta:
                    prompt = sel_meta.get("prompt", "Which company's status would you like to check?")
                    res_str = prompt
                    return (res_str, sel_meta) if return_metadata else res_str
                else:
                    res_str = "I couldn't find an employment record for the specified company."
                    return (res_str, None) if return_metadata else res_str

            lines = []
            for comp in filtered_history:
                if not isinstance(comp, dict):
                    continue
                comp_name = comp.get("company") or "Company"
                is_ver = is_truthy(comp.get("is_verified"))
                if is_ver:
                    lines.append(f"**{comp_name}** is a claimed company.")
                else:
                    lines.append(f"**{comp_name}** is not a claimed company.")

            res_str = "\n".join(lines)
            return (res_str, None) if return_metadata else res_str

        # 7. EMPLOYMENT_VERIFICATION & VERIFICATION GUIDANCE INTENTS
        elif intent in (
            UserIntent.EMPLOYMENT_VERIFICATION,
            UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS,
            UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS,
            UserIntent.EMPLOYMENT_VERIFICATION_WHY_INCOMPLETE,
            UserIntent.EMPLOYMENT_VERIFICATION_OWNERSHIP
        ):
            history = extracted_data.get("employment_history") or []
            if not history or not isinstance(history, list):
                res_str = "I couldn't find any employment verification records."
                return (res_str, None) if return_metadata else res_str

            # Structured Level-focused guidance routing (Phase 2.5K)
            req_lvl = kwargs.get("requested_level")
            if req_lvl is None:
                from backend.services.user_data_intent_parser import detect_requested_level
                req_lvl = detect_requested_level(query)

            filtered_history, sel_meta, matched_company = resolve_employment_selection_context(query, history, session_id=session_id, requested_level=req_lvl, consume_key=False)

            # If a specific verification level was requested (e.g. Level 1, 2, 3), render focused guidance
            # for resolved company context, or prompt for explicit selection if ambiguous.
            if req_lvl and 1 <= req_lvl <= 4:
                if filtered_history:
                    target_recs = filtered_history
                    level_key = f"level{req_lvl}"
                    focused_blocks = []
                    for comp in target_recs:
                        if not isinstance(comp, dict):
                            continue
                        comp_name = comp.get("company") or "Company"
                        roles = comp.get("roles") or []
                        for r in roles:
                            if not isinstance(r, dict):
                                continue
                            desig = r.get("designation") or "Role N/A"
                            verif_obj = r.get("verification") if isinstance(r.get("verification"), dict) else {}
                            contract = verif_obj.get("semantic_contract") or {}
                            lvl_data = contract.get(level_key) or {}

                            label = lvl_data.get("label", f"Level {req_lvl}")
                            state = lvl_data.get("state", "UNKNOWN")
                            guidance = lvl_data.get("guidance", "")
                            employee_act = lvl_data.get("employee_action", "")
                            company_act = lvl_data.get("company_action", "")

                            f_block = (
                                f"🏢 **{comp_name}** ({desig})\n"
                                f"**{label} Guidance** (Status: **{state}**)\n\n"
                                f"{guidance}\n\n"
                                f"• **Employee Action:** {employee_act}\n"
                                f"• **Company Action:** {company_act}"
                            )
                            focused_blocks.append(f_block)

                    if focused_blocks:
                        res_str = "\n\n".join(focused_blocks)
                        return (res_str, None) if return_metadata else res_str
                elif sel_meta:
                    prompt = sel_meta.get("prompt", "Which employment record would you like to check?")
                    res_str = prompt
                    return (res_str, sel_meta) if return_metadata else res_str


            if not filtered_history:
                if sel_meta:
                    prompt = sel_meta.get("prompt", "Which employment record would you like to check?")
                    res_str = prompt
                    return (res_str, sel_meta) if return_metadata else res_str
                else:
                    target_name = matched_company or "the specified company"
                    res_str = f"I couldn't find an employment record for '{target_name}'."
                    return (res_str, None) if return_metadata else res_str

            total_records = 0
            verified_count = 0
            not_verified_count = 0
            unknown_count = 0
            record_blocks = []

            for comp in filtered_history:
                if not isinstance(comp, dict):
                    continue
                comp_name = comp.get("company") or "Company N/A"
                c_is_verif = comp.get("is_verified")
                c_verif_str = "Verified" if c_is_verif is True else ("Not Verified" if c_is_verif is False else "Unknown")
                roles = comp.get("roles") or []

                for r in roles:
                    if not isinstance(r, dict):
                        continue
                    total_records += 1
                    desig = r.get("designation") or "Role N/A"
                    verif_obj = r.get("verification") if isinstance(r.get("verification"), dict) else {}

                    c_status = verif_obj.get("status", "unknown")
                    if c_status == "verified":
                        verified_count += 1
                        role_verif_str = "Verified"
                    elif c_status == "not_verified":
                        not_verified_count += 1
                        role_verif_str = "Not Verified"
                    elif c_status == "unknown":
                        unknown_count += 1
                        role_verif_str = "Status Unknown"
                    else:
                        unknown_count += 1
                        role_verif_str = "Status Unknown"

                    lvl = verif_obj.get("level")
                    lvl_label = verif_obj.get("highest_completed_label") or (f"Level {lvl}" if lvl else "None")
                    lvl_str = f"Level {lvl} ({lvl_label})" if lvl else "Level 1 (Work Identity)"
                    breakdown = verif_obj.get("levels_breakdown") or {}

                    next_lvl = verif_obj.get("next_level")
                    next_lvl_label = verif_obj.get("next_level_label")
                    next_owner = verif_obj.get("next_action_owner") or "unknown"
                    next_act = verif_obj.get("next_action") or verif_obj.get("next_step") or ""
                    reason = verif_obj.get("incomplete_reason") or ""
                    g_status = verif_obj.get("guidance_status") or "unknown"

                    l1_st = (breakdown.get("level1") or {}).get("status", "Completed" if lvl else "Not Completed")
                    l2_st = (breakdown.get("level2") or {}).get("status", "Completed" if (lvl and lvl >= 2) else "Not Completed")
                    l3_st = (breakdown.get("level3") or {}).get("status", "Completed" if (lvl and lvl >= 3) else "Not Completed")
                    l4_st = (breakdown.get("level4") or {}).get("status", "Completed" if (lvl and lvl == 4) else "Not Completed")

                    # All verification intents (EMPLOYMENT_VERIFICATION, PROGRESS, NEXT_STEPS, WHY_INCOMPLETE, OWNERSHIP)
                    # map strictly to the single canonical verification roadmap output:
                    claim_st = verif_obj.get("company_claim_status")
                    claim_lbl = verif_obj.get("company_claim_label")
                    domain_ver = verif_obj.get("company_domain_verified")

                    if claim_st is True or claim_lbl == "Claimed":
                        claim_icon = "✓"
                        claim_str = "Claimed"
                    elif claim_st is False or claim_lbl == "Not Claimed":
                        claim_icon = "○"
                        claim_str = "Not Claimed"
                    else:
                        claim_icon = "?"
                        claim_str = "Claim Status Unknown"

                    if domain_ver is True or is_truthy(c_is_verif):
                        domain_icon = "✓"
                        domain_str = "Company Domain Verified"
                    elif domain_ver is False or c_is_verif is False:
                        domain_icon = "○"
                        domain_str = "Company Domain Not Verified"
                    else:
                        domain_icon = "?"
                        domain_str = "Domain Verification Unknown"

                    # Level icons logic
                    r_app = str(r.get("approved") or "").strip().lower()
                    r_stat = str(r.get("status") or "").strip().lower()
                    is_declined = (r_stat in ("2", "declined") or r_app in ("0", "declined") or verif_obj.get("guidance_status") == "declined" or (breakdown.get("level3") or {}).get("status") == "Declined")

                    def _get_level_icon(level_num: int, is_comp: bool, is_next: bool, is_dec: bool) -> str:
                        if is_comp:
                            return "✓"
                        if is_dec and level_num == 3:
                            return "❌"
                        if is_next:
                            return "→"
                        return "○"

                    l1_icon = _get_level_icon(1, l1_st == "Completed", next_lvl == 1, False)
                    l2_icon = _get_level_icon(2, l2_st == "Completed", next_lvl == 2, False)
                    l3_icon = _get_level_icon(3, l3_st == "Completed", next_lvl == 3, is_declined)
                    l4_icon = _get_level_icon(4, l4_st == "Completed", next_lvl == 4, False)

                    level_num = lvl if lvl is not None else 0
                    indep_lvl = verif_obj.get("highest_independent_verified_level") or (level_num if level_num > 0 else 0)
                    indep_label = verif_obj.get("highest_independent_verified_label") or lvl_label or (f"Level {indep_lvl}" if indep_lvl > 0 else "None")
                    owner_str = "Company / Employer" if next_owner == "company" else ("Employee (You)" if next_owner == "employee" else ("None" if next_owner == "none" else "Unknown"))

                    roles_info = r.get("employment_type") or ""
                    emp_st = "Current" if is_current_role(r) else "Previous"
                    subtitle = f"{desig} · {emp_st}"

                    # Detect lower-level gaps for inconsistent API verification states (P1)
                    has_lower_gap = (indep_lvl > level_num)

                    block = (
                        f"🏢 **{comp_name}**\n"
                        f"{subtitle}\n\n"
                        f"**Company status**\n"
                        f"{claim_icon} {claim_str}\n"
                        f"{domain_icon} Company Domain Verification: {'Verified' if domain_ver is True or is_truthy(c_is_verif) else ('Not Verified' if domain_ver is False or c_is_verif is False else 'Unknown')}\n\n"
                        f"**Verification roadmap**\n\n"
                        f"{l1_icon} Level 1 — Work Identity ({l1_st})\n"
                        f"{l2_icon} Level 2 — Company Verification ({l2_st})\n"
                        f"{l3_icon} Level 3 — Employment Verification ({l3_st})\n"
                        f"{l4_icon} Level 4 — Salary Verification ({l4_st})\n\n"
                    )

                    if has_lower_gap:
                        block += (
                            f"Current verification: Level {level_num} (Sequential) | Highest verified: Level {indep_lvl} ({indep_label})\n"
                            f"Verification Level: {level_num} / 4\n"
                            f"⚠️ Note: Level {next_lvl} is still incomplete despite Level {indep_lvl} verification.\n"
                        )
                    elif level_num == 0:
                        block += (
                            f"Current verification: None\n"
                            f"Verification Level: 0 / 4\n"
                        )
                    else:
                        block += (
                            f"Current verification: Level {level_num} ({lvl_label})\n"
                            f"Verification Level: {level_num} / 4\n"
                        )

                    if level_num == 4 and not has_lower_gap:
                        block += "\n🎉 **Your employment is fully verified.**\nHighest verification: Level 4\nNo further verification is currently required.\n"
                    else:
                        if next_lvl:
                            block += f"Next recommended step: Level {next_lvl} ({next_lvl_label})\n"
                        if reason:
                            block += f"Current Blocker: {reason}\n"
                        if next_act:
                            block += f"\n**Next step:**\n{next_act}\n"
                        if next_lvl and next_lvl <= 3:
                            if is_declined and next_lvl == 3:
                                # P2 Fix: Declined Level 3 action chip
                                block += f"\n[How to resolve Level 3 rejection]\n"
                            else:
                                block += f"\n[How to complete Level {next_lvl}]\n"

                    block += "[View Employment](https://www.collarcheck.com/dashboard/user/experience)"
                    record_blocks.append(block)

            if total_records == 0:
                return "I couldn't find any employment verification records."

            header = f"Here is your employment verification status ({total_records} record{'s' if total_records != 1 else ''}: {verified_count} verified, {not_verified_count} not verified, {unknown_count} status unknown):"

            if intent == UserIntent.EMPLOYMENT_VERIFICATION and not query:
                return header

            return header + "\n\n" + "\n\n".join(record_blocks)

        # 8. EMPLOYMENT_DOCUMENTS
        elif intent == UserIntent.EMPLOYMENT_DOCUMENTS:
            history = extracted_data.get("employment_history") or []
            if not history or not isinstance(history, list):
                return "No employment documents found."

            def _has_valid_doc(val: Any) -> bool:
                if val is None or isinstance(val, (bool, int, float)):
                    return False
                if isinstance(val, str):
                    return bool(val.strip())
                if isinstance(val, dict):
                    return len(val) > 0 and any(bool(str(v).strip()) for v in val.values() if v is not None)
                if isinstance(val, list):
                    return any(_has_valid_doc(item) for item in val)
                return False

            company_docs = []
            for comp in history:
                if not isinstance(comp, dict):
                    continue
                name = comp.get("company") or "Company"
                roles = comp.get("roles") if isinstance(comp.get("roles"), list) else []

                role_entries = []
                for r in roles:
                    if not isinstance(r, dict):
                        continue

                    rtitle = r.get("designation") or r.get("title") or r.get("position") or "Employment Role"
                    role_doc = r.get("document")
                    v_proc = r.get("verificationProcess") if isinstance(r.get("verificationProcess"), dict) else {}
                    verif_doc = v_proc.get("document")
                    relieving_doc = v_proc.get("relieving_letter")
                    have_doc = r.get("have_document")

                    role_labels = []
                    if _has_valid_doc(role_doc):
                        role_labels.append("Uploaded Employment Document")
                    if _has_valid_doc(verif_doc):
                        role_labels.append("Verification Document")
                    if _has_valid_doc(relieving_doc):
                        role_labels.append("Relieving Letter")

                    if not role_labels and have_doc is True:
                        role_labels.append("Document Record Available")

                    if role_labels:
                        role_entries.append((rtitle, role_labels))

                if role_entries:
                    company_docs.append((name, role_entries))

            if not company_docs:
                return "No employment documents found."

            lines = ["**Work Documents:**"]
            for cname, role_list in company_docs:
                lines.append(f"\n**{cname}:**")
                for rtitle, labels in role_list:
                    lines.append(f"• **{rtitle}:**")
                    for lbl in labels:
                        lines.append(f"  - {lbl}")

            return "\n".join(lines)

        # Phase 3.1: EMPLOYMENT_ACTION_ADD
        elif intent == UserIntent.EMPLOYMENT_ACTION_ADD:
            return (
                "**Add Employment Information**\n\n"
                "You can add your current or previous employment through your CollarCheck Experience section.\n\n"
                "Steps:\n"
                "1. Open your CollarCheck Experience section.\n"
                "2. Navigate to the employment information area.\n"
                "3. Add the relevant company and role details.\n"
                "4. Follow the platform's submission instructions.\n\n"
                "Open Experience:\n"
                "https://www.collarcheck.com/dashboard/user/experience"
            )

        # Phase 3.1: EMPLOYMENT_ACTION_INVITE_EMPLOYER
        elif intent == UserIntent.EMPLOYMENT_ACTION_INVITE_EMPLOYER:
            return (
                "**Employer Verification**\n\n"
                "Your employer or HR team is responsible for confirming the employment details where employer approval is required.\n\n"
                "You can review your employment record through the Experience section and follow the platform's available verification workflow.\n\n"
                "Open Experience:\n"
                "https://www.collarcheck.com/dashboard/user/experience"
            )

        # Phase 3.1: EMPLOYMENT_ACTION_UPLOAD_SALARY
        elif intent == UserIntent.EMPLOYMENT_ACTION_UPLOAD_SALARY:
            return (
                "**Salary Verification**\n\n"
                "Level 4 concerns salary verification.\n\n"
                "To continue, open the verified salary-related section of your CollarCheck account and follow the platform's available instructions.\n\n"
                "Open Salary Section:\n"
                "https://www.collarcheck.com/dashboard/user/skills"
            )

        # 9. WIDGET_CLOSING_SOON
        elif intent == UserIntent.WIDGET_CLOSING_SOON:
            widgets = extracted_data.get("widgets") or []
            closing_widget = next((w for w in widgets if isinstance(w, dict) and ("closing" in str(w.get("slug")).lower() or "closing" in str(w.get("heading")).lower() or w.get("api_slug") == "auth-all-job")), None)

            items = closing_widget.get("items") if isinstance(closing_widget, dict) and isinstance(closing_widget.get("items"), list) else []
            if not items:
                return "There are currently no positions closing soon on CollarCheck."

            lines = ["**Positions Closing Soon:**"]
            for item in items[:5]:
                if not isinstance(item, dict):
                    continue
                title = item.get("job_title") or "Position"
                comp = item.get("company") or "Employer"
                loc = item.get("location") or "India"
                url = item.get("url")
                link_str = f" - [View Job]({url})" if _is_safe_public_url(url) else ""
                lines.append(f"• **{title}** at **{comp}** ({loc}){link_str}")

            return "\n".join(lines)

        # 10. WIDGET_NEARBY_ORGS
        elif intent == UserIntent.WIDGET_NEARBY_ORGS:
            widgets = extracted_data.get("widgets") or []
            nearby_widget = next((w for w in widgets if isinstance(w, dict) and ("nearby" in str(w.get("slug")).lower() or "near" in str(w.get("heading")).lower() or w.get("api_slug") == "nearby-company")), None)

            items = nearby_widget.get("items") if isinstance(nearby_widget, dict) and isinstance(nearby_widget.get("items"), list) else []
            if not items:
                return "There are currently no nearby organizations listed for your location."

            lines = ["**Organizations Near You:**"]
            for item in items[:5]:
                if not isinstance(item, dict):
                    continue
                name = item.get("company") or item.get("name") or "Organization"
                city = item.get("city_name") or item.get("location") or ""
                dist = item.get("distance")
                dist_str = f" ({dist} km away)" if dist else ""
                ind = item.get("industry_name")
                ind_str = f" - {ind}" if ind else ""
                lines.append(f"• **{name}**{dist_str}{ind_str}")

            return "\n".join(lines)

        # 11. WIDGET_ALL
        elif intent == UserIntent.WIDGET_ALL:
            widgets = extracted_data.get("widgets") or []
            if not widgets:
                return "There are currently no platform widgets available on your dashboard."

            lines = ["**Platform Dashboard Widgets:**"]
            for w in widgets:
                if not isinstance(w, dict):
                    continue
                heading = w.get("heading") or w.get("slug") or "Widget"
                count = w.get("list_count") or 0
                lines.append(f"• **{heading}**: {count} active items")

            return "\n".join(lines)

        # 12. PROFILE_SKILLS
        elif intent == UserIntent.PROFILE_SKILLS:
            eff_skills = extracted_data.get("effective_skills")
            prof_status = extracted_data.get("profile_status")
            emp_status = extracted_data.get("employment_status")

            skill_names = []
            if eff_skills is not None and isinstance(eff_skills, list):
                for s in eff_skills:
                    if isinstance(s, str) and s.strip():
                        skill_names.append(s.strip())
                    elif isinstance(s, dict) and s.get("skill"):
                        skill_names.append(str(s.get("skill")).strip())
            else:
                prof_skills = extracted_data.get("profile_skills") or []
                for s in prof_skills:
                    if isinstance(s, str) and s.strip():
                        skill_names.append(s.strip())
                    elif isinstance(s, dict) and s.get("skill"):
                        skill_names.append(str(s.get("skill")).strip())

            if not skill_names:
                if prof_status in ("FAILED", "error", "500") and emp_status in ("FAILED", "error", "500"):
                    return "I was unable to retrieve your skills at this time. Please try again later."
                return "I couldn't find any skills listed on your profile or employment history."

            seen = set()
            unique_skills = []
            for s in skill_names:
                if s.lower() not in seen:
                    seen.add(s.lower())
                    unique_skills.append(s)

            return f"**Your Skills:** {', '.join(unique_skills)}."

        # 13. PROFILE_EDUCATION
        elif intent == UserIntent.PROFILE_EDUCATION:
            edu_list = extracted_data.get("education") or []
            if not edu_list:
                return "I couldn't find any education background recorded on your profile."
            return "Here are your education details:"

        # 14. PROFILE_CERTIFICATES
        elif intent == UserIntent.PROFILE_CERTIFICATES:
            cert_list = extracted_data.get("certificates") or []
            if not cert_list:
                return "I couldn't find any certificates listed on your profile."

            lines = ["**Your Certifications:**"]
            for cert in cert_list:
                if not isinstance(cert, dict):
                    continue
                course = cert.get("course") or "Certificate"
                uni = cert.get("university") or ""
                uni_str = f" ({uni})" if uni else ""
                lines.append(f"• {course}{uni_str}")

            return "\n".join(lines)

        # 15. PROFILE_LANGUAGES
        elif intent == UserIntent.PROFILE_LANGUAGES:
            lang_list = extracted_data.get("languages") or []
            if not lang_list:
                return "I couldn't find any spoken languages listed on your profile."

            lines = ["**Spoken Languages:**"]
            for lang in lang_list:
                if not isinstance(lang, dict):
                    continue
                name = lang.get("name") or "Language"
                verbal = lang.get("verbal")
                written = lang.get("written")
                levels = []
                if verbal:
                    levels.append(f"Verbal Level {verbal}")
                if written:
                    levels.append(f"Written Level {written}")
                level_str = f" ({', '.join(levels)})" if levels else ""
                lines.append(f"• **{name}**{level_str}")

            return "\n".join(lines)

        # 16. PROFILE_PORTFOLIO
        elif intent == UserIntent.PROFILE_PORTFOLIO:
            port_list = extracted_data.get("portfolio") or []
            if not port_list:
                return "I couldn't find any portfolio entries listed on your profile."

            lines = ["**Your Portfolio & Projects:**"]
            for p in port_list:
                if not isinstance(p, dict):
                    continue
                title = p.get("title") or "Project"
                desc = p.get("description") or ""
                url = p.get("url")
                link_str = f" - [View Portfolio]({url})" if _is_safe_public_url(url) else ""
                desc_str = f": {desc}" if desc else ""
                lines.append(f"• **{title}**{link_str}{desc_str}")

            return "\n".join(lines)

        # 17. PROFILE_SUMMARY
        elif intent == UserIntent.PROFILE_SUMMARY:
            ps = extracted_data.get("profile_summary") if isinstance(extracted_data.get("profile_summary"), dict) else {}
            name = ps.get("name") or "User"
            email = ps.get("email")
            phone = ps.get("phone")
            dob = ps.get("dob")

            loc_parts = [p for p in [ps.get("city_name"), ps.get("state_name"), ps.get("country_name")] if p]
            location = ", ".join(loc_parts) if loc_parts else ps.get("state_name") or ps.get("country_name") or "Not provided"

            work_status = ps.get("work_status_name") or "Not provided"

            pos = ps.get("current_position_name")
            comp = ps.get("current_company_name")
            role_str = f"{pos} at {comp}" if pos and comp else (pos or comp or "Not provided")

            desc = ps.get("profile_description") or ""
            skills = ps.get("skills") or []

            lines = [
                f"Here is your profile information:\n",
                f"**Name:** {name}",
                f"**Email:** {email or 'Not provided'}",
                f"**Phone:** {phone or 'Not provided'}",
                f"**Date of Birth:** {dob or 'Not provided'}",
                f"**Location:** {location}",
                f"**Work Status:** {work_status}",
                f"**Current Role:** {role_str}",
                f"**Profile Description:** {desc}",
            ]

            if skills:
                lines.append("\n**Skills:**")
                for skill in skills:
                    lines.append(f"- {skill}")

            return "\n".join(lines)

        res_msg = "Your requested information was processed."
        return res_msg

    except Exception as exc:
        log.exception("Sanitized failure during single intent formatting: %s", exc)
        return "An unexpected error occurred while processing your request."

    return "Your requested information was processed."


def format_single_intent(
    intent: UserIntent,
    extracted_data: Optional[Dict[str, Any]],
    api_response: Optional[Dict[str, Any]],
    query: str = "",
    session_id: Optional[str] = None,
    return_metadata: bool = False,
    *args,
    **kwargs
) -> Union[str, Tuple[str, Optional[Dict[str, Any]]]]:
    res = _format_single_intent_impl(
        intent, extracted_data, api_response, query=query, session_id=session_id, return_metadata=return_metadata, *args, **kwargs
    )
    if return_metadata:
        if isinstance(res, tuple) and len(res) == 2:
            return res
        return (res, None)
    else:
        if isinstance(res, tuple) and len(res) == 2:
            return res[0]
        return res


def format_multi_intent_response(
    intents: List[UserIntent],
    extracted_data_by_intent: Dict[UserIntent, Dict[str, Any]],
    api_responses_by_endpoint: Dict[str, Any],
    query: str = "",
    session_id: Optional[str] = None,
    return_metadata: bool = False,
    requested_level: Optional[int] = None
) -> Union[str, Tuple[str, Optional[Dict[str, Any]]]]:
    if not intents:
        msg = "How can I assist you with your profile or employment details?"
        return (msg, None) if return_metadata else msg

    # Deduplicate overlapping verification intents to ensure single canonical response
    verification_alias_group = {
        UserIntent.EMPLOYMENT_VERIFICATION,
        UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS,
        UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS,
        UserIntent.EMPLOYMENT_VERIFICATION_WHY_INCOMPLETE,
        UserIntent.EMPLOYMENT_VERIFICATION_OWNERSHIP,
    }

    if requested_level is not None or any(i in verification_alias_group for i in intents):
        if UserIntent.EMPLOYMENT_ACTION_UPLOAD_SALARY in intents and requested_level == 4:
            verification_alias_group.add(UserIntent.EMPLOYMENT_ACTION_UPLOAD_SALARY)
        if UserIntent.EMPLOYMENT_ACTION_INVITE_EMPLOYER in intents and requested_level == 3:
            verification_alias_group.add(UserIntent.EMPLOYMENT_ACTION_INVITE_EMPLOYER)
        if UserIntent.EMPLOYMENT_ACTION_ADD in intents and requested_level == 1:
            verification_alias_group.add(UserIntent.EMPLOYMENT_ACTION_ADD)

    filtered_intents = []
    seen_verif = False

    for i in intents:
        if i in verification_alias_group:
            if not seen_verif:
                filtered_intents.append(UserIntent.EMPLOYMENT_VERIFICATION)
                seen_verif = True
        else:
            if i not in filtered_intents:
                filtered_intents.append(i)

    intents = filtered_intents

    formatted_sections = []
    captured_selection_metadata = None

    for intent in intents:
        data = (
            extracted_data_by_intent.get(intent) or
            extracted_data_by_intent.get(UserIntent.EMPLOYMENT_VERIFICATION) or
            extracted_data_by_intent.get(UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS) or
            extracted_data_by_intent.get(UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS) or
            extracted_data_by_intent.get(UserIntent.EMPLOYMENT_HISTORY) or
            {}
        )
        from backend.services.user_data_service import INTENT_ENDPOINT_MAP
        ep_key = INTENT_ENDPOINT_MAP.get(intent)
        if isinstance(ep_key, (list, tuple)):
            primary_ep = ep_key[0] if ep_key else None
            api_resp = (api_responses_by_endpoint.get(primary_ep) or api_responses_by_endpoint.get("employment_history") or {})
        else:
            api_resp = (api_responses_by_endpoint.get(ep_key) or api_responses_by_endpoint.get("employment_history") or {})

        try:
            try:
                res = format_single_intent(
                    intent,
                    data,
                    api_resp,
                    query=query,
                    session_id=session_id,
                    return_metadata=True,
                    requested_level=requested_level
                )
            except TypeError:
                res = format_single_intent(intent, data, api_resp, query=query, requested_level=requested_level)

            if isinstance(res, tuple) and len(res) == 2:
                sec_text, sel_meta = res
                if sel_meta:
                    captured_selection_metadata = sel_meta
            else:
                sec_text = str(res) if res is not None else ""
        except Exception as exc:
            log.exception("Section fault isolated for intent %s: %s", intent, exc)
            sec_text = "An unexpected error occurred while processing this section."
        formatted_sections.append(sec_text)

    full_reply = "\n\n".join(formatted_sections)

    if return_metadata:
        return (full_reply, captured_selection_metadata)
    return full_reply
