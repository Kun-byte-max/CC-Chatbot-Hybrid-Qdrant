"""
backend/utils/conversation_history.py — Redis-backed Conversation History Storage Layer.

Phase 2C.1 & 2C.1.1: Provides strict RAM-only, Redis-authoritative conversation history storage.
Guarantees:
- Cryptographic session and user identity isolation.
- Atomic completed-turn commit (user message + completed assistant response).
- Complete-turn pair preservation during history trimming (never trims half a turn).
- Cumulative history byte limit enforcement (MAX_SESSION_BYTES strictly bounds entire retained history).
- Bounded idempotency metadata (turns set pruned alongside history trimming to prevent memory leaks).
- Safe per-session turn concurrency contract with duplicate detection and sequence ordering.
- Strict payload validation, rejecting oversized or untrusted prompt data.
- Full asynchronous Redis operations with typed error handling and zero MySQL / in-memory fallbacks.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
import re
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, Any, Optional, List, Union, Literal

from redis.exceptions import (
    ConnectionError as RedisConnectionError,
    TimeoutError as RedisTimeoutError,
    ResponseError as RedisResponseError,
    WatchError,
    RedisError as BaseRedisLibraryError,
)

try:
    from backend.utils.redis_client import (
        get_async_redis_client,
        RedisSessionError,
        RedisUnavailableError,
        RedisSessionOwnershipError,
        RedisSessionOOMError,
        RedisSessionPayloadLimitError,
    )
    from backend.utils.session_state import (
        resolve_effective_user_id,
        is_valid_session_id,
    )
    from backend.config.config import (
        SESSION_TTL_SECONDS,
        MAX_SESSION_MESSAGES,
        MAX_SESSION_BYTES,
        MAX_MESSAGE_CHARS,
    )
except ModuleNotFoundError:
    from utils.redis_client import (  # type: ignore
        get_async_redis_client,
        RedisSessionError,
        RedisUnavailableError,
        RedisSessionOwnershipError,
        RedisSessionOOMError,
        RedisSessionPayloadLimitError,
    )
    from utils.session_state import (  # type: ignore
        resolve_effective_user_id,
        is_valid_session_id,
    )
    from config.config import (  # type: ignore
        SESSION_TTL_SECONDS,
        MAX_SESSION_MESSAGES,
        MAX_SESSION_BYTES,
        MAX_MESSAGE_CHARS,
    )

log = logging.getLogger(__name__)

MAX_CAS_RETRIES = 3

# Forbidden substrings that must never be stored in persistent conversation history
FORBIDDEN_CONTENT_PATTERNS = [
    re.compile(r"<<<RESULTS_DATA", re.IGNORECASE),
    re.compile(r"RESULTS_DATA>>>", re.IGNORECASE),
    re.compile(r"## CURRENT USER PROFILE CONTEXT", re.IGNORECASE),
    re.compile(r"## LIVE PLATFORM WIDGETS DATA", re.IGNORECASE),
    re.compile(r"Bearer\s+[A-Za-z0-9\-\._~\+\/]+=*", re.IGNORECASE),
]


# ============================================================================
# 1. Domain Exceptions
# ============================================================================

class ConversationHistoryError(RedisSessionError):
    """Base exception for conversation history store."""
    pass


class InvalidMessagePayloadError(ConversationHistoryError):
    """Raised when message role, content, or turn structure fails validation."""
    pass


class PayloadTooLargeError(RedisSessionPayloadLimitError, ConversationHistoryError):
    """Raised when message content or total turn payload exceeds size limits."""
    pass


class DuplicateTurnError(ConversationHistoryError):
    """Raised when committing a turn_id that has already been committed."""
    pass


class TurnConcurrencyConflictError(ConversationHistoryError):
    """Raised when concurrent or stale turn commit violates sequence or parent turn ordering."""
    pass


class CorruptedHistoryError(ConversationHistoryError):
    """Raised when stored Redis history contains unparseable or corrupted message records."""
    pass


# ============================================================================
# 2. Domain Data Structures
# ============================================================================

class TurnCommitStatus(str, Enum):
    COMMITTED = "COMMITTED"
    ALREADY_COMMITTED = "ALREADY_COMMITTED"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class ChatMessageRecord:
    """Represents an immutable, validated message record in conversation history."""
    message_id: str
    turn_id: str
    role: Literal["user", "assistant"]
    content: str
    timestamp: float
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "turn_id": self.turn_id,
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ChatMessageRecord:
        if not isinstance(data, dict):
            raise CorruptedHistoryError("Stored message entry is not a valid JSON dictionary.")

        role = data.get("role")
        if role not in ("user", "assistant"):
            raise CorruptedHistoryError(f"Stored message has invalid role: {role!r}")

        content = data.get("content")
        if not isinstance(content, str):
            raise CorruptedHistoryError("Stored message content is missing or not a string.")

        turn_id = data.get("turn_id")
        if not turn_id or not isinstance(turn_id, str) or not turn_id.strip():
            raise CorruptedHistoryError("Stored message turn_id is missing or invalid.")

        message_id = data.get("message_id")
        if not message_id or not isinstance(message_id, str) or not message_id.strip():
            raise CorruptedHistoryError("Stored message message_id is missing or invalid.")

        timestamp = float(data.get("timestamp") or time.time())
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}

        return cls(
            message_id=str(message_id),
            turn_id=str(turn_id),
            role=role,
            content=content,
            timestamp=timestamp,
            metadata=metadata,
        )


@dataclass(frozen=True)
class TurnCommitResult:
    """Result of an atomic turn commit operation."""
    status: TurnCommitStatus
    turn_id: str
    turn_seq: int
    total_messages: int
    committed: bool
    error_message: Optional[str] = None


@dataclass(frozen=True)
class ConversationHistorySnapshot:
    """Atomic snapshot of validated conversation history and session turn metadata."""
    history: List[ChatMessageRecord]
    turn_seq: int
    last_turn_id: Optional[str]
    total_messages: int
    updated_at: Optional[float] = None


# ============================================================================
# 3. Redis Key Formatting & Error Handling
# ============================================================================

def to_history_redis_key(user_id: Union[str, int], session_id: str) -> str:
    """Canonical Redis LIST storage key: chat:history:{user_id}:{session_id}."""
    eff_uid = resolve_effective_user_id(user_id, session_id)
    clean_sess = str(session_id or "").strip()
    if clean_sess.startswith("chat:history:"):
        return clean_sess
    if clean_sess.startswith(f"{eff_uid}:"):
        clean_sess = clean_sess[len(eff_uid) + 1:]
    return f"chat:history:{eff_uid}:{clean_sess}"


def to_history_meta_redis_key(user_id: Union[str, int], session_id: str) -> str:
    """Canonical Redis HASH metadata key: chat:history:meta:{user_id}:{session_id}."""
    hist_key = to_history_redis_key(user_id, session_id)
    parts = hist_key.split(":")
    eff_uid = parts[2]
    sess = ":".join(parts[3:])
    return f"chat:history:meta:{eff_uid}:{sess}"


def to_history_turns_redis_key(user_id: Union[str, int], session_id: str) -> str:
    """Canonical Redis SET committed turns key: chat:history:turns:{user_id}:{session_id}."""
    hist_key = to_history_redis_key(user_id, session_id)
    parts = hist_key.split(":")
    eff_uid = parts[2]
    sess = ":".join(parts[3:])
    return f"chat:history:turns:{eff_uid}:{sess}"


def _classify_redis_error(e: Exception, action: str = "operation") -> Exception:
    """Maps low-level Redis exceptions into typed CollarCheck domain errors."""
    if isinstance(e, (RedisUnavailableError, RedisSessionOwnershipError, RedisSessionOOMError, RedisSessionPayloadLimitError, ConversationHistoryError)):
        return e

    err_str = str(e)
    if isinstance(e, (RedisConnectionError, RedisTimeoutError)):
        log.error("Redis connection failure during conversation history %s: %s", action, e)
        return RedisUnavailableError(f"Redis conversation history storage unavailable during {action}: {err_str}")

    if isinstance(e, RedisResponseError) and "OOM" in err_str:
        log.error("Redis memory limit reached during conversation history %s: %s", action, e)
        return RedisSessionOOMError(f"Redis conversation history memory limit exceeded during {action}: {err_str}")

    return ConversationHistoryError(f"Redis conversation history failure during {action}: {err_str}")


# ============================================================================
# 4. Message Content & Turn Payload Validation
# ============================================================================

def validate_message_content(content: Any, role: str) -> str:
    """
    Validates individual message content:
    - Must be non-empty string.
    - Must not exceed MAX_MESSAGE_CHARS (configurable, default 8,000).
    - Must not contain prohibited system prompt blocks or unredacted credentials.
    """
    if role not in ("user", "assistant"):
        raise InvalidMessagePayloadError(f"Invalid message role '{role}'. Permitted roles: 'user', 'assistant'.")

    if content is None or not isinstance(content, str):
        raise InvalidMessagePayloadError(f"Message content for role '{role}' must be a non-null string.")

    trimmed = content.strip()
    if not trimmed:
        raise InvalidMessagePayloadError(f"Message content for role '{role}' cannot be empty.")

    limit_chars = int(MAX_MESSAGE_CHARS)
    if len(content) > limit_chars:
        raise PayloadTooLargeError(
            f"Message content for role '{role}' exceeds maximum limit of {limit_chars} characters "
            f"(received {len(content)})."
        )

    for pat in FORBIDDEN_CONTENT_PATTERNS:
        if pat.search(content):
            raise InvalidMessagePayloadError(
                f"Message content for role '{role}' contains prohibited system metadata or security blocks."
            )

    return content


def validate_turn_payload(
    turn_id: Any,
    user_message: Any,
    assistant_message: Any,
    metadata: Optional[Dict[str, Any]] = None,
) -> tuple[str, str, str, Dict[str, Any]]:
    """
    Validates completed-turn arguments before attempting atomic persistence:
    - Validates turn_id string format.
    - Validates user_message.
    - Validates assistant_message.
    - Ensures single turn serialized UTF-8 bytes do not exceed MAX_SESSION_BYTES.
    """
    if not turn_id or not isinstance(turn_id, str) or not turn_id.strip():
        raise InvalidMessagePayloadError("turn_id must be a non-empty string.")

    clean_turn_id = turn_id.strip()

    valid_user_msg = validate_message_content(user_message, "user")
    valid_asst_msg = validate_message_content(assistant_message, "assistant")
    clean_meta = dict(metadata) if isinstance(metadata, dict) else {}

    # Estimate serialized size of turn in UTF-8 bytes
    sample_user = ChatMessageRecord(
        message_id=f"{clean_turn_id}_u",
        turn_id=clean_turn_id,
        role="user",
        content=valid_user_msg,
        timestamp=time.time(),
        metadata={},
    ).to_dict()

    sample_asst = ChatMessageRecord(
        message_id=f"{clean_turn_id}_a",
        turn_id=clean_turn_id,
        role="assistant",
        content=valid_asst_msg,
        timestamp=time.time(),
        metadata=clean_meta,
    ).to_dict()

    turn_bytes = len(json.dumps(sample_user, ensure_ascii=False).encode("utf-8")) + \
                 len(json.dumps(sample_asst, ensure_ascii=False).encode("utf-8"))

    max_bytes = int(MAX_SESSION_BYTES)
    if turn_bytes > max_bytes:
        raise PayloadTooLargeError(
            f"Turn payload size ({turn_bytes} bytes) exceeds MAX_SESSION_BYTES limit of {max_bytes} bytes."
        )

    return clean_turn_id, valid_user_msg, valid_asst_msg, clean_meta


# ============================================================================
# 5. Production Lua Script for Completed-Turn Atomic Commit
# ============================================================================

LUA_APPEND_TURN_SCRIPT = """
-- Production Lua script for completed-turn atomic append with cumulative capacity bounds & idempotency pruning
-- KEYS[1]: history_key (Redis LIST: chat:history:{uid}:{sid})
-- KEYS[2]: meta_key    (Redis HASH: chat:history:meta:{uid}:{sid})
-- KEYS[3]: turns_key   (Redis SET:  chat:history:turns:{uid}:{sid})
-- ARGV[1]: turn_id
-- ARGV[2]: serialized_user_message_json
-- ARGV[3]: serialized_assistant_message_json
-- ARGV[4]: max_messages (even number)
-- ARGV[5]: ttl_seconds
-- ARGV[6]: timestamp_str
-- ARGV[7]: expected_previous_turn_id (empty string if not specified)
-- ARGV[8]: expected_seq (string number or empty string if not specified)
-- ARGV[9]: max_session_bytes

-- 1. Input & Numeric Validation (Read-Only)
local turn_id = ARGV[1]
local user_json = ARGV[2]
local asst_json = ARGV[3]
local max_messages = tonumber(ARGV[4])
local ttl_seconds = tonumber(ARGV[5])
local ts_str = ARGV[6]
local exp_prev_turn = ARGV[7]
local exp_seq_str = ARGV[8]
local max_session_bytes = tonumber(ARGV[9])

if not turn_id or turn_id == '' then
    return { 'ERR_VALIDATION', 'turn_id cannot be empty' }
end
if not max_messages or max_messages <= 0 then
    return { 'ERR_VALIDATION', 'max_messages must be a positive integer' }
end
if not ttl_seconds or ttl_seconds <= 0 then
    return { 'ERR_VALIDATION', 'ttl_seconds must be a positive integer' }
end
if not max_session_bytes or max_session_bytes <= 0 then
    return { 'ERR_VALIDATION', 'max_session_bytes must be a positive integer' }
end

local new_turn_bytes = string.len(user_json) + string.len(asst_json)
if new_turn_bytes > max_session_bytes then
    return { 'PAYLOAD_TOO_LARGE', tostring(new_turn_bytes), tostring(max_session_bytes) }
end

-- 2. Validate Key Types (Read-Only)
local function check_type(k, expected)
    local t_obj = redis.call('TYPE', k)
    local t = type(t_obj) == 'table' and (t_obj.ok or tostring(t_obj[1])) or tostring(t_obj)
    if t ~= 'none' and t ~= expected then
        return false, t
    end
    return true, t
end

local ok1, t1 = check_type(KEYS[1], 'list')
if not ok1 then
    return { 'ERR_WRONG_TYPE', KEYS[1] .. ' is ' .. t1 .. ', expected list' }
end
local ok2, t2 = check_type(KEYS[2], 'hash')
if not ok2 then
    return { 'ERR_WRONG_TYPE', KEYS[2] .. ' is ' .. t2 .. ', expected hash' }
end
local ok3, t3 = check_type(KEYS[3], 'set')
if not ok3 then
    return { 'ERR_WRONG_TYPE', KEYS[3] .. ' is ' .. t3 .. ', expected set' }
end

-- 3. Idempotency Check (Read-Only)
local is_dup = redis.call('SISMEMBER', KEYS[3], turn_id)
if is_dup == 1 then
    local cur_seq = tonumber(redis.call('HGET', KEYS[2], 'turn_seq') or '0')
    local cur_len = tonumber(redis.call('LLEN', KEYS[1]) or '0')
    return { 'ALREADY_COMMITTED', tostring(cur_seq), tostring(cur_len) }
end

-- 4. Concurrency Contract: Previous Turn ID (Read-Only)
local last_turn = redis.call('HGET', KEYS[2], 'last_turn_id') or ''
if exp_prev_turn ~= '' and exp_prev_turn ~= last_turn then
    return { 'CONFLICT_STALE_TURN', last_turn }
end

-- 5. Concurrency Contract: Sequence (Read-Only)
local cur_seq = tonumber(redis.call('HGET', KEYS[2], 'turn_seq') or '0')
if exp_seq_str ~= '' then
    local exp_seq = tonumber(exp_seq_str)
    if exp_seq ~= cur_seq then
        return { 'CONFLICT_SEQUENCE_MISMATCH', tostring(cur_seq) }
    end
end

-- 6. Cumulative Capacity & Trimming Calculation (Read-Only)
local elements = redis.call('LRANGE', KEYS[1], 0, -1)
local total_bytes = 0
for _, el in ipairs(elements) do
    total_bytes = total_bytes + string.len(el)
end

local trim_pairs = 0
local n_elem = #elements
while (n_elem - (trim_pairs * 2) > 0) and
      ((n_elem - (trim_pairs * 2) + 2 > max_messages) or (total_bytes + new_turn_bytes > max_session_bytes)) do
    local u_idx = (trim_pairs * 2) + 1
    local a_idx = (trim_pairs * 2) + 2
    local u_len = elements[u_idx] and string.len(elements[u_idx]) or 0
    local a_len = elements[a_idx] and string.len(elements[a_idx]) or 0
    total_bytes = total_bytes - u_len - a_len
    trim_pairs = trim_pairs + 1
end

-- If after trimming all existing elements the turn STILL cannot fit
if total_bytes + new_turn_bytes > max_session_bytes then
    return { 'PAYLOAD_TOO_LARGE', tostring(total_bytes + new_turn_bytes), tostring(max_session_bytes) }
end

-- ALL PRE-CHECKS AND VALIDATIONS COMPLETE. ZERO MUTATIONS OCCURRED PRIOR TO THIS POINT.

-- 7. Execute Trimming & Remove Trimmed Turn IDs from Idempotency Set
if trim_pairs > 0 then
    for p = 0, trim_pairs - 1 do
        local u_msg = elements[(p * 2) + 1]
        if u_msg then
            local trimmed_tid = string.match(u_msg, '"turn_id"%s*:%s*"([^"]+)"')
            if trimmed_tid then
                redis.call('SREM', KEYS[3], trimmed_tid)
            end
        end
    end
    redis.call('LTRIM', KEYS[1], trim_pairs * 2, -1)
end

-- 8. Append New Completed Turn Pair
redis.call('RPUSH', KEYS[1], user_json, asst_json)

-- 9. Update Metadata & Idempotency Set
local new_seq = cur_seq + 1
redis.call('HSET', KEYS[2], 'last_turn_id', turn_id, 'turn_seq', tostring(new_seq), 'updated_at', ts_str)
redis.call('SADD', KEYS[3], turn_id)

-- 10. Atomic TTL Renewal across all associated keys
redis.call('EXPIRE', KEYS[1], ttl_seconds)
redis.call('EXPIRE', KEYS[2], ttl_seconds)
redis.call('EXPIRE', KEYS[3], ttl_seconds)

local final_len = tonumber(redis.call('LLEN', KEYS[1]))
return { 'COMMITTED', tostring(new_seq), tostring(final_len) }
"""


LUA_SNAPSHOT_SCRIPT = """
-- Production Lua script for atomic conversation history snapshot
-- KEYS[1]: history_key (Redis LIST: chat:history:{uid}:{sid})
-- KEYS[2]: meta_key    (Redis HASH: chat:history:meta:{uid}:{sid})
-- KEYS[3]: turns_key   (Redis SET:  chat:history:turns:{uid}:{sid})
-- ARGV[1]: range_start (number: -eff_limit or 0)
-- ARGV[2]: ttl_seconds (number)

local range_start = tonumber(ARGV[1]) or 0
local ttl_seconds = tonumber(ARGV[2])

local elements = redis.call('LRANGE', KEYS[1], range_start, -1)
local meta = redis.call('HGETALL', KEYS[2])
local total_count = redis.call('LLEN', KEYS[1])

if ttl_seconds and ttl_seconds > 0 and (total_count > 0 or #meta > 0) then
    redis.call('EXPIRE', KEYS[1], ttl_seconds)
    redis.call('EXPIRE', KEYS[2], ttl_seconds)
    redis.call('EXPIRE', KEYS[3], ttl_seconds)
end

return { elements, meta, tostring(total_count) }
"""


# ============================================================================
# 6. Fallback / Test Transaction Implementation (FakeRedis Compatibility)
# ============================================================================

async def _execute_atomic_turn_commit_fallback_async(
    rc: Any,
    history_key: str,
    meta_key: str,
    turns_key: str,
    turn_id: str,
    user_json: str,
    asst_json: str,
    max_messages: int,
    max_session_bytes: int,
    ttl_seconds: int,
    timestamp: float,
    expected_previous_turn_id: Optional[str] = None,
    expected_seq: Optional[int] = None,
) -> tuple[str, str, str]:
    """
    Alternative atomic test/fallback path using WATCH/MULTI/EXEC.
    Preserves exact production Lua semantics when running against test FakeRedis instances
    that lack the native C-based Lua 'eval' engine.
    """
    new_turn_bytes = len(user_json.encode("utf-8")) + len(asst_json.encode("utf-8"))
    if new_turn_bytes > max_session_bytes:
        return ("PAYLOAD_TOO_LARGE", str(new_turn_bytes), str(max_session_bytes))

    for attempt in range(MAX_CAS_RETRIES):
        pipe = rc.pipeline(transaction=True)
        try:
            # Watch ALL relevant keys that affect commit state
            await pipe.watch(history_key, meta_key, turns_key)

            # 1. Validate Key Types
            for k, exp_t in [(history_key, "list"), (meta_key, "hash"), (turns_key, "set")]:
                t_raw = await rc.type(k)
                t_str = t_raw.decode("utf-8") if isinstance(t_raw, bytes) else str(t_raw)
                if t_str != "none" and t_str != exp_t:
                    await pipe.unwatch()
                    return ("ERR_WRONG_TYPE", f"{k} is {t_str}, expected {exp_t}", "")

            # 2. Idempotency Check
            is_dup = await rc.sismember(turns_key, turn_id)
            if is_dup:
                await pipe.unwatch()
                cur_seq_raw = await rc.hget(meta_key, "turn_seq")
                cur_len = await rc.llen(history_key)
                cur_seq = int(cur_seq_raw or 0)
                return ("ALREADY_COMMITTED", str(cur_seq), str(cur_len))

            # 3. Concurrency Contract: Previous Turn ID
            last_turn_raw = await rc.hget(meta_key, "last_turn_id")
            last_turn = last_turn_raw.decode("utf-8") if isinstance(last_turn_raw, bytes) else (str(last_turn_raw) if last_turn_raw else "")
            if expected_previous_turn_id is not None and expected_previous_turn_id != last_turn:
                await pipe.unwatch()
                return ("CONFLICT_STALE_TURN", last_turn, "")

            # 4. Concurrency Contract: Sequence
            cur_seq_raw = await rc.hget(meta_key, "turn_seq")
            cur_seq = int(cur_seq_raw or 0)
            if expected_seq is not None and expected_seq != cur_seq:
                await pipe.unwatch()
                return ("CONFLICT_SEQUENCE_MISMATCH", str(cur_seq), "")

            # 5. Cumulative Capacity & Trimming Calculation
            elements = await rc.lrange(history_key, 0, -1)
            total_bytes = sum(len(el if isinstance(el, bytes) else el.encode("utf-8")) for el in elements)

            trim_pairs = 0
            n_elem = len(elements)
            while (n_elem - (trim_pairs * 2) > 0) and (
                (n_elem - (trim_pairs * 2) + 2 > max_messages) or
                (total_bytes + new_turn_bytes > max_session_bytes)
            ):
                u_el = elements[trim_pairs * 2]
                a_el = elements[(trim_pairs * 2) + 1] if ((trim_pairs * 2) + 1) < n_elem else b""
                u_len = len(u_el if isinstance(u_el, bytes) else u_el.encode("utf-8"))
                a_len = len(a_el if isinstance(a_el, bytes) else a_el.encode("utf-8"))
                total_bytes -= (u_len + a_len)
                trim_pairs += 1

            if total_bytes + new_turn_bytes > max_session_bytes:
                await pipe.unwatch()
                return ("PAYLOAD_TOO_LARGE", str(total_bytes + new_turn_bytes), str(max_session_bytes))

            # Extract turn IDs to remove from turns set
            trimmed_tids = []
            for p in range(trim_pairs):
                u_msg = elements[p * 2]
                u_str = u_msg.decode("utf-8") if isinstance(u_msg, bytes) else str(u_msg)
                m = re.search(r'"turn_id"\s*:\s*"([^"]+)"', u_str)
                if m:
                    trimmed_tids.append(m.group(1))

            # 6. Atomic Multi Execution
            pipe.multi()
            for tid in trimmed_tids:
                pipe.srem(turns_key, tid)

            if trim_pairs > 0:
                pipe.ltrim(history_key, trim_pairs * 2, -1)

            pipe.rpush(history_key, user_json, asst_json)

            new_seq = cur_seq + 1
            pipe.hset(meta_key, mapping={
                "last_turn_id": turn_id,
                "turn_seq": str(new_seq),
                "updated_at": str(timestamp),
            })
            pipe.sadd(turns_key, turn_id)
            pipe.expire(history_key, ttl_seconds)
            pipe.expire(meta_key, ttl_seconds)
            pipe.expire(turns_key, ttl_seconds)
            pipe.llen(history_key)

            res = await pipe.execute()
            final_len = res[-1]
            return ("COMMITTED", str(new_seq), str(final_len))

        except WatchError:
            if attempt == MAX_CAS_RETRIES - 1:
                return ("CONFLICT_CONCURRENT_RACE", str(cur_seq), "")
            continue
        finally:
            try:
                await pipe.reset()
            except Exception:
                pass

    return ("CONFLICT_CONCURRENT_RACE", "", "")


async def _execute_snapshot_fallback_async(
    rc: Any,
    history_key: str,
    meta_key: str,
    turns_key: str,
    range_start: int,
    ttl_seconds: int,
) -> tuple[List[Any], Dict[str, str], int]:
    """
    Fallback implementation of atomic history snapshot using WATCH/MULTI/EXEC.
    Guarantees consistency between history list and metadata hash under concurrency.
    """
    for attempt in range(MAX_CAS_RETRIES):
        pipe = rc.pipeline(transaction=True)
        try:
            await pipe.watch(history_key, meta_key, turns_key)
            raw_entries = await rc.lrange(history_key, range_start, -1)
            meta_raw = await rc.hgetall(meta_key)
            total_count = await rc.llen(history_key)

            has_data = bool(raw_entries or meta_raw or total_count > 0)
            pipe.multi()
            if has_data and ttl_seconds > 0:
                pipe.expire(history_key, ttl_seconds)
                pipe.expire(meta_key, ttl_seconds)
                pipe.expire(turns_key, ttl_seconds)
            await pipe.execute()

            meta_dict: Dict[str, str] = {}
            if isinstance(meta_raw, dict):
                for k, v in meta_raw.items():
                    k_str = k.decode("utf-8") if isinstance(k, bytes) else str(k)
                    v_str = v.decode("utf-8") if isinstance(v, bytes) else str(v)
                    meta_dict[k_str] = v_str
            elif isinstance(meta_raw, (list, tuple)):
                for i in range(0, len(meta_raw), 2):
                    k_str = meta_raw[i].decode("utf-8") if isinstance(meta_raw[i], bytes) else str(meta_raw[i])
                    v_str = meta_raw[i + 1].decode("utf-8") if isinstance(meta_raw[i + 1], bytes) else str(meta_raw[i + 1])
                    meta_dict[k_str] = v_str

            return raw_entries, meta_dict, int(total_count)
        except WatchError:
            if attempt == MAX_CAS_RETRIES - 1:
                # Contention exhaustion fallback: read consistently via non-transactional pipeline
                p2 = rc.pipeline(transaction=False)
                p2.lrange(history_key, range_start, -1)
                p2.hgetall(meta_key)
                p2.llen(history_key)
                res = await p2.execute()
                raw_entries, meta_raw, total_count = res[0], res[1], res[2]
                meta_dict = {}
                if isinstance(meta_raw, dict):
                    for k, v in meta_raw.items():
                        k_str = k.decode("utf-8") if isinstance(k, bytes) else str(k)
                        v_str = v.decode("utf-8") if isinstance(v, bytes) else str(v)
                        meta_dict[k_str] = v_str
                return raw_entries, meta_dict, int(total_count)
            continue
        finally:
            try:
                await pipe.reset()
            except Exception:
                pass
    return [], {}, 0


# ============================================================================
# 7. Public Asynchronous API
# ============================================================================

def parse_and_validate_history_records(
    raw_entries: List[Union[bytes, str]],
    session_id: str,
    key_name: str = "",
) -> List[ChatMessageRecord]:
    """
    Parses and validates stored Redis JSON message entries enforcing turn-pair integrity:
    - Even message count.
    - Role alternation: records[2k] is 'user', records[2k+1] is 'assistant'.
    - Matching turn_id across each pair.
    - Non-empty message_id and turn_id.
    - Raises CorruptedHistoryError on any corruption; never returns partial or fabricated records.
    """
    if not raw_entries:
        return []

    if len(raw_entries) % 2 != 0:
        raise CorruptedHistoryError(
            f"Stored history has odd message count ({len(raw_entries)}) in session {session_id}. Incomplete turn pair detected."
        )

    records: List[ChatMessageRecord] = []
    for idx, entry in enumerate(raw_entries):
        raw_str = entry.decode("utf-8") if isinstance(entry, bytes) else str(entry)
        try:
            data = json.loads(raw_str)
        except Exception as json_err:
            log.error("Corrupted JSON at index %d in key %s: %s", idx, key_name, json_err)
            raise CorruptedHistoryError(
                f"Stored message at index {idx} contains corrupted non-JSON data in session {session_id}."
            ) from json_err

        record = ChatMessageRecord.from_dict(data)
        records.append(record)

    for i in range(0, len(records), 2):
        u_rec = records[i]
        a_rec = records[i + 1]

        if u_rec.role != "user":
            raise CorruptedHistoryError(
                f"Turn pair {i // 2} role corruption: expected role 'user' at index {i}, found '{u_rec.role}'."
            )
        if a_rec.role != "assistant":
            raise CorruptedHistoryError(
                f"Turn pair {i // 2} role corruption: expected role 'assistant' at index {i + 1}, found '{a_rec.role}'."
            )
        if u_rec.turn_id != a_rec.turn_id:
            raise CorruptedHistoryError(
                f"Turn pair {i // 2} turn_id mismatch: user has '{u_rec.turn_id}', assistant has '{a_rec.turn_id}'."
            )

    return records

async def get_conversation_history_async(
    user_id: Union[str, int],
    session_id: str,
    limit: Optional[int] = None,
) -> List[ChatMessageRecord]:
    """
    Retrieves chronological conversation history records from Redis for a validated user session.
    
    Guarantees:
    - Cryptographic identity isolation scoped to effective user_id.
    - Validates complete turn-pair integrity: alternating roles [user, assistant], matching turn_id,
      even message count, and non-empty message IDs.
    - Raises CorruptedHistoryError on any corruption; never returns partial or fabricated records.
    - Sliding window limit preserves complete turns (rounded to even count).
    
    Args:
        user_id: Authenticated user identity (verified from JWT).
        session_id: Canonical session ID string.
        limit: Optional maximum number of recent messages to return (sliding window).
               Preserves complete turn pairs (rounded to even message count).
    
    Returns:
        List of ChatMessageRecord objects in chronological order [oldest ... newest].
        Returns empty list if history does not exist or has expired.
    
    Raises:
        RedisSessionOwnershipError: If user_id fails ownership validation.
        CorruptedHistoryError: If stored entries contain malformed JSON, corrupted fields,
                               odd message counts, or mismatched turn IDs.
        RedisUnavailableError: If Redis connection/ping fails.
    """
    eff_uid = resolve_effective_user_id(user_id, session_id)
    if not is_valid_session_id(session_id):
        raise RedisSessionOwnershipError(f"Invalid session_id format: {session_id!r}")

    history_key = to_history_redis_key(eff_uid, session_id)
    meta_key = to_history_meta_redis_key(eff_uid, session_id)
    turns_key = to_history_turns_redis_key(eff_uid, session_id)

    rc = get_async_redis_client()

    try:
        if limit is not None and limit > 0:
            eff_limit = (limit // 2) * 2
            if eff_limit == 0:
                eff_limit = 2
            raw_entries = await rc.lrange(history_key, -eff_limit, -1)
        else:
            raw_entries = await rc.lrange(history_key, 0, -1)

        if not raw_entries:
            return []

        # Refresh TTL on successful read
        try:
            await rc.expire(history_key, SESSION_TTL_SECONDS)
            await rc.expire(meta_key, SESSION_TTL_SECONDS)
            await rc.expire(turns_key, SESSION_TTL_SECONDS)
        except Exception as ttl_ex:
            log.warning("Failed to renew conversation history TTL during read: %s", ttl_ex)

        return parse_and_validate_history_records(raw_entries, session_id=session_id, key_name=history_key)

    except BaseRedisLibraryError as e:
        raise _classify_redis_error(e, action="read") from e


async def append_turn_atomic_async(
    user_id: Union[str, int],
    session_id: str,
    turn_id: str,
    user_message: str,
    assistant_message: str,
    metadata: Optional[Dict[str, Any]] = None,
    expected_previous_turn_id: Optional[str] = None,
    expected_seq: Optional[int] = None,
    raise_on_duplicate: bool = False,
) -> TurnCommitResult:
    """
    Atomically appends a completed turn (user message + assistant reply) to Redis.
    
    Guarantees:
    - Atomicity: Both messages are appended together, or neither is appended.
    - Cumulative capacity enforcement: Rejects turns that exceed MAX_SESSION_BYTES across retained history.
    - Bounded idempotency: Obsolete turn IDs are pruned from turns set when pairs leave the sliding window.
    - Idempotency: Duplicate commits with the same `turn_id` within the retention window are detected and rejected.
    - Concurrency contract: Enforces expected parent turn and sequence order without interleaving.
      NOTE ON OMISSION: If both expected_previous_turn_id and expected_seq are omitted, commits follow blind
      arrival ordering (FIFO append). To reject concurrent branching requests, callers must pass expected_seq
      or expected_previous_turn_id.
    - Turn pair preservation: Trimming preserves complete user/assistant pairs (even message cap).
    - TTL renewal: Atomically extends TTL to SESSION_TTL_SECONDS on all associated keys.
    
    Args:
        user_id: Authenticated user identity.
        session_id: Canonical session ID string.
        turn_id: Unique identifier for this turn.
        user_message: Validated text of user prompt.
        assistant_message: Validated completed assistant response text.
        metadata: Optional dictionary of turn metadata (e.g. intent_hint, result_type).
        expected_previous_turn_id: If provided, enforces that the last committed turn matches this ID.
        expected_seq: If provided, enforces that the current turn sequence matches this integer.
        raise_on_duplicate: If True, raises DuplicateTurnError on duplicates instead of returning result.
    
    Returns:
        TurnCommitResult detailing commit status, sequence number, and total message count.
    
    Raises:
        InvalidMessagePayloadError: If roles, content, or turn_id fail validation.
        PayloadTooLargeError: If message characters or cumulative history bytes exceed limits.
        DuplicateTurnError: If raise_on_duplicate is True and turn_id was already committed.
        TurnConcurrencyConflictError: If expected_previous_turn_id or expected_seq fails.
        RedisUnavailableError: If Redis connection/ping fails.
        RedisSessionOOMError: If Redis memory limit is reached.
    """
    eff_uid = resolve_effective_user_id(user_id, session_id)
    if not is_valid_session_id(session_id):
        raise RedisSessionOwnershipError(f"Invalid session_id format: {session_id!r}")

    clean_turn_id, clean_user_msg, clean_asst_msg, clean_meta = validate_turn_payload(
        turn_id, user_message, assistant_message, metadata
    )

    history_key = to_history_redis_key(eff_uid, session_id)
    meta_key = to_history_meta_redis_key(eff_uid, session_id)
    turns_key = to_history_turns_redis_key(eff_uid, session_id)

    raw_max_msgs = int(MAX_SESSION_MESSAGES)
    max_messages = (raw_max_msgs // 2) * 2  # round down to nearest even integer
    max_session_bytes = int(MAX_SESSION_BYTES)
    ttl_seconds = int(SESSION_TTL_SECONDS)
    now_ts = time.time()

    user_record = ChatMessageRecord(
        message_id=f"{clean_turn_id}_u",
        turn_id=clean_turn_id,
        role="user",
        content=clean_user_msg,
        timestamp=now_ts,
        metadata={},
    )

    asst_record = ChatMessageRecord(
        message_id=f"{clean_turn_id}_a",
        turn_id=clean_turn_id,
        role="assistant",
        content=clean_asst_msg,
        timestamp=now_ts,
        metadata=clean_meta,
    )

    user_json = json.dumps(user_record.to_dict(), ensure_ascii=False)
    asst_json = json.dumps(asst_record.to_dict(), ensure_ascii=False)

    rc = get_async_redis_client()

    exp_prev = str(expected_previous_turn_id or "").strip()
    exp_seq_str = str(expected_seq) if expected_seq is not None else ""

    try:
        # 1. Attempt Native Lua Script Execution (Production Path)
        try:
            raw_res = await rc.eval(
                LUA_APPEND_TURN_SCRIPT,
                3,
                history_key,
                meta_key,
                turns_key,
                clean_turn_id,
                user_json,
                asst_json,
                str(max_messages),
                str(ttl_seconds),
                str(now_ts),
                exp_prev,
                exp_seq_str,
                str(max_session_bytes),
            )
        except RedisResponseError as r_err:
            # If Lua 'eval' is unsupported in this environment (e.g. FakeRedis without lupa), use test fallback
            err_msg = str(r_err).lower()
            if "unknown command 'eval'" in err_msg or "no such command 'eval'" in err_msg:
                log.info("Lua unsupported by Redis client; executing test transactional fallback.")
                raw_res = await _execute_atomic_turn_commit_fallback_async(
                    rc=rc,
                    history_key=history_key,
                    meta_key=meta_key,
                    turns_key=turns_key,
                    turn_id=clean_turn_id,
                    user_json=user_json,
                    asst_json=asst_json,
                    max_messages=max_messages,
                    max_session_bytes=max_session_bytes,
                    ttl_seconds=ttl_seconds,
                    timestamp=now_ts,
                    expected_previous_turn_id=expected_previous_turn_id,
                    expected_seq=expected_seq,
                )
            else:
                raise

        # Decode Lua / Fallback Response
        if isinstance(raw_res, (list, tuple)) and len(raw_res) >= 2:
            status_code = raw_res[0].decode("utf-8") if isinstance(raw_res[0], bytes) else str(raw_res[0])
            val1 = raw_res[1].decode("utf-8") if isinstance(raw_res[1], bytes) else str(raw_res[1])
            val2 = raw_res[2].decode("utf-8") if (len(raw_res) > 2 and isinstance(raw_res[2], bytes)) else (str(raw_res[2]) if len(raw_res) > 2 else "0")
        else:
            raise ConversationHistoryError(f"Unexpected Redis return payload: {raw_res!r}")

        # Handle Outcomes
        if status_code == "COMMITTED":
            return TurnCommitResult(
                status=TurnCommitStatus.COMMITTED,
                turn_id=clean_turn_id,
                turn_seq=int(val1),
                total_messages=int(val2),
                committed=True,
            )

        if status_code == "ALREADY_COMMITTED":
            if raise_on_duplicate:
                raise DuplicateTurnError(
                    f"Turn ID '{clean_turn_id}' has already been committed in session {session_id}."
                )
            return TurnCommitResult(
                status=TurnCommitStatus.ALREADY_COMMITTED,
                turn_id=clean_turn_id,
                turn_seq=int(val1),
                total_messages=int(val2),
                committed=False,
                error_message=f"Turn ID '{clean_turn_id}' already committed.",
            )

        if status_code == "CONFLICT_STALE_TURN":
            raise TurnConcurrencyConflictError(
                f"Turn conflict: expected parent turn '{exp_prev}', but current latest turn is '{val1}'."
            )

        if status_code == "CONFLICT_SEQUENCE_MISMATCH":
            raise TurnConcurrencyConflictError(
                f"Turn sequence conflict: expected sequence {exp_seq_str}, but current sequence is {val1}."
            )

        if status_code == "CONFLICT_CONCURRENT_RACE":
            raise TurnConcurrencyConflictError(
                "Turn commit conflicted with another concurrent commit after retries."
            )

        if status_code == "PAYLOAD_TOO_LARGE":
            raise PayloadTooLargeError(
                f"Turn payload ({val1} bytes) cannot fit within MAX_SESSION_BYTES limit of {val2} bytes."
            )

        if status_code == "ERR_WRONG_TYPE":
            raise ConversationHistoryError(f"Redis key type corruption: {val1}")

        if status_code == "ERR_VALIDATION":
            raise InvalidMessagePayloadError(f"Validation error: {val1}")

        raise ConversationHistoryError(f"Unknown turn commit outcome: {status_code!r}")

    except BaseRedisLibraryError as e:
        raise _classify_redis_error(e, action="append_turn") from e


async def clear_conversation_history_async(
    user_id: Union[str, int],
    session_id: str,
) -> bool:
    """
    Clears all conversation history, metadata, and turn tracking records for a validated session.
    
    Race Behavior:
    - Atomically deletes history_key, meta_key, and turns_key in a single Redis DEL command.
    - Domain session-state key (chat:session:{uid}:{sid}) is strictly preserved and never deleted.
    - If clear races with append_turn_atomic_async:
      - If clear executes first, prior history is wiped, and the in-flight append commits as Turn 1 of a clean session.
      - If append executes first, the turn is committed, and the clear deletes all history keys cleanly.
      - In neither case can orphaned, partial, or mismatched metadata/idempotency keys survive.
    
    Returns:
        True if history was deleted, False if already empty/cleared.
    """
    eff_uid = resolve_effective_user_id(user_id, session_id)
    if not is_valid_session_id(session_id):
        raise RedisSessionOwnershipError(f"Invalid session_id format: {session_id!r}")

    history_key = to_history_redis_key(eff_uid, session_id)
    meta_key = to_history_meta_redis_key(eff_uid, session_id)
    turns_key = to_history_turns_redis_key(eff_uid, session_id)

    rc = get_async_redis_client()

    try:
        deleted = await rc.delete(history_key, meta_key, turns_key)
        return bool(deleted > 0)
    except BaseRedisLibraryError as e:
        raise _classify_redis_error(e, action="clear") from e


async def get_history_metadata_async(
    user_id: Union[str, int],
    session_id: str,
) -> Dict[str, Any]:
    """
    Retrieves history metadata for a session (latest turn ID, turn sequence, total message count).
    """
    eff_uid = resolve_effective_user_id(user_id, session_id)
    if not is_valid_session_id(session_id):
        raise RedisSessionOwnershipError(f"Invalid session_id format: {session_id!r}")

    history_key = to_history_redis_key(eff_uid, session_id)
    meta_key = to_history_meta_redis_key(eff_uid, session_id)

    rc = get_async_redis_client()

    try:
        meta_raw = await rc.hgetall(meta_key)
        msg_count = await rc.llen(history_key)

        meta = {}
        for k, v in meta_raw.items():
            k_str = k.decode("utf-8") if isinstance(k, bytes) else str(k)
            v_str = v.decode("utf-8") if isinstance(v, bytes) else str(v)
            meta[k_str] = v_str

        return {
            "last_turn_id": meta.get("last_turn_id"),
            "turn_seq": int(meta.get("turn_seq", 0)),
            "updated_at": float(meta.get("updated_at", 0)) if meta.get("updated_at") else None,
            "total_messages": int(msg_count),
        }
    except BaseRedisLibraryError as e:
        raise _classify_redis_error(e, action="get_metadata") from e


async def get_conversation_history_snapshot_async(
    user_id: Union[str, int],
    session_id: str,
    limit: Optional[int] = None,
) -> ConversationHistorySnapshot:
    """
    Atomically retrieves a consistent snapshot of conversation history, turn sequence, and metadata.
    
    Guarantees:
    - Atomicity & Isolation: History records and turn sequence are read consistently in a single
      atomic operation; concurrent commits or clear operations cannot cause torn reads.
    - Correct Turn Sequence: Returned `turn_seq` reflects the absolute sequence number from metadata,
      remaining strictly accurate even after older turn pairs have been trimmed from history.
    - Turn-Pair Integrity: Validates complete turn pairs (even count, alternating roles, matching turn IDs)
      raising CorruptedHistoryError on any corruption.
    - TTL Renewal: Refreshes TTL to SESSION_TTL_SECONDS across history, meta, and turns keys.
    
    Args:
        user_id: Authenticated user identity (verified from JWT).
        session_id: Canonical session ID string.
        limit: Optional maximum number of recent messages to return (sliding window).
               Preserves complete turn pairs (rounded to even message count).
    
    Returns:
        ConversationHistorySnapshot dataclass containing validated history, turn_seq,
        last_turn_id, total_messages, and updated_at.
    """
    eff_uid = resolve_effective_user_id(user_id, session_id)
    if not is_valid_session_id(session_id):
        raise RedisSessionOwnershipError(f"Invalid session_id format: {session_id!r}")

    history_key = to_history_redis_key(eff_uid, session_id)
    meta_key = to_history_meta_redis_key(eff_uid, session_id)
    turns_key = to_history_turns_redis_key(eff_uid, session_id)

    range_start = 0
    if limit is not None and limit > 0:
        eff_limit = (limit // 2) * 2
        if eff_limit == 0:
            eff_limit = 2
        range_start = -eff_limit

    rc = get_async_redis_client()

    try:
        try:
            raw_res = await rc.eval(
                LUA_SNAPSHOT_SCRIPT,
                3,
                history_key,
                meta_key,
                turns_key,
                str(range_start),
                str(SESSION_TTL_SECONDS),
            )
            raw_entries = raw_res[0] if isinstance(raw_res, (list, tuple)) and len(raw_res) > 0 else []
            meta_raw = raw_res[1] if isinstance(raw_res, (list, tuple)) and len(raw_res) > 1 else []
            total_count = int(raw_res[2]) if isinstance(raw_res, (list, tuple)) and len(raw_res) > 2 else 0

            meta_dict: Dict[str, str] = {}
            if isinstance(meta_raw, dict):
                for k, v in meta_raw.items():
                    k_str = k.decode("utf-8") if isinstance(k, bytes) else str(k)
                    v_str = v.decode("utf-8") if isinstance(v, bytes) else str(v)
                    meta_dict[k_str] = v_str
            elif isinstance(meta_raw, (list, tuple)):
                for i in range(0, len(meta_raw), 2):
                    k_str = meta_raw[i].decode("utf-8") if isinstance(meta_raw[i], bytes) else str(meta_raw[i])
                    v_str = meta_raw[i + 1].decode("utf-8") if isinstance(meta_raw[i + 1], bytes) else str(meta_raw[i + 1])
                    meta_dict[k_str] = v_str
        except RedisResponseError as r_err:
            err_msg = str(r_err).lower()
            if "unknown command 'eval'" in err_msg or "no such command 'eval'" in err_msg:
                raw_entries, meta_dict, total_count = await _execute_snapshot_fallback_async(
                    rc=rc,
                    history_key=history_key,
                    meta_key=meta_key,
                    turns_key=turns_key,
                    range_start=range_start,
                    ttl_seconds=SESSION_TTL_SECONDS,
                )
            else:
                raise

        records = parse_and_validate_history_records(raw_entries, session_id=session_id, key_name=history_key)
        turn_seq = int(meta_dict.get("turn_seq", 0))
        last_turn_id = meta_dict.get("last_turn_id")
        updated_at = float(meta_dict["updated_at"]) if meta_dict.get("updated_at") else None

        return ConversationHistorySnapshot(
            history=records,
            turn_seq=turn_seq,
            last_turn_id=last_turn_id,
            total_messages=total_count,
            updated_at=updated_at,
        )

    except BaseRedisLibraryError as e:
        raise _classify_redis_error(e, action="get_snapshot") from e
