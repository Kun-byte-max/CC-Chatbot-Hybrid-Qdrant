import asyncio
import httpx
import logging
from typing import Dict, Any, Optional

try:
    from backend.config.config import SEARCH_API_URL, SEARCH_API_KEY
except ModuleNotFoundError:
    from config.config import SEARCH_API_URL, SEARCH_API_KEY

log = logging.getLogger(__name__)

CONFIG_VALID = bool(SEARCH_API_URL)
if not CONFIG_VALID:
    log.warning("SEARCH_API_URL is not set. All search API client calls will short-circuit to failure.")

# Application-managed client state (loop-aware)
_app_client: Optional[httpx.AsyncClient] = None
_app_loop: Optional[asyncio.AbstractEventLoop] = None


def init_search_client(
    timeout: float = 10.0,
    max_connections: int = 30,
    max_keepalive_connections: int = 20,
    keepalive_expiry: float = 30.0,
) -> httpx.AsyncClient:
    """
    Initializes a shared, connection-pooled httpx.AsyncClient bound to the current running event loop.
    Intended to be called during application lifespan startup.
    """
    global _app_client, _app_loop
    loop = asyncio.get_running_loop()
    limits = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive_connections,
        keepalive_expiry=keepalive_expiry,
    )
    _app_client = httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
    )
    _app_loop = loop
    return _app_client


async def close_search_client() -> None:
    """
    Closes the application-managed search client and drains connections.
    Intended to be called during application lifespan shutdown.
    """
    global _app_client, _app_loop
    client = _app_client
    _app_client = None
    _app_loop = None
    if client is not None and not client.is_closed:
        await client.aclose()


def set_search_client(client: Optional[httpx.AsyncClient]) -> None:
    """
    Sets or resets the application client (useful for testing and dependency injection).
    """
    global _app_client, _app_loop
    _app_client = client
    if client is not None:
        try:
            _app_loop = asyncio.get_running_loop()
        except RuntimeError:
            _app_loop = None
    else:
        _app_loop = None


def get_search_client() -> Optional[httpx.AsyncClient]:
    """Returns the current application-managed search client, or None."""
    return _app_client


def get_search_client_loop() -> Optional[asyncio.AbstractEventLoop]:
    """Returns the event loop that owns the current application-managed search client."""
    return _app_loop


async def _make_request(endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if not CONFIG_VALID:
        return {"success": False, "data": [], "total": 0}

    url = f"{SEARCH_API_URL.rstrip('/')}{endpoint}"
    headers = {"X-API-Key": SEARCH_API_KEY, "Authorization": f"Bearer {SEARCH_API_KEY}".strip()}

    # Filter out None values from params to keep query clean
    clean_params = {}
    for k, v in params.items():
        if v is not None:
            clean_params[k] = v

    try:
        # Determine if we can safely use the pooled application client
        use_pooled = False
        current_loop = None
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if (
            _app_client is not None
            and not _app_client.is_closed
            and _app_loop is not None
            and current_loop is not None
            and _app_loop == current_loop
            and not current_loop.is_closed()
        ):
            use_pooled = True

        if use_pooled and _app_client is not None:
            resp = await _app_client.get(url, headers=headers, params=clean_params)
        else:
            async with httpx.AsyncClient(timeout=10.0) as transient_client:
                resp = await transient_client.get(url, headers=headers, params=clean_params)

        if resp.status_code == 200:
            data = resp.json()
            return {
                "success": True,
                "data": data.get("data", []),
                "total": data.get("total", 0),
                "has_more": data.get("has_more", False),
                "next_offset": data.get("next_offset", None)
            }
        else:
            log.error(f"Search API error response status={resp.status_code}: {resp.text}")
            return {"success": False, "data": [], "total": 0}
    except Exception as e:
        log.exception(f"Search API request failed to {url}")
        return {"success": False, "data": [], "total": 0}


async def search_users(**filters) -> Dict[str, Any]:
    return await _make_request("/search/users", filters)


async def search_jobs(**filters) -> Dict[str, Any]:
    return await _make_request("/search/jobs", filters)

