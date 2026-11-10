import logging
import os
import queue
import random
import socket
import threading
import time
import warnings
from typing import Optional
import pymysql
import pymysql.cursors
import pymysql.constants.CR
import pymysql.constants.ER

log = logging.getLogger(__name__)

# Explicitly classified transient connection-establishment error codes
TRANSIENT_CONNECT_ERROR_CODES = {
    pymysql.constants.CR.CR_CONNECTION_ERROR,      # 2002: Can't connect to local/remote server
    pymysql.constants.CR.CR_CONN_HOST_ERROR,        # 2003: Can't connect to MySQL server on ...
    pymysql.constants.CR.CR_UNKNOWN_HOST,           # 2005: Unknown MySQL server host
    pymysql.constants.CR.CR_SERVER_GONE_ERROR,      # 2006: MySQL server has gone away
    pymysql.constants.CR.CR_SERVER_HANDSHAKE_ERR,   # 2012: Handshake error
    pymysql.constants.CR.CR_SERVER_LOST,            # 2013: Lost connection to MySQL server during query/connect
    pymysql.constants.ER.CON_COUNT_ERROR,           # 1040: Too many connections
}

try:
    from backend.config.config import (
        DB_HOST,
        DB_PORT,
        DB_USER,
        DB_PASSWORD,
        DB_NAME,
        DB_CONNECT_TIMEOUT_SECONDS,
        DB_READ_TIMEOUT_SECONDS,
        DB_WRITE_TIMEOUT_SECONDS,
        DB_CONNECT_MAX_RETRIES,
        DB_RETRY_BASE_DELAY_SECONDS,
        DB_RETRY_MAX_DELAY_SECONDS,
    )
except ModuleNotFoundError:
    from config.config import (
        DB_HOST,
        DB_PORT,
        DB_USER,
        DB_PASSWORD,
        DB_NAME,
        DB_CONNECT_TIMEOUT_SECONDS,
        DB_READ_TIMEOUT_SECONDS,
        DB_WRITE_TIMEOUT_SECONDS,
        DB_CONNECT_MAX_RETRIES,
        DB_RETRY_BASE_DELAY_SECONDS,
        DB_RETRY_MAX_DELAY_SECONDS,
    )

# Pool configuration defaults (conservative standard bounds)
DEFAULT_DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))
DEFAULT_DB_POOL_TIMEOUT_SECONDS = float(os.getenv("DB_POOL_TIMEOUT_SECONDS", "5.0"))


class DatabaseUnavailableError(pymysql.err.OperationalError):
    """Raised when the database connection pool is exhausted or unavailable."""
    pass


class DictAndIndexRow(dict):
    def __getitem__(self, item):
        if isinstance(item, int):
            return list(self.values())[item]
        return super().__getitem__(item)


def _wrap_row(row):
    if row is None:
        return None
    if isinstance(row, dict) and not isinstance(row, DictAndIndexRow):
        return DictAndIndexRow(row)
    return row


class MySQLCursorWrapper:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, query: str, args=None):
        if args is not None:
            self._cursor.execute(query, args)
        else:
            self._cursor.execute(query)
        return self

    def executemany(self, query: str, args=None):
        if args is not None:
            self._cursor.executemany(query, args)
        else:
            self._cursor.executemany(query)
        return self

    def fetchone(self):
        return _wrap_row(self._cursor.fetchone())

    def fetchall(self):
        rows = self._cursor.fetchall()
        return [_wrap_row(r) for r in rows] if rows else []

    def fetchmany(self, size=None):
        rows = self._cursor.fetchmany(size) if size else self._cursor.fetchmany()
        return [_wrap_row(r) for r in rows] if rows else []

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class MySQLConnectionWrapper:
    def __init__(self, conn, pool: Optional["PyMySQLConnectionPool"] = None):
        self._conn = conn
        self._pool = pool
        self._closed = False
        self._broken = False

    def mark_broken(self):
        """Marks connection as broken so it will be discarded rather than returned to pool."""
        self._broken = True

    def cursor(self, *args, **kwargs):
        cursor = self._conn.cursor(pymysql.cursors.DictCursor)
        return MySQLCursorWrapper(cursor)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        """
        Idempotent close:
        - If pooled: resets transaction state (rollback) and returns connection to pool (or discards if broken).
        - If unpooled: physically closes connection.
        """
        if self._closed:
            return
        self._closed = True

        if self._pool is not None:
            self._pool.release(self._conn, broken=self._broken)
        else:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        primary_exc = exc_val
        if exc_type:
            try:
                self._conn.rollback()
            except BaseException as rollback_err:
                self.mark_broken()
                if primary_exc is None:
                    primary_exc = rollback_err
                elif hasattr(primary_exc, "add_note"):
                    primary_exc.add_note(f"Secondary failure during rollback: {type(rollback_err).__name__}: {rollback_err}")
        else:
            try:
                self._conn.commit()
            except BaseException as commit_err:
                self.mark_broken()
                primary_exc = commit_err

        try:
            self.close()
        except BaseException as close_err:
            if primary_exc is None:
                primary_exc = close_err
            elif hasattr(primary_exc, "add_note"):
                primary_exc.add_note(f"Secondary failure during close: {type(close_err).__name__}: {close_err}")

        if primary_exc is not None and primary_exc is not exc_val:
            raise primary_exc

    def __getattr__(self, name):
        return getattr(self._conn, name)


class PyMySQLConnectionPool:
    """
    A bounded, thread-safe synchronous connection pool for PyMySQL.
    - Limits total live connections (idle + checked-out) to max_size.
    - Synchronizes acquisition, creation, release, and shutdown without holding a global lock during caller execution.
    - Reuses healthy connections after validating via ping(reconnect=False).
    - Discards broken connections and closes connections returned after shutdown.
    - Ensures connection-creation failures release reserved capacity.
    """
    def __init__(self, max_size: int = DEFAULT_DB_POOL_SIZE, checkout_timeout: float = DEFAULT_DB_POOL_TIMEOUT_SECONDS):
        if max_size <= 0:
            raise ValueError(f"max_size must be positive, got {max_size}")
        self.max_size = max_size
        self.checkout_timeout = checkout_timeout

        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._idle_conns = []
        self._live_count = 0
        self._checked_out = set()
        self._is_shutdown = False

    def get_stats(self):
        with self._lock:
            return {
                "max_size": self.max_size,
                "live_count": self._live_count,
                "idle_count": len(self._idle_conns),
                "checked_out_count": len(self._checked_out),
                "is_shutdown": self._is_shutdown,
            }

    def acquire(self) -> MySQLConnectionWrapper:
        """
        Acquires a connection from the pool wrapped in MySQLConnectionWrapper.
        Blocks up to checkout_timeout. Raises DatabaseUnavailableError on timeout or if shutdown.
        """
        deadline = time.monotonic() + self.checkout_timeout
        raw_conn = None

        while True:
            with self._lock:
                if self._is_shutdown:
                    raise DatabaseUnavailableError(2002, "Database connection pool has been shut down.")

                # Try acquiring an idle connection
                while self._idle_conns:
                    candidate = self._idle_conns.pop()
                    # Validate outside global lock
                    raw_conn = candidate
                    break

                if raw_conn is None:
                    # Can we create a new connection?
                    if self._live_count < self.max_size:
                        # Reserve a slot in the pool
                        self._live_count += 1
                        try_create = True
                    else:
                        try_create = False

                if raw_conn is None and not try_create:
                    # Wait for a connection to be released or capacity freed
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DatabaseUnavailableError(
                            1040,
                            f"Database connection pool exhausted (max={self.max_size}, live={self._live_count}) after {self.checkout_timeout}s timeout."
                        )
                    self._condition.wait(timeout=remaining)
                    continue

            # If we took an idle connection, ping it outside the lock
            if raw_conn is not None:
                is_healthy = False
                try:
                    raw_conn.ping(reconnect=False)
                    is_healthy = True
                except Exception as ping_err:
                    log.warning("Discarding dead/stale connection from pool: %s", ping_err)
                    try:
                        raw_conn.close()
                    except Exception:
                        pass

                if is_healthy:
                    with self._lock:
                        if self._is_shutdown:
                            # Pool was shut down while validating
                            try:
                                raw_conn.close()
                            except Exception:
                                pass
                            self._live_count = max(0, self._live_count - 1)
                            self._condition.notify()
                            raise DatabaseUnavailableError(2002, "Database connection pool has been shut down.")
                        self._checked_out.add(raw_conn)
                        return MySQLConnectionWrapper(raw_conn, pool=self)
                else:
                    # Connection died; decrement live count and loop to get/create another
                    with self._lock:
                        self._live_count = max(0, self._live_count - 1)
                        self._condition.notify()
                    raw_conn = None
                    continue

            # If we reserved capacity to create a new connection
            if try_create:
                try:
                    new_conn = _create_raw_connection()
                except BaseException as create_err:
                    # Release reserved capacity
                    with self._lock:
                        self._live_count = max(0, self._live_count - 1)
                        self._condition.notify()
                    raise create_err

                with self._lock:
                    if self._is_shutdown:
                        try:
                            new_conn.close()
                        except Exception:
                            pass
                        self._live_count = max(0, self._live_count - 1)
                        self._condition.notify()
                        raise DatabaseUnavailableError(2002, "Database connection pool has been shut down.")
                    self._checked_out.add(new_conn)
                    return MySQLConnectionWrapper(new_conn, pool=self)

    def release(self, conn, broken: bool = False):
        """
        Releases a connection back to the pool.
        - If broken or pool is shut down: physically closes conn and decrements live count.
        - Otherwise: resets transaction state via rollback() and returns to idle list.
        """
        discard = broken
        if not discard:
            try:
                conn.rollback()
            except Exception as rb_err:
                log.warning("Connection rollback failed on release; discarding: %s", rb_err)
                discard = True

        with self._lock:
            self._checked_out.discard(conn)

            if discard or self._is_shutdown:
                try:
                    conn.close()
                except Exception:
                    pass
                self._live_count = max(0, self._live_count - 1)
                self._condition.notify()
            else:
                self._idle_conns.append(conn)
                self._condition.notify()

    def close(self):
        """
        Shuts down the pool.
        Closes all idle connections. Checked out connections will be closed when released.
        """
        with self._lock:
            if self._is_shutdown:
                return
            self._is_shutdown = True
            to_close = list(self._idle_conns)
            self._idle_conns.clear()
            self._live_count = max(0, self._live_count - len(to_close))
            self._condition.notify_all()

        for conn in to_close:
            try:
                conn.close()
            except Exception:
                pass


# Global pool singleton and lock
_POOL_LOCK = threading.Lock()
_DB_POOL: Optional[PyMySQLConnectionPool] = None


def init_pool(max_size: int = DEFAULT_DB_POOL_SIZE, checkout_timeout: float = DEFAULT_DB_POOL_TIMEOUT_SECONDS) -> PyMySQLConnectionPool:
    """
    Initializes or replaces the global database connection pool.
    Lazy initialization: does NOT open physical connections on startup.
    """
    global _DB_POOL
    with _POOL_LOCK:
        if _DB_POOL is not None:
            _DB_POOL.close()
        _DB_POOL = PyMySQLConnectionPool(max_size=max_size, checkout_timeout=checkout_timeout)
        log.info("Initialized PyMySQL connection pool (max_size=%d, timeout=%.1fs)", max_size, checkout_timeout)
        return _DB_POOL


def close_pool():
    """Shuts down the global database connection pool."""
    global _DB_POOL
    with _POOL_LOCK:
        if _DB_POOL is not None:
            _DB_POOL.close()
            _DB_POOL = None
            log.info("Closed PyMySQL connection pool.")


def get_pool() -> PyMySQLConnectionPool:
    """Retrieves the global connection pool, initializing lazily if needed."""
    global _DB_POOL
    if _DB_POOL is None:
        with _POOL_LOCK:
            if _DB_POOL is None:
                _DB_POOL = PyMySQLConnectionPool()
    return _DB_POOL


def is_transient_connect_error(exc: BaseException) -> bool:
    """
    Determines if an exception raised during pymysql.connect() represents a transient
    connection-establishment failure that is safe to retry.
    """
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    if isinstance(exc, (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError)):
        return True
    if isinstance(exc, pymysql.err.OperationalError):
        args = getattr(exc, "args", None)
        if args and len(args) > 0:
            errno = args[0]
            if errno in TRANSIENT_CONNECT_ERROR_CODES:
                return True
    return False


def compute_backoff_delay(attempt: int, base_delay: float = None, max_delay: float = None) -> float:
    """
    Computes exponential backoff with full jitter for attempt index (1-based: 1, 2, ...).
    delay = random.uniform(0, min(max_delay, base_delay * (2 ** (attempt - 1))))
    """
    base = base_delay if base_delay is not None else float(DB_RETRY_BASE_DELAY_SECONDS)
    cap = max_delay if max_delay is not None else float(DB_RETRY_MAX_DELAY_SECONDS)
    ceiling = min(cap, base * (2 ** max(0, attempt - 1)))
    return random.uniform(0, ceiling)


def _create_raw_connection():
    """
    Creates a single raw pymysql.Connection applying bounded retry with
    exponential backoff and jitter strictly for transient connection-establishment failures.
    """
    max_attempts = int(DB_CONNECT_MAX_RETRIES)
    connect_kwargs = {
        "host": DB_HOST,
        "port": DB_PORT,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "database": DB_NAME,
        "charset": "utf8mb4",
        "autocommit": True,
        "connect_timeout": float(DB_CONNECT_TIMEOUT_SECONDS),
        "read_timeout": float(DB_READ_TIMEOUT_SECONDS),
        "write_timeout": float(DB_WRITE_TIMEOUT_SECONDS),
    }

    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            conn = pymysql.connect(**connect_kwargs)
            if attempt > 1:
                log.info("Database connection recovered successfully on attempt %d/%d.", attempt, max_attempts)
            return conn
        except Exception as exc:
            last_exc = exc
            if not is_transient_connect_error(exc) or attempt >= max_attempts:
                if attempt > 1:
                    log.warning(
                        "Database connection failed after %d/%d attempts. Propagating %s.",
                        attempt, max_attempts, type(exc).__name__
                    )
                raise

            delay = compute_backoff_delay(attempt)
            log.warning(
                "Transient database connection failure on attempt %d/%d (%s). Retrying in %.3fs...",
                attempt, max_attempts, type(exc).__name__, delay
            )
            time.sleep(delay)

    if last_exc is not None:
        raise last_exc


def get_db():
    """
    Acquires a database connection from the pool wrapped in MySQLConnectionWrapper.
    Preserves existing calling conventions:
      - with get_db() as conn:
      - conn = get_db(); conn.close()
    """
    pool = get_pool()
    return pool.acquire()

