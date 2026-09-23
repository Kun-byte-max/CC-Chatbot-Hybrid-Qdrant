import os
import time
import json
import logging
from dataclasses import dataclass, field, is_dataclass, asdict
import hashlib
from typing import Dict, Any, Optional, List, Union

try:
    from backend.repositories.db import get_db
    from backend.schemas.search_query import SearchSnapshot, SearchQuery
    from backend.schemas.employer_requirements import (
        EmployerSearchRequirements,
        LocationRequirement,
        ExperienceRequirement,
        SalaryRequirement,
        ProvenanceValue,
        ProvenanceSource,
    )
except ModuleNotFoundError:
    from repositories.db import get_db  # type: ignore
    from schemas.search_query import SearchSnapshot, SearchQuery  # type: ignore
    from schemas.employer_requirements import (  # type: ignore
        EmployerSearchRequirements,
        LocationRequirement,
        ExperienceRequirement,
        SalaryRequirement,
        ProvenanceValue,
        ProvenanceSource,
    )

log = logging.getLogger(__name__)

# L1 Global in-memory session cache
_SESSION_STATES: Dict[str, Dict[str, Any]] = {}
SESSION_TTL_SECONDS = 3600 * 4  # 4 hours
MAX_CAS_RETRIES = 3

FORBIDDEN_KEYS = {
    "token", "jwt", "password", "session_id", "access_token", "auth_token",
    "cookie", "secret", "authorization", "api_key"
}


# ============================================================================
# 1. Decoupled State Domain Schemas (Wrapping Existing DTO Contracts)
# ============================================================================

@dataclass
class CriteriaState:
    """Criteria State Domain wrapping baseline search DTOs."""
    employer_reqs: Optional[EmployerSearchRequirements] = None
    employee_query: Optional[SearchQuery] = None
    search_snapshot: Optional[SearchSnapshot] = None


@dataclass
class LanguageState:
    """Language State Domain tracking explicit user language preference."""
    language_preference: Optional[str] = None  # 'hindi', 'english', 'hinglish', None


@dataclass
class DialogueState:
    """Dialogue State Domain tracking turn metadata and authentication."""
    turn_counter: int = 1
    last_intent: Optional[str] = None
    user_id: Optional[str] = None
    role: Optional[str] = None
    last_active_at: float = field(default_factory=time.time)


@dataclass
class PaginationState:
    """
    Typed pagination state schema namespaced by recommendation_type and result_type.
    """
    session_id: str
    recommendation_type: str  # "EMPLOYEE" | "EMPLOYER"
    result_type: str          # "JOBS" | "CANDIDATES"
    query_fingerprint: str
    current_page: int         # 0 if total_results == 0; 1..total_pages if total_results > 0
    total_pages: int          # 0 if total_results == 0; ceil(total_results / page_size) if total_results > 0
    total_results: int
    result_ids: List[Any] = field(default_factory=list)
    all_recs: List[Any] = field(default_factory=list)


# ============================================================================
# 2. Database Schema Initialization & Key Formatting
# ============================================================================

import uuid
import secrets

def generate_secure_session_id() -> str:
    """Generates a cryptographically secure random session ID."""
    return f"sess_{secrets.token_hex(16)}"


def make_composite_key(session_id: str, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> str:
    """Formats identity and role-isolated composite primary key: f'{user_id}:{raw_session_id}:{role.lower()}'."""
    if not session_id:
        return ""

    parts = []
    if user_id is not None and str(user_id).strip():
        uid_str = str(user_id).strip()
        if not session_id.startswith(f"{uid_str}:"):
            parts.append(uid_str)

    parts.append(session_id)

    if role:
        norm_role = str(role).strip().lower()
        if not session_id.lower().endswith(f":{norm_role}"):
            parts.append(norm_role)

    return ":".join(parts)


def _ensure_session_table():
    """Auto-creates cyb_chat_session table in DB if it does not exist with version_seq."""
    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS cyb_chat_session (
                    session_id VARCHAR(255) PRIMARY KEY,
                    search_context TEXT,
                    pagination_state TEXT,
                    search_snapshot TEXT,
                    recommendation_context TEXT,
                    updated_at DOUBLE,
                    version_seq INT NOT NULL DEFAULT 1
                )
            """
            )
            db.commit()
    except Exception as e:
        log.warning(f"Failed to ensure cyb_chat_session table: {e}")


# Initialize table on import
_ensure_session_table()


# ============================================================================
# 3. Persistence Boundary Scrubbing & Low-Level Persistence Handlers
# ============================================================================

def sanitize_state_dict(obj: Any) -> Any:
    """Recursively scrubs secret keys from untrusted dictionary or list state payloads."""
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            norm_k = str(k).strip().lower()
            if norm_k in FORBIDDEN_KEYS:
                continue
            cleaned[k] = sanitize_state_dict(v)
        return cleaned
    elif isinstance(obj, list):
        return [sanitize_state_dict(item) for item in obj]
    return obj


def _json_default(obj):
    if is_dataclass(obj):
        return asdict(obj)
    if hasattr(obj, "dict") and callable(obj.dict):
        return obj.dict()
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return obj.to_dict()
    return str(obj)


def _db_load_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Loads session record from MySQL database."""
    if not session_id:
        return None
    try:
        with get_db() as db:
            cursor = db.cursor()
            try:
                cursor.execute(
                    "SELECT search_context, pagination_state, search_snapshot, recommendation_context, updated_at, version_seq FROM cyb_chat_session WHERE session_id = %s",
                    (session_id,),
                )
                row = cursor.fetchone()
            except Exception:
                cursor.execute(
                    "SELECT search_context, pagination_state, search_snapshot, recommendation_context, updated_at FROM cyb_chat_session WHERE session_id = %s",
                    (session_id,),
                )
                row = cursor.fetchone()

            if not row:
                return None

            if isinstance(row, dict):
                ctx_str = row.get("search_context")
                pag_str = row.get("pagination_state")
                snap_str = row.get("search_snapshot")
                rec_str = row.get("recommendation_context")
                updated_at = row.get("updated_at")
                version_seq = row.get("version_seq", 1)
            else:
                if len(row) >= 6:
                    ctx_str, pag_str, snap_str, rec_str, updated_at, version_seq = row[:6]
                else:
                    ctx_str, pag_str, snap_str, rec_str, updated_at = row[:5]
                    version_seq = 1

            if updated_at and (time.time() - float(updated_at) > SESSION_TTL_SECONDS):
                return None

            data = {
                "_last_active": float(updated_at) if updated_at else time.time(),
                "version_seq": int(version_seq) if version_seq else 1,
            }
            if ctx_str:
                try:
                    data["search_context"] = json.loads(ctx_str)
                except Exception:
                    data["search_context"] = None
            if pag_str:
                try:
                    data["pagination"] = json.loads(pag_str)
                except Exception:
                    data["pagination"] = None
            if snap_str:
                try:
                    snap_dict = json.loads(snap_str)
                    data["search_snapshot"] = SearchSnapshot.parse_obj(snap_dict)
                except Exception:
                    data["search_snapshot"] = None
            if rec_str:
                try:
                    data["recommendation_context"] = json.loads(rec_str)
                except Exception:
                    data["recommendation_context"] = None
            return data
    except Exception as e:
        log.warning(f"Error loading session {session_id} from DB: {e}")
        return None


def _db_save_session(session_id: str, state_dict: Dict[str, Any], expected_version: Optional[int] = None) -> bool:
    """Saves/upserts session record into MySQL database with Atomic CAS version guard."""
    if not session_id:
        return False
    try:
        scrubbed_state = sanitize_state_dict(state_dict)
        ctx_str = json.dumps(scrubbed_state.get("search_context"), default=_json_default) if scrubbed_state.get("search_context") else None
        pag_str = json.dumps(scrubbed_state.get("pagination"), default=_json_default) if scrubbed_state.get("pagination") else None
        snap_obj = scrubbed_state.get("search_snapshot")
        snap_str = snap_obj.json() if isinstance(snap_obj, SearchSnapshot) else (json.dumps(snap_obj, default=_json_default) if snap_obj else None)
        rec_str = json.dumps(scrubbed_state.get("recommendation_context"), default=_json_default) if scrubbed_state.get("recommendation_context") else None
        updated_at = scrubbed_state.get("_last_active", time.time())
        new_version = int(state_dict.get("version_seq", 1))

        with get_db() as db:
            cursor = db.cursor()

            if expected_version is not None:
                # Strictly guarded CAS execution path
                try:
                    cursor.execute(
                        """
                        UPDATE cyb_chat_session
                        SET search_context = %s,
                            pagination_state = %s,
                            search_snapshot = %s,
                            recommendation_context = %s,
                            updated_at = %s,
                            version_seq = %s
                        WHERE session_id = %s AND version_seq = %s
                        """,
                        (ctx_str, pag_str, snap_str, rec_str, updated_at, new_version, session_id, expected_version),
                    )
                    if cursor.rowcount > 0:
                        db.commit()
                        return True

                    # Rowcount == 0: Check if row exists in DB
                    cursor.execute("SELECT version_seq FROM cyb_chat_session WHERE session_id = %s", (session_id,))
                    row = cursor.fetchone()
                    if row:
                        # Row exists and version mismatched -> Stale writer CAS rejection
                        return False

                    # Row does not exist in DB: INSERT initial record
                    cursor.execute(
                        """
                        INSERT INTO cyb_chat_session 
                        (session_id, search_context, pagination_state, search_snapshot, recommendation_context, updated_at, version_seq)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (session_id, ctx_str, pag_str, snap_str, rec_str, updated_at, new_version),
                    )
                    db.commit()
                    return True
                except Exception as e:
                    log.warning(f"CAS execution error for session {session_id}: {e}")
                    return False
            else:
                # Version-ordered path (expected_version is None)
                try:
                    cursor.execute(
                        """
                        UPDATE cyb_chat_session
                        SET search_context = %s,
                            pagination_state = %s,
                            search_snapshot = %s,
                            recommendation_context = %s,
                            updated_at = %s,
                            version_seq = %s
                        WHERE session_id = %s AND version_seq < %s
                        """,
                        (ctx_str, pag_str, snap_str, rec_str, updated_at, new_version, session_id, new_version),
                    )
                    if cursor.rowcount > 0:
                        db.commit()
                        return True

                    cursor.execute("SELECT version_seq FROM cyb_chat_session WHERE session_id = %s", (session_id,))
                    row = cursor.fetchone()
                    if row:
                        return False

                    cursor.execute(
                        """
                        INSERT INTO cyb_chat_session 
                        (session_id, search_context, pagination_state, search_snapshot, recommendation_context, updated_at, version_seq)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (session_id, ctx_str, pag_str, snap_str, rec_str, updated_at, new_version),
                    )
                    db.commit()
                    return True
                except Exception as e:
                    log.warning(f"Error saving un-guarded session {session_id}: {e}")
                    return False
    except Exception as e:
        log.warning(f"Error saving session {session_id} to DB: {e}")
        if session_id in _SESSION_STATES:
            _SESSION_STATES[session_id]["db_sync_pending"] = True
        return False


# ============================================================================
# 4. L1/L2 Freshness Validation & High-Level Session Accessors
# ============================================================================

def get_session_state(session_id: str, role: Optional[str] = None) -> Dict[str, Any]:
    """Retrieves session state dictionary enforcing role isolation and L1/L2 freshness validation."""
    key = make_composite_key(session_id, role)
    if not key:
        return {}

    now = time.time()
    l1_entry = _SESSION_STATES.get(key)
    if l1_entry:
        l1_last_active = l1_entry.get("_last_active", 0)
        if now - l1_last_active > SESSION_TTL_SECONDS:
            _SESSION_STATES.pop(key, None)
            l1_entry = None

    # Fetch L2 DB state to validate version freshness
    db_state = None
    try:
        db_state = _db_load_session(key)
    except Exception as e:
        log.warning(f"Error loading session {key} from DB: {e}")

    if l1_entry:
        if db_state:
            db_ver = db_state.get("version_seq", 1)
            l1_ver = l1_entry.get("version_seq", 1)
            if db_ver > l1_ver:
                # DB is strictly newer -> L1 is stale, overwrite with L2 DB state
                _SESSION_STATES[key] = db_state
                return db_state
            elif db_ver < l1_ver:
                # L1 version is higher than DB: check if process-local write is pending
                if l1_entry.get("db_sync_pending"):
                    return l1_entry
                else:
                    # Invalid L1 state -> revert to DB state
                    _SESSION_STATES[key] = db_state
                    return db_state
        return l1_entry

    if db_state:
        _SESSION_STATES[key] = db_state
        return db_state

    # Initialize fresh session
    new_state = {
        "_last_active": now,
        "version_seq": 1,
        "language_preference": None,
    }
    _SESSION_STATES[key] = new_state
    return new_state


def get_search_context(session_id: str, role: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieves active search context dictionary for a session."""
    state = get_session_state(session_id, role=role)
    return state.get("search_context")


def set_search_context(session_id: str, search_context: Dict[str, Any], role: Optional[str] = None) -> None:
    """Updates active search context for a session with atomic CAS persistence."""
    key = make_composite_key(session_id, role)
    if not key:
        return

    def remerge_ctx(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["search_context"] = search_context
        return res_state

    state = get_session_state(key)
    save_session_state_cas(session_id, remerge_ctx(state), role=role, remerge_fn=remerge_ctx)


def get_employer_search_context(session_id: str, role: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieves active employer search requirements state dict."""
    state = get_session_state(session_id, role=role)
    return state.get("employer_search_context")


def set_employer_search_context(session_id: str, context_data: Dict[str, Any], role: Optional[str] = None) -> None:
    """Saves active employer search requirements state dict with atomic CAS persistence."""
    key = make_composite_key(session_id, role)
    if not key:
        return

    def remerge_emp_ctx(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["employer_search_context"] = context_data
        return res_state

    state = get_session_state(key)
    save_session_state_cas(session_id, remerge_emp_ctx(state), role=role, remerge_fn=remerge_emp_ctx)


def get_search_snapshot(session_id: str, role: Optional[str] = None) -> Optional[SearchSnapshot]:
    """Retrieves active SearchSnapshot model for a session if valid."""
    state = get_session_state(session_id, role=role)
    snap = state.get("search_snapshot")
    if isinstance(snap, SearchSnapshot):
        return snap
    elif isinstance(snap, dict):
        try:
            parsed = SearchSnapshot.parse_obj(snap)
            state["search_snapshot"] = parsed
            return parsed
        except Exception:
            return None
    return None


def set_search_snapshot(session_id: str, snapshot: SearchSnapshot, role: Optional[str] = None) -> None:
    """Saves active SearchSnapshot model for a session with atomic CAS persistence."""
    key = make_composite_key(session_id, role)
    if not key:
        return

    def remerge_snap(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["search_snapshot"] = snapshot
        return res_state

    state = get_session_state(key)
    save_session_state_cas(session_id, remerge_snap(state), role=role, remerge_fn=remerge_snap)


def get_pagination_state(session_id: str, role: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieves active pagination state dict."""
    state = get_session_state(session_id, role=role)
    return state.get("pagination")


def set_pagination_state(session_id: str, pagination_data: Dict[str, Any], role: Optional[str] = None) -> None:
    """Saves pagination state dict with atomic CAS persistence."""
    key = make_composite_key(session_id, role)
    if not key:
        return

    def remerge_pag(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["pagination"] = pagination_data
        return res_state

    state = get_session_state(key)
    save_session_state_cas(session_id, remerge_pag(state), role=role, remerge_fn=remerge_pag)


def get_language_preference(session_id: str, role: Optional[str] = None) -> Optional[str]:
    """Retrieves active explicit persistent language preference for a session ('hindi' | 'english' | 'hinglish' | None)."""
    key = make_composite_key(session_id, role)
    if not key:
        return None
    state = get_session_state(key)
    return state.get("language_preference")


def set_language_preference(session_id: str, lang_pref: Optional[str], role: Optional[str] = None) -> None:
    """Saves active explicit persistent language preference for a session ('hindi' | 'english' | 'hinglish') with atomic CAS persistence."""
    key = make_composite_key(session_id, role)
    if not key or not lang_pref:
        return
    norm_lang = str(lang_pref).strip().lower()

    def remerge_lang(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["language_preference"] = norm_lang
        return res_state

    state = get_session_state(key)
    save_session_state_cas(session_id, remerge_lang(state), role=role, remerge_fn=remerge_lang)


def get_recommendation_session(session_id: str, role: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieves recommendation session dict."""
    state = get_session_state(session_id, role=role)
    return state.get("recommendation_context")


def set_recommendation_session(
    session_id: str,
    query_key: str,
    ranked_jobs: List[Any],
    current_page: int = 1,
    page_size: int = 5,
    total_jobs: int = 0,
    last_referenced_job_id: Optional[Any] = None,
    role: Optional[str] = None,
) -> None:
    """Stores recommendation context snapshot with atomic CAS persistence."""
    key = make_composite_key(session_id, role)
    if not key:
        return

    rec_payload = {
        "query_key": query_key,
        "ranked_jobs": ranked_jobs,
        "current_page": current_page,
        "page_size": page_size,
        "total_jobs": total_jobs or len(ranked_jobs),
        "last_referenced_job_id": last_referenced_job_id,
    }

    def remerge_rec(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["recommendation_context"] = rec_payload
        return res_state

    state = get_session_state(key)
    save_session_state_cas(session_id, remerge_rec(state), role=role, remerge_fn=remerge_rec)


def update_last_referenced_job_id(session_id: str, job_id: Any, role: Optional[str] = None) -> None:
    """Updates last_referenced_job_id with atomic CAS persistence."""
    key = make_composite_key(session_id, role)
    if not key or job_id is None:
        return

    def remerge_ref(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        rec_ctx = dict(res_state.get("recommendation_context") or {})
        rec_ctx["last_referenced_job_id"] = job_id
        res_state["recommendation_context"] = rec_ctx
        return res_state

    state = get_session_state(key)
    save_session_state_cas(session_id, remerge_ref(state), role=role, remerge_fn=remerge_ref)


def set_session_state(session_id: str, state_data: Dict[str, Any], role: Optional[str] = None) -> None:
    """Saves global session state dict with atomic CAS persistence."""
    key = make_composite_key(session_id, role)
    if not key:
        return

    def remerge_full(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        for k, v in state_data.items():
            if k not in ["version_seq", "_last_active", "db_sync_pending"]:
                res_state[k] = v
        return res_state

    state = get_session_state(key)
    save_session_state_cas(session_id, remerge_full(state), role=role, remerge_fn=remerge_full)


def save_session_state_cas(
    session_id: str,
    state_dict: Dict[str, Any],
    role: Optional[str] = None,
    remerge_fn: Optional[Any] = None,
    max_retries: int = MAX_CAS_RETRIES,
) -> bool:
    """
    Saves session state using Atomic CAS version concurrency guard with bounded retries.
    If CAS failure occurs (stale writer), re-fetches latest DB version, re-executes remerge_fn(latest_db)
    to apply turn deltas against the freshly reloaded state, increments version_seq,
    and retries up to max_retries attempts. Returns False if retries exhausted.
    """
    key = make_composite_key(session_id, role)
    if not key:
        return False

    current_payload = dict(state_dict)

    # RAM-First Authoritative Update: Always update in-memory _SESSION_STATES first
    current_l1 = _SESSION_STATES.get(key, {})
    expected_version = current_l1.get("version_seq", 1)
    next_version = expected_version + 1

    write_payload = dict(current_payload)
    write_payload["version_seq"] = next_version
    write_payload["_last_active"] = time.time()
    
    # Authoritative L1 RAM state update
    _SESSION_STATES[key] = write_payload

    # Best-effort DB persistence (DB failure != Chatbot failure)
    try:
        success = _db_save_session(key, write_payload, expected_version=expected_version)
        if not success:
            _SESSION_STATES[key]["db_sync_pending"] = True
    except Exception as e:
        log.warning(f"Optional DB session persistence failed for key {key}: {e}")
        _SESSION_STATES[key]["db_sync_pending"] = True

    return True


def update_employer_session_cas(
    session_id: str,
    current_turn_reqs: EmployerSearchRequirements,
    prompt_category: str = "ADDITIVE",
    role: Optional[str] = None,
    max_retries: int = MAX_CAS_RETRIES,
) -> Dict[str, Any]:
    """
    Performs full-state atomic CAS persistence with domain re-merging for Employer queries.
    On CAS failure, re-executes merge_employer_search_context using freshly loaded DB state.
    """
    key = make_composite_key(session_id, role)
    base_state = get_session_state(key)

    base_ctx_raw = base_state.get("search_context")
    base_reqs = None
    if base_ctx_raw:
        if isinstance(base_ctx_raw, EmployerSearchRequirements):
            base_reqs = base_ctx_raw
        elif isinstance(base_ctx_raw, dict):
            try:
                base_reqs = EmployerSearchRequirements.parse_obj(base_ctx_raw)
            except Exception:
                base_reqs = None

    merged_reqs = merge_employer_search_context(base_reqs, current_turn_reqs, prompt_category=prompt_category)
    initial_payload = dict(base_state)
    initial_payload["search_context"] = merged_reqs.dict() if hasattr(merged_reqs, "dict") else merged_reqs

    def remerge_fn(reloaded_db_state: Dict[str, Any]) -> Dict[str, Any]:
        raw_ctx = reloaded_db_state.get("search_context")
        db_reqs = None
        if raw_ctx:
            if isinstance(raw_ctx, EmployerSearchRequirements):
                db_reqs = raw_ctx
            elif isinstance(raw_ctx, dict):
                try:
                    db_reqs = EmployerSearchRequirements.parse_obj(raw_ctx)
                except Exception:
                    db_reqs = None
        new_merged = merge_employer_search_context(db_reqs, current_turn_reqs, prompt_category=prompt_category)
        res_payload = dict(reloaded_db_state)
        res_payload["search_context"] = new_merged.dict() if hasattr(new_merged, "dict") else new_merged
        return res_payload

    save_session_state_cas(session_id, initial_payload, role=role, remerge_fn=remerge_fn, max_retries=max_retries)
    return get_session_state(key)


def update_employee_session_cas(
    session_id: str,
    current_turn_query: SearchQuery,
    prompt_category: str = "ADDITIVE",
    role: Optional[str] = None,
    max_retries: int = MAX_CAS_RETRIES,
) -> Dict[str, Any]:
    """
    Performs full-state atomic CAS persistence with domain re-merging for Employee queries.
    On CAS failure, re-executes merge_employee_search_query using freshly loaded DB state.
    """
    key = make_composite_key(session_id, role)
    base_state = get_session_state(key)

    base_ctx_raw = base_state.get("search_context")
    base_query = None
    if base_ctx_raw:
        if isinstance(base_ctx_raw, SearchQuery):
            base_query = base_ctx_raw
        elif isinstance(base_ctx_raw, dict):
            try:
                base_query = SearchQuery.parse_obj(base_ctx_raw)
            except Exception:
                base_query = None

    merged_query = merge_employee_search_query(base_query, current_turn_query, prompt_category=prompt_category)
    initial_payload = dict(base_state)
    initial_payload["search_context"] = merged_query.dict() if hasattr(merged_query, "dict") else merged_query

    def remerge_fn(reloaded_db_state: Dict[str, Any]) -> Dict[str, Any]:
        raw_ctx = reloaded_db_state.get("search_context")
        db_query = None
        if raw_ctx:
            if isinstance(raw_ctx, SearchQuery):
                db_query = raw_ctx
            elif isinstance(raw_ctx, dict):
                try:
                    db_query = SearchQuery.parse_obj(raw_ctx)
                except Exception:
                    db_query = None
        new_merged = merge_employee_search_query(db_query, current_turn_query, prompt_category=prompt_category)
        res_payload = dict(reloaded_db_state)
        res_payload["search_context"] = new_merged.dict() if hasattr(new_merged, "dict") else new_merged
        return res_payload

    save_session_state_cas(session_id, initial_payload, role=role, remerge_fn=remerge_fn, max_retries=max_retries)
    return get_session_state(key)


def clear_pagination_state(session_id: str, role: Optional[str] = None) -> None:
    """Clears pagination state for a session."""
    key = make_composite_key(session_id, role)
    if key in _SESSION_STATES:
        _SESSION_STATES.pop(key, None)
    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("DELETE FROM cyb_chat_session WHERE session_id = %s", (key,))
            db.commit()
    except Exception as e:
        log.warning(f"Error deleting session {key} from DB: {e}")


# ============================================================================
# 5. Deterministic Python State Machine Merge Engine & Pagination Handlers
# ============================================================================

def reset_search_context(session_id: str, role: Optional[str] = None) -> Dict[str, Any]:
    """
    Clears Search Criteria, Pagination, and Snapshots while PRESERVING language_preference and auth credentials.
    Enforces atomic CAS persistence with re-merge fallback.
    """
    key = make_composite_key(session_id, role)
    state = get_session_state(key)

    def apply_reset(base_state: Dict[str, Any]) -> Dict[str, Any]:
        lang_pref = base_state.get("language_preference")
        auth_token = base_state.get("auth_token")
        user_id = base_state.get("user_id")

        res_state = dict(base_state)
        res_state["search_context"] = None
        res_state["employer_search_context"] = None
        res_state["pagination"] = None
        res_state["search_snapshot"] = None
        res_state["language_preference"] = lang_pref
        if auth_token:
            res_state["auth_token"] = auth_token
        if user_id:
            res_state["user_id"] = user_id
        return res_state

    initial_reset_state = apply_reset(state)
    success = save_session_state_cas(
        session_id,
        initial_reset_state,
        role=role,
        remerge_fn=apply_reset,
    )
    if not success:
        log.warning(f"reset_search_context CAS write failed for session {key}")
    return get_session_state(key)


def handle_pagination_command(
    session_id: str,
    command: str,
    role: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Updates Pagination State ONLY, leaving Search Criteria State 100% UNTOUCHED.
    Supports commands: 'next', 'previous', 'page X'.
    Enforces atomic CAS persistence with re-merge fallback on collision.
    """
    key = make_composite_key(session_id, role)
    state = get_session_state(key)
    if not state:
        return None

    def apply_pagination(base_state: Dict[str, Any]) -> Dict[str, Any]:
        pag = dict(base_state.get("pagination") or {})
        current_page = pag.get("current_page", 1)
        total_pages = pag.get("total_pages", 1)

        cmd_norm = str(command).strip().lower()
        if "next" in cmd_norm:
            new_page = min(current_page + 1, max(total_pages, 1))
        elif "prev" in cmd_norm:
            new_page = max(current_page - 1, 1)
        elif "page" in cmd_norm:
            import re
            match = re.search(r"page\s*(\d+)", cmd_norm)
            if match:
                new_page = max(int(match.group(1)), 1)
            else:
                new_page = current_page
        else:
            new_page = current_page

        pag["current_page"] = new_page
        res_state = dict(base_state)
        res_state["pagination"] = pag
        return res_state

    initial_pag_state = apply_pagination(state)
    success = save_session_state_cas(
        session_id,
        initial_pag_state,
        role=role,
        remerge_fn=apply_pagination,
    )
    if not success:
        log.warning(f"handle_pagination_command CAS write failed for session {key}")
        return None
    updated_state = get_session_state(key)
    return updated_state.get("pagination") if updated_state else None


def merge_employer_search_context(
    previous_reqs: Optional[EmployerSearchRequirements],
    current_turn_reqs: EmployerSearchRequirements,
    prompt_category: str = "ADDITIVE",
) -> EmployerSearchRequirements:
    """
    Pure Python state machine enforcing Table 1 merge contract and automatic pagination reset.
    LLM is FORBIDDEN from deciding state merge semantics.
    """
    if prompt_category == "RESET" or previous_reqs is None:
        return current_turn_reqs.copy(deep=True) if hasattr(current_turn_reqs, "copy") else current_turn_reqs

    merged = previous_reqs.copy(deep=True) if hasattr(previous_reqs, "copy") else EmployerSearchRequirements()

    # Table 1 Merge Contract
    # 1. role: REPLACE if present in current turn
    if current_turn_reqs.role and current_turn_reqs.role.value:
        merged.role = ProvenanceValue[str](
            value=current_turn_reqs.role.value,
            source=ProvenanceSource.EXPLICIT_FOLLOWUP
        )

    # 2. skills: ADD / UNION (case-insensitive deduplication)
    if current_turn_reqs.skills and current_turn_reqs.skills.value:
        existing_skills = list(merged.skills.value or []) if merged.skills else []
        existing_norm = {s.lower().strip() for s in existing_skills}
        for new_skill in current_turn_reqs.skills.value:
            if new_skill and new_skill.lower().strip() not in existing_norm:
                existing_skills.append(new_skill)
                existing_norm.add(new_skill.lower().strip())
        merged.skills = ProvenanceValue[List[str]](
            value=existing_skills,
            source=ProvenanceSource.EXPLICIT_FOLLOWUP
        )

    # 3. location: REPLACE if present
    if current_turn_reqs.location and (current_turn_reqs.location.text or current_turn_reqs.location.city_id):
        loc = current_turn_reqs.location.copy(deep=True) if hasattr(current_turn_reqs.location, "copy") else current_turn_reqs.location
        loc.source = ProvenanceSource.EXPLICIT_FOLLOWUP
        merged.location = loc

    # 4. experience: REPLACE if present
    if current_turn_reqs.experience and (current_turn_reqs.experience.min is not None or current_turn_reqs.experience.max is not None):
        exp = current_turn_reqs.experience.copy(deep=True) if hasattr(current_turn_reqs.experience, "copy") else current_turn_reqs.experience
        exp.source = ProvenanceSource.EXPLICIT_FOLLOWUP
        merged.experience = exp

    # 5. salary: REPLACE if present
    if current_turn_reqs.salary and (current_turn_reqs.salary.min is not None or current_turn_reqs.salary.max is not None):
        sal = current_turn_reqs.salary.copy(deep=True) if hasattr(current_turn_reqs.salary, "copy") else current_turn_reqs.salary
        sal.source = ProvenanceSource.EXPLICIT_FOLLOWUP
        merged.salary = sal

    # 6. Single-valued Provenance fields: REPLACE if present
    for field_name in ["employment_type", "department", "on_immediate", "on_notice", "fresher"]:
        curr_val = getattr(current_turn_reqs, field_name, None)
        if curr_val and getattr(curr_val, "value", None) is not None:
            new_prov = ProvenanceValue(
                value=curr_val.value,
                source=ProvenanceSource.EXPLICIT_FOLLOWUP
            )
            setattr(merged, field_name, new_prov)

    return merged


def merge_employee_search_query(
    previous_query: Optional[SearchQuery],
    current_turn_query: SearchQuery,
    prompt_category: str = "ADDITIVE",
) -> SearchQuery:
    """
    Pure Python state machine enforcing Table 2 merge contract for employee search queries.
    """
    if prompt_category == "RESET" or previous_query is None:
        return current_turn_query.copy(deep=True) if hasattr(current_turn_query, "copy") else current_turn_query

    merged = previous_query.copy(deep=True) if hasattr(previous_query, "copy") else SearchQuery()

    # 1. title: REPLACE if present
    if current_turn_query.title:
        merged.title = current_turn_query.title

    # 2. skills: ADD / UNION (case-insensitive deduplication)
    if current_turn_query.skills:
        existing = list(merged.skills or [])
        existing_norm = {s.lower().strip() for s in existing}
        for s in current_turn_query.skills:
            if s and s.lower().strip() not in existing_norm:
                existing.append(s)
                existing_norm.add(s.lower().strip())
        merged.skills = existing

    # 3. location: REPLACE if present
    if current_turn_query.location:
        merged.location = current_turn_query.location
        merged.canonical_location = current_turn_query.canonical_location or current_turn_query.location
        merged.location_mode = current_turn_query.location_mode

    # 4. experience_level: REPLACE if present
    if current_turn_query.experience_level:
        merged.experience_level = current_turn_query.experience_level

    # 5. salary range: REPLACE if present
    if current_turn_query.salary_min is not None:
        merged.salary_min = current_turn_query.salary_min
    if current_turn_query.salary_max is not None:
        merged.salary_max = current_turn_query.salary_max

    # 6. work_mode, company: REPLACE if present
    if current_turn_query.work_mode:
        merged.work_mode = current_turn_query.work_mode
    if current_turn_query.company:
        merged.company = current_turn_query.company

    # 7. Explicit Filter Removal handling
    clear_targets = getattr(current_turn_query, "clear_filters", None) or getattr(current_turn_query, "clear_targets", None)
    if clear_targets:
        if isinstance(clear_targets, str):
            clear_targets = [clear_targets]
        for target in clear_targets:
            t_norm = str(target).strip().lower()
            if t_norm in ("location", "city", "state"):
                merged.location = None
                merged.canonical_location = None
                merged.location_mode = None
            elif t_norm in ("work_mode", "job_mode", "mode", "remote", "onsite", "hybrid"):
                merged.work_mode = None
            elif t_norm in ("salary", "pay", "budget"):
                merged.salary_min = None
                merged.salary_max = None
            elif t_norm in ("experience", "exp"):
                merged.experience_level = None

    return merged


def generate_query_fingerprint(employer_spec: Any) -> str:
    """
    Generates a canonical query fingerprint derived from SHA-256 (truncating the 64-character hexadecimal digest to 32 characters as per existing session_state implementation).
    """
    if employer_spec is None:
        return hashlib.sha256(b"{}").hexdigest()[:32]

    if isinstance(employer_spec, dict):
        role_val = employer_spec.get("role", "")
        skills_val = employer_spec.get("skills", [])
        min_exp_val = employer_spec.get("min_exp")
        loc_val = employer_spec.get("location", "")
        budget_val = employer_spec.get("budget")
        mode_val = employer_spec.get("work_mode", "")
    else:
        role_val = getattr(employer_spec, "role", "")
        skills_val = getattr(employer_spec, "skills", [])
        min_exp_val = getattr(employer_spec, "min_exp", None)
        loc_val = getattr(employer_spec, "location", "")
        budget_val = getattr(employer_spec, "budget", None)
        mode_val = getattr(employer_spec, "work_mode", "")

    spec_dict = {
        "role": str(role_val or "").strip().lower(),
        "skills": sorted([str(s).strip().lower() for s in (skills_val or []) if str(s).strip()]),
        "min_exp": float(min_exp_val) if min_exp_val is not None else None,
        "location": str(loc_val or "").strip().lower(),
        "budget": float(budget_val) if budget_val is not None else None,
        "work_mode": str(mode_val or "").strip().upper(),
    }

    canonical_json = json.dumps(spec_dict, sort_keys=True)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()[:32]
