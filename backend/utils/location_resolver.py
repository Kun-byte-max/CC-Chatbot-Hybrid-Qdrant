import math
import logging
from typing import Dict, Any, Optional, Tuple, List
from backend.schemas.search_query import SearchQuery, LocationMode, LocationSource

log = logging.getLogger(__name__)

# Known City Center Coordinates for Indian Metros / Cities
CITY_COORDINATES: Dict[str, Tuple[float, float]] = {
    "delhi": (28.6139, 77.2090),
    "new delhi": (28.6139, 77.2090),
    "gurgaon": (28.4595, 77.0266),
    "gurugram": (28.4595, 77.0266),
    "noida": (28.5355, 77.3910),
    "greater noida": (28.4744, 77.5040),
    "faridabad": (28.4089, 77.3178),
    "ghaziabad": (28.6692, 77.4538),
    "mumbai": (19.0760, 72.8777),
    "navi mumbai": (19.0330, 73.0297),
    "thane": (19.2183, 72.9781),
    "bangalore": (12.9716, 77.5946),
    "bengaluru": (12.9716, 77.5946),
    "hyderabad": (17.3850, 78.4867),
    "secunderabad": (17.4399, 78.4983),
    "pune": (18.5204, 73.8567),
    "chennai": (13.0827, 80.2707),
    "kolkata": (22.5726, 88.3639),
    "ahmedabad": (23.0225, 72.5714),
    "chandigarh": (30.7333, 76.7794),
    "jaipur": (26.9124, 75.7873),
    "lucknow": (26.8467, 80.9462),
}

# Alias & Synonyms Canonical Mapping
CANONICAL_ALIASES: Dict[str, str] = {
    "new delhi": "delhi",
    "south delhi": "delhi",
    "north delhi": "delhi",
    "east delhi": "delhi",
    "west delhi": "delhi",
    "gurugram": "gurgaon",
    "bengaluru": "bangalore",
    "bombay": "mumbai",
    "cyberabad": "hyderabad",
}

# Metro Region Clusters
METRO_CLUSTERS: Dict[str, List[str]] = {
    "delhi": ["delhi", "new delhi", "delhi ncr", "gurgaon", "gurugram", "noida", "greater noida", "faridabad", "ghaziabad"],
    "delhi ncr": ["delhi", "new delhi", "delhi ncr", "gurgaon", "gurugram", "noida", "greater noida", "faridabad", "ghaziabad"],
    "mumbai": ["mumbai", "navi mumbai", "thane", "mumbai suburban"],
    "bangalore": ["bangalore", "bengaluru"],
    "hyderabad": ["hyderabad", "secunderabad"],
}


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Computes the Great Circle Haversine distance between two points in kilometers.
    """
    R = 6371.0  # Earth's radius in kilometers

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi / 2.0) ** 2) + (
        math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2)
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c


class LocationResolver:
    """
    Engine for resolving location modes, canonical names, metro clusters,
    GPS / profile location fallbacks, and geospatial Haversine distance calculations.
    """

    @staticmethod
    def normalize_location(location_name: Optional[str]) -> Optional[str]:
        """Normalizes raw location string into canonical location name."""
        if not location_name:
            return None
        clean = location_name.lower().strip()
        return CANONICAL_ALIASES.get(clean, clean)

    @staticmethod
    def get_metro_cluster(location_name: Optional[str]) -> List[str]:
        """Returns member location strings for metro region clusters."""
        if not location_name:
            return []
        canonical = LocationResolver.normalize_location(location_name)
        if not canonical:
            return []
        return METRO_CLUSTERS.get(canonical, [canonical, location_name.lower().strip()])

    @staticmethod
    def get_coordinates_for_city(city_name: Optional[str]) -> Optional[Tuple[float, float]]:
        """
        Returns (lat, lon) tuple for city names.
        Dynamically geocodes city coordinates via OpenStreetMap Nominatim API if missing from in-memory cache.
        """
        if not city_name:
            return None
        clean = city_name.lower().strip()
        if clean in CITY_COORDINATES:
            return CITY_COORDINATES[clean]
        canonical = CANONICAL_ALIASES.get(clean)
        if canonical and canonical in CITY_COORDINATES:
            return CITY_COORDINATES[canonical]

        # Dynamic Geocoding lookup via HTTP API
        try:
            import httpx
            url = "https://nominatim.openstreetmap.org/search"
            headers = {"User-Agent": "CollarCheckChatbot/1.0"}
            params = {"q": city_name, "format": "json", "limit": 1}
            res = httpx.get(url, params=params, headers=headers, timeout=3.0)
            if res.status_code == 200 and res.json():
                item = res.json()[0]
                coords = (float(item["lat"]), float(item["lon"]))
                CITY_COORDINATES[clean] = coords
                return coords
        except Exception as e:
            log.warning("Dynamic geocoding for city '%s' failed: %s", city_name, e)

        return None

    @classmethod
    def resolve_location(
        cls,
        parsed_location: Optional[str],
        parsed_location_mode: Optional[str],
        parsed_radius_km: Optional[float],
        request_lat: Optional[float] = None,
        request_lon: Optional[float] = None,
        profile_location: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Resolves query location criteria into a structured dict compatible with SearchQuery fields:
        - location_mode (EXPLICIT | NEAR_ME | RADIUS | NONE)
        - location_source (GPS | PROFILE | EXPLICIT | NONE)
        - location, canonical_location, metro_cluster
        - latitude, longitude, radius_km
        """
        location_mode = LocationMode.NONE
        location_source = None
        final_location = None
        canonical_loc = None
        metro_cluster = []
        final_lat = request_lat
        final_lon = request_lon
        radius_km = parsed_radius_km

        # 1. EXPLICIT LOCATION IN QUERY (highest priority)
        if parsed_location and parsed_location.strip():
            final_location = parsed_location.strip()
            canonical_loc = cls.normalize_location(final_location)
            metro_cluster = cls.get_metro_cluster(final_location)
            location_source = LocationSource.EXPLICIT

            if parsed_radius_km and parsed_radius_km > 0:
                location_mode = LocationMode.RADIUS
            else:
                location_mode = LocationMode.EXPLICIT

            # Geocode city coords if lat/lon not explicitly given
            if not final_lat or not final_lon:
                city_coords = cls.get_coordinates_for_city(final_location)
                if city_coords:
                    final_lat, final_lon = city_coords

        # 2. NEAR ME / RADIUS QUERY (Without explicit location string)
        elif parsed_location_mode in ("NEAR_ME", "RADIUS") or (parsed_radius_km and parsed_radius_km > 0):
            if parsed_radius_km and parsed_radius_km > 0:
                location_mode = LocationMode.RADIUS
            else:
                location_mode = LocationMode.NEAR_ME

            # Option A: Browser-provided GPS
            if request_lat is not None and request_lon is not None:
                final_lat = request_lat
                final_lon = request_lon
                location_source = LocationSource.GPS
                final_location = "Current Location"
                canonical_loc = "current_location"

            # Option B: Profile Location Fallback
            elif profile_location and profile_location.strip():
                final_location = profile_location.strip()
                canonical_loc = cls.normalize_location(final_location)
                metro_cluster = cls.get_metro_cluster(final_location)
                location_source = LocationSource.PROFILE

                city_coords = cls.get_coordinates_for_city(final_location)
                if city_coords:
                    final_lat, final_lon = city_coords

            # Option C: No location data available anywhere
            else:
                location_source = None
                log.warning("Location requested but neither GPS nor profile location is available.")

        # 3. FALLBACK TO PROFILE LOCATION (If query has no location at all)
        elif profile_location and profile_location.strip():
            final_location = profile_location.strip()
            canonical_loc = cls.normalize_location(final_location)
            metro_cluster = cls.get_metro_cluster(final_location)
            location_mode = LocationMode.NONE
            location_source = LocationSource.PROFILE

            city_coords = cls.get_coordinates_for_city(final_location)
            if city_coords:
                final_lat, final_lon = city_coords

        return {
            "location_mode": location_mode,
            "location_source": location_source,
            "location": final_location,
            "canonical_location": canonical_loc,
            "metro_cluster": metro_cluster,
            "latitude": final_lat,
            "longitude": final_lon,
            "radius_km": radius_km,
        }

    @classmethod
    def compute_job_distance(
        cls,
        user_lat: Optional[float],
        user_lon: Optional[float],
        job_location_str: Optional[str],
        job_lat: Optional[float] = None,
        job_lon: Optional[float] = None,
    ) -> Optional[float]:
        """Calculates distance in kilometers from user coordinates to job location."""
        if user_lat is None or user_lon is None:
            return None

        # Prefer explicit job lat/lon
        if job_lat is not None and job_lon is not None:
            return round(haversine_distance(user_lat, user_lon, job_lat, job_lon), 2)

        # Infer job city coordinates
        if job_location_str:
            coords = cls.get_coordinates_for_city(job_location_str)
            if coords:
                return round(haversine_distance(user_lat, user_lon, coords[0], coords[1]), 2)

        return None

    @classmethod
    def format_distance_label(cls, distance_km: Optional[float]) -> str:
        """Formats a human-friendly distance label (e.g. 📍 4.7 km away)."""
        if distance_km is None:
            return ""
        if distance_km < 1.0:
            return f"📍 {int(distance_km * 1000)} m away"
        return f"📍 {distance_km:.1f} km away"
