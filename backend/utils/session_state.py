import os
import time
import json
import logging
import threading
import re
from dataclasses import dataclass, field, is_dataclass, asdict
import hashlib
import secrets
from typing import Dict, Any, Optional, List, Union
from starlette.concurrency import run_in_threadpool
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
    TimeoutError as RedisTimeoutError,
    ResponseError as RedisResponseError,
    RedisError as BaseRedisLibraryError,
    WatchError,
)

try:
    from backend.utils.redis_client import (
        get_async_redis_client,
        get_sync_redis_client,
        RedisSessionError,
        RedisUnavailableError,
        RedisSessionOwnershipError,
        RedisSessionOOMError,
        RedisSessionPayloadLimitError,
    )
    from backend.config.config import SESSION_TTL_SECONDS, MAX_SESSION_BYTES
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
    from utils.redis_client import (  # type: ignore
        get_async_redis_client,
        get_sync_redis_client,
        RedisSessionError,
        RedisUnavailableError,
        RedisSessionOwnershipError,
        RedisSessionOOMError,
        RedisSessionPayloadLimitError,
    )
    from config.config import SESSION_TTL_SECONDS, MAX_SESSION_BYTES  # type: ignore
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

# Deprecated test-compatibility dictionary; NOT used for production storage.
_SESSION_STATES: Dict[str, Dict[str, Any]] = {}
PENDING_OPTION_MAPPING_TTL_SECONDS = int(os.getenv("PENDING_OPTION_MAPPING_TTL_SECONDS", "900"))  # 15 minutes
MAX_CAS_RETRIES = 3

FORBIDDEN_KEYS = {
    "token", "jwt", "password", "session_id", "access_token", "auth_token",
    "cookie", "secret", "authorization", "api_key"
}

_SESSION_LOCKS: Dict[str, threading.RLock] = {}
_LOCKS_MUTEX = threading.Lock()


def get_session_lock(key: str) -> threading.RLock:
    """Retrieves or creates a thread-safe RLock for a specific session composite key."""
    with _LOCKS_MUTEX:
        if key not in _SESSION_LOCKS:
            _SESSION_LOCKS[key] = threading.RLock()
        return _SESSION_LOCKS[key]


def begin_search_generation(session_id: str, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> int:
    """
    Atomically advances and returns the search generation integer for a session under the session lock.
    Must be called at the beginning of a fresh search before retrieval or network operations,
    so that in-flight pagination requests from an earlier search generation are prevented from
    overwriting the new search result pool.
    Uses CAS optimistic concurrency with remerge_fn so cross-worker contention cleanly increments
    the generation against the freshest state.
    """
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return 1

    with get_session_lock(key):
        state = get_session_state(session_id, role=role, user_id=user_id)
        assigned_gen = [int(state.get("search_gen", 0)) + 1]

        def _remerge_gen(fresh_state: Dict[str, Any]) -> Dict[str, Any]:
            merged = dict(fresh_state)
            next_g = int(merged.get("search_gen", 0)) + 1
            merged["search_gen"] = next_g
            assigned_gen[0] = next_g
            return merged

        res_state = dict(state)
        res_state["search_gen"] = assigned_gen[0]
        ok = save_session_state_cas(
            session_id,
            res_state,
            role=role,
            remerge_fn=_remerge_gen,
            user_id=user_id,
        )
        if not ok:
            log.warning(
                "Failed to commit advanced search_gen via CAS for session %s, returning latest known generation %d",
                key, assigned_gen[0]
            )
        return assigned_gen[0]


def get_search_generation(session_id: str, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> int:
    """Retrieves current search generation integer for a session."""
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return 1
    state = get_session_state(session_id, role=role, user_id=user_id)
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
# 2. Redis Key Formatting & Cryptographic Session Isolation
# ============================================================================

_SESSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.:]+$")


def is_valid_session_id(session_id: Any) -> bool:
    """
    Validates session_id string format and prevents static defaults or injections.
    Must be a string between 3 and 128 characters containing only alphanumeric chars,
    underscores, dashes, dots, and colons.
    Explicitly rejects static default fallbacks like 'default_session'.
    """
    if not session_id or not isinstance(session_id, str):
        return False
    clean = session_id.strip()
    if len(clean) < 3 or len(clean) > 128:
        return False
    if clean.lower() in ("default_session", "none", "null", "undefined", "anon", "anonymous"):
        return False
    return bool(_SESSION_ID_PATTERN.match(clean))


def generate_secure_session_id() -> str:
    """Generates a cryptographically secure random session ID."""
    return f"sess_{secrets.token_hex(16)}"


def resolve_effective_user_id(user_id: Optional[Union[str, int]], session_id: str) -> str:
    """
    Derives canonical authenticated user identity.
    Derives user_id exclusively from verified server-side authentication context.
    Never trusts a frontend-supplied user_id.
    Rejects missing or invalid authenticated identity for production session operations.
    Does NOT silently convert missing identity into anon_<hash>.
    """
    if user_id is not None and str(user_id).strip() and str(user_id).strip().lower() not in ("none", "null", "anon", "anonymous", "unknown"):
        return str(user_id).strip()

    clean_sess = str(session_id or "").strip()
    if clean_sess.startswith("chat:session:"):
        parts = clean_sess.split(":")
        if len(parts) >= 3 and parts[2] and parts[2].lower() not in ("none", "null", "anon", "anonymous", "unknown"):
            return parts[2]

    # If session_id is a composite key format with uid prefix (e.g. "4001:sess_abc:employee" or "usr_999:sess_abc:employee")
    parts = clean_sess.split(":")
    if len(parts) >= 2:
        candidate_uid = parts[0]
        if candidate_uid.startswith("usr_"):
            candidate_uid = candidate_uid[4:]
        if candidate_uid.isdigit() and int(candidate_uid) > 0:
            return parts[0]

    raise RedisSessionOwnershipError("Authenticated user identity is required for session operations.")


def make_composite_key(session_id: str, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> str:
    """Formats identity and role-isolated composite key: f'{user_id}:{raw_session_id}:{role.lower()}'."""
    if not session_id:
        return ""

    if session_id.startswith("chat:session:"):
        # Strip prefix if already in full Redis format
        session_id = session_id[len("chat:session:"):]

    parts = []
    if user_id is not None and str(user_id).strip() and str(user_id).strip().lower() not in ("none", "null"):
        uid_str = str(user_id).strip()
        if not session_id.startswith(f"{uid_str}:"):
            parts.append(uid_str)

    parts.append(session_id)

    if role:
        norm_role = str(role).strip().lower()
        if not session_id.lower().endswith(f":{norm_role}"):
            parts.append(norm_role)

    return ":".join(parts)


def to_redis_key(composite_key: str, user_id: Optional[Union[str, int]] = None, session_id: Optional[str] = None) -> str:
    """
    Transforms composite key into canonical Redis storage key: chat:session:{user_id}:{session_id}.
    Requires verified authenticated identity.
    """
    if not composite_key:
        return ""
    if composite_key.startswith("chat:session:"):
        return composite_key

    eff_uid = resolve_effective_user_id(user_id, session_id or composite_key)
    clean_key = composite_key
    if clean_key.startswith(f"{eff_uid}:"):
        clean_key = clean_key[len(eff_uid) + 1:]

    return f"chat:session:{eff_uid}:{clean_key}"


def check_session_ownership(session_id: str, user_id: Optional[Union[str, int]]) -> None:
    """
    Validates ownership of a session against an explicitly provided user_id.
    Requires authenticated user identity and rejects attempts by a user to access another user's session.
    """
    norm_uid = None
    if user_id is not None and str(user_id).strip() and str(user_id).strip().lower() not in ("none", "null", "anon", "anonymous", "unknown"):
        norm_uid = str(user_id).strip()

    clean_s = str(session_id or "").strip()
    embedded_uid = None
    if clean_s.startswith("chat:session:"):
        parts = clean_s.split(":")
        if len(parts) >= 3 and parts[2]:
            embedded_uid = parts[2]
    else:
        parts = clean_s.split(":")
        if len(parts) >= 2:
            candidate_uid = parts[0]
            if candidate_uid.startswith("usr_"):
                candidate_uid = candidate_uid[4:]
            if candidate_uid.isdigit() and int(candidate_uid) > 0:
                embedded_uid = parts[0]

    if not norm_uid:
        if embedded_uid and embedded_uid.lower() not in ("none", "null", "anon", "anonymous", "unknown"):
            norm_uid = str(embedded_uid)
        else:
            raise RedisSessionOwnershipError("Authenticated user identity is required for session operations.")

    if embedded_uid and str(embedded_uid) != str(norm_uid):
        raise RedisSessionOwnershipError(
            f"Session {session_id} is owned by user {embedded_uid}, not {user_id}"
        )


# ============================================================================
# 3. Persistence Boundary Scrubbing & Serialization Helpers
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


# Deprecated MySQL compatibility stubs (no MySQL calls are executed)
_DIAGNOSTIC_LAST_SAVE_DURATION_MS: float = 0.0

def get_last_session_save_duration_ms() -> float:
    return _DIAGNOSTIC_LAST_SAVE_DURATION_MS

def _db_load_session(session_id: str) -> Optional[Dict[str, Any]]:
    return None

async def _db_load_session_async(session_id: str) -> Optional[Dict[str, Any]]:
    return None

def _db_save_session(session_id: str, state_dict: Dict[str, Any], expected_version: Optional[int] = None) -> bool:
    return True


# ============================================================================
# 4. Redis-Backed Session State Accessors & Mutators
# ============================================================================

def cleanup_stale_sessions(force: bool = False) -> int:
    """No-op; Redis native TTL handles session eviction automatically."""
    return 0


def get_session_state(
    session_id: str,
    role: Optional[str] = None,
    touch: bool = False,
    user_id: Optional[Union[str, int]] = None,
) -> Dict[str, Any]:
    """
    Retrieves session state dictionary from Redis enforcing ownership validation,
    session isolation, and passive read vs active request touch semantics.
    """
    check_session_ownership(session_id, user_id)
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return {}

    rkey = to_redis_key(key, user_id=user_id, session_id=session_id)
    client = get_sync_redis_client()
    eff_uid = resolve_effective_user_id(user_id, session_id)
    norm_role = str(role).strip().lower() if role else "employee"

    try:
        raw = client.get(rkey)
        if raw:
            try:
                data = json.loads(raw)
            except Exception:
                data = None
            if isinstance(data, dict):
                # Ownership validation
                stored_uid = data.get("user_id")
                if stored_uid and str(stored_uid) != str(eff_uid):
                    raise RedisSessionOwnershipError(
                        f"Session {session_id} is owned by user {stored_uid}, not {eff_uid}"
                    )
                data["session_id"] = session_id
                data["user_id"] = str(eff_uid)
                # Inactivity expiration check
                last_active = float(data.get("_last_active") or 0)
                if last_active > 0 and (time.time() - last_active > SESSION_TTL_SECONDS):
                    preserved_lang = data.get("language_preference")
                    data = {
                        "user_id": eff_uid,
                        "session_id": session_id,
                        "role": norm_role,
                        "version_seq": 1,
                        "_last_active": time.time(),
                        "created_at": data.get("created_at", time.time()),
                        "updated_at": time.time(),
                        "language_preference": preserved_lang,
                        "search_context": None,
                        "pagination": None,
                        "recommendation_context": None,
                        "search_snapshot": None,
                    }
                    client.set(rkey, json.dumps(data, default=_json_default), ex=SESSION_TTL_SECONDS)
                    return data

                if touch:
                    client.expire(rkey, SESSION_TTL_SECONDS)
                    data["_last_active"] = time.time()
                    data["updated_at"] = time.time()

                # Hydrate SearchSnapshot if present
                snap_obj = data.get("search_snapshot")
                if snap_obj and isinstance(snap_obj, dict):
                    try:
                        data["search_snapshot"] = SearchSnapshot.parse_obj(snap_obj)
                    except Exception:
                        pass
                return data
    except (RedisSessionOwnershipError, RedisUnavailableError):
        raise
    except Exception as err:
        log.error("Redis error in get_session_state for key %s: %s", rkey, err)
        raise RedisUnavailableError(f"Redis unavailable while fetching session {rkey}: {err}") from err

    # Brand-new session: return fresh state
    now = time.time()
    return {
        "user_id": eff_uid,
        "session_id": session_id,
        "role": norm_role,
        "version_seq": 1,
        "_last_active": now,
        "created_at": now,
        "updated_at": now,
        "language_preference": None,
        "search_context": None,
        "pagination": None,
        "recommendation_context": None,
        "search_snapshot": None,
    }


async def get_session_state_async(
    session_id: str,
    role: Optional[str] = None,
    touch: bool = False,
    user_id: Optional[Union[str, int]] = None,
) -> Dict[str, Any]:
    """
    Asynchronous variant of get_session_state directly awaiting Redis client without blocking event loop.
    """
    check_session_ownership(session_id, user_id)
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return {}

    rkey = to_redis_key(key, user_id=user_id, session_id=session_id)
    client = get_async_redis_client()
    eff_uid = resolve_effective_user_id(user_id, session_id)
    norm_role = str(role).strip().lower() if role else "employee"

    try:
        raw = await client.get(rkey)
        if raw:
            try:
                data = json.loads(raw)
            except Exception:
                data = None
            if isinstance(data, dict):
                # Ownership validation
                stored_uid = data.get("user_id")
                if stored_uid and str(stored_uid) != str(eff_uid):
                    raise RedisSessionOwnershipError(
                        f"Session {session_id} is owned by user {stored_uid}, not {eff_uid}"
                    )
                data["session_id"] = session_id
                data["user_id"] = str(eff_uid)
                # Inactivity expiration check
                last_active = float(data.get("_last_active") or 0)
                if last_active > 0 and (time.time() - last_active > SESSION_TTL_SECONDS):
                    preserved_lang = data.get("language_preference")
                    data = {
                        "user_id": eff_uid,
                        "session_id": session_id,
                        "role": norm_role,
                        "version_seq": 1,
                        "_last_active": time.time(),
                        "created_at": data.get("created_at", time.time()),
                        "updated_at": time.time(),
                        "language_preference": preserved_lang,
                        "search_context": None,
                        "pagination": None,
                        "recommendation_context": None,
                        "search_snapshot": None,
                    }
                    await client.set(rkey, json.dumps(data, default=_json_default), ex=SESSION_TTL_SECONDS)
                    return data

                if touch:
                    await client.expire(rkey, SESSION_TTL_SECONDS)
                    data["_last_active"] = time.time()
                    data["updated_at"] = time.time()

                # Hydrate SearchSnapshot if present
                snap_obj = data.get("search_snapshot")
                if snap_obj and isinstance(snap_obj, dict):
                    try:
                        data["search_snapshot"] = SearchSnapshot.parse_obj(snap_obj)
                    except Exception:
                        pass
                return data
    except (RedisSessionOwnershipError, RedisUnavailableError):
        raise
    except Exception as err:
        log.error("Redis error in get_session_state_async for key %s: %s", rkey, err)
        raise RedisUnavailableError(f"Redis unavailable while fetching session {rkey}: {err}") from err

    now = time.time()
    return {
        "user_id": eff_uid,
        "session_id": session_id,
        "role": norm_role,
        "version_seq": 1,
        "_last_active": now,
        "created_at": now,
        "updated_at": now,
        "language_preference": None,
        "search_context": None,
        "pagination": None,
        "recommendation_context": None,
        "search_snapshot": None,
    }


def touch_session_state(
    session_id: str,
    role: Optional[str] = None,
    user_id: Optional[Union[str, int]] = None,
) -> bool:
    """
    Refreshes session TTL and _last_active timestamp in Redis for a genuine incoming interaction.
    Will not resurrect an already expired session.
    """
    check_session_ownership(session_id, user_id)
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return False

    rkey = to_redis_key(key, user_id=user_id, session_id=session_id)
    client = get_sync_redis_client()
    eff_uid = resolve_effective_user_id(user_id, session_id)
    try:
        raw = client.get(rkey)
        if not raw:
            return False
        data = json.loads(raw)
        stored_uid = data.get("user_id")
        if stored_uid and str(stored_uid) != str(eff_uid):
            raise RedisSessionOwnershipError(f"Session {session_id} is owned by user {stored_uid}, not {eff_uid}")
        data["_last_active"] = time.time()
        data["updated_at"] = time.time()
        client.set(rkey, json.dumps(data, default=_json_default), ex=SESSION_TTL_SECONDS)
        return True
    except (RedisSessionOwnershipError, RedisUnavailableError, RedisSessionOOMError, RedisSessionPayloadLimitError):
        raise
    except (RedisConnectionError, RedisTimeoutError) as conn_err:
        raise RedisUnavailableError(f"Redis unavailable during touch_session_state: {conn_err}") from conn_err
    except RedisResponseError as resp_err:
        if "oom" in str(resp_err).lower():
            raise RedisSessionOOMError(f"Redis OOM during touch_session_state: {resp_err}") from resp_err
        raise RedisUnavailableError(f"Redis error during touch_session_state: {resp_err}") from resp_err
    except BaseRedisLibraryError as rerr:
        raise RedisUnavailableError(f"Redis error during touch_session_state: {rerr}") from rerr
    except Exception as err:
        log.warning("Failed to touch session %s in Redis: %s", rkey, err)
        return False


async def touch_session_state_async(
    session_id: str,
    role: Optional[str] = None,
    user_id: Optional[Union[str, int]] = None,
) -> bool:
    """Async variant of touch_session_state."""
    check_session_ownership(session_id, user_id)
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return False

    rkey = to_redis_key(key, user_id=user_id, session_id=session_id)
    client = get_async_redis_client()
    eff_uid = resolve_effective_user_id(user_id, session_id)
    try:
        raw = await client.get(rkey)
        if not raw:
            return False
        data = json.loads(raw)
        stored_uid = data.get("user_id")
        if stored_uid and str(stored_uid) != str(eff_uid):
            raise RedisSessionOwnershipError(f"Session {session_id} is owned by user {stored_uid}, not {eff_uid}")
        data["_last_active"] = time.time()
        data["updated_at"] = time.time()
        await client.set(rkey, json.dumps(data, default=_json_default), ex=SESSION_TTL_SECONDS)
        return True
    except (RedisSessionOwnershipError, RedisUnavailableError, RedisSessionOOMError, RedisSessionPayloadLimitError):
        raise
    except (RedisConnectionError, RedisTimeoutError) as conn_err:
        raise RedisUnavailableError(f"Redis unavailable during touch_session_state_async: {conn_err}") from conn_err
    except RedisResponseError as resp_err:
        if "oom" in str(resp_err).lower():
            raise RedisSessionOOMError(f"Redis OOM during touch_session_state_async: {resp_err}") from resp_err
        raise RedisUnavailableError(f"Redis error during touch_session_state_async: {resp_err}") from resp_err
    except BaseRedisLibraryError as rerr:
        raise RedisUnavailableError(f"Redis error during touch_session_state_async: {rerr}") from rerr
    except Exception as err:
        log.warning("Failed to touch session %s in Redis: %s", rkey, err)
        return False


def delete_session(
    session_id: str,
    role: Optional[str] = None,
    user_id: Optional[Union[str, int]] = None,
) -> bool:
    """Deletes an active session from Redis with ownership verification."""
    check_session_ownership(session_id, user_id)
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return False

    rkey = to_redis_key(key, user_id=user_id, session_id=session_id)
    client = get_sync_redis_client()
    eff_uid = resolve_effective_user_id(user_id, session_id)
    try:
        raw = client.get(rkey)
        if raw:
            data = json.loads(raw)
            stored_uid = data.get("user_id")
            if stored_uid and str(stored_uid) != str(eff_uid):
                raise RedisSessionOwnershipError(f"Session {session_id} is owned by user {stored_uid}, not {eff_uid}")
        return bool(client.delete(rkey))
    except (RedisSessionOwnershipError, RedisUnavailableError):
        raise
    except Exception as err:
        log.warning("Failed to delete session %s from Redis: %s", rkey, err)
        return False


async def delete_session_async(
    session_id: str,
    role: Optional[str] = None,
    user_id: Optional[Union[str, int]] = None,
) -> bool:
    """Async variant of delete_session."""
    check_session_ownership(session_id, user_id)
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return False

    rkey = to_redis_key(key, user_id=user_id, session_id=session_id)
    client = get_async_redis_client()
    eff_uid = resolve_effective_user_id(user_id, session_id)
    try:
        raw = await client.get(rkey)
        if raw:
            data = json.loads(raw)
            stored_uid = data.get("user_id")
            if stored_uid and str(stored_uid) != str(eff_uid):
                raise RedisSessionOwnershipError(f"Session {session_id} is owned by user {stored_uid}, not {eff_uid}")
        return bool(await client.delete(rkey))
    except (RedisSessionOwnershipError, RedisUnavailableError):
        raise
    except Exception as err:
        log.warning("Failed to delete session %s from Redis: %s", rkey, err)
        return False


def get_search_context(session_id: str, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> Optional[Dict[str, Any]]:
    """Retrieves active search context dictionary for a session."""
    key = make_composite_key(session_id, role, user_id=user_id)
    state = get_session_state(session_id, role=role, user_id=user_id)
    return state.get("search_context")


def set_search_context(session_id: str, search_context: Dict[str, Any], role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> None:
    """Updates active search context for a session with atomic CAS persistence."""
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return

    def remerge_ctx(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["search_context"] = search_context
        return res_state

    state = get_session_state(session_id, role=role, user_id=user_id)
    save_session_state_cas(session_id, remerge_ctx(state), role=role, remerge_fn=remerge_ctx, user_id=user_id)


def get_employer_search_context(session_id: str, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> Optional[Dict[str, Any]]:
    """Retrieves active employer search requirements state dict."""
    key = make_composite_key(session_id, role, user_id=user_id)
    state = get_session_state(session_id, role=role, user_id=user_id)
    return state.get("employer_search_context")


def set_employer_search_context(session_id: str, context_data: Dict[str, Any], role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> None:
    """Saves active employer search requirements state dict with atomic CAS persistence."""
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return

    def remerge_emp_ctx(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["employer_search_context"] = context_data
        return res_state

    state = get_session_state(session_id, role=role, user_id=user_id)
    save_session_state_cas(session_id, remerge_emp_ctx(state), role=role, remerge_fn=remerge_emp_ctx, user_id=user_id)


def get_search_snapshot(session_id: str, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> Optional[SearchSnapshot]:
    """Retrieves active SearchSnapshot model for a session if valid."""
    key = make_composite_key(session_id, role, user_id=user_id)
    state = get_session_state(session_id, role=role, user_id=user_id)
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


def set_search_snapshot(session_id: str, snapshot: SearchSnapshot, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> None:
    """Saves active SearchSnapshot model for a session with atomic CAS persistence."""
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return

    def remerge_snap(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["search_snapshot"] = snapshot
        return res_state

    state = get_session_state(session_id, role=role, user_id=user_id)
    save_session_state_cas(session_id, remerge_snap(state), role=role, remerge_fn=remerge_snap, user_id=user_id)


def get_pagination_state(session_id: str, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> Optional[Dict[str, Any]]:
    """Retrieves active pagination state dict."""
    key = make_composite_key(session_id, role, user_id=user_id)
    state = get_session_state(session_id, role=role, user_id=user_id)
    return state.get("pagination")


def set_pagination_state(session_id: str, pagination_data: Dict[str, Any], role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> None:
    """
    Saves pagination state dict with atomic CAS persistence under per-session lock.
    Conditionally merges pagination updates:
    1. Rejects pagination writes from an older search generation than the session's active search_gen.
    2. Rejects pagination writes if the session was reset after the update's _reset_seq was captured.
    3. Within the same search generation, applies the monotonic cursor guard to prevent stale
       in-flight pagination responses from regressing an already-advanced cursor.
    4. For a new search generation (or fresh pool initialization), accepts and replaces the result pool.
    """
    key = make_composite_key(session_id, role, user_id=user_id)
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

            # Detect cross-type transition (e.g. people <-> jobs)
            curr_type = str(current_pag.get("result_type") or "").strip().lower() if (current_pag and isinstance(current_pag, dict)) else ""
            new_type = str(pagination_data.get("result_type") or "").strip().lower()
            is_type_transition = bool(curr_type and new_type and curr_type != new_type)

            raw_pag_gen = pagination_data.get("search_gen")
            if raw_pag_gen is not None:
                try:
                    pag_gen = int(raw_pag_gen)
                except (ValueError, TypeError):
                    # Malformed generation is rejected to prevent crashing or bypassing validation
                    return res_state
            else:
                # Missing search_gen:
                # When result type changes (e.g. people <-> jobs) or a fresh search result pool
                # is provided at current_page 1, this represents a new search rather than a stale cursor advance.
                # Otherwise, if the session already has an active pool or generation advanced past 1,
                # a write lacking search_gen is treated as stale (generation 0) and rejected.
                is_fresh_pool_init = bool(
                    is_type_transition or
                    pagination_data.get("_is_fresh_init") or
                    (pagination_data.get("all_recs") and int(pagination_data.get("current_page", 1)) == 1)
                )
                if is_fresh_pool_init:
                    pag_gen = cur_gen
                elif current_pag or cur_gen > 1:
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

                # If switching result types (people <-> jobs) or if this write is from a newer search generation,
                # accept and replace the old pool completely without carrying over the old cursor or state.
                if is_type_transition or pag_gen > cur_pag_gen:
                    merged_pag = dict(pagination_data)
                    merged_pag["search_gen"] = max(cur_gen, pag_gen)
                    res_state["pagination"] = merged_pag
                    res_state["search_gen"] = max(cur_gen, pag_gen)
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
                is_fresh_init = bool(
                    pagination_data.get("_is_fresh_init") or
                    (pagination_data.get("all_recs") and new_page == 1)
                )
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

        state = get_session_state(session_id, role=role, user_id=user_id)
        save_session_state_cas(session_id, remerge_pag(state), role=role, remerge_fn=remerge_pag, user_id=user_id)




def get_language_preference(session_id: str, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> Optional[str]:
    """Retrieves active explicit persistent language preference for a session ('hindi' | 'english' | 'hinglish' | None)."""
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return None
    state = get_session_state(session_id, role=role, user_id=user_id)
    return state.get("language_preference")


def set_language_preference(session_id: str, lang_pref: Optional[str], role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> None:
    """Saves active explicit persistent language preference for a session ('hindi' | 'english' | 'hinglish') with atomic CAS persistence."""
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key or not lang_pref:
        return
    norm_lang = str(lang_pref).strip().lower()

    def remerge_lang(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["language_preference"] = norm_lang
        return res_state

    state = get_session_state(session_id, role=role, user_id=user_id)
    save_session_state_cas(session_id, remerge_lang(state), role=role, remerge_fn=remerge_lang, user_id=user_id)


def get_selected_employment_context(session_id: str, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> Optional[str]:
    """Retrieves stored selected employment company name for a session."""
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return None
    state = get_session_state(session_id, role=role, user_id=user_id)
    return state.get("selected_employment_company")


def get_selected_employment_record_key(session_id: str, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> Optional[str]:
    """Retrieves stored selected exact employment record key for a session."""
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return None
    state = get_session_state(session_id, role=role, user_id=user_id)
    return state.get("selected_employment_record_key")


def set_selected_employment_context(session_id: str, company_name: str, record_key: Optional[str] = None, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> None:
    """Saves selected employment company name and optional exact record key into RAM session state."""
    key = make_composite_key(session_id, role, user_id=user_id)
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

    state = get_session_state(session_id, role=role, user_id=user_id)
    save_session_state_cas(session_id, remerge_emp(state), role=role, remerge_fn=remerge_emp, user_id=user_id)


def clear_selected_employment_context(session_id: str, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> None:
    """Clears selected employment company name and exact record key from session state."""
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return

    def remerge_clear(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["selected_employment_company"] = None
        res_state["selected_employment_record_key"] = None
        return res_state

    state = get_session_state(session_id, role=role, user_id=user_id)
    save_session_state_cas(session_id, remerge_clear(state), role=role, remerge_fn=remerge_clear, user_id=user_id)


def set_pending_requested_level(session_id: str, level: int, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> None:
    """Saves pending verification requested_level (1-4) into RAM session state."""
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key or not session_id or level not in (1, 2, 3, 4):
        return

    def remerge_level(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["pending_requested_level"] = int(level)
        return res_state

    state = get_session_state(session_id, role=role, user_id=user_id)
    save_session_state_cas(session_id, remerge_level(state), role=role, remerge_fn=remerge_level, user_id=user_id)


def get_pending_requested_level(session_id: str, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> Optional[int]:
    """Retrieves stored pending verification requested_level for a session."""
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key or not session_id:
        return None
    state = get_session_state(session_id, role=role, user_id=user_id)
    val = state.get("pending_requested_level")
    try:
        return int(val) if val in (1, 2, 3, 4, "1", "2", "3", "4") else None
    except Exception:
        return None


def clear_pending_requested_level(session_id: str, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> None:
    """Clears pending verification requested_level from RAM session state."""
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key or not session_id:
        return

    def remerge_clear_lvl(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["pending_requested_level"] = None
        return res_state

    state = get_session_state(session_id, role=role, user_id=user_id)
    save_session_state_cas(session_id, remerge_clear_lvl(state), role=role, remerge_fn=remerge_clear_lvl, user_id=user_id)


def get_session_option_mapping(session_id: str, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> Dict[str, str]:
    """
    Retrieves session-bound pending option mapping dict (e.g. {'emp_rec_1': composite_key}).
    Enforces PENDING_OPTION_MAPPING_TTL_SECONDS expiration.
    """
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return {}
    state = get_session_state(session_id, role=role, user_id=user_id)
    timestamped_mapping = state.get("pending_option_mapping")
    if not isinstance(timestamped_mapping, dict):
        return {}

    created_at = float(timestamped_mapping.get("created_at", 0))
    mapping = timestamped_mapping.get("mapping")
    now = time.time()

    if now - created_at > PENDING_OPTION_MAPPING_TTL_SECONDS:
        clear_session_option_mapping(session_id, role=role, user_id=user_id)
        return {}

    if isinstance(mapping, dict):
        return dict(mapping)
    return {}


def set_session_option_mapping(session_id: str, mapping: Dict[str, str], role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> None:
    """Saves session-bound pending option mapping dict into RAM session state with creation timestamp."""
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return

    payload = {
        "created_at": time.time(),
        "mapping": dict(mapping) if isinstance(mapping, dict) else {},
        "user_id": str(user_id).strip() if (user_id is not None and str(user_id).strip()) else None,
    }

    def remerge_opt(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["pending_option_mapping"] = payload
        return res_state

    state = get_session_state(session_id, role=role, user_id=user_id)
    save_session_state_cas(session_id, remerge_opt(state), role=role, remerge_fn=remerge_opt, user_id=user_id)


def consume_session_option_mapping(session_id: str, submitted_key: str, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> Optional[str]:
    """
    Atomically resolves and consumes a single-use pending option key.
    Prevents replay attacks while preserving selected employment context.
    """
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key or not submitted_key:
        return None

    opt_map = get_session_option_mapping(session_id, role=role, user_id=user_id)
    target_composite_key = opt_map.get(submitted_key.strip().lower())
    if not target_composite_key:
        return None

    # Consume the mapping payload so key cannot be replayed
    clear_session_option_mapping(session_id, role=role, user_id=user_id)
    return target_composite_key


def clear_session_option_mapping(session_id: str, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> None:
    """Clears pending option mapping dict from RAM session state."""
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return

    def remerge_clear_opt(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["pending_option_mapping"] = None
        return res_state

    state = get_session_state(session_id, role=role, user_id=user_id)
    save_session_state_cas(session_id, remerge_clear_opt(state), role=role, remerge_fn=remerge_clear_opt, user_id=user_id)





def get_recommendation_session(session_id: str, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> Optional[Dict[str, Any]]:
    """Retrieves recommendation session dict."""
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return None
    state = get_session_state(session_id, role=role, user_id=user_id)
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
    user_id: Optional[Union[str, int]] = None,
    search_gen: Optional[int] = None,
) -> None:
    """Stores recommendation context snapshot with atomic CAS persistence and generation guard."""
    key = make_composite_key(session_id, role, user_id=user_id)
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
        try:
            cur_gen = int(res_state.get("search_gen", 1))
        except (ValueError, TypeError):
            cur_gen = 1

        existing_rec = res_state.get("recommendation_context")

        if search_gen is not None:
            try:
                write_gen = int(search_gen)
            except (ValueError, TypeError):
                # Reject malformed generation parameter
                return res_state
            # Reject writes from an older search generation than the session's active search_gen
            if cur_gen > write_gen:
                return res_state
            rec_payload["search_gen"] = write_gen
        else:
            # Unversioned legacy callers:
            # If the session has already advanced past generation 1 (multi-generation versioned search),
            # an unversioned write cannot be assumed to be current search and is rejected as stale.
            # Within an unversioned session (cur_gen <= 1), sequential unversioned replacement is preserved.
            if cur_gen > 1:
                return res_state
            rec_payload["search_gen"] = cur_gen

        res_state["recommendation_context"] = rec_payload
        return res_state

    state = get_session_state(session_id, role=role, user_id=user_id)
    save_session_state_cas(session_id, remerge_rec(state), role=role, remerge_fn=remerge_rec, user_id=user_id)


def update_last_referenced_job_id(session_id: str, job_id: Any, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> None:
    """Updates last_referenced_job_id with atomic CAS persistence."""
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key or job_id is None:
        return

    def remerge_ref(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        rec_ctx = dict(res_state.get("recommendation_context") or {})
        rec_ctx["last_referenced_job_id"] = job_id
        res_state["recommendation_context"] = rec_ctx
        return res_state

    state = get_session_state(session_id, role=role, user_id=user_id)
    save_session_state_cas(session_id, remerge_ref(state), role=role, remerge_fn=remerge_ref, user_id=user_id)


def set_session_state(session_id: str, state_data: Dict[str, Any], role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> None:
    """Saves global session state dict with atomic CAS persistence."""
    eff_uid = user_id if (user_id is not None and str(user_id).strip()) else state_data.get("user_id")
    key = make_composite_key(session_id, role, user_id=eff_uid)
    if not key:
        return

    def remerge_full(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        for k, v in state_data.items():
            if k not in ["version_seq", "_last_active", "db_sync_pending"]:
                res_state[k] = v
        return res_state

    state = get_session_state(session_id, role=role, user_id=eff_uid)
    save_session_state_cas(session_id, remerge_full(state), role=role, remerge_fn=remerge_full, user_id=eff_uid)


def save_session_state_cas(
    session_id: str,
    state_dict: Dict[str, Any],
    role: Optional[str] = None,
    remerge_fn: Optional[Any] = None,
    max_retries: int = MAX_CAS_RETRIES,
    user_id: Optional[Union[str, int]] = None,
    expected_version: Optional[int] = None,
) -> bool:
    """
    Saves session state using Atomic CAS optimistic locking in Redis with bounded retries.
    Enforces ownership validation, version sequencing, payload size guard, and native TTL refresh.
    Synchronizes under local session lock to prevent thread-level contention in the same process.
    """
    check_session_ownership(session_id, user_id)
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return False

    rkey = to_redis_key(key, user_id=user_id, session_id=session_id)
    client = get_sync_redis_client()
    eff_uid = resolve_effective_user_id(user_id, session_id)
    current_payload = dict(state_dict)

    with get_session_lock(key):
        for attempt in range(max_retries):
            pipe = None
            try:
                pipe = client.pipeline()
                pipe.watch(rkey)
                raw = pipe.get(rkey)
                existing = {}
                if raw:
                    try:
                        existing = json.loads(raw)
                    except Exception:
                        existing = {}

                # Ownership validation
                stored_uid = existing.get("user_id")
                if stored_uid and str(stored_uid) != str(eff_uid):
                    pipe.unwatch()
                    raise RedisSessionOwnershipError(
                        f"Session {session_id} is owned by user {stored_uid}, not {eff_uid}"
                    )

                current_ver = int(existing.get("version_seq", 1)) if existing else 1
                exp_ver = expected_version if expected_version is not None else int(current_payload.get("version_seq", current_ver))

                if existing and (current_ver != exp_ver if expected_version is not None else current_ver > exp_ver):
                    pipe.unwatch()
                    if remerge_fn and callable(remerge_fn):
                        current_payload = remerge_fn(existing)
                        continue
                    else:
                        log.warning(
                            "CAS rejected stale write for session %s: expected version %d != current version %d without remerge_fn",
                            rkey, exp_ver, current_ver
                        )
                        return False

                next_ver = current_ver + 1 if existing else (int(current_payload.get("version_seq", 0)) + 1 if "version_seq" in current_payload else 1)
                write_payload = dict(current_payload)
                write_payload["version_seq"] = next_ver
                now = time.time()
                write_payload["_last_active"] = now
                write_payload["updated_at"] = now
                if "created_at" not in write_payload or not write_payload["created_at"]:
                    write_payload["created_at"] = existing.get("created_at", now)
                if "user_id" not in write_payload or not write_payload["user_id"] or (not str(eff_uid).startswith("anon_")):
                    write_payload["user_id"] = eff_uid
                write_payload["session_id"] = session_id
                if role:
                    write_payload["role"] = str(role).strip().lower()

                scrubbed = sanitize_state_dict(write_payload)
                serialized = json.dumps(scrubbed, default=_json_default)
                if len(serialized.encode("utf-8")) > MAX_SESSION_BYTES:
                    pipe.unwatch()
                    raise RedisSessionPayloadLimitError(
                        f"Session {rkey} exceeds maximum allowed size of {MAX_SESSION_BYTES} bytes"
                    )

                pipe.multi()
                pipe.set(rkey, serialized, ex=SESSION_TTL_SECONDS)
                pipe.execute()
                return True
            except Exception as ex:
                if isinstance(ex, WatchError) or type(ex).__name__ == "WatchError":
                    if remerge_fn and callable(remerge_fn):
                        try:
                            fresh_raw = client.get(rkey)
                            if fresh_raw:
                                current_payload = remerge_fn(json.loads(fresh_raw))
                        except Exception:
                            pass
                    continue
                if isinstance(ex, (RedisSessionOwnershipError, RedisSessionPayloadLimitError)):
                    raise
                if isinstance(ex, (RedisConnectionError, RedisTimeoutError, RedisUnavailableError)):
                    log.error("Redis connection/timeout error during CAS for session %s: %s", rkey, ex)
                    raise RedisUnavailableError(f"Redis unavailable during CAS write: {ex}") from ex
                if isinstance(ex, RedisResponseError) and "oom" in str(ex).lower():
                    log.error("Redis OOM error during CAS for session %s: %s", rkey, ex)
                    raise RedisSessionOOMError(f"Redis OOM during CAS write: {ex}") from ex
                if isinstance(ex, BaseRedisLibraryError):
                    log.error("Redis library error during CAS for session %s: %s", rkey, ex)
                    raise RedisUnavailableError(f"Redis error during CAS write: {ex}") from ex
                log.error("Unexpected Redis CAS failure for session %s: %s", rkey, ex)
                raise RedisUnavailableError(f"Unexpected Redis failure during CAS write: {ex}") from ex
            finally:
                if pipe is not None:
                    try:
                        pipe.reset()
                    except Exception:
                        pass

        log.warning("CAS write retries exhausted (%d) for session %s", max_retries, rkey)
        return False


async def save_session_state_cas_async(
    session_id: str,
    state_dict: Dict[str, Any],
    role: Optional[str] = None,
    remerge_fn: Optional[Any] = None,
    max_retries: int = MAX_CAS_RETRIES,
    user_id: Optional[Union[str, int]] = None,
    expected_version: Optional[int] = None,
) -> bool:
    """
    Asynchronously saves session state using Atomic CAS optimistic locking in Redis.
    Directly awaits async Redis client without blocking event loop.
    """
    if user_id is None and state_dict.get("user_id"):
        user_id = state_dict.get("user_id")
    check_session_ownership(session_id, user_id)
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return False

    rkey = to_redis_key(key, user_id=user_id, session_id=session_id)
    client = get_async_redis_client()
    eff_uid = resolve_effective_user_id(user_id, session_id)
    current_payload = dict(state_dict)

    for attempt in range(max_retries):
        pipe = None
        try:
            pipe = client.pipeline()
            await pipe.watch(rkey)
            raw = await pipe.get(rkey)
            existing = {}
            if raw:
                try:
                    existing = json.loads(raw)
                except Exception:
                    existing = {}

            # Ownership validation
            stored_uid = existing.get("user_id")
            if stored_uid and str(stored_uid) != str(eff_uid):
                await pipe.unwatch()
                raise RedisSessionOwnershipError(
                    f"Session {session_id} is owned by user {stored_uid}, not {eff_uid}"
                )

            current_ver = int(existing.get("version_seq", 1)) if existing else 1
            exp_ver = expected_version if expected_version is not None else int(current_payload.get("version_seq", current_ver))

            if existing and (current_ver != exp_ver if expected_version is not None else current_ver > exp_ver):
                await pipe.unwatch()
                if remerge_fn and callable(remerge_fn):
                    current_payload = remerge_fn(existing)
                    continue
                else:
                    log.warning(
                        "Async CAS rejected stale write for session %s: expected version %d != current version %d without remerge_fn",
                        rkey, exp_ver, current_ver
                    )
                    return False

            next_ver = current_ver + 1 if existing else (int(current_payload.get("version_seq", 0)) + 1 if "version_seq" in current_payload else 1)
            write_payload = dict(current_payload)
            write_payload["version_seq"] = next_ver
            now = time.time()
            write_payload["_last_active"] = now
            write_payload["updated_at"] = now
            if "created_at" not in write_payload or not write_payload["created_at"]:
                write_payload["created_at"] = existing.get("created_at", now)
            write_payload["user_id"] = eff_uid
            write_payload["session_id"] = session_id
            if role:
                write_payload["role"] = str(role).strip().lower()

            scrubbed = sanitize_state_dict(write_payload)
            serialized = json.dumps(scrubbed, default=_json_default)
            if len(serialized.encode("utf-8")) > MAX_SESSION_BYTES:
                await pipe.unwatch()
                raise RedisSessionPayloadLimitError(
                    f"Session {rkey} exceeds maximum allowed size of {MAX_SESSION_BYTES} bytes"
                )

            pipe.multi()
            pipe.set(rkey, serialized, ex=SESSION_TTL_SECONDS)
            await pipe.execute()
            return True
        except Exception as ex:
            if isinstance(ex, WatchError) or type(ex).__name__ == "WatchError":
                if remerge_fn and callable(remerge_fn):
                    try:
                        fresh_raw = await client.get(rkey)
                        if fresh_raw:
                            current_payload = remerge_fn(json.loads(fresh_raw))
                    except Exception:
                        pass
                continue
            if isinstance(ex, (RedisSessionOwnershipError, RedisSessionPayloadLimitError)):
                raise
            if isinstance(ex, (RedisConnectionError, RedisTimeoutError, RedisUnavailableError)):
                log.error("Async Redis connection/timeout error during CAS for session %s: %s", rkey, ex)
                raise RedisUnavailableError(f"Redis unavailable during async CAS write: {ex}") from ex
            if isinstance(ex, RedisResponseError) and "oom" in str(ex).lower():
                log.error("Async Redis OOM error during CAS for session %s: %s", rkey, ex)
                raise RedisSessionOOMError(f"Redis OOM during async CAS write: {ex}") from ex
            if isinstance(ex, BaseRedisLibraryError):
                log.error("Async Redis library error during CAS for session %s: %s", rkey, ex)
                raise RedisUnavailableError(f"Redis error during async CAS write: {ex}") from ex
            log.error("Unexpected async Redis CAS failure for session %s: %s", rkey, ex)
            raise RedisUnavailableError(f"Unexpected Redis failure during async CAS write: {ex}") from ex
        finally:
            if pipe is not None:
                try:
                    await pipe.reset()
                except Exception:
                    pass

    log.warning("Async CAS write retries exhausted (%d) for session %s", max_retries, rkey)
    return False


def update_employer_session_cas(
    session_id: str,
    current_turn_reqs: EmployerSearchRequirements,
    prompt_category: str = "ADDITIVE",
    role: Optional[str] = None,
    max_retries: int = MAX_CAS_RETRIES,
    user_id: Optional[Union[str, int]] = None,
) -> Dict[str, Any]:
    """
    Performs full-state atomic CAS persistence with domain re-merging for Employer queries.
    On CAS failure, re-executes merge_employer_search_context using freshly loaded DB state.
    """
    key = make_composite_key(session_id, role, user_id=user_id)
    base_state = get_session_state(session_id, role=role, user_id=user_id)

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

    save_session_state_cas(session_id, initial_payload, role=role, remerge_fn=remerge_fn, max_retries=max_retries, user_id=user_id)
    return get_session_state(session_id, role=role, user_id=user_id)


def update_employee_session_cas(
    session_id: str,
    current_turn_query: SearchQuery,
    prompt_category: str = "ADDITIVE",
    role: Optional[str] = None,
    max_retries: int = MAX_CAS_RETRIES,
    user_id: Optional[Union[str, int]] = None,
) -> Dict[str, Any]:
    """
    Performs full-state atomic CAS persistence with domain re-merging for Employee queries.
    On CAS failure, re-executes merge_employee_search_query using freshly loaded DB state.
    """
    key = make_composite_key(session_id, role, user_id=user_id)
    base_state = get_session_state(session_id, role=role, user_id=user_id)

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

    save_session_state_cas(session_id, initial_payload, role=role, remerge_fn=remerge_fn, max_retries=max_retries, user_id=user_id)
    return get_session_state(session_id, role=role, user_id=user_id)


def clear_pagination_state(
    session_id: str,
    role: Optional[str] = None,
    user_id: Optional[Union[str, int]] = None,
) -> None:
    """Clears pagination state for a session in Redis."""
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return

    def remerge_clear_pag(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["pagination"] = None
        return res_state

    state = get_session_state(session_id, role=role, user_id=user_id)
    save_session_state_cas(
        session_id,
        remerge_clear_pag(state),
        role=role,
        remerge_fn=remerge_clear_pag,
        user_id=user_id,
    )


def get_candidate_search_criteria(session_id: str, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> Optional[SearchCriteria]:
    """Retrieves candidate search criteria from session state."""
    key = make_composite_key(session_id, role, user_id=user_id)
    state = get_session_state(session_id, role=role, user_id=user_id)
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


def set_candidate_search_criteria(session_id: str, criteria: SearchCriteria, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> None:
    """Saves candidate search criteria to session state with atomic CAS persistence and remerge fallback."""
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return
    crit_data = criteria.model_dump() if hasattr(criteria, "model_dump") else (criteria.dict() if hasattr(criteria, "dict") else dict(criteria))

    def remerge_crit(base_state: Dict[str, Any]) -> Dict[str, Any]:
        res_state = dict(base_state)
        res_state["candidate_search_context"] = crit_data
        return res_state

    state = get_session_state(session_id, role=role, user_id=user_id)
    ok = save_session_state_cas(session_id, remerge_crit(state), role=role, remerge_fn=remerge_crit, user_id=user_id)
    if not ok:
        log.warning("set_candidate_search_criteria CAS write failed for session %s", key)


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

def reset_search_context(session_id: str, role: Optional[str] = None, user_id: Optional[Union[str, int]] = None) -> Dict[str, Any]:
    """
    Clears Search Criteria, Pagination, and Snapshots while PRESERVING language_preference and auth credentials.
    Enforces atomic CAS persistence with re-merge fallback.
    """
    key = make_composite_key(session_id, role, user_id=user_id)
    state = get_session_state(session_id, role=role, user_id=user_id)

    def apply_reset(base_state: Dict[str, Any]) -> Dict[str, Any]:
        lang_pref = base_state.get("language_preference")
        auth_token = base_state.get("auth_token")
        u_id = base_state.get("user_id") or user_id

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
        if u_id:
            res_state["user_id"] = u_id
        return res_state

    initial_reset_state = apply_reset(state)
    success = save_session_state_cas(
        session_id,
        initial_reset_state,
        role=role,
        remerge_fn=apply_reset,
        user_id=user_id,
    )
    if not success:
        log.warning(f"reset_search_context CAS write failed for session {key}")
    return get_session_state(session_id, role=role, user_id=user_id)


def handle_pagination_command(
    session_id: str,
    command: str,
    role: Optional[str] = None,
    user_id: Optional[Union[str, int]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Updates Pagination State ONLY, leaving Search Criteria State 100% UNTOUCHED.
    Supports commands: 'next', 'previous', 'page X'.
    Enforces atomic CAS persistence with re-merge fallback on collision.
    """
    key = make_composite_key(session_id, role, user_id=user_id)
    state = get_session_state(session_id, role=role, user_id=user_id)
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
        user_id=user_id,
    )
    if not success:
        log.warning(f"handle_pagination_command CAS write failed for session {key}")
        return None
    updated_state = get_session_state(session_id, role=role, user_id=user_id)
    return updated_state.get("pagination") if updated_state else None


def claim_next_pagination_page(
    session_id: str,
    command: str,
    role: Optional[str] = None,
    user_id: Optional[Union[str, int]] = None,
) -> Dict[str, Any]:
    """
    Atomically claims the next pagination page under a per-session lock.
    Calculates and persists the new current_page using CAS optimistic locking with remerge fallback,
    preventing concurrent requests from claiming duplicate page slices or losing cursor updates.
    Returns:
        {
            "success": bool,
            "claimed_page": int,
            "is_oob": bool,
            "saved_p_state": dict,
            "error": Optional[str]
        }
    """
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return {"success": False, "claimed_page": 1, "is_oob": False, "saved_p_state": {}, "error": "Invalid session identifier"}

    with get_session_lock(key):
        state = get_session_state(session_id, role=role, user_id=user_id)
        pag = dict(state.get("pagination") or {})
        if not pag:
            return {"success": False, "claimed_page": 1, "is_oob": False, "saved_p_state": {}, "error": "No active pagination state found"}

        committed_meta = {
            "claimed_page": 1,
            "is_oob": False,
            "saved_p_state": {},
        }

        def _remerge_claim(fresh_state: Dict[str, Any]) -> Dict[str, Any]:
            merged = dict(fresh_state)
            fresh_pag = dict(merged.get("pagination") or {})
            if not fresh_pag:
                # If pagination was cleared concurrently (e.g. by new search), do not resurrect old pagination
                return merged

            curr_p = int(fresh_pag.get("current_page", 1))
            tot_p = int(fresh_pag.get("total_pages", 1))

            cmd_norm = str(command).strip().lower()
            if "next" in cmd_norm:
                c_p = curr_p + 1
            elif "prev" in cmd_norm:
                c_p = max(curr_p - 1, 1)
            elif "page" in cmd_norm:
                m = re.search(r"page\s*(\d+)", cmd_norm)
                c_p = max(int(m.group(1)), 1) if m else curr_p
            else:
                c_p = curr_p

            oob = c_p > tot_p
            if oob:
                c_p = max(tot_p + 1, 1)

            # Inherit generation of existing result pool
            p_gen = int(fresh_pag.get("search_gen") or merged.get("search_gen", 1))
            fresh_pag["search_gen"] = p_gen
            fresh_pag["current_page"] = c_p
            fresh_pag["has_next"] = c_p < tot_p
            fresh_pag["has_prev"] = c_p > 1

            merged["pagination"] = fresh_pag

            committed_meta["claimed_page"] = c_p
            committed_meta["is_oob"] = oob
            committed_meta["saved_p_state"] = fresh_pag
            return merged

        initial_state = _remerge_claim(state)
        if not committed_meta["saved_p_state"]:
            return {"success": False, "claimed_page": 1, "is_oob": False, "saved_p_state": {}, "error": "No active pagination state found"}

        ok = save_session_state_cas(
            session_id,
            initial_state,
            role=role,
            remerge_fn=_remerge_claim,
            user_id=user_id,
        )

        if not ok:
            log.warning("claim_next_pagination_page CAS write failed for session %s", key)
            return {
                "success": False,
                "claimed_page": committed_meta["claimed_page"],
                "is_oob": committed_meta["is_oob"],
                "saved_p_state": committed_meta["saved_p_state"],
                "error": "CAS write conflict or retries exhausted"
            }

        return {
            "success": True,
            "claimed_page": committed_meta["claimed_page"],
            "is_oob": committed_meta["is_oob"],
            "saved_p_state": committed_meta["saved_p_state"],
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


def get_conversation_context(
    session_id: str,
    role: Optional[str] = None,
    user_id: Optional[Union[str, int]] = None,
) -> Any:
    """
    Phase 6B: Read-only facade providing an instantaneous, coherent typed snapshot of
    the conversation state under the session lock.
    Does not mutate state, advance search generation, or create competing state stores.
    """
    try:
        from backend.schemas.conversation_context import ConversationContext
    except ModuleNotFoundError:
        from schemas.conversation_context import ConversationContext  # type: ignore

    if not session_id:
        return ConversationContext(
            session_id="",
            role=role or "employee",
            composite_key="",
            context_version=1,
            search_generation=1,
        )

    key = make_composite_key(session_id, role, user_id=user_id)
    norm_role = str(role or "employee").lower().strip()

    try:
        with get_session_lock(key):
            state = get_session_state(session_id, role=role, user_id=user_id)
    except RedisSessionOwnershipError:
        return ConversationContext(
            session_id=session_id,
            role=norm_role,
            user_id=str(user_id) if user_id is not None else None,
            composite_key=key,
            context_version=1,
            search_generation=1,
        )

    with get_session_lock(key):
        version_seq = int(state.get("version_seq", 1))
        last_active = float(state.get("_last_active", 0)) if state.get("_last_active") else None
        search_gen = int(state.get("search_gen", 1))

        # 1. Search criteria provenance
        active_criteria = state.get("search_context") or state.get("employer_search_context")
        has_active_search = bool(active_criteria or state.get("search_snapshot"))

        # 2. Recommendation context (Jobs)
        rec_ctx = state.get("recommendation_context") or {}
        ranked_jobs = rec_ctx.get("ranked_jobs") or []
        has_jobs = bool(ranked_jobs)
        job_count = len(ranked_jobs) if isinstance(ranked_jobs, list) else 0
        last_ref_id = rec_ctx.get("last_referenced_job_id")

        # 3. Employment disambiguation (Step 5A)
        # Evaluates 15-minute TTL expiration for option map
        is_authenticated = bool(user_id is not None and str(user_id).strip())

        # Select consistent state source for employment disambiguation:
        # Canonical composite-key state always takes precedence.
        # Fallback to role-level key or raw session_id is restricted strictly to
        # unauthenticated/anonymous calls (user_id is None/empty) within employee role.
        # Authenticated requests must never inherit unowned legacy keys or mix fields across scopes.
        emp_state = state
        if not is_authenticated and norm_role == "employee":
            has_emp_fields = any(
                state.get(f) is not None
                for f in ("pending_option_mapping", "pending_requested_level", "selected_employment_company", "selected_employment_record_key")
            )
            if not has_emp_fields:
                role_key = make_composite_key(session_id, role=norm_role)
                role_state = get_session_state(role_key) if role_key != key else {}
                has_role_fields = any(
                    role_state.get(f) is not None
                    for f in ("pending_option_mapping", "pending_requested_level", "selected_employment_company", "selected_employment_record_key")
                )
                if has_role_fields:
                    emp_state = role_state
                elif key != session_id:
                    raw_sess_state = get_session_state(session_id)
                    has_raw_fields = any(
                        raw_sess_state.get(f) is not None
                        for f in ("pending_option_mapping", "pending_requested_level", "selected_employment_company", "selected_employment_record_key")
                    )
                    if has_raw_fields:
                        emp_state = raw_sess_state

        ts_map = emp_state.get("pending_option_mapping")
        pending_lvl = emp_state.get("pending_requested_level")
        sel_ctx = emp_state.get("selected_employment_company")
        sel_key = emp_state.get("selected_employment_record_key")

        valid_opt_map = None
        if isinstance(ts_map, dict):
            created_at = float(ts_map.get("created_at", 0))
            if time.time() - created_at <= PENDING_OPTION_MAPPING_TTL_SECONDS:
                raw_m = ts_map.get("mapping")
                if isinstance(raw_m, dict):
                    valid_opt_map = dict(raw_m)

        # 4. Pagination state
        pag_state = state.get("pagination") or {}
        has_pag = bool(pag_state.get("all_recs") or pag_state.get("all_candidate_cards"))
        curr_page = int(pag_state.get("current_page", 1))
        total_p = int(pag_state.get("total_pages", 0))
        total_r = int(pag_state.get("total_results", 0))

        return ConversationContext(
            session_id=session_id,
            role=norm_role,
            user_id=user_id,
            composite_key=key,
            context_version=version_seq,
            last_active_at=last_active,
            search_generation=search_gen,
            has_active_search_context=has_active_search,
            active_search_criteria=active_criteria if isinstance(active_criteria, dict) else None,
            has_active_job_results=has_jobs,
            active_job_count=job_count,
            last_referenced_job_id=last_ref_id,
            pending_option_map=valid_opt_map,
            pending_requested_level=int(pending_lvl) if pending_lvl is not None else None,
            selected_employment_context=sel_ctx,
            selected_employment_record_key=sel_key,
            has_active_pagination=has_pag,
            current_page=curr_page,
            total_pages=total_p,
            total_results=total_r,
        )


async def get_conversation_context_async(
    session_id: str,
    role: Optional[str] = None,
    user_id: Optional[Union[str, int]] = None,
) -> Any:
    """
    Asynchronous variant of get_conversation_context providing a non-blocking, coherent
    snapshot of the conversation state.
    Uses get_session_state_async to offload DB I/O without blocking the event loop.
    Does not hold get_session_lock across any await boundary.
    """
    try:
        from backend.schemas.conversation_context import ConversationContext
    except ModuleNotFoundError:
        from schemas.conversation_context import ConversationContext  # type: ignore

    if not session_id:
        return ConversationContext(
            session_id="",
            role=role or "employee",
            composite_key="",
            context_version=1,
            search_generation=1,
        )

    key = make_composite_key(session_id, role, user_id=user_id)
    norm_role = str(role or "employee").lower().strip()

    # Offload DB reads asynchronously outside the session lock
    try:
        state = await get_session_state_async(session_id, role=role, user_id=user_id)
    except RedisSessionOwnershipError:
        return ConversationContext(
            session_id=session_id,
            role=norm_role,
            user_id=str(user_id) if user_id is not None else None,
            composite_key=key,
            context_version=1,
            search_generation=1,
        )

    version_seq = int(state.get("version_seq", 1))
    last_active = float(state.get("_last_active", 0)) if state.get("_last_active") else None
    search_gen = int(state.get("search_gen", 1))

    # 1. Search criteria provenance
    active_criteria = state.get("search_context") or state.get("employer_search_context")
    has_active_search = bool(active_criteria or state.get("search_snapshot"))

    # 2. Recommendation context (Jobs)
    rec_ctx = state.get("recommendation_context") or {}
    ranked_jobs = rec_ctx.get("ranked_jobs") or []
    has_jobs = bool(ranked_jobs)
    job_count = len(ranked_jobs) if isinstance(ranked_jobs, list) else 0
    last_ref_id = rec_ctx.get("last_referenced_job_id")

    # 3. Employment disambiguation (Step 5A)
    is_authenticated = bool(user_id is not None and str(user_id).strip())

    emp_state = state
    if not is_authenticated and norm_role == "employee":
        has_emp_fields = any(
            state.get(f) is not None
            for f in ("pending_option_mapping", "pending_requested_level", "selected_employment_company", "selected_employment_record_key")
        )
        if not has_emp_fields:
            role_key = make_composite_key(session_id, role=norm_role)
            role_state = await get_session_state_async(role_key) if role_key != key else {}
            has_role_fields = any(
                role_state.get(f) is not None
                for f in ("pending_option_mapping", "pending_requested_level", "selected_employment_company", "selected_employment_record_key")
            )
            if has_role_fields:
                emp_state = role_state
            elif key != session_id:
                raw_sess_state = await get_session_state_async(session_id)
                has_raw_fields = any(
                    raw_sess_state.get(f) is not None
                    for f in ("pending_option_mapping", "pending_requested_level", "selected_employment_company", "selected_employment_record_key")
                )
                if has_raw_fields:
                    emp_state = raw_sess_state

    ts_map = emp_state.get("pending_option_mapping")
    pending_lvl = emp_state.get("pending_requested_level")
    sel_ctx = emp_state.get("selected_employment_company")
    sel_key = emp_state.get("selected_employment_record_key")

    valid_opt_map = None
    if isinstance(ts_map, dict):
        created_at = float(ts_map.get("created_at", 0))
        if time.time() - created_at <= PENDING_OPTION_MAPPING_TTL_SECONDS:
            raw_m = ts_map.get("mapping")
            if isinstance(raw_m, dict):
                valid_opt_map = dict(raw_m)

    # 4. Pagination state
    pag_state = state.get("pagination") or {}
    has_pag = bool(pag_state.get("all_recs") or pag_state.get("all_candidate_cards"))
    curr_page = int(pag_state.get("current_page", 1))
    total_p = int(pag_state.get("total_pages", 0))
    total_r = int(pag_state.get("total_results", 0))

    return ConversationContext(
        session_id=session_id,
        role=norm_role,
        user_id=user_id,
        composite_key=key,
        context_version=version_seq,
        last_active_at=last_active,
        search_generation=search_gen,
        has_active_search_context=has_active_search,
        active_search_criteria=active_criteria if isinstance(active_criteria, dict) else None,
        has_active_job_results=has_jobs,
        active_job_count=job_count,
        last_referenced_job_id=last_ref_id,
        pending_option_map=valid_opt_map,
        pending_requested_level=int(pending_lvl) if pending_lvl is not None else None,
        selected_employment_context=sel_ctx,
        selected_employment_record_key=sel_key,
        has_active_pagination=has_pag,
        current_page=curr_page,
        total_pages=total_p,
        total_results=total_r,
    )
