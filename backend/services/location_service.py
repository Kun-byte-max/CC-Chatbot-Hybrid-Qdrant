"""
location_service.py — Centralized database-driven location resolution engine.
Resolves location text against cyb_cities and cyb_state without converting cities to states.
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional, Dict, Any

try:
    from backend.repositories.db import get_db
except ModuleNotFoundError:
    from repositories.db import get_db  # type: ignore

log = logging.getLogger(__name__)


@dataclass
class LocationResult:
    resolved: bool = False
    location_type: str = "none"         # "city" | "state" | "none" | "unknown"
    resolution_method: str = "none"     # "GPS_REVERSE_GEOCODE" | "PROFILE_DB" | "EXPLICIT_QUERY" | "none"
    confidence: float = 1.0             # 1.0 (Exact GPS / DB Match), 0.8 (Substring DB Match)
    city_id: Optional[int] = None
    city_name: Optional[str] = None
    state_id: Optional[int] = None
    state_name: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    raw_query: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "resolved": self.resolved,
            "location_type": self.location_type,
            "resolution_method": self.resolution_method,
            "confidence": self.confidence,
            "city_id": self.city_id,
            "city_name": self.city_name,
            "state_id": self.state_id,
            "state_name": self.state_name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "raw_query": self.raw_query,
        }


import difflib

STATE_ALIASES: Dict[str, str] = {
    "uttar pardesh": "uttar pradesh",
    "utar pradesh": "uttar pradesh",
    "uttar prades": "uttar pradesh",
    "uttar pradeshh": "uttar pradesh",
    "up": "uttar pradesh",
    "u.p.": "uttar pradesh",
    "u p": "uttar pradesh",
    "west bangal": "west bengal",
    "west bengol": "west bengal",
    "wb": "west bengal",
    "w.b.": "west bengal",
    "madhya pardesh": "madhya pradesh",
    "mp": "madhya pradesh",
    "m.p.": "madhya pradesh",
    "himachal pardesh": "himachal pradesh",
    "hp": "himachal pradesh",
    "h.p.": "himachal pradesh",
    "andhra pardesh": "andhra pradesh",
    "ap": "andhra pradesh",
    "a.p.": "andhra pradesh",
    "tamilnadu": "tamil nadu",
    "tn": "tamil nadu",
    "t.n.": "tamil nadu",
    "kl": "kerala",
    "keralam": "kerala",
    "mh": "maharashtra",
}


def normalize_location_text(location_text: Optional[str]) -> Optional[str]:
    """
    Applies multi-stage string normalization (trim, lower, stripping state/country suffixes,
    punctuation/hyphen normalization, and alias/typo mapping).
    """
    if not location_text or not str(location_text).strip():
        return None

    clean = str(location_text).strip().lower()
    clean = re.sub(r',\s*india$', '', clean)
    clean = re.sub(r'\s+city$', '', clean)
    if ',' in clean:
        clean = clean.split(',')[0].strip()

    # Punctuation & hyphen normalization
    clean = re.sub(r'[\-_+]+', ' ', clean)
    clean = re.sub(r'\s+', ' ', clean).strip()

    # Apply alias and typo mapping if present
    if clean in STATE_ALIASES:
        clean = STATE_ALIASES[clean]

    return clean if clean else None


def resolve_location_from_db(raw_location: Optional[str]) -> LocationResult:
    """
    Resolves raw location text against database tables using a 5-pass architecture:
    Pass 1: Exact State Match in cyb_state (State Precedence Rule for dual entries like 'Delhi')
    Pass 2: Centralized Alias & Fuzzy Normalization
    Pass 3: Exact State Match with Corrected Canonical Name
    Pass 4: Exact City Match in cyb_cities
    Pass 5: Substring Matches with Ambiguity Check (>1 matches marked ambiguous)
    """
    clean_loc = normalize_location_text(raw_location)
    if not clean_loc:
        log.info("[LOCATION_RESOLUTION] input=%s normalized=None match_type=none resolved=True", raw_location)
        return LocationResult(resolved=True, location_type="none", raw_query=raw_location)

    conn = get_db()
    cursor = conn.cursor()

    try:
        # Pass 1: Exact State Match in cyb_state
        cursor.execute(
            "SELECT id, name FROM cyb_state WHERE LOWER(name) = %s AND status = 1 LIMIT 1",
            (clean_loc,)
        )
        s_row = cursor.fetchone()
        if s_row:
            state_id = int(s_row[0])
            state_name = str(s_row[1])
            log.info(
                '[LOCATION_RESOLUTION] input="%s" normalized="%s" match_type="exact_state" state_id=%d state_name="%s"',
                raw_location, clean_loc, state_id, state_name
            )
            return LocationResult(
                resolved=True,
                location_type="state",
                city_id=None,
                city_name=None,
                state_id=state_id,
                state_name=state_name,
                raw_query=raw_location,
            )

        # Pass 2 & 3: Fuzzy State Matching against DB States
        cursor.execute("SELECT id, name FROM cyb_state WHERE status = 1")
        all_states = cursor.fetchall()
        state_name_map = {str(row[1]).lower(): (int(row[0]), str(row[1])) for row in all_states}
        
        fuzzy_matches = difflib.get_close_matches(clean_loc, state_name_map.keys(), n=1, cutoff=0.85)
        if fuzzy_matches:
            canon_key = fuzzy_matches[0]
            state_id, state_name = state_name_map[canon_key]
            log.info(
                '[LOCATION_RESOLUTION] input="%s" normalized="%s" match_type="fuzzy_state" state_id=%d state_name="%s"',
                raw_location, clean_loc, state_id, state_name
            )
            return LocationResult(
                resolved=True,
                location_type="state",
                city_id=None,
                city_name=None,
                state_id=state_id,
                state_name=state_name,
                raw_query=raw_location,
            )

        # Pass 4: Exact City Match in cyb_cities
        cursor.execute(
            "SELECT c.id, c.name, c.state, s.name FROM cyb_cities c "
            "LEFT JOIN cyb_state s ON c.state = s.id "
            "WHERE LOWER(c.name) = %s AND c.status = 1 LIMIT 1",
            (clean_loc,)
        )
        c_row = cursor.fetchone()
        if c_row:
            city_id = int(c_row[0])
            city_name = str(c_row[1])
            state_id = int(c_row[2]) if c_row[2] is not None else None
            state_name = str(c_row[3]) if c_row[3] is not None else None
            log.info(
                '[LOCATION_RESOLUTION] input="%s" normalized="%s" match_type="exact_city" city_id=%d city_name="%s" state_id=%s state_name="%s"',
                raw_location, clean_loc, city_id, city_name, str(state_id), str(state_name)
            )
            return LocationResult(
                resolved=True,
                location_type="city",
                city_id=city_id,
                city_name=city_name,
                state_id=state_id,
                state_name=state_name,
                raw_query=raw_location,
            )

        # Pass 5: Substring Matches with Disambiguation Rule (>1 candidate matches treated as ambiguous)
        cursor.execute(
            "SELECT id, name, 'state' AS loc_type FROM cyb_state WHERE LOWER(name) LIKE %s AND status = 1",
            (f"%{clean_loc}%",)
        )
        s_matches = cursor.fetchall()

        cursor.execute(
            "SELECT c.id, c.name, c.state, s.name, 'city' AS loc_type FROM cyb_cities c "
            "LEFT JOIN cyb_state s ON c.state = s.id "
            "WHERE LOWER(c.name) LIKE %s AND c.status = 1",
            (f"%{clean_loc}%",)
        )
        c_matches = cursor.fetchall()

        total_substring_candidates = len(s_matches) + len(c_matches)

        if total_substring_candidates > 1:
            log.warning(
                '[LOCATION_RESOLUTION] input="%s" normalized="%s" match_type="ambiguous_substring" candidate_count=%d | Search blocked',
                raw_location, clean_loc, total_substring_candidates
            )
            return LocationResult(
                resolved=False,
                location_type="unknown",
                city_id=None,
                city_name=None,
                state_id=None,
                state_name=None,
                raw_query=raw_location,
            )

        if len(s_matches) == 1 and len(c_matches) == 0:
            state_id = int(s_matches[0][0])
            state_name = str(s_matches[0][1])
            log.info(
                '[LOCATION_RESOLUTION] input="%s" normalized="%s" match_type="single_substring_state" state_id=%d state_name="%s"',
                raw_location, clean_loc, state_id, state_name
            )
            return LocationResult(
                resolved=True,
                location_type="state",
                city_id=None,
                city_name=None,
                state_id=state_id,
                state_name=state_name,
                raw_query=raw_location,
            )

        if len(c_matches) == 1 and len(s_matches) == 0:
            c_item = c_matches[0]
            city_id = int(c_item[0])
            city_name = str(c_item[1])
            state_id = int(c_item[2]) if c_item[2] is not None else None
            state_name = str(c_item[3]) if c_item[3] is not None else None
            log.info(
                '[LOCATION_RESOLUTION] input="%s" normalized="%s" match_type="single_substring_city" city_id=%d city_name="%s" state_id=%s state_name="%s"',
                raw_location, clean_loc, city_id, city_name, str(state_id), str(state_name)
            )
            return LocationResult(
                resolved=True,
                location_type="city",
                city_id=city_id,
                city_name=city_name,
                state_id=state_id,
                state_name=state_name,
                raw_query=raw_location,
            )

    except Exception:
        log.exception("Error querying location from DB for raw_location=%s", raw_location)
    finally:
        conn.close()

    # Unresolved explicit location
    log.warning('[LOCATION_RESOLUTION] input="%s" normalized="%s" match_type="unresolved" | Search blocked', raw_location, clean_loc)
    return LocationResult(
        resolved=False,
        location_type="unknown",
        city_id=None,
        city_name=None,
        state_id=None,
        state_name=None,
        raw_query=raw_location,
    )

