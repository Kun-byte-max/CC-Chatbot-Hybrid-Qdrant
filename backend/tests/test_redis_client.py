import pytest
import fakeredis
from unittest.mock import patch

from backend.utils.redis_client import (
    get_async_redis_client,
    ping_redis,
    close_redis_pool,
    set_test_redis_client,
    RedisUnavailableError,
    RedisSessionOwnershipError,
    RedisSessionOOMError,
    RedisSessionPayloadLimitError,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_fakeredis_client_ping():
    fake_client = fakeredis.FakeAsyncRedis(decode_responses=True)
    set_test_redis_client(fake_client)
    try:
        is_alive = await ping_redis()
        assert is_alive is True

        client = get_async_redis_client()
        await client.set("test_key", "test_value")
        val = await client.get("test_key")
        assert val == "test_value"
    finally:
        set_test_redis_client(None)
        await fake_client.aclose()


@pytest.mark.anyio
async def test_redis_disabled_raises_unavailable():
    set_test_redis_client(None)
    with patch("backend.utils.redis_client.REDIS_ENABLED", False):
        with pytest.raises(RedisUnavailableError) as exc_info:
            get_async_redis_client()
        assert "disabled" in str(exc_info.value).lower()


def test_sync_fakeredis_client():
    fake_sync = fakeredis.FakeRedis(decode_responses=True)
    from backend.utils.redis_client import set_test_sync_redis_client, get_sync_redis_client
    set_test_sync_redis_client(fake_sync)
    try:
        client = get_sync_redis_client()
        assert client.ping() is True
        client.set("sync_key", "sync_val")
        assert client.get("sync_key") == "sync_val"
    finally:
        set_test_sync_redis_client(None)


def test_exception_types():
    assert issubclass(RedisSessionOwnershipError, Exception)
    assert issubclass(RedisSessionOOMError, Exception)
    assert issubclass(RedisSessionPayloadLimitError, Exception)
    assert issubclass(RedisUnavailableError, Exception)
