import socket
import pytest
import pymysql
import pymysql.constants.CR
import pymysql.constants.ER
from unittest.mock import MagicMock, patch, call
from backend.repositories.db import (
    MySQLConnectionWrapper,
    get_db,
    is_transient_connect_error,
    compute_backoff_delay,
    TRANSIENT_CONNECT_ERROR_CODES,
)
from backend.config.config import (
    validate_positive_timeout,
    DB_CONNECT_TIMEOUT_SECONDS,
    DB_READ_TIMEOUT_SECONDS,
    DB_WRITE_TIMEOUT_SECONDS,
    DB_CONNECT_MAX_RETRIES,
    DB_RETRY_BASE_DELAY_SECONDS,
    DB_RETRY_MAX_DELAY_SECONDS,
)


def test_normal_successful_commit_and_close():
    """Verify that a successful context block commits and closes the connection."""
    mock_conn = MagicMock()
    call_order = []
    mock_conn.commit.side_effect = lambda: call_order.append("commit")
    mock_conn.close.side_effect = lambda: call_order.append("close")
    mock_conn.rollback.side_effect = lambda: call_order.append("rollback")

    wrapper = MySQLConnectionWrapper(mock_conn)
    with wrapper as conn:
        assert conn is wrapper

    assert call_order == ["commit", "close"]
    mock_conn.commit.assert_called_once()
    mock_conn.close.assert_called_once()
    mock_conn.rollback.assert_not_called()


def test_normal_rollback_after_body_exception():
    """Verify normal rollback after body exception, with original exception propagating."""
    mock_conn = MagicMock()
    call_order = []
    mock_conn.commit.side_effect = lambda: call_order.append("commit")
    mock_conn.close.side_effect = lambda: call_order.append("close")
    mock_conn.rollback.side_effect = lambda: call_order.append("rollback")

    wrapper = MySQLConnectionWrapper(mock_conn)
    with pytest.raises(ValueError, match="test body error"):
        with wrapper:
            raise ValueError("test body error")

    assert call_order == ["rollback", "close"]
    mock_conn.rollback.assert_called_once()
    mock_conn.close.assert_called_once()
    mock_conn.commit.assert_not_called()


def test_body_exception_and_rollback_failure():
    """
    Verify that when context body raises and rollback also fails:
    - Primary exception is the body exception.
    - Rollback failure is attached as a note (or secondary info).
    - Close is still attempted.
    """
    mock_conn = MagicMock()
    call_order = []
    def rollback_err():
        call_order.append("rollback")
        raise RuntimeError("Rollback network failure")

    mock_conn.rollback.side_effect = rollback_err
    mock_conn.close.side_effect = lambda: call_order.append("close")

    wrapper = MySQLConnectionWrapper(mock_conn)
    with pytest.raises(ValueError, match="Original body error") as exc_info:
        with wrapper:
            raise ValueError("Original body error")

    assert call_order == ["rollback", "close"]
    mock_conn.rollback.assert_called_once()
    mock_conn.close.assert_called_once()
    notes = getattr(exc_info.value, "__notes__", [])
    assert any("Secondary failure during rollback" in note for note in notes)


def test_body_exception_and_rollback_failure_and_close_failure():
    """
    Verify that when body raises, rollback fails, and close fails:
    - Primary exception is the body exception.
    - Both rollback and close failures are recorded (e.g. attached as notes).
    - Close was attempted.
    """
    mock_conn = MagicMock()
    call_order = []
    def rollback_err():
        call_order.append("rollback")
        raise RuntimeError("Rollback failed")

    def close_err():
        call_order.append("close")
        raise OSError("Close socket failed")

    mock_conn.rollback.side_effect = rollback_err
    mock_conn.close.side_effect = close_err

    wrapper = MySQLConnectionWrapper(mock_conn)
    with pytest.raises(ValueError, match="Body failure") as exc_info:
        with wrapper:
            raise ValueError("Body failure")

    assert call_order == ["rollback", "close"]
    mock_conn.rollback.assert_called_once()
    mock_conn.close.assert_called_once()
    notes = getattr(exc_info.value, "__notes__", [])
    assert any("Secondary failure during rollback" in note for note in notes)
    assert any("Secondary failure during close" in note for note in notes)


def test_commit_failure_and_close_failure():
    """
    Verify that when body succeeds but commit fails and close also fails:
    - Primary exception is the commit exception.
    - Close failure is recorded as secondary note.
    - Close was attempted.
    """
    mock_conn = MagicMock()
    call_order = []
    def commit_err():
        call_order.append("commit")
        raise RuntimeError("Commit lock timeout")

    def close_err():
        call_order.append("close")
        raise OSError("Close socket failed")

    mock_conn.commit.side_effect = commit_err
    mock_conn.close.side_effect = close_err

    wrapper = MySQLConnectionWrapper(mock_conn)
    with pytest.raises(RuntimeError, match="Commit lock timeout") as exc_info:
        with wrapper:
            pass

    assert call_order == ["commit", "close"]
    mock_conn.commit.assert_called_once()
    mock_conn.close.assert_called_once()
    notes = getattr(exc_info.value, "__notes__", [])
    assert any("Secondary failure during close" in note for note in notes)


def test_successful_commit_and_close_failure():
    """
    Verify that when transaction succeeds (commit succeeds) but close fails:
    - Close failure is propagated rather than silently reporting success.
    - Close was attempted.
    """
    mock_conn = MagicMock()
    call_order = []
    mock_conn.commit.side_effect = lambda: call_order.append("commit")
    def close_err():
        call_order.append("close")
        raise OSError("Close socket error")
    mock_conn.close.side_effect = close_err

    wrapper = MySQLConnectionWrapper(mock_conn)
    with pytest.raises(OSError, match="Close socket error"):
        with wrapper:
            pass

    assert call_order == ["commit", "close"]
    mock_conn.commit.assert_called_once()
    mock_conn.close.assert_called_once()


def test_explicit_close_compatibility():
    """Verify existing explicit conn.close() callers remain compatible."""
    mock_conn = MagicMock()
    wrapper = MySQLConnectionWrapper(mock_conn)
    wrapper.close()
    mock_conn.close.assert_called_once()


def test_context_manager_manual_exit_exc_type_without_exc_val():
    """
    Verify behavior when __exit__ is called directly with exc_type but exc_val is None:
    - Rolls back the transaction because exc_type is truthy.
    - Closes the connection.
    - If rollback and close succeed, does not raise secondary errors.
    """
    mock_conn = MagicMock()
    call_order = []
    mock_conn.rollback.side_effect = lambda: call_order.append("rollback")
    mock_conn.close.side_effect = lambda: call_order.append("close")

    wrapper = MySQLConnectionWrapper(mock_conn)
    # Direct manual call to __exit__ with exc_type but exc_val=None
    wrapper.__exit__(ValueError, None, None)

    assert call_order == ["rollback", "close"]
    mock_conn.rollback.assert_called_once()
    mock_conn.close.assert_called_once()
    mock_conn.commit.assert_not_called()


def test_context_manager_manual_exit_exc_type_without_exc_val_and_rollback_failure():
    """
    Verify that if __exit__ is called with exc_type but exc_val is None,
    and rollback fails, the rollback failure is propagated as primary_exc.
    """
    mock_conn = MagicMock()
    call_order = []
    def rollback_err():
        call_order.append("rollback")
        raise RuntimeError("Rollback failed")

    mock_conn.rollback.side_effect = rollback_err
    mock_conn.close.side_effect = lambda: call_order.append("close")

    wrapper = MySQLConnectionWrapper(mock_conn)
    with pytest.raises(RuntimeError, match="Rollback failed"):
        wrapper.__exit__(ValueError, None, None)

    assert call_order == ["rollback", "close"]
    mock_conn.rollback.assert_called_once()
    mock_conn.close.assert_called_once()


def test_get_db_passes_socket_timeouts_as_floats():
    """Verify that get_db() forwards configured socket timeouts to pymysql.connect() without truncation."""
    with patch("pymysql.connect") as mock_connect:
        mock_raw_conn = MagicMock()
        mock_connect.return_value = mock_raw_conn

        wrapper = get_db()
        assert isinstance(wrapper, MySQLConnectionWrapper)
        assert wrapper._conn is mock_raw_conn

        mock_connect.assert_called_once()
        kwargs = mock_connect.call_args.kwargs
        assert isinstance(kwargs["connect_timeout"], float)
        assert isinstance(kwargs["read_timeout"], float)
        assert isinstance(kwargs["write_timeout"], float)
        assert kwargs["connect_timeout"] == float(DB_CONNECT_TIMEOUT_SECONDS)
        assert kwargs["read_timeout"] == float(DB_READ_TIMEOUT_SECONDS)
        assert kwargs["write_timeout"] == float(DB_WRITE_TIMEOUT_SECONDS)
        assert kwargs["autocommit"] is True
        assert kwargs["charset"] == "utf8mb4"


def test_validate_positive_timeout_fractional_and_edge_cases():
    """
    Verify positive timeout parser accepts valid positive numbers including fractions,
    values below 1 second, and rejects 0, negatives, non-finite, and malformed strings.
    """
    # Valid positive integers and floats
    assert validate_positive_timeout("5.0", default=5.0) == 5.0
    assert validate_positive_timeout(10, default=10.0) == 10.0
    assert validate_positive_timeout("0.5", default=5.0) == 0.5
    assert validate_positive_timeout(0.25, default=5.0) == 0.25
    assert validate_positive_timeout("0.001", default=5.0) == 0.001
    assert validate_positive_timeout(None, default=5.0) == 5.0

    # Non-positive numbers (must fall back)
    assert validate_positive_timeout("0", default=5.0) == 5.0
    assert validate_positive_timeout("0.0", default=5.0) == 5.0
    assert validate_positive_timeout(0, default=5.0) == 5.0
    assert validate_positive_timeout("-1", default=5.0) == 5.0
    assert validate_positive_timeout("-0.5", default=5.0) == 5.0
    assert validate_positive_timeout(-10.5, default=10.0) == 10.0

    # Malformed / non-numeric / non-finite (must fall back)
    assert validate_positive_timeout("not-a-number", default=5.0) == 5.0
    assert validate_positive_timeout("nan", default=5.0) == 5.0
    assert validate_positive_timeout("inf", default=5.0) == 5.0
    assert validate_positive_timeout("-inf", default=5.0) == 5.0
    assert validate_positive_timeout([], default=5.0) == 5.0
    assert validate_positive_timeout({}, default=5.0) == 5.0


# ==============================================================================
# BOUNDED CONNECTION-ESTABLISHMENT RETRY TESTS (PHASE 0.3)
# ==============================================================================

def test_get_db_transient_connection_retry_recovery():
    """
    1. Transient connection-establishment error followed by a successful connection:
    - Fails on attempt 1 with CR_CONN_HOST_ERROR (2003).
    - Succeeds on attempt 2 returning valid MySQLConnectionWrapper.
    - Exactly 1 sleep occurs with delay > 0.
    """
    mock_raw_conn = MagicMock()
    transient_err = pymysql.err.OperationalError(2003, "Can't connect to MySQL server on 'localhost'")

    with patch("pymysql.connect", side_effect=[transient_err, mock_raw_conn]) as mock_connect, \
         patch("time.sleep") as mock_sleep:

        wrapper = get_db()
        assert isinstance(wrapper, MySQLConnectionWrapper)
        assert wrapper._conn is mock_raw_conn

        assert mock_connect.call_count == 2
        mock_sleep.assert_called_once()
        delay_arg = mock_sleep.call_args[0][0]
        assert 0.0 <= delay_arg <= float(DB_RETRY_MAX_DELAY_SECONDS)


def test_get_db_socket_timeout_retry_recovery():
    """
    Transient socket.timeout on attempt 1 recovers on attempt 2.
    """
    mock_raw_conn = MagicMock()
    with patch("pymysql.connect", side_effect=[socket.timeout("timed out"), mock_raw_conn]) as mock_connect, \
         patch("time.sleep") as mock_sleep:

        wrapper = get_db()
        assert isinstance(wrapper, MySQLConnectionWrapper)
        assert mock_connect.call_count == 2
        mock_sleep.assert_called_once()


def test_get_db_exhaustion_propagates_final_exception():
    """
    2. Exhaustion of all 3 attempts and propagation of the final exception:
    - Fails on attempt 1, 2, and 3 with CR_SERVER_GONE_ERROR (2006).
    - Final OperationalError propagates untouched with original code and message.
    - Exactly 2 backoff sleeps occur (between attempt 1->2 and 2->3).
    """
    transient_err = pymysql.err.OperationalError(2006, "MySQL server has gone away")

    with patch("pymysql.connect", side_effect=transient_err) as mock_connect, \
         patch("time.sleep") as mock_sleep:

        with pytest.raises(pymysql.err.OperationalError) as exc_info:
            get_db()

        assert exc_info.value.args[0] == 2006
        assert "MySQL server has gone away" in exc_info.value.args[1]
        assert mock_connect.call_count == int(DB_CONNECT_MAX_RETRIES)
        assert mock_sleep.call_count == int(DB_CONNECT_MAX_RETRIES) - 1


def test_get_db_permanent_error_fails_immediately():
    """
    3. Permanent error failing immediately without retries:
    - ER_ACCESS_DENIED_ERROR (1045) fails immediately on attempt 1.
    - Zero sleep calls; mock_connect called exactly once.
    """
    perm_err = pymysql.err.OperationalError(1045, "Access denied for user 'root'@'localhost'")

    with patch("pymysql.connect", side_effect=perm_err) as mock_connect, \
         patch("time.sleep") as mock_sleep:

        with pytest.raises(pymysql.err.OperationalError) as exc_info:
            get_db()

        assert exc_info.value.args[0] == 1045
        assert mock_connect.call_count == 1
        mock_sleep.assert_not_called()


def test_get_db_programming_error_fails_immediately():
    """
    Non-operational errors like ProgrammingError (syntax, unknown table) fail immediately without retries.
    """
    prog_err = pymysql.err.ProgrammingError(1146, "Table 'collarcheck.missing' doesn't exist")

    with patch("pymysql.connect", side_effect=prog_err) as mock_connect, \
         patch("time.sleep") as mock_sleep:

        with pytest.raises(pymysql.err.ProgrammingError):
            get_db()

        assert mock_connect.call_count == 1
        mock_sleep.assert_not_called()


def test_backoff_delays_within_configured_bounds():
    """
    4. Backoff delays remaining within the configured bounds:
    - Verifies compute_backoff_delay stays within [0, ceiling].
    - Verifies exponential expansion for attempts 1, 2, 3 bounded by max_delay.
    """
    base = 0.1
    cap = 1.0

    # For attempt 1: ceiling = min(1.0, 0.1 * 2^0) = 0.1
    delays_1 = [compute_backoff_delay(1, base_delay=base, max_delay=cap) for _ in range(50)]
    assert all(0.0 <= d <= 0.100001 for d in delays_1)

    # For attempt 2: ceiling = min(1.0, 0.1 * 2^1) = 0.2
    delays_2 = [compute_backoff_delay(2, base_delay=base, max_delay=cap) for _ in range(50)]
    assert all(0.0 <= d <= 0.200001 for d in delays_2)

    # For attempt 5: ceiling = min(1.0, 0.1 * 2^4) = min(1.0, 1.6) = 1.0
    delays_5 = [compute_backoff_delay(5, base_delay=base, max_delay=cap) for _ in range(50)]
    assert all(0.0 <= d <= 1.000001 for d in delays_5)


def test_no_retry_around_cursor_query_execution():
    """
    7. No retry around query execution:
    - Cursor execute() failures are NOT intercepted or retried.
    - Guarantees execution safety and non-idempotency protection.
    """
    mock_raw_cursor = MagicMock()
    mock_raw_cursor.execute.side_effect = pymysql.err.OperationalError(2013, "Lost connection during query")

    mock_raw_conn = MagicMock()
    mock_raw_conn.cursor.return_value = mock_raw_cursor

    with patch("pymysql.connect", return_value=mock_raw_conn) as mock_connect:
        wrapper = get_db()
        cursor = wrapper.cursor()

        with pytest.raises(pymysql.err.OperationalError) as exc_info:
            cursor.execute("UPDATE cyb_user SET status = 1 WHERE id = 123")

        assert exc_info.value.args[0] == 2013
        # Query execute was called exactly once — zero retries
        mock_raw_cursor.execute.assert_called_once()
        mock_connect.assert_called_once()


def test_no_retry_around_commit():
    """
    7. No retry around commit():
    - Commit failures inside context manager raise without repeating commit.
    - Connection close is still attempted.
    """
    mock_raw_conn = MagicMock()
    mock_raw_conn.commit.side_effect = pymysql.err.OperationalError(2013, "Lost connection during commit")

    with patch("pymysql.connect", return_value=mock_raw_conn):
        wrapper = get_db()
        with pytest.raises(pymysql.err.OperationalError):
            with wrapper:
                pass

        # Commit was called once — zero retries
        mock_raw_conn.commit.assert_called_once()
        mock_raw_conn.close.assert_called_once()


# ==============================================================================
# BOUNDED CONNECTION POOL TESTS (PHASE 3 STAGE B.2)
# ==============================================================================

import threading
import time
from backend.repositories.db import (
    PyMySQLConnectionPool,
    DatabaseUnavailableError,
    init_pool,
    close_pool,
    get_pool,
)


@pytest.fixture(autouse=True)
def clean_pool_fixture():
    """Ensure global pool is closed before and after each test."""
    close_pool()
    yield
    close_pool()


def test_pool_connection_reuse_across_sequential_get_db_scopes():
    """Verify healthy connection reuse across sequential get_db() scopes."""
    mock_raw_conn = MagicMock()
    mock_raw_conn.ping.return_value = None

    with patch("pymysql.connect", return_value=mock_raw_conn) as mock_connect:
        # First checkout
        with get_db() as conn1:
            assert conn1._conn is mock_raw_conn
            assert conn1._pool is not None

        # Second checkout (should reuse the same raw connection from pool)
        with get_db() as conn2:
            assert conn2._conn is mock_raw_conn

        # Only 1 physical connection created across two get_db scopes
        assert mock_connect.call_count == 1
        # Ping checked before second checkout
        mock_raw_conn.ping.assert_called_once_with(reconnect=False)
        # Rollback called to clean state between checkouts
        assert mock_raw_conn.rollback.call_count >= 1


def test_pool_normal_commit_and_release():
    """Verify normal commit inside context block and connection returned to idle pool."""
    mock_raw_conn = MagicMock()
    pool = PyMySQLConnectionPool(max_size=5)

    with patch("pymysql.connect", return_value=mock_raw_conn):
        wrapper = pool.acquire()
        with wrapper:
            pass

        mock_raw_conn.commit.assert_called_once()
        # Not physically closed because it was returned to pool
        mock_raw_conn.close.assert_not_called()
        stats = pool.get_stats()
        assert stats["live_count"] == 1
        assert stats["idle_count"] == 1
        assert stats["checked_out_count"] == 0


def test_pool_rollback_on_exception_and_safe_reuse():
    """Verify rollback on exception and subsequent safe reuse with clean state."""
    mock_raw_conn = MagicMock()
    pool = PyMySQLConnectionPool(max_size=5)

    with patch("pymysql.connect", return_value=mock_raw_conn):
        # Scope 1 raises an error
        with pytest.raises(RuntimeError, match="DB query failed"):
            with pool.acquire():
                raise RuntimeError("DB query failed")

        mock_raw_conn.rollback.assert_called()
        assert pool.get_stats()["idle_count"] == 1

        # Scope 2 reuses connection cleanly
        with pool.acquire() as conn:
            assert conn._conn is mock_raw_conn

        assert pool.get_stats()["live_count"] == 1


def test_pool_broken_connection_detection_and_disposal():
    """Verify ping failure discards broken connection and replaces it within pool limit."""
    dead_conn = MagicMock()
    dead_conn.ping.side_effect = pymysql.err.OperationalError(2006, "MySQL server has gone away")

    fresh_conn = MagicMock()
    fresh_conn.ping.return_value = None

    pool = PyMySQLConnectionPool(max_size=2)

    with patch("pymysql.connect", side_effect=[dead_conn, fresh_conn]) as mock_connect:
        # Step 1: acquire and release dead_conn into idle pool
        wrapper = pool.acquire()
        wrapper.close()
        assert pool.get_stats()["idle_count"] == 1

        # Step 2: next acquire discovers dead_conn during ping validation, discards it, and creates fresh_conn
        wrapper2 = pool.acquire()
        assert wrapper2._conn is fresh_conn

        # dead_conn physically closed
        dead_conn.close.assert_called()
        assert mock_connect.call_count == 2
        stats = pool.get_stats()
        assert stats["live_count"] == 1
        assert stats["checked_out_count"] == 1


def test_pool_exhaustion_with_short_timeout():
    """Verify that pool exhaustion raises DatabaseUnavailableError after bounded timeout."""
    pool = PyMySQLConnectionPool(max_size=1, checkout_timeout=0.05)
    mock_raw = MagicMock()

    with patch("pymysql.connect", return_value=mock_raw):
        conn1 = pool.acquire()
        assert pool.get_stats()["checked_out_count"] == 1

        with pytest.raises(DatabaseUnavailableError) as exc_info:
            pool.acquire()

        assert exc_info.value.args[0] == 1040
        assert "exhausted" in str(exc_info.value)
        conn1.close()


def test_pool_idempotent_close():
    """Verify calling close multiple times does not double-release or corrupt accounting."""
    pool = PyMySQLConnectionPool(max_size=2)
    mock_raw = MagicMock()

    with patch("pymysql.connect", return_value=mock_raw):
        wrapper = pool.acquire()
        assert pool.get_stats()["checked_out_count"] == 1

        wrapper.close()
        assert pool.get_stats()["checked_out_count"] == 0
        assert pool.get_stats()["idle_count"] == 1

        # Second close should be no-op
        wrapper.close()
        assert pool.get_stats()["checked_out_count"] == 0
        assert pool.get_stats()["idle_count"] == 1


def test_pool_concurrent_checkout_release_bounded():
    """Verify concurrent checkout/release never exceeds max_size or corrupts accounting."""
    pool = PyMySQLConnectionPool(max_size=4, checkout_timeout=2.0)
    conns_created = 0
    create_lock = threading.Lock()

    def make_conn(**kwargs):
        nonlocal conns_created
        with create_lock:
            conns_created += 1
            c = MagicMock()
            c.ping.return_value = None
            return c

    threads = []
    errors = []
    active_concurrent = 0
    max_observed_concurrent = 0
    obs_lock = threading.Lock()

    def worker():
        nonlocal active_concurrent, max_observed_concurrent
        try:
            for _ in range(10):
                with pool.acquire() as conn:
                    with obs_lock:
                        active_concurrent += 1
                        if active_concurrent > max_observed_concurrent:
                            max_observed_concurrent = active_concurrent
                    time.sleep(0.005)
                    with obs_lock:
                        active_concurrent -= 1
        except Exception as e:
            errors.append(e)

    with patch("pymysql.connect", side_effect=make_conn):
        for _ in range(8):
            t = threading.Thread(target=worker)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

    assert not errors, f"Concurrent workers experienced errors: {errors}"
    assert max_observed_concurrent <= 4
    stats = pool.get_stats()
    assert stats["live_count"] <= 4
    assert stats["checked_out_count"] == 0
    assert stats["live_count"] == stats["idle_count"]


def test_pool_connection_creation_failure_releases_reserved_capacity():
    """Verify failure during pymysql.connect() releases reserved capacity for future attempts."""
    pool = PyMySQLConnectionPool(max_size=2, checkout_timeout=0.2)

    with patch("pymysql.connect", side_effect=pymysql.err.OperationalError(2003, "Can't connect")):
        with pytest.raises(pymysql.err.OperationalError):
            pool.acquire()

        # Capacity must be reset, not leaked
        assert pool.get_stats()["live_count"] == 0

    # Subsequent attempt with working connect succeeds
    working_conn = MagicMock()
    with patch("pymysql.connect", return_value=working_conn):
        wrapper = pool.acquire()
        assert wrapper._conn is working_conn
        assert pool.get_stats()["live_count"] == 1
        wrapper.close()


def test_pool_rollback_failure_on_release_causes_disposal():
    """Verify that if rollback fails when releasing connection, connection is discarded."""
    mock_raw = MagicMock()
    mock_raw.rollback.side_effect = RuntimeError("Socket write error during rollback")
    pool = PyMySQLConnectionPool(max_size=2)

    with patch("pymysql.connect", return_value=mock_raw):
        wrapper = pool.acquire()
        wrapper.close()

        # Conn must be discarded and physically closed, not left in idle pool
        mock_raw.close.assert_called_once()
        assert pool.get_stats()["idle_count"] == 0
        assert pool.get_stats()["live_count"] == 0


def test_pool_shutdown_closes_idle_and_checked_out():
    """Verify pool shutdown closes idle connections and checked-out connections when released."""
    idle_conn = MagicMock()
    idle_conn.ping.return_value = None
    checked_conn = MagicMock()
    checked_conn.ping.return_value = None
    pool = PyMySQLConnectionPool(max_size=3)

    with patch("pymysql.connect", side_effect=[idle_conn, checked_conn]):
        # Setup 1 idle, 1 checked-out
        w1 = pool.acquire()
        w1.close()  # Returned to idle pool

        # Now acquire checked_conn (it reuses idle_conn unless pool has multiple or idle_conn is checked out)
        # To test having 1 idle AND 1 checked-out simultaneously:
        # Check out conn A, check out conn B, then close conn A
        w_a = pool.acquire()  # takes idle_conn
        w_b = pool.acquire()  # creates checked_conn
        w_a.close()  # returns w_a (idle_conn) to idle pool

        assert pool.get_stats()["idle_count"] == 1
        assert pool.get_stats()["checked_out_count"] == 1

        # Shutdown
        pool.close()
        idle_conn.close.assert_called_once()

        # Attempting new acquire after shutdown must fail
        with pytest.raises(DatabaseUnavailableError, match="shut down"):
            pool.acquire()

        # Releasing w_b after shutdown physically closes checked_conn
        w_b.close()
        checked_conn.close.assert_called_once()
        assert pool.get_stats()["live_count"] == 0


def test_pool_acquisition_rejected_after_shutdown():
    """Verify acquire immediately raises DatabaseUnavailableError once pool is closed."""
    pool = PyMySQLConnectionPool(max_size=2)
    pool.close()
    with pytest.raises(DatabaseUnavailableError, match="shut down"):
        pool.acquire()


def test_mock_connection_reuse_benchmark():
    """
    Deterministic mock benchmark comparing 100 sequential acquisitions:
    - Fresh baseline vs. Pooled path.
    Verifies connection count and respects pool limits.
    """
    # 1. Fresh baseline (simulating unpooled get_db)
    fresh_creations = 0
    def make_fresh(**kwargs):
        nonlocal fresh_creations
        fresh_creations += 1
        return MagicMock()

    with patch("pymysql.connect", side_effect=make_fresh):
        for _ in range(100):
            conn = MySQLConnectionWrapper(make_fresh())
            with conn:
                pass

    assert fresh_creations == 100

    # 2. Pooled path (100 acquisitions through pool with max_size=5)
    pool = PyMySQLConnectionPool(max_size=5)
    pooled_creations = 0
    def make_pooled(**kwargs):
        nonlocal pooled_creations
        pooled_creations += 1
        c = MagicMock()
        c.ping.return_value = None
        return c

    with patch("pymysql.connect", side_effect=make_pooled):
        for _ in range(100):
            with pool.acquire() as conn:
                pass

    # In sequential usage, exactly 1 connection is created and reused 99 times
    assert pooled_creations == 1
    stats = pool.get_stats()
    assert stats["live_count"] == 1
    assert stats["idle_count"] == 1
    assert stats["checked_out_count"] == 0


