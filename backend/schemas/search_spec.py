"""
search_spec.py — Unified Search Specification Dataclass
"""

from dataclasses import dataclass, field
from typing import Optional, List, Literal


@dataclass
class SearchSpec:
    skill_source: Literal["PROFILE", "QUERY", "NONE"] = "NONE"
    location_source: Literal["CURRENT", "QUERY", "NONE"] = "NONE"
    location_type: Literal["city", "state", "city_state", "unknown", "none"] = "none"
    raw_location: Optional[str] = None
    city_id: Optional[int] = None
    state_id: Optional[int] = None
    keywords: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    experience_ids: List[int] = field(default_factory=list)
    min_salary: Optional[float] = None
    job_mode: Optional[int] = None
    urgent: bool = False
    sort: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    captured_at: Optional[str] = None
    resolution_method: str = "none"     # "GPS_REVERSE_GEOCODE" | "PROFILE_DB" | "EXPLICIT_QUERY" | "none"
    confidence: float = 1.0

    use_profile_skills: bool = False
    explicit_location: bool = False

    @property
    def has_hard_location_filter(self) -> bool:
        return self.explicit_location and self.location_type in ("city", "state", "city_state")
