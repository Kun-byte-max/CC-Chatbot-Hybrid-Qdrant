import os
import time
import json
import logging
from typing import Dict, Any, Optional, List

try:
    from backend.repositories.db import get_db
    from backend.schemas.search_query import SearchSnapshot, SearchQuery
except ModuleNotFoundError:
    from repositories.db import get_db  # type: ignore
    from schemas.search_query import SearchSnapshot, SearchQuery  # type: ignore

log = logging.getLogger(__name__)

# L1 Global in-memory session cache
_SESSION_STATES: Dict[str, Dict[str, Any]] = {}
SESSION_TTL_SECONDS = 3600 * 4  # 4 hours


def _ensure_session_table():
    """Auto-creates cyb_chat_session table in DB if it does not exist."""
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
                    updated_at DOUBLE
                )
            """
            )
            db.commit()
    except Exception as e:
        log.warning(f"Failed to ensure cyb_chat_session table: {e}")


# Initialize table on import
_ensure_session_table()


def _db_load_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Loads session record from MySQL database."""
    if not session_id:
        return None
    try:
        with get_db() as db:
            cursor = db.cursor()
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
            else:
                ctx_str, pag_str, snap_str, rec_str, updated_at = row

            if updated_at and (time.time() - float(updated_at) > SESSION_TTL_SECONDS):
                return None

            data = {"_last_active": float(updated_at) if updated_at else time.time()}
            if ctx_str:
                data["search_context"] = json.loads(ctx_str)
            if pag_str:
                data["pagination"] = json.loads(pag_str)
            if snap_str:
                snap_dict = json.loads(snap_str)
                data["search_snapshot"] = SearchSnapshot.parse_obj(snap_dict)
            if rec_str:
                data["recommendation_context"] = json.loads(rec_str)
            return data
    except Exception as e:
        log.warning(f"Error loading session {session_id} from DB: {e}")
        return None


from dataclasses import is_dataclass, asdict

def _json_default(obj):
    if is_dataclass(obj):
        return asdict(obj)
    if hasattr(obj, "dict") and callable(obj.dict):
        return obj.dict()
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return obj.to_dict()
    return str(obj)


def _db_save_session(session_id: str, state_dict: Dict[str, Any]) -> None:
    """Saves/upserts session record into MySQL database."""
    if not session_id:
        return
    try:
        ctx_str = json.dumps(state_dict.get("search_context"), default=_json_default) if state_dict.get("search_context") else None
        pag_str = json.dumps(state_dict.get("pagination"), default=_json_default) if state_dict.get("pagination") else None
        snap_obj = state_dict.get("search_snapshot")
        snap_str = snap_obj.json() if isinstance(snap_obj, SearchSnapshot) else (json.dumps(snap_obj, default=_json_default) if snap_obj else None)
        rec_str = json.dumps(state_dict.get("recommendation_context"), default=_json_default) if state_dict.get("recommendation_context") else None
        updated_at = state_dict.get("_last_active", time.time())

        with get_db() as db:
            cursor = db.cursor()
            cursor.execute(
                """
                INSERT INTO cyb_chat_session 
                (session_id, search_context, pagination_state, search_snapshot, recommendation_context, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                search_context = VALUES(search_context),
                pagination_state = VALUES(pagination_state),
                search_snapshot = VALUES(search_snapshot),
                recommendation_context = VALUES(recommendation_context),
                updated_at = VALUES(updated_at)
            """,
                (session_id, ctx_str, pag_str, snap_str, rec_str, updated_at),
            )
            db.commit()
    except Exception as e:
        log.warning(f"Error saving session {session_id} to DB: {e}")


def get_session_state(session_id: str) -> Dict[str, Any]:
    """Retrieves session state dictionary (L1 cache -> L2 DB fallback)."""
    if not session_id:
        return {}

    # Check L1 memory cache
    state = _SESSION_STATES.get(session_id)
    if state:
        last_active = state.get("_last_active", 0)
        if time.time() - last_active <= SESSION_TTL_SECONDS:
            return state
        _SESSION_STATES.pop(session_id, None)

    # Check L2 DB persistence
    db_state = _db_load_session(session_id)
    if db_state:
        _SESSION_STATES[session_id] = db_state
        return db_state

    # Initialize new
    new_state = {"_last_active": time.time()}
    _SESSION_STATES[session_id] = new_state
    return new_state


def get_search_context(session_id: str) -> Optional[Dict[str, Any]]:
    """Retrieves active search context dictionary for a session."""
    state = get_session_state(session_id)
    return state.get("search_context")


def set_search_context(session_id: str, search_context: Dict[str, Any]) -> None:
    """Updates active search context for a session."""
    if not session_id:
        return
    state = get_session_state(session_id)
    state["search_context"] = search_context
    state["_last_active"] = time.time()
    _SESSION_STATES[session_id] = state
    _db_save_session(session_id, state)


def get_search_snapshot(session_id: str) -> Optional[SearchSnapshot]:
    """Retrieves active SearchSnapshot model for a session if valid."""
    state = get_session_state(session_id)
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


def set_search_snapshot(session_id: str, snapshot: SearchSnapshot) -> None:
    """Saves active SearchSnapshot model for a session."""
    if not session_id:
        return
    state = get_session_state(session_id)
    state["search_snapshot"] = snapshot
    state["_last_active"] = time.time()
    _SESSION_STATES[session_id] = state
    _db_save_session(session_id, state)


def get_pagination_state(session_id: str) -> Optional[Dict[str, Any]]:
    """Retrieves active pagination state dict."""
    state = get_session_state(session_id)
    return state.get("pagination")


def set_pagination_state(session_id: str, pagination_data: Dict[str, Any]) -> None:
    """Saves pagination state dict."""
    if not session_id:
        return
    state = get_session_state(session_id)
    state["pagination"] = pagination_data
    state["_last_active"] = time.time()
    _SESSION_STATES[session_id] = state
    _db_save_session(session_id, state)


def get_recommendation_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Retrieves recommendation session dict."""
    state = get_session_state(session_id)
    return state.get("recommendation_context")


def set_recommendation_session(
    session_id: str,
    query_key: str,
    ranked_jobs: List[Any],
    current_page: int = 1,
    page_size: int = 5,
    total_jobs: int = 0,
    last_referenced_job_id: Optional[Any] = None,
) -> None:
    """Stores recommendation context snapshot."""
    if not session_id:
        return
    state = get_session_state(session_id)
    state["recommendation_context"] = {
        "query_key": query_key,
        "ranked_jobs": ranked_jobs,
        "current_page": current_page,
        "page_size": page_size,
        "total_jobs": total_jobs or len(ranked_jobs),
        "last_referenced_job_id": last_referenced_job_id,
    }
    state["_last_active"] = time.time()
    _SESSION_STATES[session_id] = state
    _db_save_session(session_id, state)


def update_last_referenced_job_id(session_id: str, job_id: Any) -> None:
    """Updates last_referenced_job_id."""
    state = get_session_state(session_id)
    rec_ctx = state.get("recommendation_context")
    if rec_ctx and job_id is not None:
        rec_ctx["last_referenced_job_id"] = job_id
        state["_last_active"] = time.time()
        _SESSION_STATES[session_id] = state
        _db_save_session(session_id, state)


def set_session_state(session_id: str, state_data: Dict[str, Any]) -> None:
    """Saves global session state dict."""
    if not session_id:
        return
    _SESSION_STATES[session_id] = state_data
    _db_save_session(session_id, state_data)


def clear_pagination_state(session_id: str) -> None:

    """Clears pagination state for a session."""
    if session_id in _SESSION_STATES:
        _SESSION_STATES.pop(session_id, None)
    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("DELETE FROM cyb_chat_session WHERE session_id = %s", (session_id,))
            db.commit()
    except Exception as e:
        log.warning(f"Error deleting session {session_id} from DB: {e}")


def get_employer_search_context(session_id: str) -> Optional[Dict[str, Any]]:
    """Retrieves active employer search requirements state dict."""
    state = get_session_state(session_id)
    return state.get("employer_search_context")


def set_employer_search_context(session_id: str, context_data: Dict[str, Any]) -> None:
    """Saves active employer search requirements state dict."""
    if not session_id:
        return
    state = get_session_state(session_id)
    state["employer_search_context"] = context_data
    state["_last_active"] = time.time()
    _SESSION_STATES[session_id] = state
    _db_save_session(session_id, state)

