import os
import re
import logging
from typing import Optional, List, Dict, Any
import httpx
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

BASE = os.getenv("PLATFORM_API_BASE", "https://admin.collarcheck.com/wapi").rstrip("/")
WIDGET_URL = os.getenv("PLATFORM_WIDGET_API_URL", f"{BASE}/random-widget")

TOKEN = os.getenv("PLATFORM_TEST_TOKEN", "")
USER_ID = os.getenv("PLATFORM_TEST_USER_ID", "200014")


def _get(path: str, params: Optional[Dict[str, Any]] = None, token: Optional[str] = None, user_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Executes HTTP GET request against the platform API with proper headers.
    """
    auth_token = token or TOKEN or os.getenv("PLATFORM_ADMIN_TOKEN", "")
    admin_token = os.getenv("PLATFORM_ADMIN_TOKEN", "")
    uid = user_id or USER_ID

    def _headers(tok):
        formatted_tok = tok if not tok or tok.startswith("Bearer ") else f"Bearer {tok}"
        return {
            "Accept": "application/json, text/plain, */*",
            "Authorization": formatted_tok,
            "X-Auth-Token": formatted_tok,
            "X-User-Id": str(uid),
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }

    url = path if path.startswith("http://") or path.startswith("https://") else f"{BASE}/{path.lstrip('/')}"

    if "user-detail" in url and not auth_token:
        log.info("Skipping external user-detail HTTP call: no auth_token provided.")
        return {"status": False, "data": None}

    try:
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            res = client.get(url, params=params, headers=_headers(auth_token))
            # Retry with admin_token if auth_token returns 401 for user-profile, widgets, or non-user-detail endpoints
            if res.status_code == 401 and admin_token and auth_token != admin_token:
                if "user-detail" not in url:
                    res = client.get(url, params=params, headers=_headers(admin_token))
            res.raise_for_status()
            return res.json()
    except Exception as e:
        log.warning(f"Failed to fetch from platform API ({url}): {e}")
        return {"status": False, "data": [], "error": str(e)}




def fetch_random_widgets(token: Optional[str] = None, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Fetches the list of random widgets from random-widget endpoint.
    """
    res = _get("random-widget", token=token, user_id=user_id)
    if isinstance(res, dict) and res.get("status") and isinstance(res.get("data"), list):
        return res["data"]
    return []


def get_closing_soon_jobs(keyword: Optional[str] = None, limit: int = 5, token: Optional[str] = None, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
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


def get_nearby_organizations(location: Optional[str] = None, limit: int = 5, token: Optional[str] = None, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
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

    # Skip filtering if location keyword is generic ("near me", "my location", "nearby", etc.)
    generic_locations = {"near me", "my location", "nearby", "here", "current location", ""}
    if loc_lower not in generic_locations:
        # Split search terms (e.g. "East Delhi" -> "east", "delhi")
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

    # If filtering returned results, return them; otherwise fallback to default widget items
    if results:
        return results

    return raw_list[:limit]


def fetch_user_profile_by_slug(user_slug: str, token: Optional[str] = None) -> Dict[str, Any]:
    """
    Fetches user profile data from CollarCheck auth user-profile endpoint using the user Bearer token.
    """
    auth_token = token or TOKEN
    url = f"https://admin.collarcheck.com/wapi/auth/user-profile/{user_slug}"

    return _get(url, token=auth_token)


