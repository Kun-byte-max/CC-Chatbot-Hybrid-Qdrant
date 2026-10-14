import os
import time
import json
import logging
import threading
from dataclasses import dataclass, field, is_dataclass, asdict
import hashlib
from typing import Dict, Any, Optional, List, Union

try:
    from backend.repositories.db import get_db
    from backend.schemas.search_query import SearchSnapshot, SearchQuery, SearchTarget, LocationMode
    from backend.schemas.search_criteria import SearchCriteria
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
    from schemas.search_query import SearchSnapshot, SearchQuery, SearchTarget, LocationMode  # type: ignore
    from schemas.search_criteria import SearchCriteria  # type: ignore
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
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "14400"))  # 4 hours
PENDING_OPTION_MAPPING_TTL_SECONDS = int(os.getenv("PENDING_OPTION_MAPPING_TTL_SECONDS", "900"))  # 15 minutes
SWEEP_INTERVAL_SECONDS = 300  # 5 minutes
_LAST_SWEEP_TIME: float = 0.0
MAX_CAS_RETRIES = 3

FORBIDDEN_KEYS = {
    "token", "jwt", "password", "session_id", "access_token", "auth_token",
    "cookie", "secret", "authorization", "api_key"
}

_SESSION_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_MUTEX = threading.Lock()


def get_session_lock(key: str) -> threading.Lock:
    """Retrieves or creates a thread-safe Lock for a specific session composite key."""
    with _LOCKS_MUTEX:
        if key not in _SESSION_LOCKS:
            _SESSION_LOCKS[key] = threading.Lock()
        return _SESSION_LOCKS[key]


def begin_search_generation(session_id: str, role: Optional[str] = None) -> int:
    """
    Atomically advances and returns the search generation integer for a session under the session lock.
    Must be called at the beginning of a fresh search before retrieval or network operations,
    so that in-flight pagination requests from an earlier search generation are prevented from
    overwriting the new search result pool.
    """
    key = make_composite_key(session_id, role)
    if not key:
        return 1

    with get_session_lock(key):
        state = get_session_state(key)
        new_gen = int(state.get("search_gen", 0)) + 1
        res_state = dict(state)
        res_state["search_gen"] = new_gen
        save_session_state_cas(session_id, res_state, role=role)
        return new_gen


def get_search_generation(session_id: str, role: Optional[str] = None) -> int:
    """Retrieves current search generation integer for a session."""
    key = make_composite_key(session_id, role)
    if not key:
        return 1
    state = get_session_state(key)
    return int(state.get("search_gen", 1))


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
    if hasattr(obj, "model_dump") and callable(obj.model_dump):
        return obj.model_dump()
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
# 4. L1/L2 Freshness Validation, Cleanup & High-Level Session Accessors
# ============================================================================

def cleanup_stale_sessions(force: bool = False) -> int:
    """
    Safely purges RAM sessions inactive beyond SESSION_TTL_SECONDS.
    Performs opportunistic sweeps at most once every SWEEP_INTERVAL_SECONDS unless force=True.
    Does NOT update session activity timestamps during inspection.
    """
    global _LAST_SWEEP_TIME
    now = time.time()
    if not force and (now - _LAST_SWEEP_TIME < SWEEP_INTERVAL_SECONDS):
        return 0

    _LAST_SWEEP_TIME = now
    purged_count = 0
    # Create key snapshot to safely iterate over mutable dict
    keys_snapshot = list(_SESSION_STATES.keys())
    for k in keys_snapshot:
        entry = _SESSION_STATES.get(k)
        if entry:
            last_act = float(entry.get("_last_active", 0))
            if now - last_act > SESSION_TTL_SECONDS:
                _SESSION_STATES.pop(k, None)
                purged_count += 1
    return purged_count


def get_session_state(session_id: str, role: Optional[str] = None, touch: bool = False) -> Dict[str, Any]:
    """
    Retrieves session state dictionary enforcing role isolation, L1/L2 freshness validation,
    and passive read vs active request touch semantics.
    """
    key = make_composite_key(session_id, role)
    if not key:
        return {}

    now = time.time()
    # Trigger opportunistic sweep check
    cleanup_stale_sessions()

    l1_entry = _SESSION_STATES.get(key)
    if l1_entry:
        l1_last_active = float(l1_entry.get("_last_active", 0))
        if now - l1_last_active > SESSION_TTL_SECONDS:
            _SESSION_STATES.pop(key, None)
            l1_entry = None

    # Fetch L2 DB state to validate version freshness
    db_state = None
    try:
        db_state = _db_load_session(key)
    except Exception as e:
        log.warning(f"Error loading session {key} from DB: {e}")

    active_state = None
    if l1_entry:
        if db_state:
            db_ver = db_state.get("version_seq", 1)
            l1_ver = l1_entry.get("version_seq", 1)
            if db_ver > l1_ver:
                _SESSION_STATES[key] = db_state
                active_state = db_state
            elif db_ver < l1_ver:
                active_state = l1_entry
            else:
                active_state = l1_entry
        else:
            active_state = l1_entry
    elif db_state:
        _SESSION_STATES[key] = db_state
        active_state = db_state

    if active_state:
        if touch:
            active_state["_last_active"] = now
        return active_state

    # Initialize fresh session
    new_state = {
        "_last_active": now,
        "version_seq": 1,
        "language_preference": None,
    }
    _SESSION_STATES[key] = new_state
    return new_state


def touch_session_state(session_id: str, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> bool:
    """
    Refreshes _last_active timestamp for a genuine incoming user chat request.
    Will NOT resurrect an already expired session.
    """
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return False

    now = time.time()
    entry = _SESSION_STATES.get(key)
    if not entry:
        return False

    last_act = float(entry.get("_last_active", 0))
    if now - last_act > SESSION_TTL_SECONDS:
        _SESSION_STATES.pop(key, None)
        return False

    entry["_last_active"] = now
    return True


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
    """
    Saves pagination state dict with atomic CAS persistence under per-session lock.
    Conditionally merges pagination updates:
    1. Rejects pagination writes from an older search generation than the session's active search_gen.
    2. Rejects pagination writes if the session was reset after the update's _reset_seq was captured.
    3. Within the same search generation, applies the monotonic cursor guard to prevent stale
       in-flight pagination responses from regressing an already-advanced cursor.
    4. For a new search generation (or fresh pool initialization), accepts and replaces the result pool.
    """
    key = make_composite_key(session_id, role)
    if not key:
        return

    with get_session_lock(key):
        def remerge_pag(base_state: Dict[str, Any]) -> Dict[str, Any]:
            res_state = dict(base_state)
            current_pag = res_state.get("pagination")

            if pagination_data is None:
                res_state["pagination"] = None
                return res_state

            cur_reset_seq = res_state.get("_reset_seq", 0)
            pag_reset_seq = pagination_data.get("_reset_seq", cur_reset_seq)
            # If session was reset after this pagination snapshot was produced, do not write
            if cur_reset_seq > pag_reset_seq:
                return res_state

            try:
                cur_gen = int(res_state.get("search_gen", 1))
            except (ValueError, TypeError):
                cur_gen = 1

            raw_pag_gen = pagination_data.get("search_gen")
            if raw_pag_gen is not None:
                try:
                    pag_gen = int(raw_pag_gen)
                except (ValueError, TypeError):
                    # Malformed generation is rejected to prevent crashing or bypassing validation
                    return res_state
            else:
                # Missing search_gen:
                # If the session already has an active pool or generation advanced past 1,
                # a write lacking search_gen is treated as stale (generation 0) and rejected.
                if current_pag or cur_gen > 1:
                    pag_gen = 0
                else:
                    # Legitimate first-time / legacy session initialization before multi-generation
                    pag_gen = cur_gen

            # Reject writes from an older search generation than the session's active search_gen
            if cur_gen > pag_gen:
                return res_state

            if current_pag and isinstance(current_pag, dict):
                try:
                    cur_pag_gen = int(current_pag.get("search_gen", cur_gen))
                except (ValueError, TypeError):
                    cur_pag_gen = cur_gen

                # If this write is from a newer search generation, accept and replace the old pool
                if pag_gen > cur_pag_gen:
                    merged_pag = dict(pagination_data)
                    merged_pag["search_gen"] = pag_gen
                    res_state["pagination"] = merged_pag
                    res_state["search_gen"] = pag_gen
                    return res_state

                # Check for query/search identity match if query_fingerprint is tracked
                curr_fp = current_pag.get("query_fingerprint")
                new_fp = pagination_data.get("query_fingerprint")
                if curr_fp and new_fp and curr_fp != new_fp:
                    return res_state

                merged_pag = dict(pagination_data)
                merged_pag["search_gen"] = cur_gen
                curr_page = int(current_pag.get("current_page", 1))
                new_page = int(pagination_data.get("current_page", 1))

                # Monotonic cursor guard within the same generation
                is_fresh_init = bool(pagination_data.get("_is_fresh_init") or (pagination_data.get("all_recs") and pagination_data.get("current_page") == 1 and curr_page > 1 and not current_pag.get("has_next", True)))
                if not is_fresh_init and curr_page > new_page:
                    merged_pag["current_page"] = curr_page
                    total_pages = int(merged_pag.get("total_pages", current_pag.get("total_pages", 1)))
                    merged_pag["has_next"] = curr_page < total_pages
                    merged_pag["has_prev"] = curr_page > 1
                res_state["pagination"] = merged_pag
            else:
                merged_pag = dict(pagination_data)
                merged_pag["search_gen"] = max(cur_gen, pag_gen)
                res_state["pagination"] = merged_pag
                res_state["search_gen"] = max(cur_gen, pag_gen)

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


def get_selected_employment_context(session_id: str, role: Optional[str] = None) -> Optional[str]:
    """Retrieves stored selected employment company name for a session."""
    key = make_composite_key(session_id, role)
    if not key:
        return None
    state = get_session_state(key)
    return state.get("selected_employment_company")


def get_selected_employment_record_key(session_id: str, role: Optional[str] = None) -> Optional[str]:
    """Retrieves stored selected exact employment record key for a session."""
    key = make_composite_key(session_id, role)
    if not key:
        return None
    state = get_session_state(key)
    return state.get("selected_employment_record_key")


def set_selected_employment_context(session_id: str, company_name: str, record_key: Optional[str] = None, role: Optional[str] = None) -> None:
    """Saves selected employment company name and optional exact record key into RAM session state."""
    key = make_composite_key(session_id, role)
    if not key or not company_name:
        return

    def remerge_emp(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["selected_employment_company"] = company_name.strip()
        if record_key:
            res_state["selected_employment_record_key"] = record_key.strip()
        elif "selected_employment_record_key" in res_state and not record_key:
            res_state["selected_employment_record_key"] = None
        return res_state

    state = get_session_state(key)
    save_session_state_cas(session_id, remerge_emp(state), role=role, remerge_fn=remerge_emp)


def clear_selected_employment_context(session_id: str, role: Optional[str] = None) -> None:
    """Clears selected employment company name and exact record key from session state."""
    key = make_composite_key(session_id, role)
    if not key:
        return

    def remerge_clear(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["selected_employment_company"] = None
        res_state["selected_employment_record_key"] = None
        return res_state

    state = get_session_state(key)
    save_session_state_cas(session_id, remerge_clear(state), role=role, remerge_fn=remerge_clear)


def set_pending_requested_level(session_id: str, level: int, role: Optional[str] = None) -> None:
    """Saves pending verification requested_level (1-4) into RAM session state."""
    key = make_composite_key(session_id, role)
    if not key or not session_id or level not in (1, 2, 3, 4):
        return

    def remerge_level(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["pending_requested_level"] = int(level)
        return res_state

    state = get_session_state(key)
    save_session_state_cas(session_id, remerge_level(state), role=role, remerge_fn=remerge_level)


def get_pending_requested_level(session_id: str, role: Optional[str] = None) -> Optional[int]:
    """Retrieves stored pending verification requested_level for a session."""
    key = make_composite_key(session_id, role)
    if not key or not session_id:
        return None
    state = get_session_state(key)
    val = state.get("pending_requested_level")
    try:
        return int(val) if val in (1, 2, 3, 4, "1", "2", "3", "4") else None
    except Exception:
        return None


def clear_pending_requested_level(session_id: str, role: Optional[str] = None) -> None:
    """Clears pending verification requested_level from RAM session state."""
    key = make_composite_key(session_id, role)
    if not key or not session_id:
        return

    def remerge_clear_lvl(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["pending_requested_level"] = None
        return res_state

    state = get_session_state(key)
    save_session_state_cas(session_id, remerge_clear_lvl(state), role=role, remerge_fn=remerge_clear_lvl)


def get_session_option_mapping(session_id: str, role: Optional[str] = None) -> Dict[str, str]:
    """
    Retrieves session-bound pending option mapping dict (e.g. {'emp_rec_1': composite_key}).
    Enforces PENDING_OPTION_MAPPING_TTL_SECONDS expiration.
    """
    key = make_composite_key(session_id, role)
    if not key:
        return {}
    state = get_session_state(key)
    timestamped_mapping = state.get("pending_option_mapping")
    if not isinstance(timestamped_mapping, dict):
        return {}

    created_at = float(timestamped_mapping.get("created_at", 0))
    mapping = timestamped_mapping.get("mapping")
    now = time.time()

    if now - created_at > PENDING_OPTION_MAPPING_TTL_SECONDS:
        clear_session_option_mapping(session_id, role=role)
        return {}

    if isinstance(mapping, dict):
        return dict(mapping)
    return {}


def set_session_option_mapping(session_id: str, mapping: Dict[str, str], role: Optional[str] = None) -> None:
    """Saves session-bound pending option mapping dict into RAM session state with creation timestamp."""
    key = make_composite_key(session_id, role)
    if not key:
        return

    payload = {
        "created_at": time.time(),
        "mapping": dict(mapping) if isinstance(mapping, dict) else {}
    }

    def remerge_opt(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["pending_option_mapping"] = payload
        return res_state

    state = get_session_state(key)
    save_session_state_cas(session_id, remerge_opt(state), role=role, remerge_fn=remerge_opt)


def consume_session_option_mapping(session_id: str, submitted_key: str, role: Optional[str] = None) -> Optional[str]:
    """
    Atomically resolves and consumes a single-use pending option key.
    Prevents replay attacks while preserving selected employment context.
    """
    key = make_composite_key(session_id, role)
    if not key or not submitted_key:
        return None

    opt_map = get_session_option_mapping(session_id, role=role)
    target_composite_key = opt_map.get(submitted_key.strip().lower())
    if not target_composite_key:
        return None

    # Consume the mapping payload so key cannot be replayed
    clear_session_option_mapping(session_id, role=role)
    return target_composite_key


def clear_session_option_mapping(session_id: str, role: Optional[str] = None) -> None:
    """Clears pending option mapping dict from RAM session state."""
    key = make_composite_key(session_id, role)
    if not key:
        return

    def remerge_clear_opt(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["pending_option_mapping"] = None
        return res_state

    state = get_session_state(key)
    save_session_state_cas(session_id, remerge_clear_opt(state), role=role, remerge_fn=remerge_clear_opt)





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
    return get_session_state(session_id, role=role)


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
    base_state = get_session_state(session_id, role=role)

    base_ctx_raw = base_state.get("search_context")
    base_query = None
    if base_ctx_raw:
        if isinstance(base_ctx_raw, SearchQuery):
            base_query = base_ctx_raw
        elif isinstance(base_ctx_raw, dict):
            try:
                if hasattr(SearchQuery, "model_validate"):
                    base_query = SearchQuery.model_validate(base_ctx_raw)
                else:
                    base_query = SearchQuery.parse_obj(base_ctx_raw)
            except Exception as e:
                log.warning("Failed to validate base SearchQuery: %s", e)
                base_query = None

    merged_query = merge_employee_search_query(base_query, current_turn_query, prompt_category=prompt_category)
    initial_payload = dict(base_state)
    initial_payload["search_context"] = merged_query.model_dump() if hasattr(merged_query, "model_dump") else (merged_query.dict() if hasattr(merged_query, "dict") else merged_query)

    def remerge_fn(reloaded_db_state: Dict[str, Any]) -> Dict[str, Any]:
        raw_ctx = reloaded_db_state.get("search_context")
        db_query = None
        if raw_ctx:
            if isinstance(raw_ctx, SearchQuery):
                db_query = raw_ctx
            elif isinstance(raw_ctx, dict):
                try:
                    if hasattr(SearchQuery, "model_validate"):
                        db_query = SearchQuery.model_validate(raw_ctx)
                    else:
                        db_query = SearchQuery.parse_obj(raw_ctx)
                except Exception as e:
                    log.warning("Failed to validate reloaded SearchQuery: %s", e)
                    db_query = None
        new_merged = merge_employee_search_query(db_query, current_turn_query, prompt_category=prompt_category)
        res_payload = dict(reloaded_db_state)
        res_payload["search_context"] = new_merged.model_dump() if hasattr(new_merged, "model_dump") else (new_merged.dict() if hasattr(new_merged, "dict") else new_merged)
        return res_payload

    save_session_state_cas(session_id, initial_payload, role=role, remerge_fn=remerge_fn, max_retries=max_retries)
    return get_session_state(session_id, role=role)


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


def get_candidate_search_criteria(session_id: str, role: Optional[str] = None) -> Optional[SearchCriteria]:
    """Retrieves candidate search criteria from session state."""
    key = make_composite_key(session_id, role)
    state = get_session_state(key)
    raw_ctx = state.get("candidate_search_context")
    if not raw_ctx:
        return None
    if isinstance(raw_ctx, SearchCriteria):
        return raw_ctx
    if isinstance(raw_ctx, dict):
        try:
            return SearchCriteria.from_parsed_dict(raw_ctx)
        except Exception as e:
            log.warning(f"Error parsing candidate_search_context: {e}")
            return None
    return None


def set_candidate_search_criteria(session_id: str, criteria: SearchCriteria, role: Optional[str] = None) -> None:
    """Saves candidate search criteria to session state."""
    key = make_composite_key(session_id, role)
    state = dict(get_session_state(key))
    state["candidate_search_context"] = criteria.model_dump() if hasattr(criteria, "model_dump") else criteria.dict()
    save_session_state_cas(session_id, state, role=role)


def merge_candidate_search_criteria(
    previous: Optional[SearchCriteria],
    current: SearchCriteria,
    query_type: Optional[str] = None,
    parsed_dict: Optional[Dict[str, Any]] = None,
) -> SearchCriteria:
    """
    Merges previous turn candidate search criteria with current turn criteria for multi-turn refinement.
    - If query_type is 'NEW_SEARCH' or previous is None: returns current criteria fresh.
    - If query_type is 'REFINEMENT':
      - Skills: Merges unique skills (previous + current)
      - Location: Replaces location if current.location is present
      - Experience: Replaces experience_min if current.experience_min is present
      - Title: Replaces title if current.title is present
    """
    if not previous or query_type == "NEW_SEARCH":
        return current

    # Explicit Filter Clearing handling check upfront
    clear_targets = getattr(current, "clear_filters", None) or getattr(current, "clear_targets", None)
    if parsed_dict:
        clear_targets = clear_targets or parsed_dict.get("clear_filters") or parsed_dict.get("clear_targets")

    # Check explicit skill_operation signal or query_type
    skill_op = getattr(current, "skill_operation", None)
    if parsed_dict and not skill_op:
        skill_op = parsed_dict.get("skill_operation")

    if not clear_targets:
        # If current turn searches for a specific person by name, start fresh for that name
        if current.candidate_name:
            return current

        # If current turn specifies a new title or keyword, start fresh for new topic
        curr_kw = getattr(current, "keyword", None) or current.title
        prev_kw = getattr(previous, "keyword", None) or previous.title
        has_different_title = bool(current.title and previous.title and current.title.lower().strip() != previous.title.lower().strip())
        has_different_kw = bool(curr_kw and prev_kw and str(curr_kw).lower().strip() != str(prev_kw).lower().strip())

        # Check non-overlapping skills only if query_type == "NEW_SEARCH" or skill_op is not explicitly defined
        prev_skills_lower = [str(s).lower() for s in (previous.skills or []) if s]
        curr_skills_lower = [str(s).lower() for s in (current.skills or []) if s]
        has_no_skill_overlap = bool(curr_skills_lower and prev_skills_lower and not any(s in prev_skills_lower for s in curr_skills_lower))

        if has_different_title or has_different_kw:
            return current
        if has_no_skill_overlap and skill_op != "REPLACE" and getattr(current, "query_type", None) == "NEW_SEARCH":
            return current

    merged_dict = previous.model_dump() if hasattr(previous, "model_dump") else previous.dict()

    # Skills merge handling (ADD vs REPLACE vs default additive merge):
    if current.skills:
        if skill_op == "REPLACE":
            merged_dict["skills"] = list(current.skills)
        else:
            prev_skills = merged_dict.get("skills") or []
            combined_skills = list(prev_skills)
            for s in current.skills:
                if s and str(s).strip():
                    clean_s = str(s).strip()
                    if not any(clean_s.lower() == existing.lower() for existing in combined_skills):
                        combined_skills.append(clean_s)
            merged_dict["skills"] = combined_skills

    # Location replacement:
    if current.location:
        merged_dict["location"] = current.location
        merged_dict["canonical_location"] = current.canonical_location or current.location
        merged_dict["location_mode"] = current.location_mode
    elif current.location_mode == LocationMode.NEAR_ME:
        merged_dict["location_mode"] = LocationMode.NEAR_ME

    # Experience_min / Experience_max replacement & validation:
    if current.experience_min is not None:
        merged_dict["experience_min"] = current.experience_min
        merged_dict["experience_level"] = current.experience_level

    if current.experience_max is not None:
        merged_dict["experience_max"] = current.experience_max

    # Sanity validation: If min > max, invalidate criteria by setting experience_max = None or clear bounds
    exp_min_val = merged_dict.get("experience_min")
    exp_max_val = merged_dict.get("experience_max")
    if exp_min_val is not None and exp_max_val is not None and exp_min_val > exp_max_val:
        merged_dict["experience_max"] = None

    # Title replacement:
    if current.title:
        merged_dict["title"] = current.title

    # Company replacement:
    if current.company:
        merged_dict["company"] = current.company

    # Seniority level replacement:
    if current.seniority_level:
        merged_dict["seniority_level"] = current.seniority_level

    # Work mode / remote replacement:
    if current.work_mode:
        merged_dict["work_mode"] = current.work_mode
    if current.remote is not None:
        merged_dict["remote"] = current.remote

    # Salary min replacement:
    if current.salary_min is not None:
        merged_dict["salary_min"] = current.salary_min

    # Explicit Filter Clearing handling
    clear_targets = getattr(current, "clear_filters", None) or getattr(current, "clear_targets", None)
    if parsed_dict:
        clear_targets = clear_targets or parsed_dict.get("clear_filters") or parsed_dict.get("clear_targets")

    if clear_targets:
        if isinstance(clear_targets, str):
            clear_targets = [clear_targets]
        for target in clear_targets:
            t_norm = str(target).strip().lower()
            if t_norm in ("location", "city", "state"):
                merged_dict["location"] = None
                merged_dict["canonical_location"] = None
                merged_dict["location_mode"] = None
            elif t_norm in ("experience", "exp"):
                merged_dict["experience_min"] = None
                merged_dict["experience_max"] = None
                merged_dict["experience_level"] = None
            elif t_norm in ("experience_min", "min_experience", "min_exp"):
                merged_dict["experience_min"] = None
            elif t_norm in ("experience_max", "max_experience", "max_exp"):
                merged_dict["experience_max"] = None
            elif t_norm in ("company", "current_company"):
                merged_dict["company"] = None
            elif t_norm in ("skills", "skill"):
                merged_dict["skills"] = []
            elif t_norm in ("title", "role", "designation"):
                merged_dict["title"] = None

    merged_dict["search_target"] = "PEOPLE_SEARCH"
    merged_dict["target"] = SearchTarget.PEOPLE_SEARCH

    return SearchCriteria.from_parsed_dict(merged_dict)


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
        res_state["_reset_seq"] = int(base_state.get("_reset_seq", 0)) + 1
        res_state["search_context"] = None
        res_state["employer_search_context"] = None
        res_state["candidate_search_context"] = None
        res_state["pagination"] = None
        res_state["search_snapshot"] = None
        res_state["recommendation_context"] = None
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


def claim_next_pagination_page(
    session_id: str,
    command: str,
    role: Optional[str] = None
) -> Dict[str, Any]:
    """
    Atomically claims the next pagination page under a per-session lock.
    Calculates and persists the new current_page in a single critical section,
    preventing concurrent requests from claiming and returning the same page slice.
    Returns:
        {
            "success": bool,
            "claimed_page": int,
            "is_oob": bool,
            "saved_p_state": dict,
            "error": Optional[str]
        }
    """
    key = make_composite_key(session_id, role)
    if not key:
        return {"success": False, "error": "Invalid session identifier"}

    with get_session_lock(key):
        state = get_session_state(key)
        pag = dict(state.get("pagination") or {})
        if not pag:
            return {"success": False, "error": "No active pagination state found"}

        current_page = int(pag.get("current_page", 1))
        total_pages = int(pag.get("total_pages", 1))

        cmd_norm = str(command).strip().lower()
        if "next" in cmd_norm:
            claimed_page = current_page + 1
        elif "prev" in cmd_norm:
            claimed_page = max(current_page - 1, 1)
        elif "page" in cmd_norm:
            import re
            match = re.search(r"page\s*(\d+)", cmd_norm)
            if match:
                claimed_page = max(int(match.group(1)), 1)
            else:
                claimed_page = current_page
        else:
            claimed_page = current_page

        is_oob = claimed_page > total_pages
        # Bound exhausted cursor so repeated 'next' requests after exhaustion do not advance indefinitely
        if is_oob:
            claimed_page = max(total_pages + 1, 1)

        # Inherit generation of existing result pool
        pag_gen = int(pag.get("search_gen") or state.get("search_gen", 1))
        pag["search_gen"] = pag_gen

        # Atomically advance and persist stored pagination state under the lock
        pag["current_page"] = claimed_page
        pag["has_next"] = claimed_page < total_pages
        pag["has_prev"] = claimed_page > 1

        res_state = dict(state)
        res_state["pagination"] = pag
        save_session_state_cas(session_id, res_state, role=role)

        return {
            "success": True,
            "claimed_page": claimed_page,
            "is_oob": is_oob,
            "saved_p_state": pag,
            "error": None
        }


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
        if hasattr(current_turn_reqs, "model_copy"):
            return current_turn_reqs.model_copy(deep=True)
        return current_turn_reqs.copy(deep=True) if hasattr(current_turn_reqs, "copy") else current_turn_reqs

    if hasattr(previous_reqs, "model_copy"):
        merged = previous_reqs.model_copy(deep=True)
    else:
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
        if hasattr(current_turn_reqs.location, "model_copy"):
            loc = current_turn_reqs.location.model_copy(deep=True)
        else:
            loc = current_turn_reqs.location.copy(deep=True) if hasattr(current_turn_reqs.location, "copy") else current_turn_reqs.location
        loc.source = ProvenanceSource.EXPLICIT_FOLLOWUP
        merged.location = loc

    # 4. experience: REPLACE if present
    if current_turn_reqs.experience and (current_turn_reqs.experience.min is not None or current_turn_reqs.experience.max is not None):
        if hasattr(current_turn_reqs.experience, "model_copy"):
            exp = current_turn_reqs.experience.model_copy(deep=True)
        else:
            exp = current_turn_reqs.experience.copy(deep=True) if hasattr(current_turn_reqs.experience, "copy") else current_turn_reqs.experience
        exp.source = ProvenanceSource.EXPLICIT_FOLLOWUP
        merged.experience = exp

    # 5. salary: REPLACE if present
    if current_turn_reqs.salary and (current_turn_reqs.salary.min is not None or current_turn_reqs.salary.max is not None):
        if hasattr(current_turn_reqs.salary, "model_copy"):
            sal = current_turn_reqs.salary.model_copy(deep=True)
        else:
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
        if hasattr(current_turn_query, "model_copy"):
            return current_turn_query.model_copy(deep=True)
        return current_turn_query.copy(deep=True) if hasattr(current_turn_query, "copy") else current_turn_query

    if hasattr(previous_query, "model_copy"):
        merged = previous_query.model_copy(deep=True)
    else:
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

    # 6. work_mode, employment_type, company, urgent: REPLACE if present
    if current_turn_query.work_mode:
        merged.work_mode = current_turn_query.work_mode
    if getattr(current_turn_query, "employment_type", None):
        merged.employment_type = current_turn_query.employment_type
    if getattr(current_turn_query, "role_type_id", None) is not None:
        merged.role_type_id = current_turn_query.role_type_id
    if current_turn_query.company:
        merged.company = current_turn_query.company
    if getattr(current_turn_query, "urgent", None) is not None:
        merged.urgent = current_turn_query.urgent

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
            elif t_norm in ("work_mode", "job_mode", "mode", "remote", "onsite", "hybrid", "field"):
                merged.work_mode = None
            elif t_norm in ("employment_type", "job_type", "role_type", "employment", "internship"):
                merged.employment_type = None
                merged.role_type_id = None
            elif t_norm in ("salary", "pay", "budget"):
                merged.salary_min = None
                merged.salary_max = None
            elif t_norm in ("experience", "exp"):
                merged.experience_level = None
            elif t_norm in ("urgent", "urgency"):
                merged.urgent = None

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
