import httpx
import logging
from typing import Dict, Any

try:
    from backend.config.config import SEARCH_API_URL, SEARCH_API_KEY
except ModuleNotFoundError:
    from config.config import SEARCH_API_URL, SEARCH_API_KEY

log = logging.getLogger(__name__)

CONFIG_VALID = bool(SEARCH_API_URL)
if not CONFIG_VALID:
    log.warning("SEARCH_API_URL is not set. All search API client calls will short-circuit to failure.")

async def _make_request(endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if not CONFIG_VALID:
        return {"success": False, "data": [], "total": 0}
    
    url = f"{SEARCH_API_URL.rstrip('/')}{endpoint}"
    headers = {"X-API-Key": SEARCH_API_KEY, "Authorization": f"Bearer {SEARCH_API_KEY}"}
    
    # Filter out None values from params to keep query clean
    clean_params = {}
    for k, v in params.items():
        if v is not None:
            clean_params[k] = v
            
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers, params=clean_params)
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

