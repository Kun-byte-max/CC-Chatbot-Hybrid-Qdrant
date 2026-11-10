"""
backend/utils/redis_client.py — High-performance, connection-pooled Async Redis Client Manager.
Strict RAM-only session and conversation-history store client.
"""

from __future__ import annotations

import logging
from typing import Optional, Any
import redis as syncredis
import redis.asyncio as aioredis
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
    TimeoutError as RedisTimeoutError,
    ResponseError as RedisResponseError,
    RedisError as BaseRedisLibraryError,
)

try:
    from backend.config.config import (
        REDIS_ENABLED,
        REDIS_HOST,
        REDIS_PORT,
        REDIS_PASSWORD,
        REDIS_DB,
        REDIS_MAX_CONNECTIONS,
        REDIS_CONNECT_TIMEOUT_SECONDS,
        REDIS_SOCKET_TIMEOUT_SECONDS,
    )
except ModuleNotFoundError:
    from config.config import (  # type: ignore
        REDIS_ENABLED,
        REDIS_HOST,
        REDIS_PORT,
        REDIS_PASSWORD,
        REDIS_DB,
        REDIS_MAX_CONNECTIONS,
        REDIS_CONNECT_TIMEOUT_SECONDS,
        REDIS_SOCKET_TIMEOUT_SECONDS,
    )

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain Exceptions (Strictly Controlled Failure Paths)
# ---------------------------------------------------------------------------

class RedisSessionError(Exception):
    """Base exception for Redis session operations."""
    pass


class RedisUnavailableError(RedisSessionError):
    """Raised when Redis is disabled, down, or fails connection/ping."""
    def __init__(self, message: str = "Redis session store is unavailable"):
        super().__init__(message)


class RedisSessionOwnershipError(RedisSessionError):
    """Raised when session access violates identity or ownership validation."""
    def __init__(self, message: str = "Access denied: session ownership mismatch"):
        super().__init__(message)


class RedisSessionOOMError(RedisSessionError):
    """Raised when Redis memory limit is reached (noeviction policy active)."""
    def __init__(self, message: str = "Redis session store memory limit reached (OOM)"):
        super().__init__(message)


class RedisSessionPayloadLimitError(RedisSessionError):
    """Raised when turn payload, message size, or session byte limit is exceeded."""
    def __init__(self, message: str = "Session payload limit exceeded"):
        super().__init__(message)


# ---------------------------------------------------------------------------
# Connection Pool & Client Management
# ---------------------------------------------------------------------------

_redis_pool: Optional[aioredis.ConnectionPool] = None
_sync_redis_pool: Optional[syncredis.ConnectionPool] = None
_test_redis_client: Optional[Any] = None
_test_sync_redis_client: Optional[Any] = None


def set_test_redis_client(client: Optional[Any]) -> None:
    """
    Test-only fixture injection (e.g. fakeredis.aioredis.FakeRedis or fakeredis.FakeRedis).
    Never used in production.
    """
    global _test_redis_client, _test_sync_redis_client
    _test_redis_client = client
    # If the provided client is synchronous (e.g. fakeredis.FakeRedis), wire it to sync client as well
    if client is not None and not hasattr(client, "aclose"):
        _test_sync_redis_client = client


def set_test_sync_redis_client(client: Optional[Any]) -> None:
    """Test-only fixture injection for synchronous Redis client."""
    global _test_sync_redis_client
    _test_sync_redis_client = client


def get_test_redis_client() -> Optional[Any]:
    """Returns the test injected client if one is active."""
    return _test_redis_client


def get_sync_redis_client() -> syncredis.Redis:
    """
    Retrieves a thread-safe synchronous Redis client backed by a connection pool.
    Raises RedisUnavailableError if Redis is disabled or uninitialized.
    """
    if _test_sync_redis_client is not None:
        return _test_sync_redis_client

    if _test_redis_client is not None and not hasattr(_test_redis_client, "aclose"):
        return _test_redis_client

    if not REDIS_ENABLED:
        raise RedisUnavailableError("Redis is disabled via REDIS_ENABLED=false")

    global _sync_redis_pool
    if _sync_redis_pool is None:
        try:
            _sync_redis_pool = syncredis.ConnectionPool(
                host=REDIS_HOST,
                port=REDIS_PORT,
                password=REDIS_PASSWORD,
                db=REDIS_DB,
                max_connections=REDIS_MAX_CONNECTIONS,
                socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
                socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
                decode_responses=True,
                retry_on_timeout=True,
            )
        except Exception as exc:
            log.error("Failed to initialize sync Redis connection pool: %s", exc)
            _sync_redis_pool = None
            raise RedisUnavailableError(f"Failed to initialize sync Redis pool: {exc}") from exc

    return syncredis.Redis(connection_pool=_sync_redis_pool)


def init_redis_pool() -> aioredis.ConnectionPool:
    """
    Initializes a shared connection pool for Redis.
    Called during application lifespan startup.
    """
    global _redis_pool
    if _redis_pool is not None:
        return _redis_pool

    if not REDIS_ENABLED:
        log.warning("Redis is disabled via configuration (REDIS_ENABLED=false).")
        return None

    try:
        _redis_pool = aioredis.ConnectionPool(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD,
            db=REDIS_DB,
            max_connections=REDIS_MAX_CONNECTIONS,
            socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
            socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
            decode_responses=True,
            retry_on_timeout=True,
        )
        log.info(
            "Redis connection pool initialized (host=%s, port=%d, db=%d, max_conn=%d)",
            REDIS_HOST,
            REDIS_PORT,
            REDIS_DB,
            REDIS_MAX_CONNECTIONS,
        )
        return _redis_pool
    except Exception as exc:
        log.error("Failed to initialize Redis connection pool: %s", exc)
        _redis_pool = None
        raise RedisUnavailableError(f"Failed to initialize Redis pool: {exc}") from exc


async def close_redis_pool() -> None:
    """
    Drains connections and shuts down the Redis connection pool.
    Called during application lifespan shutdown.
    """
    global _redis_pool, _sync_redis_pool, _test_redis_client, _test_sync_redis_client
    if _test_redis_client is not None:
        try:
            if hasattr(_test_redis_client, "aclose"):
                await _test_redis_client.aclose()
            elif hasattr(_test_redis_client, "close"):
                _test_redis_client.close()
        except Exception:
            pass
        _test_redis_client = None

    if _test_sync_redis_client is not None:
        try:
            if hasattr(_test_sync_redis_client, "close"):
                _test_sync_redis_client.close()
        except Exception:
            pass
        _test_sync_redis_client = None

    if _redis_pool is not None:
        try:
            await _redis_pool.disconnect()
            log.info("Redis async connection pool disconnected successfully.")
        except Exception as exc:
            log.warning("Error disconnecting Redis connection pool: %s", exc)
        finally:
            _redis_pool = None

    if _sync_redis_pool is not None:
        try:
            _sync_redis_pool.disconnect()
            log.info("Redis sync connection pool disconnected successfully.")
        except Exception as exc:
            log.warning("Error disconnecting Redis sync connection pool: %s", exc)
        finally:
            _sync_redis_pool = None


def get_async_redis_client() -> aioredis.Redis:
    """
    Retrieves an async Redis client backed by the global connection pool.
    Raises RedisUnavailableError if Redis is disabled or uninitialized.
    """
    if _test_redis_client is not None:
        return _test_redis_client

    if not REDIS_ENABLED:
        raise RedisUnavailableError("Redis is disabled via REDIS_ENABLED=false")

    global _redis_pool
    if _redis_pool is None:
        _redis_pool = init_redis_pool()

    if _redis_pool is None:
        raise RedisUnavailableError("Redis connection pool is not available")

    return aioredis.Redis(connection_pool=_redis_pool)


async def ping_redis() -> bool:
    """
    Performs a lightweight health check against Redis.
    Returns True if healthy; raises RedisUnavailableError if unreachable.
    """
    try:
        client = get_async_redis_client()
        pong = await client.ping()
        return bool(pong)
    except (RedisConnectionError, RedisTimeoutError, RedisUnavailableError) as err:
        log.error("Redis ping failed: %s", err)
        raise RedisUnavailableError(f"Redis health check failed: {err}") from err
    except Exception as exc:
        log.error("Unexpected error during Redis ping: %s", exc)
        raise RedisUnavailableError(f"Unexpected Redis error: {exc}") from exc

