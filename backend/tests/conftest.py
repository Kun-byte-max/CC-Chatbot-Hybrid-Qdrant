import pytest
import fakeredis
import fakeredis.aioredis
from backend.utils.redis_client import set_test_redis_client, set_test_sync_redis_client


@pytest.fixture(autouse=True)
def auto_fakeredis():
    """Automatically wires FakeRedis for all backend tests to prevent network socket timeouts."""
    server = fakeredis.FakeServer()
    fake_sync = fakeredis.FakeRedis(server=server, decode_responses=False)
    fake_async = fakeredis.aioredis.FakeRedis(server=server, decode_responses=False)

    set_test_sync_redis_client(fake_sync)
    set_test_redis_client(fake_async)
    yield fake_sync
    set_test_sync_redis_client(None)
    set_test_redis_client(None)
