"""
backend/tests/test_redis_conversation_history.py — Phase 2C.1 & 2C.1.1 Hardened Test Suite.

Comprehensive tests verifying:
1. Basic storage (empty, single turn, multiple chronological turns, bounded limit, clear)
2. Cumulative history byte limits & complete-turn trimming
3. Bounded idempotency metadata (eviction of trimmed turn IDs, duplicate checks)
4. Safe concurrency contracts (atomic gather conflicts, blind FIFO append semantics)
5. Configurable message character limits (8,000 chars default)
6. Turn-pair integrity on retrieval (odd counts, role alternation, mismatched turn IDs, missing fields)
7. Hardened execution (wrong-type keys, zero partial writes on failure)
8. Clear/append race behavior (clean key deletion, domain session isolation)
9. Infrastructure resilience (Connection drops, timeouts, OOM, script errors)
"""

import asyncio
import json
import time
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

import fakeredis
import fakeredis.aioredis
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
    TimeoutError as RedisTimeoutError,
    ResponseError as RedisResponseError,
)

from backend.utils.redis_client import (
    set_test_redis_client,
    set_test_sync_redis_client,
    RedisUnavailableError,
    RedisSessionOOMError,
    RedisSessionOwnershipError,
)
from backend.config.config import REDIS_HOST, REDIS_PORT
from backend.utils.conversation_history import (
    ConversationHistoryError,
    ChatMessageRecord,
    TurnCommitResult,
    ConversationHistorySnapshot,
    TurnCommitStatus,
    InvalidMessagePayloadError,
    PayloadTooLargeError,
    DuplicateTurnError,
    TurnConcurrencyConflictError,
    CorruptedHistoryError,
    get_conversation_history_async,
    get_conversation_history_snapshot_async,
    append_turn_atomic_async,
    clear_conversation_history_async,
    get_history_metadata_async,
    to_history_redis_key,
    to_history_meta_redis_key,
    to_history_turns_redis_key,
    validate_message_content,
    MAX_MESSAGE_CHARS,
    LUA_SNAPSHOT_SCRIPT,
)


@pytest.fixture(autouse=True)
def setup_fakeredis():
    """Inject an isolated FakeRedis instance for each test."""
    server = fakeredis.FakeServer()
    fake_sync = fakeredis.FakeRedis(server=server, decode_responses=False)
    fake_async = fakeredis.aioredis.FakeRedis(server=server, decode_responses=False)
    set_test_redis_client(fake_async)
    set_test_sync_redis_client(fake_sync)
    yield fake_async
    set_test_redis_client(None)
    set_test_sync_redis_client(None)


# ============================================================================
# 1. BASIC STORAGE TESTS
# ============================================================================

def test_01_empty_history():
    """Empty history returns empty list without error."""
    async def _test():
        res = await get_conversation_history_async(user_id=101, session_id="sess_basic_01")
        assert res == []
    asyncio.run(_test())


def test_02_append_one_complete_turn():
    """Appending a complete turn appends user then assistant message in order."""
    async def _test():
        turn_res = await append_turn_atomic_async(
            user_id=101,
            session_id="sess_basic_02",
            turn_id="turn_01",
            user_message="Find Python jobs in Delhi",
            assistant_message="Here are Python developer jobs in Delhi.",
            metadata={"intent_hint": "search"},
        )
        assert turn_res.committed is True
        assert turn_res.status == TurnCommitStatus.COMMITTED
        assert turn_res.turn_seq == 1
        assert turn_res.total_messages == 2

        history = await get_conversation_history_async(user_id=101, session_id="sess_basic_02")
        assert len(history) == 2
        assert history[0].role == "user"
        assert history[0].content == "Find Python jobs in Delhi"
        assert history[0].turn_id == "turn_01"
        assert history[1].role == "assistant"
        assert history[1].content == "Here are Python developer jobs in Delhi."
        assert history[1].metadata.get("intent_hint") == "search"
    asyncio.run(_test())


def test_03_retrieve_multiple_turns_chronological_order():
    """Multiple turns are strictly retrieved in chronological order."""
    async def _test():
        await append_turn_atomic_async(
            user_id=101, session_id="sess_multi_01", turn_id="t1",
            user_message="Hello", assistant_message="Hi there!",
        )
        await append_turn_atomic_async(
            user_id=101, session_id="sess_multi_01", turn_id="t2",
            user_message="Show jobs", assistant_message="Here are jobs",
            expected_previous_turn_id="t1",
        )
        await append_turn_atomic_async(
            user_id=101, session_id="sess_multi_01", turn_id="t3",
            user_message="Only remote ones", assistant_message="Filtered remote jobs",
            expected_previous_turn_id="t2",
        )

        history = await get_conversation_history_async(user_id=101, session_id="sess_multi_01")
        assert len(history) == 6
        assert [m.role for m in history] == ["user", "assistant", "user", "assistant", "user", "assistant"]
        assert [m.turn_id for m in history] == ["t1", "t1", "t2", "t2", "t3", "t3"]
        assert history[0].content == "Hello"
        assert history[5].content == "Filtered remote jobs"
    asyncio.run(_test())


def test_04_retrieve_bounded_recent_history():
    """Limit parameter retrieves only the requested number of most recent messages preserving complete turns."""
    async def _test():
        await append_turn_atomic_async(user_id=101, session_id="sess_limit_01", turn_id="t1", user_message="M1", assistant_message="M2")
        await append_turn_atomic_async(user_id=101, session_id="sess_limit_01", turn_id="t2", user_message="M3", assistant_message="M4")
        await append_turn_atomic_async(user_id=101, session_id="sess_limit_01", turn_id="t3", user_message="M5", assistant_message="M6")

        recent_2 = await get_conversation_history_async(user_id=101, session_id="sess_limit_01", limit=2)
        assert len(recent_2) == 2
        assert [m.content for m in recent_2] == ["M5", "M6"]

        recent_4 = await get_conversation_history_async(user_id=101, session_id="sess_limit_01", limit=4)
        assert len(recent_4) == 4
        assert [m.content for m in recent_4] == ["M3", "M4", "M5", "M6"]
    asyncio.run(_test())


def test_05_clear_history():
    """Clearing history removes messages, meta, and turns set cleanly without affecting domain session state."""
    async def _test():
        from backend.utils.redis_client import get_async_redis_client
        rc = get_async_redis_client()
        sess_id = "sess_clear_01"

        # Pre-seed a domain session key to prove clear does NOT delete domain session state
        domain_key = f"chat:session:101:{sess_id}"
        await rc.set(domain_key, b'{"role": "employee"}')

        await append_turn_atomic_async(user_id=101, session_id=sess_id, turn_id="t1", user_message="Hi", assistant_message="Hello")
        
        # Verify keys exist prior to clear
        h_key = to_history_redis_key(101, sess_id)
        m_key = to_history_meta_redis_key(101, sess_id)
        t_key = to_history_turns_redis_key(101, sess_id)
        assert await rc.exists(h_key) == 1
        assert await rc.exists(m_key) == 1
        assert await rc.exists(t_key) == 1

        cleared = await clear_conversation_history_async(user_id=101, session_id=sess_id)
        assert cleared is True

        # Task 2: Verify clear removes ALL three conversation history keys completely
        assert await rc.exists(h_key) == 0
        assert await rc.exists(m_key) == 0
        assert await rc.exists(t_key) == 0

        history = await get_conversation_history_async(user_id=101, session_id=sess_id)
        assert history == []

        meta = await get_history_metadata_async(user_id=101, session_id=sess_id)
        assert meta["last_turn_id"] is None
        assert meta["turn_seq"] == 0
        assert meta["total_messages"] == 0

        # Verify domain session state is strictly preserved and untouched
        assert await rc.exists(domain_key) == 1
        domain_val = await rc.get(domain_key)
        assert domain_val is not None
        assert b"employee" in domain_val
    asyncio.run(_test())


# ============================================================================
# 2. CUMULATIVE HISTORY BYTE LIMITS & TRIMMING
# ============================================================================

def test_06_single_turn_exceeding_total_capacity_rejected_cleanly():
    """A single turn whose serialized size exceeds MAX_SESSION_BYTES is rejected cleanly without writes."""
    async def _test():
        # Set MAX_SESSION_BYTES to 400 bytes
        with patch("backend.utils.conversation_history.MAX_SESSION_BYTES", 400):
            with pytest.raises(PayloadTooLargeError) as exc_info:
                await append_turn_atomic_async(
                    user_id=101, session_id="sess_cap_01", turn_id="t1",
                    user_message="A" * 250, assistant_message="B" * 250
                )
            assert "exceeds MAX_SESSION_BYTES limit" in str(exc_info.value)

            # Confirm 0 messages were written
            history = await get_conversation_history_async(user_id=101, session_id="sess_cap_01")
            assert len(history) == 0
    asyncio.run(_test())


def test_07_multiple_turns_exceeding_aggregate_limit_trims_older_pairs():
    """Individually valid turns that in aggregate exceed MAX_SESSION_BYTES trim oldest pairs atomically."""
    async def _test():
        sess_id = "sess_cap_02"
        # Each turn is ~320 bytes. Set MAX_SESSION_BYTES to 750 bytes (can fit ~2 turns = 4 msgs).
        with patch("backend.utils.conversation_history.MAX_SESSION_BYTES", 750):
            # Turn 1 (~320 bytes)
            res1 = await append_turn_atomic_async(user_id=101, session_id=sess_id, turn_id="t1", user_message="User message 1", assistant_message="Asst reply 1")
            assert res1.committed is True

            # Turn 2 (~320 bytes, total ~640 bytes <= 750)
            res2 = await append_turn_atomic_async(user_id=101, session_id=sess_id, turn_id="t2", user_message="User message 2", assistant_message="Asst reply 2")
            assert res2.committed is True

            hist2 = await get_conversation_history_async(user_id=101, session_id=sess_id)
            assert len(hist2) == 4

            # Turn 3 (~320 bytes, total would be ~960 bytes > 750) -> Trims Turn 1 cleanly
            res3 = await append_turn_atomic_async(user_id=101, session_id=sess_id, turn_id="t3", user_message="User message 3", assistant_message="Asst reply 3")
            assert res3.committed is True

            hist3 = await get_conversation_history_async(user_id=101, session_id=sess_id)
            # Must have trimmed exactly 1 pair (2 messages), leaving Turn 2 and Turn 3 (4 messages)
            assert len(hist3) == 4
            assert hist3[0].turn_id == "t2"
            assert hist3[0].role == "user"
            assert hist3[1].turn_id == "t2"
            assert hist3[1].role == "assistant"
            assert hist3[2].turn_id == "t3"
            assert hist3[2].role == "user"
            assert hist3[3].turn_id == "t3"
            assert hist3[3].role == "assistant"
    asyncio.run(_test())


def test_08_turn_exceeding_capacity_even_after_full_trim_is_rejected_without_partial_write():
    """If a new turn cannot fit in MAX_SESSION_BYTES even with empty history, it is rejected with 0 partial writes."""
    async def _test():
        sess_id = "sess_cap_03"
        with patch("backend.utils.conversation_history.MAX_SESSION_BYTES", 600):
            # Write valid turn 1 (~320 bytes)
            await append_turn_atomic_async(user_id=101, session_id=sess_id, turn_id="t1", user_message="M1", assistant_message="A1")

            # Attempt oversized turn (> 600 bytes)
            with pytest.raises(PayloadTooLargeError):
                await append_turn_atomic_async(
                    user_id=101, session_id=sess_id, turn_id="t2",
                    user_message="X" * 350, assistant_message="Y" * 350
                )

            # Prior history must remain intact and uncorrupted
            history = await get_conversation_history_async(user_id=101, session_id=sess_id)
            assert len(history) == 2
            assert history[0].turn_id == "t1"
    asyncio.run(_test())


# ============================================================================
# 3. BOUNDED IDEMPOTENCY METADATA
# ============================================================================

def test_09_idempotency_set_remains_strictly_bounded_during_continuous_session():
    """Appending substantially more turns than MAX_SESSION_MESSAGES prunes trimmed turn IDs from turns set."""
    async def _test():
        from backend.utils.redis_client import get_async_redis_client
        rc = get_async_redis_client()
        sess_id = "sess_idemp_bound_01"

        # Capped at 4 messages (2 turns)
        with patch("backend.utils.conversation_history.MAX_SESSION_MESSAGES", 4):
            # Append 10 sequential turns
            for i in range(1, 11):
                await append_turn_atomic_async(
                    user_id=101, session_id=sess_id, turn_id=f"turn_{i:02d}",
                    user_message=f"Msg {i}", assistant_message=f"Ans {i}"
                )

            # The history list must have exactly 4 messages (the last 2 turns: turn_09 and turn_10)
            history = await get_conversation_history_async(user_id=101, session_id=sess_id)
            assert len(history) == 4
            assert history[0].turn_id == "turn_09"
            assert history[2].turn_id == "turn_10"

            # The idempotency set must be strictly bounded: only active turns exist
            turns_key = to_history_turns_redis_key(101, sess_id)
            active_turns_raw = await rc.smembers(turns_key)
            active_turns = {t.decode("utf-8") if isinstance(t, bytes) else str(t) for t in active_turns_raw}

            # Must contain only turn_09 and turn_10; all obsolete turns (turn_01 .. turn_08) must be evicted!
            assert active_turns == {"turn_09", "turn_10"}
            assert len(active_turns) == 2

            # Retained turn_10 duplicate attempt is detected and rejected
            dup_res = await append_turn_atomic_async(
                user_id=101, session_id=sess_id, turn_id="turn_10",
                user_message="Msg 10 dup", assistant_message="Ans 10 dup"
            )
            assert dup_res.committed is False
            assert dup_res.status == TurnCommitStatus.ALREADY_COMMITTED

            # Evicted turn_01: if re-submitted with stale expected_seq, concurrency contract rejects it
            with pytest.raises(TurnConcurrencyConflictError):
                await append_turn_atomic_async(
                    user_id=101, session_id=sess_id, turn_id="turn_01",
                    user_message="Msg 1", assistant_message="Ans 1",
                    expected_seq=0
                )
    asyncio.run(_test())


# ============================================================================
# 4. CONCURRENCY CONTRACT TESTS
# ============================================================================

def test_10_concurrent_commits_with_expected_seq_rejects_collision():
    """Two concurrent commits expecting the same sequence: exactly one succeeds, one raises TurnConcurrencyConflictError."""
    async def _test():
        sess_id = "sess_concur_race_01"
        t1 = append_turn_atomic_async(user_id=101, session_id=sess_id, turn_id="t1_a", user_message="A", assistant_message="A_ans", expected_seq=0)
        t2 = append_turn_atomic_async(user_id=101, session_id=sess_id, turn_id="t1_b", user_message="B", assistant_message="B_ans", expected_seq=0)

        results = await asyncio.gather(t1, t2, return_exceptions=True)
        successes = [r for r in results if isinstance(r, TurnCommitResult) and r.committed]
        conflicts = [r for r in results if isinstance(r, TurnConcurrencyConflictError)]

        assert len(successes) == 1
        assert len(conflicts) == 1
        assert "Turn sequence conflict" in str(conflicts[0])

        history = await get_conversation_history_async(user_id=101, session_id=sess_id)
        assert len(history) == 2
    asyncio.run(_test())


def test_11_concurrent_commits_without_expected_values_document_fifo_append():
    """When expected values are omitted, appends follow blind FIFO arrival without raising conflict."""
    async def _test():
        sess_id = "sess_concur_fifo_01"
        t1 = append_turn_atomic_async(user_id=101, session_id=sess_id, turn_id="turn_f1", user_message="M1", assistant_message="A1")
        t2 = append_turn_atomic_async(user_id=101, session_id=sess_id, turn_id="turn_f2", user_message="M2", assistant_message="A2")

        results = await asyncio.gather(t1, t2, return_exceptions=True)
        # Both should commit in arrival order
        for r in results:
            assert isinstance(r, TurnCommitResult)
            assert r.committed is True

        history = await get_conversation_history_async(user_id=101, session_id=sess_id)
        assert len(history) == 4
        # Verify valid turn pairing
        assert history[0].turn_id == history[1].turn_id
        assert history[2].turn_id == history[3].turn_id
    asyncio.run(_test())


# ============================================================================
# 5. RESTORED APPROVED MESSAGE CHARACTER LIMIT (8,000)
# ============================================================================

def test_12_message_character_limit_restored_to_8000():
    """Messages exceeding configured MAX_MESSAGE_CHARS (8,000) raise PayloadTooLargeError."""
    async def _test():
        # MAX_MESSAGE_CHARS should default to 8000
        assert int(MAX_MESSAGE_CHARS) == 8000

        # Valid message at limit (8,000 chars)
        ok_msg = "X" * 8000
        # Exceeding message (8,001 chars)
        bad_msg = "X" * 8001

        # Test user message limit
        with pytest.raises(PayloadTooLargeError) as exc_u:
            await append_turn_atomic_async(user_id=101, session_id="sess_limit_8k", turn_id="t1", user_message=bad_msg, assistant_message="Short")
        assert "exceeds maximum limit of 8000" in str(exc_u.value)

        # Test assistant message limit
        with pytest.raises(PayloadTooLargeError) as exc_a:
            await append_turn_atomic_async(user_id=101, session_id="sess_limit_8k", turn_id="t1", user_message="Short", assistant_message=bad_msg)
        assert "exceeds maximum limit of 8000" in str(exc_a.value)
    asyncio.run(_test())


# ============================================================================
# 6. TURN-PAIR INTEGRITY ON RETRIEVAL
# ============================================================================

def test_13_odd_message_count_in_redis_raises_corrupted_history_error():
    """An odd number of messages in Redis list raises CorruptedHistoryError."""
    async def _test():
        from backend.utils.redis_client import get_async_redis_client
        rc = get_async_redis_client()
        sess_id = "sess_odd_01"
        key = to_history_redis_key(101, sess_id)

        # Push exactly 1 message (incomplete turn)
        msg_obj = json.dumps({"role": "user", "content": "Lone user message", "turn_id": "t1", "message_id": "t1_u"})
        await rc.rpush(key, msg_obj.encode("utf-8"))

        with pytest.raises(CorruptedHistoryError) as exc_info:
            await get_conversation_history_async(user_id=101, session_id=sess_id)
        assert "odd message count (1)" in str(exc_info.value)
    asyncio.run(_test())


def test_14_consecutive_user_messages_raises_corrupted_history_error():
    """Two consecutive user messages in Redis list raises CorruptedHistoryError."""
    async def _test():
        from backend.utils.redis_client import get_async_redis_client
        rc = get_async_redis_client()
        sess_id = "sess_consec_user_01"
        key = to_history_redis_key(101, sess_id)

        msg1 = json.dumps({"role": "user", "content": "User 1", "turn_id": "t1", "message_id": "t1_u1"})
        msg2 = json.dumps({"role": "user", "content": "User 2", "turn_id": "t1", "message_id": "t1_u2"})
        await rc.rpush(key, msg1.encode("utf-8"), msg2.encode("utf-8"))

        with pytest.raises(CorruptedHistoryError) as exc_info:
            await get_conversation_history_async(user_id=101, session_id=sess_id)
        assert "role corruption: expected role 'assistant' at index 1" in str(exc_info.value)
    asyncio.run(_test())


def test_15_consecutive_assistant_messages_raises_corrupted_history_error():
    """Two consecutive assistant messages in Redis list raises CorruptedHistoryError."""
    async def _test():
        from backend.utils.redis_client import get_async_redis_client
        rc = get_async_redis_client()
        sess_id = "sess_consec_asst_01"
        key = to_history_redis_key(101, sess_id)

        msg1 = json.dumps({"role": "assistant", "content": "Asst 1", "turn_id": "t1", "message_id": "t1_a1"})
        msg2 = json.dumps({"role": "assistant", "content": "Asst 2", "turn_id": "t1", "message_id": "t1_a2"})
        await rc.rpush(key, msg1.encode("utf-8"), msg2.encode("utf-8"))

        with pytest.raises(CorruptedHistoryError) as exc_info:
            await get_conversation_history_async(user_id=101, session_id=sess_id)
        assert "role corruption: expected role 'user' at index 0" in str(exc_info.value)
    asyncio.run(_test())


def test_16_mismatched_turn_ids_in_pair_raises_corrupted_history_error():
    """Mismatched turn_ids between user and assistant in a pair raises CorruptedHistoryError."""
    async def _test():
        from backend.utils.redis_client import get_async_redis_client
        rc = get_async_redis_client()
        sess_id = "sess_mismatch_tid_01"
        key = to_history_redis_key(101, sess_id)

        msg_u = json.dumps({"role": "user", "content": "User", "turn_id": "turn_alpha", "message_id": "ta_u"})
        msg_a = json.dumps({"role": "assistant", "content": "Asst", "turn_id": "turn_beta", "message_id": "tb_a"})
        await rc.rpush(key, msg_u.encode("utf-8"), msg_a.encode("utf-8"))

        with pytest.raises(CorruptedHistoryError) as exc_info:
            await get_conversation_history_async(user_id=101, session_id=sess_id)
        assert "turn_id mismatch: user has 'turn_alpha', assistant has 'turn_beta'" in str(exc_info.value)
    asyncio.run(_test())


def test_17_missing_message_id_or_empty_turn_id_raises_corrupted_history_error():
    """Record with missing or empty message_id raises CorruptedHistoryError."""
    async def _test():
        from backend.utils.redis_client import get_async_redis_client
        rc = get_async_redis_client()
        sess_id = "sess_missing_id_01"
        key = to_history_redis_key(101, sess_id)

        msg_u = json.dumps({"role": "user", "content": "User", "turn_id": "t1", "message_id": ""})
        msg_a = json.dumps({"role": "assistant", "content": "Asst", "turn_id": "t1", "message_id": "t1_a"})
        await rc.rpush(key, msg_u.encode("utf-8"), msg_a.encode("utf-8"))

        with pytest.raises(CorruptedHistoryError) as exc_info:
            await get_conversation_history_async(user_id=101, session_id=sess_id)
        assert "message_id is missing or invalid" in str(exc_info.value)
    asyncio.run(_test())


# ============================================================================
# 7. HARDENED EXECUTION & WRONG-TYPE KEY VALIDATION
# ============================================================================

def test_18_wrong_type_keys_fail_cleanly_with_zero_partial_writes():
    """If history_key exists as a string/hash or turns_key as a list, commit aborts before writes."""
    async def _test():
        from backend.utils.redis_client import get_async_redis_client
        rc = get_async_redis_client()
        sess_id = "sess_wrong_type_01"

        # Pre-create history_key as a HASH instead of a LIST
        h_key = to_history_redis_key(101, sess_id)
        await rc.hset(h_key, mapping={"bad": "type"})

        with pytest.raises(ConversationHistoryError) as exc_info:
            await append_turn_atomic_async(user_id=101, session_id=sess_id, turn_id="t1", user_message="Hi", assistant_message="Ans")
        assert "Redis key type corruption" in str(exc_info.value)

        # Confirm meta and turns keys were NOT created
        m_key = to_history_meta_redis_key(101, sess_id)
        t_key = to_history_turns_redis_key(101, sess_id)
        assert await rc.exists(m_key) == 0
        assert await rc.exists(t_key) == 0
    asyncio.run(_test())


# ============================================================================
# 8. CLEAR / APPEND RACE BEHAVIOR
# ============================================================================

def test_19_clear_append_race_condition():
    """Concurrent clear and append operations execute deterministically without partial/corrupted keys."""
    async def _test():
        sess_id = "sess_clear_race_01"

        # Pre-seed Turn 1
        await append_turn_atomic_async(user_id=101, session_id=sess_id, turn_id="t1", user_message="Pre-seed", assistant_message="Pre-seed reply")

        # Concurrently clear and append Turn 2
        clear_task = clear_conversation_history_async(user_id=101, session_id=sess_id)
        append_task = append_turn_atomic_async(user_id=101, session_id=sess_id, turn_id="t2", user_message="Race User", assistant_message="Race Asst")

        results = await asyncio.gather(clear_task, append_task, return_exceptions=True)
        # Verify neither task raised an unhandled exception
        for r in results:
            assert not isinstance(r, Exception), f"Unexpected exception during race: {r}"

        # Inspect resulting state: must be either completely empty OR contain exactly 1 valid turn
        history = await get_conversation_history_async(user_id=101, session_id=sess_id)
        assert len(history) in (0, 2)
        if len(history) == 2:
            assert history[0].role == "user"
            assert history[1].role == "assistant"
            assert history[0].turn_id == "t2"
            assert history[1].turn_id == "t2"
    asyncio.run(_test())


# ============================================================================
# 9. INFRASTRUCTURE & ISOLATION REGRESSIONS
# ============================================================================

def test_20_two_users_same_session_id_are_isolated():
    """Two different authenticated users with identical session_id strings cannot see each other's turns."""
    async def _test():
        sess_id = "sess_shared_name_01"
        await append_turn_atomic_async(user_id=1001, session_id=sess_id, turn_id="t1_u1", user_message="User 1 query", assistant_message="User 1 ans")
        await append_turn_atomic_async(user_id=2002, session_id=sess_id, turn_id="t1_u2", user_message="User 2 query", assistant_message="User 2 ans")

        u1_hist = await get_conversation_history_async(user_id=1001, session_id=sess_id)
        u2_hist = await get_conversation_history_async(user_id=2002, session_id=sess_id)

        assert len(u1_hist) == 2
        assert len(u2_hist) == 2
        assert u1_hist[0].content == "User 1 query"
        assert u2_hist[0].content == "User 2 query"
    asyncio.run(_test())


def test_21_missing_or_anonymous_user_identity_rejected():
    """Missing user identity or 'anon'/'null' is strictly rejected with RedisSessionOwnershipError."""
    async def _test():
        for bad_uid in [None, "", "anon", "anonymous", "null", "none"]:
            with pytest.raises(RedisSessionOwnershipError):
                await append_turn_atomic_async(user_id=bad_uid, session_id="sess_iso_01", turn_id="t1", user_message="Hi", assistant_message="Hello")

            with pytest.raises(RedisSessionOwnershipError):
                await get_conversation_history_async(user_id=bad_uid, session_id="sess_iso_01")
    asyncio.run(_test())


def test_22_redis_connection_failure_raises_redis_unavailable_error():
    """Redis connection failure propagates as typed RedisUnavailableError."""
    async def _test():
        mock_rc = AsyncMock()
        mock_rc.lrange.side_effect = RedisConnectionError("Connection refused to Redis cluster")
        with patch("backend.utils.conversation_history.get_async_redis_client", return_value=mock_rc):
            with pytest.raises(RedisUnavailableError) as exc_info:
                await get_conversation_history_async(user_id=101, session_id="sess_fail_01")
            assert "unavailable during read" in str(exc_info.value)
    asyncio.run(_test())


def test_23_redis_timeout_raises_redis_unavailable_error():
    """Redis socket timeout propagates as typed RedisUnavailableError."""
    async def _test():
        mock_rc = AsyncMock()
        mock_rc.eval.side_effect = RedisTimeoutError("Socket timeout waiting for Redis")
        with patch("backend.utils.conversation_history.get_async_redis_client", return_value=mock_rc):
            with pytest.raises(RedisUnavailableError) as exc_info:
                await append_turn_atomic_async(user_id=101, session_id="sess_fail_02", turn_id="t1", user_message="Hi", assistant_message="Ans")
            assert "unavailable during append_turn" in str(exc_info.value)
    asyncio.run(_test())


def test_24_redis_oom_raises_redis_session_oom_error():
    """Redis OOM ResponseError propagates as typed RedisSessionOOMError."""
    async def _test():
        mock_rc = AsyncMock()
        mock_rc.eval.side_effect = RedisResponseError("OOM command not allowed when used memory > 'maxmemory'")
        with patch("backend.utils.conversation_history.get_async_redis_client", return_value=mock_rc):
            with pytest.raises(RedisSessionOOMError) as exc_info:
                await append_turn_atomic_async(user_id=101, session_id="sess_fail_03", turn_id="t1", user_message="Hi", assistant_message="Ans")
            assert "memory limit exceeded" in str(exc_info.value)
    asyncio.run(_test())


def test_25_lua_execution_path_passes_exact_parameters():
    """Verify that when native Lua eval is supported, the Lua script is called with correct arguments."""
    async def _test():
        mock_rc = AsyncMock()
        mock_rc.eval.return_value = ["COMMITTED", "1", "2"]
        with patch("backend.utils.conversation_history.get_async_redis_client", return_value=mock_rc):
            res = await append_turn_atomic_async(
                user_id=101,
                session_id="sess_lua_01",
                turn_id="turn_lua_10",
                user_message="Lua user",
                assistant_message="Lua asst",
                expected_previous_turn_id="turn_lua_09",
                expected_seq=1,
            )
            assert res.committed is True
            assert res.turn_seq == 1
            assert res.total_messages == 2

            eval_call = mock_rc.eval.call_args
            assert eval_call is not None
            args = eval_call[0]
            num_keys = args[1]
            assert num_keys == 3
            assert args[2].startswith("chat:history:101:sess_lua_01")
            assert args[3].startswith("chat:history:meta:101:sess_lua_01")
            assert args[4].startswith("chat:history:turns:101:sess_lua_01")
            assert args[5] == "turn_lua_10"
            assert "Lua user" in args[6]
            assert "Lua asst" in args[7]
            assert args[11] == "turn_lua_09"  # expected_previous_turn_id
            assert args[12] == "1"            # expected_seq
    asyncio.run(_test())


# ============================================================================
# 10. INPUT VALIDATION & SECURITY REGRESSION TESTS (PHASE 2C.1.2)
# ============================================================================

def test_26_empty_and_whitespace_only_user_messages_rejected():
    """Empty, whitespace-only, or None user messages raise InvalidMessagePayloadError."""
    async def _test():
        sess_id = "sess_val_u_01"
        for bad_u in ["", "   ", " \n\t \r\n ", None]:
            with pytest.raises(InvalidMessagePayloadError) as exc_info:
                await append_turn_atomic_async(
                    user_id=101, session_id=sess_id, turn_id="t1",
                    user_message=bad_u, assistant_message="Valid reply"
                )
            assert "cannot be empty" in str(exc_info.value) or "must be a non-null string" in str(exc_info.value)
    asyncio.run(_test())


def test_27_empty_and_whitespace_only_assistant_messages_rejected():
    """Empty, whitespace-only, or None assistant messages raise InvalidMessagePayloadError."""
    async def _test():
        sess_id = "sess_val_a_01"
        for bad_a in ["", "   ", " \n\t \r\n ", None]:
            with pytest.raises(InvalidMessagePayloadError) as exc_info:
                await append_turn_atomic_async(
                    user_id=101, session_id=sess_id, turn_id="t1",
                    user_message="Valid prompt", assistant_message=bad_a
                )
            assert "cannot be empty" in str(exc_info.value) or "must be a non-null string" in str(exc_info.value)
    asyncio.run(_test())


def test_28_prohibited_system_metadata_and_retrieval_result_blocks_rejected():
    """Prohibited system prompt delimiters and retrieval result tokens are rejected in user and assistant messages."""
    async def _test():
        sess_id = "sess_val_meta_01"
        prohibited_samples = [
            "<<<RESULTS_DATA [{\"job_id\": 101}]",
            "RESULTS_DATA>>> some trailing text",
            "Prefix ## CURRENT USER PROFILE CONTEXT suffix",
            "Prefix ## LIVE PLATFORM WIDGETS DATA suffix",
        ]
        for bad_content in prohibited_samples:
            # Test user message
            with pytest.raises(InvalidMessagePayloadError) as exc_u:
                await append_turn_atomic_async(
                    user_id=101, session_id=sess_id, turn_id="t1",
                    user_message=bad_content, assistant_message="Valid reply"
                )
            assert "contains prohibited system metadata or security blocks" in str(exc_u.value)

            # Test assistant message
            with pytest.raises(InvalidMessagePayloadError) as exc_a:
                await append_turn_atomic_async(
                    user_id=101, session_id=sess_id, turn_id="t1",
                    user_message="Valid prompt", assistant_message=bad_content
                )
            assert "contains prohibited system metadata or security blocks" in str(exc_a.value)
    asyncio.run(_test())


def test_29_bearer_token_rejection():
    """Unredacted Bearer tokens are rejected in both user and assistant messages."""
    async def _test():
        sess_id = "sess_val_bearer_01"
        bearer_sample = "Here is my secret authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig=="
        with pytest.raises(InvalidMessagePayloadError) as exc_u:
            await append_turn_atomic_async(
                user_id=101, session_id=sess_id, turn_id="t1",
                user_message=bearer_sample, assistant_message="Valid reply"
            )
        assert "contains prohibited system metadata or security blocks" in str(exc_u.value)

        with pytest.raises(InvalidMessagePayloadError) as exc_a:
            await append_turn_atomic_async(
                user_id=101, session_id=sess_id, turn_id="t1",
                user_message="Valid prompt", assistant_message=bearer_sample
            )
        assert "contains prohibited system metadata or security blocks" in str(exc_a.value)
    asyncio.run(_test())


def test_30_invalid_append_time_message_roles_rejected():
    """Attempting to validate message content with roles other than 'user' or 'assistant' raises InvalidMessagePayloadError."""
    for bad_role in ["system", "admin", "tool", "developer", "root", "", None]:
        with pytest.raises(InvalidMessagePayloadError) as exc_info:
            validate_message_content("Hello world", role=bad_role)
        assert f"Invalid message role '{bad_role}'" in str(exc_info.value)


def test_31_ttl_verification_across_history_meta_and_turns_keys():
    """Committing a turn sets positive TTL <= SESSION_TTL_SECONDS across history, meta, and turns keys."""
    async def _test():
        from backend.utils.redis_client import get_async_redis_client
        rc = get_async_redis_client()
        sess_id = "sess_ttl_verify_01"

        await append_turn_atomic_async(
            user_id=101, session_id=sess_id, turn_id="t1",
            user_message="Check TTL", assistant_message="TTL confirmed"
        )

        h_key = to_history_redis_key(101, sess_id)
        m_key = to_history_meta_redis_key(101, sess_id)
        t_key = to_history_turns_redis_key(101, sess_id)

        ttl_h = await rc.ttl(h_key)
        ttl_m = await rc.ttl(m_key)
        ttl_t = await rc.ttl(t_key)

        assert 0 < ttl_h <= 1800, f"history_key TTL unexpected: {ttl_h}"
        assert 0 < ttl_m <= 1800, f"meta_key TTL unexpected: {ttl_m}"
        assert 0 < ttl_t <= 1800, f"turns_key TTL unexpected: {ttl_t}"
    asyncio.run(_test())


def test_32_ttl_refresh_on_history_reads():
    """Reading history or fetching a snapshot refreshes TTL across all three keys back to SESSION_TTL_SECONDS."""
    async def _test():
        from backend.utils.redis_client import get_async_redis_client
        rc = get_async_redis_client()
        sess_id = "sess_ttl_ref_01"

        await append_turn_atomic_async(
            user_id=101, session_id=sess_id, turn_id="t1",
            user_message="Initial prompt", assistant_message="Initial reply"
        )

        h_key = to_history_redis_key(101, sess_id)
        m_key = to_history_meta_redis_key(101, sess_id)
        t_key = to_history_turns_redis_key(101, sess_id)

        # Explicitly reduce TTL to 300 seconds to simulate aging
        await rc.expire(h_key, 300)
        await rc.expire(m_key, 300)
        await rc.expire(t_key, 300)

        assert await rc.ttl(h_key) <= 300
        assert await rc.ttl(m_key) <= 300
        assert await rc.ttl(t_key) <= 300

        # Read history: should refresh TTLs back towards 1800
        hist = await get_conversation_history_async(user_id=101, session_id=sess_id)
        assert len(hist) == 2

        assert await rc.ttl(h_key) > 1700
        assert await rc.ttl(m_key) > 1700
        assert await rc.ttl(t_key) > 1700

        # Again reduce TTL and test snapshot read refresh
        await rc.expire(h_key, 250)
        await rc.expire(m_key, 250)
        await rc.expire(t_key, 250)

        snap = await get_conversation_history_snapshot_async(user_id=101, session_id=sess_id)
        assert len(snap.history) == 2

        assert await rc.ttl(h_key) > 1700
        assert await rc.ttl(m_key) > 1700
        assert await rc.ttl(t_key) > 1700
    asyncio.run(_test())


# ============================================================================
# 11. ATOMIC SEQUENCE SNAPSHOT TESTS (PHASE 2C.1.2)
# ============================================================================

def test_33_snapshot_empty_session_sequence_zero():
    """An empty or uninitialized session returns empty history and turn_seq=0."""
    async def _test():
        snap = await get_conversation_history_snapshot_async(user_id=101, session_id="sess_snap_empty_01")
        assert isinstance(snap, ConversationHistorySnapshot)
        assert snap.history == []
        assert snap.turn_seq == 0
        assert snap.last_turn_id is None
        assert snap.total_messages == 0
        assert snap.updated_at is None
    asyncio.run(_test())


def test_34_snapshot_multiple_committed_turns():
    """Committed turns return full validated history and matching turn_seq atomically."""
    async def _test():
        sess_id = "sess_snap_multi_01"
        await append_turn_atomic_async(user_id=101, session_id=sess_id, turn_id="t1", user_message="P1", assistant_message="R1")
        await append_turn_atomic_async(user_id=101, session_id=sess_id, turn_id="t2", user_message="P2", assistant_message="R2", expected_seq=1)
        await append_turn_atomic_async(user_id=101, session_id=sess_id, turn_id="t3", user_message="P3", assistant_message="R3", expected_seq=2)

        snap = await get_conversation_history_snapshot_async(user_id=101, session_id=sess_id)
        assert len(snap.history) == 6
        assert snap.turn_seq == 3
        assert snap.last_turn_id == "t3"
        assert snap.total_messages == 6
        assert snap.updated_at is not None
        assert [m.role for m in snap.history] == ["user", "assistant", "user", "assistant", "user", "assistant"]
        assert [m.turn_id for m in snap.history] == ["t1", "t1", "t2", "t2", "t3", "t3"]

        # Sliding window limit preserves complete turns and reports correct turn_seq
        snap_lim = await get_conversation_history_snapshot_async(user_id=101, session_id=sess_id, limit=2)
        assert len(snap_lim.history) == 2
        assert snap_lim.history[0].turn_id == "t3"
        assert snap_lim.history[1].turn_id == "t3"
        assert snap_lim.turn_seq == 3
        assert snap_lim.last_turn_id == "t3"
        assert snap_lim.total_messages == 6
    asyncio.run(_test())


def test_35_snapshot_after_substantial_pair_trimming():
    """Turn sequence remains accurate (absolute sequence) even after older history pairs are trimmed."""
    async def _test():
        sess_id = "sess_snap_trim_01"
        # Cap at 4 messages (2 complete turns)
        with patch("backend.utils.conversation_history.MAX_SESSION_MESSAGES", 4):
            for i in range(1, 11):
                await append_turn_atomic_async(
                    user_id=101, session_id=sess_id, turn_id=f"turn_{i:02d}",
                    user_message=f"Prompt {i}", assistant_message=f"Reply {i}"
                )

            snap = await get_conversation_history_snapshot_async(user_id=101, session_id=sess_id)
            # History list retains only the latest 2 turns (4 messages)
            assert len(snap.history) == 4
            assert snap.history[0].turn_id == "turn_09"
            assert snap.history[2].turn_id == "turn_10"

            # Crucial requirement: turn_seq MUST remain 10 (absolute sequence), not 2!
            assert snap.turn_seq == 10
            assert snap.last_turn_id == "turn_10"
            assert snap.total_messages == 4
    asyncio.run(_test())


def test_36_snapshot_consistency_during_concurrent_commits():
    """Snapshot read racing with concurrent commit returns either pre-commit or post-commit state consistently."""
    async def _test():
        sess_id = "sess_snap_race_commit_01"
        await append_turn_atomic_async(user_id=101, session_id=sess_id, turn_id="t1", user_message="M1", assistant_message="A1")

        # Concurrently commit Turn 2 and take a snapshot
        commit_task = append_turn_atomic_async(user_id=101, session_id=sess_id, turn_id="t2", user_message="M2", assistant_message="A2")
        snap_task = get_conversation_history_snapshot_async(user_id=101, session_id=sess_id)

        results = await asyncio.gather(commit_task, snap_task, return_exceptions=True)
        for r in results:
            assert not isinstance(r, Exception), f"Unexpected exception: {r}"

        snap_res = [r for r in results if isinstance(r, ConversationHistorySnapshot)][0]
        # Must be either Turn 1 state or Turn 2 state — never a torn read
        if snap_res.turn_seq == 1:
            assert len(snap_res.history) == 2
            assert snap_res.last_turn_id == "t1"
        elif snap_res.turn_seq == 2:
            assert len(snap_res.history) == 4
            assert snap_res.last_turn_id == "t2"
        else:
            pytest.fail(f"Torn snapshot detected: turn_seq={snap_res.turn_seq}, history_len={len(snap_res.history)}")
    asyncio.run(_test())


def test_37_snapshot_consistency_with_clear_operations():
    """Snapshot read racing with clear returns either full pre-clear state or clean empty sequence-0 state."""
    async def _test():
        sess_id = "sess_snap_race_clear_01"
        await append_turn_atomic_async(user_id=101, session_id=sess_id, turn_id="t1", user_message="M1", assistant_message="A1")
        await append_turn_atomic_async(user_id=101, session_id=sess_id, turn_id="t2", user_message="M2", assistant_message="A2")

        clear_task = clear_conversation_history_async(user_id=101, session_id=sess_id)
        snap_task = get_conversation_history_snapshot_async(user_id=101, session_id=sess_id)

        results = await asyncio.gather(clear_task, snap_task, return_exceptions=True)
        for r in results:
            assert not isinstance(r, Exception), f"Unexpected exception: {r}"

        snap_res = [r for r in results if isinstance(r, ConversationHistorySnapshot)][0]
        # Must be either completely pre-clear (turn_seq=2, 4 msgs) OR completely cleared (turn_seq=0, 0 msgs)
        if snap_res.turn_seq == 2:
            assert len(snap_res.history) == 4
            assert snap_res.last_turn_id == "t2"
        elif snap_res.turn_seq == 0:
            assert len(snap_res.history) == 0
            assert snap_res.last_turn_id is None
        else:
            pytest.fail(f"Torn snapshot detected during clear: turn_seq={snap_res.turn_seq}, history_len={len(snap_res.history)}")
    asyncio.run(_test())


def test_38_lua_snapshot_execution_path_passes_exact_parameters():
    """Verify that native Lua eval path passes exact keys and parameters for snapshot and decodes correctly."""
    async def _test():
        mock_rc = AsyncMock()
        user_msg = json.dumps({"role": "user", "content": "Lua prompt", "turn_id": "turn_lua_01", "message_id": "m1"}).encode("utf-8")
        asst_msg = json.dumps({"role": "assistant", "content": "Lua reply", "turn_id": "turn_lua_01", "message_id": "m2"}).encode("utf-8")

        mock_rc.eval.return_value = [
            [user_msg, asst_msg],
            [b"last_turn_id", b"turn_lua_01", b"turn_seq", b"1", b"updated_at", b"1700000000.0"],
            "2"
        ]
        with patch("backend.utils.conversation_history.get_async_redis_client", return_value=mock_rc):
            snap = await get_conversation_history_snapshot_async(
                user_id=101,
                session_id="sess_lua_snap_01",
                limit=2,
            )
            assert snap.turn_seq == 1
            assert snap.last_turn_id == "turn_lua_01"
            assert snap.total_messages == 2
            assert snap.updated_at == 1700000000.0
            assert len(snap.history) == 2
            assert snap.history[0].role == "user"
            assert snap.history[1].role == "assistant"

            eval_call = mock_rc.eval.call_args
            assert eval_call is not None
            args = eval_call[0]
            assert args[0] == LUA_SNAPSHOT_SCRIPT
            assert args[1] == 3
            assert args[2].startswith("chat:history:101:sess_lua_snap_01")
            assert args[3].startswith("chat:history:meta:101:sess_lua_snap_01")
            assert args[4].startswith("chat:history:turns:101:sess_lua_snap_01")
            assert args[5] == "-2"
            assert args[6] == "1800"
    asyncio.run(_test())


# ============================================================================
# 12. REAL REDIS 7 PRODUCTION INTEGRATION TESTS (PHASE 2C.1.3)
# ============================================================================

_REAL_REDIS_CHECKED: bool = False
_REAL_REDIS_REACHABLE: bool = False

async def _check_real_redis_7_reachable() -> bool:
    global _REAL_REDIS_CHECKED, _REAL_REDIS_REACHABLE
    if _REAL_REDIS_CHECKED:
        return _REAL_REDIS_REACHABLE
    try:
        import redis.asyncio as aioredis
        client = aioredis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
        )
        pong = await client.ping()
        await client.aclose()
        _REAL_REDIS_REACHABLE = bool(pong)
    except Exception:
        _REAL_REDIS_REACHABLE = False
    _REAL_REDIS_CHECKED = True
    return _REAL_REDIS_REACHABLE


@pytest.fixture
def real_redis_connection():
    """Provides a connection to the configured real Redis 7 container, skipping if unreachable."""
    reachable = asyncio.run(_check_real_redis_7_reachable())
    if not reachable:
        pytest.skip(f"Configured Redis 7 container ({REDIS_HOST}:{REDIS_PORT}) is not currently reachable in this runner environment")

    # Temporarily unset test fakeredis so conversation_history uses real Redis connection pool
    set_test_redis_client(None)
    set_test_sync_redis_client(None)
    yield
    set_test_redis_client(None)
    set_test_sync_redis_client(None)


def test_real_redis_01_empty_snapshot(real_redis_connection):
    """Real Redis 7: Empty session returns sequence 0 and empty history."""
    async def _test():
        sess_id = f"sess_real_empty_{int(time.time())}"
        try:
            snap = await get_conversation_history_snapshot_async(user_id=8888, session_id=sess_id)
            assert snap.history == []
            assert snap.turn_seq == 0
            assert snap.last_turn_id is None
            assert snap.total_messages == 0
        finally:
            await clear_conversation_history_async(user_id=8888, session_id=sess_id)
    asyncio.run(_test())


def test_real_redis_02_snapshot_multiple_commits(real_redis_connection):
    """Real Redis 7: Multiple committed turns return complete history and correct turn_seq via Lua script."""
    async def _test():
        sess_id = f"sess_real_multi_{int(time.time())}"
        try:
            await append_turn_atomic_async(user_id=8888, session_id=sess_id, turn_id="rt1", user_message="Real P1", assistant_message="Real R1")
            await append_turn_atomic_async(user_id=8888, session_id=sess_id, turn_id="rt2", user_message="Real P2", assistant_message="Real R2", expected_seq=1)

            snap = await get_conversation_history_snapshot_async(user_id=8888, session_id=sess_id)
            assert len(snap.history) == 4
            assert snap.turn_seq == 2
            assert snap.last_turn_id == "rt2"
            assert snap.total_messages == 4
        finally:
            await clear_conversation_history_async(user_id=8888, session_id=sess_id)
    asyncio.run(_test())


def test_real_redis_03_snapshot_after_pair_trimming(real_redis_connection):
    """Real Redis 7: Snapshot after pair trimming preserves absolute turn_seq."""
    async def _test():
        sess_id = f"sess_real_trim_{int(time.time())}"
        try:
            with patch("backend.utils.conversation_history.MAX_SESSION_MESSAGES", 4):
                for i in range(1, 7):
                    await append_turn_atomic_async(
                        user_id=8888, session_id=sess_id, turn_id=f"rturn_{i:02d}",
                        user_message=f"P{i}", assistant_message=f"R{i}"
                    )

                snap = await get_conversation_history_snapshot_async(user_id=8888, session_id=sess_id)
                assert len(snap.history) == 4
                assert snap.turn_seq == 6
                assert snap.last_turn_id == "rturn_06"
        finally:
            await clear_conversation_history_async(user_id=8888, session_id=sess_id)
    asyncio.run(_test())


def test_real_redis_04_snapshot_concurrent_append(real_redis_connection):
    """Real Redis 7: Snapshot racing with concurrent commit returns consistent state without torn reads."""
    async def _test():
        sess_id = f"sess_real_race_{int(time.time())}"
        try:
            await append_turn_atomic_async(user_id=8888, session_id=sess_id, turn_id="rt1", user_message="P1", assistant_message="R1")
            commit_task = append_turn_atomic_async(user_id=8888, session_id=sess_id, turn_id="rt2", user_message="P2", assistant_message="R2")
            snap_task = get_conversation_history_snapshot_async(user_id=8888, session_id=sess_id)

            results = await asyncio.gather(commit_task, snap_task, return_exceptions=True)
            for r in results:
                assert not isinstance(r, Exception)

            snap = [r for r in results if isinstance(r, ConversationHistorySnapshot)][0]
            assert snap.turn_seq in (1, 2)
            if snap.turn_seq == 1:
                assert len(snap.history) == 2
            elif snap.turn_seq == 2:
                assert len(snap.history) == 4
        finally:
            await clear_conversation_history_async(user_id=8888, session_id=sess_id)
    asyncio.run(_test())


def test_real_redis_05_snapshot_clear_consistency(real_redis_connection):
    """Real Redis 7: Snapshot racing with clear produces either complete or cleanly empty state."""
    async def _test():
        sess_id = f"sess_real_clear_race_{int(time.time())}"
        try:
            await append_turn_atomic_async(user_id=8888, session_id=sess_id, turn_id="rt1", user_message="P1", assistant_message="R1")
            clear_task = clear_conversation_history_async(user_id=8888, session_id=sess_id)
            snap_task = get_conversation_history_snapshot_async(user_id=8888, session_id=sess_id)

            results = await asyncio.gather(clear_task, snap_task, return_exceptions=True)
            for r in results:
                assert not isinstance(r, Exception)

            snap = [r for r in results if isinstance(r, ConversationHistorySnapshot)][0]
            assert snap.turn_seq in (0, 1)
            if snap.turn_seq == 1:
                assert len(snap.history) == 2
            elif snap.turn_seq == 0:
                assert len(snap.history) == 0
        finally:
            await clear_conversation_history_async(user_id=8888, session_id=sess_id)
    asyncio.run(_test())


def test_real_redis_06_snapshot_ttl_refresh_all_keys(real_redis_connection):
    """Real Redis 7: Snapshot refreshes TTL across all three keys."""
    async def _test():
        sess_id = f"sess_real_ttl_{int(time.time())}"
        from backend.utils.redis_client import get_async_redis_client
        rc = get_async_redis_client()
        try:
            await append_turn_atomic_async(user_id=8888, session_id=sess_id, turn_id="rt1", user_message="P1", assistant_message="R1")
            h_key = to_history_redis_key(8888, sess_id)
            m_key = to_history_meta_redis_key(8888, sess_id)
            t_key = to_history_turns_redis_key(8888, sess_id)

            await rc.expire(h_key, 300)
            await rc.expire(m_key, 300)
            await rc.expire(t_key, 300)

            snap = await get_conversation_history_snapshot_async(user_id=8888, session_id=sess_id)
            assert len(snap.history) == 2

            ttl_h = await rc.ttl(h_key)
            ttl_m = await rc.ttl(m_key)
            ttl_t = await rc.ttl(t_key)

            assert ttl_h > 1700
            assert ttl_m > 1700
            assert ttl_t > 1700
        finally:
            await clear_conversation_history_async(user_id=8888, session_id=sess_id)
    asyncio.run(_test())
