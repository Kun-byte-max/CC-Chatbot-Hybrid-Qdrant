import os
import re
import logging
from typing import Optional, List, Dict, Any, Union
import httpx
from dotenv import load_dotenv

try:
    from backend.services.token_resolver import get_user_platform_token, TokenResolutionStatus
except ModuleNotFoundError:
    from token_resolver import get_user_platform_token, TokenResolutionStatus  # type: ignore

load_dotenv()

log = logging.getLogger(__name__)

BASE = os.getenv("PLATFORM_API_BASE", "https://admin.collarcheck.com/wapi").rstrip("/")
WIDGET_URL = os.getenv("PLATFORM_WIDGET_API_URL", f"{BASE}/random-widget")


def _parse_positive_int_id(user_id_val: Any) -> Optional[int]:
    """
    Validates and converts user_id_val to a positive integer.
    Rejects bool (True/False), None, floats, zero, negative numbers, and non-numeric strings.
    """
    if user_id_val is None or isinstance(user_id_val, bool):
        return None
    if isinstance(user_id_val, int):
        return user_id_val if user_id_val > 0 else None
    if isinstance(user_id_val, float):
        return None
    if isinstance(user_id_val, str):
        val_str = user_id_val.strip()
        if val_str.isdigit():
            parsed = int(val_str)
            return parsed if parsed > 0 else None
    return None


def _get(
    path: str,
    params: Optional[Dict[str, Any]] = None,
    token: Optional[str] = None,
    user_id: Optional[Union[str, int]] = None
) -> Dict[str, Any]:
    """
    Executes HTTP GET request against the platform API with proper headers.
    Dynamic per-user token resolution without shared static test/admin token fallbacks.
    """
    auth_token = (token or "").strip()
    valid_uid = _parse_positive_int_id(user_id)

    # Dynamic token resolution if token not explicitly provided
    if not auth_token and valid_uid is not None:
        resolution = get_user_platform_token(valid_uid)
        if resolution.status == TokenResolutionStatus.SUCCESS and resolution.token:
            auth_token = resolution.token

    url = path if path.startswith("http://") or path.startswith("https://") else f"{BASE}/{path.lstrip('/')}"

    def _headers(tok: str):
        formatted_tok = tok if not tok or tok.startswith("Bearer ") else f"Bearer {tok}"
        headers_dict = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        if formatted_tok:
            headers_dict["Authorization"] = formatted_tok
            headers_dict["X-Auth-Token"] = formatted_tok
        if valid_uid is not None:
            headers_dict["X-User-Id"] = str(valid_uid)
        return headers_dict

    # Authorization Check: Skip user-specific calls if no valid token resolved
    # Explicit endpoint check without fragile substring matching
    user_specific_paths = {"employee/user-detail", "employee/allEmployementNew"}
    clean_path = path.strip().lstrip("/")
    is_user_specific = clean_path in user_specific_paths or clean_path.startswith("auth/user-profile/")

    if is_user_specific and not auth_token:
        log.info(f"Skipping platform API HTTP call for '{clean_path}': no authenticated token resolved.")
        return {"status": False, "message": "No authenticated user token available.", "data": None}

    try:
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            res = client.get(url, params=params, headers=_headers(auth_token))
            # HTTP 401/403 responses fail cleanly without retrying using admin credentials
            res.raise_for_status()
            return res.json()
    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code if e.response is not None else None
        log.warning(f"HTTP status error from platform API ({url}): {status_code} - {e}")
        return {"status": False, "data": [], "error": str(e), "status_code": status_code}
    except Exception as e:
        log.warning(f"Failed to fetch from platform API ({url}): {e}")
        return {"status": False, "data": [], "error": str(e), "status_code": None}


def fetch_random_widgets(
    token: Optional[str] = None,
    user_id: Optional[Union[str, int]] = None
) -> List[Dict[str, Any]]:
    """
    Fetches the list of random widgets from random-widget endpoint.
    Passes resolved user token and user_id if present.
    """
    res = _get("random-widget", token=token, user_id=user_id)
    if isinstance(res, dict) and res.get("status") and isinstance(res.get("data"), list):
        return res["data"]
    return []


def get_closing_soon_jobs(
    keyword: Optional[str] = None,
    limit: int = 5,
    token: Optional[str] = None,
    user_id: Optional[Union[str, int]] = None
) -> List[Dict[str, Any]]:
    """
    Retrieves jobs from the 'Position Closing Soon' widget.
    Filters by keyword if provided and slices up to `limit`.
    """
    widgets = fetch_random_widgets(token=token, user_id=user_id)
    raw_list = []

    for w in widgets:
        slug = (w.get("slug") or "").lower()
        heading = (w.get("heading") or "").lower()
        if slug == "position-closing-soon" or "position closing soon" in heading or w.get("api_slug") == "auth-all-job":
            raw_list = w.get("list") or []
            break

    results = []
    kw_lower = (keyword or "").strip().lower()

    for item in raw_list:
        if kw_lower:
            title = str(item.get("job_title") or "").lower()
            desc = str(item.get("job_description") or "").lower()
            skills = str(item.get("skills") or "").lower()
            if kw_lower not in title and kw_lower not in desc and kw_lower not in skills:
                continue

        results.append(item)
        if len(results) >= limit:
            break

    return results


def get_nearby_organizations(
    location: Optional[str] = None,
    limit: int = 5,
    token: Optional[str] = None,
    user_id: Optional[Union[str, int]] = None
) -> List[Dict[str, Any]]:
    """
    Retrieves organizations from the 'Organizations Near You' widget.
    Filters by location if provided; falls back gracefully to default top nearby organizations if filter yields 0 results.
    """
    widgets = fetch_random_widgets(token=token, user_id=user_id)
    raw_list = []

    for w in widgets:
        slug = (w.get("slug") or "").lower()
        heading = (w.get("heading") or "").lower()
        if slug == "organizations-near-you" or "organizations near you" in heading or w.get("api_slug") == "nearby-company":
            raw_list = w.get("list") or []
            break

    if not raw_list:
        return []

    results = []
    loc_lower = (location or "").strip().lower()

    generic_locations = {"near me", "my location", "nearby", "here", "current location", ""}
    if loc_lower not in generic_locations:
        terms = [t for t in re.split(r"[\s,]+", loc_lower) if len(t) > 2]
        for item in raw_list:
            city = str(item.get("city_name") or "").lower()
            state = str(item.get("state_name") or "").lower()
            country = str(item.get("country_name") or "").lower()
            name = str(item.get("name") or "").lower()
            text = f"{city} {state} {country} {name}"

            if any(term in text for term in terms):
                results.append(item)
                if len(results) >= limit:
                    break

    if results:
        return results

    return raw_list[:limit]


def fetch_user_profile_by_slug(
    user_slug: str,
    token: Optional[str] = None,
    user_id: Optional[Union[str, int]] = None
) -> Dict[str, Any]:
    """
    Fetches user profile data from CollarCheck auth user-profile endpoint using the user Bearer token.
    Supports dynamic token resolution via user_id when token is not explicitly passed.
    """
    url = f"auth/user-profile/{user_slug}"
    return _get(url, token=token, user_id=user_id)
