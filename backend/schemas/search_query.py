import hashlib
import json
import time
from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field

MAX_SNAPSHOT_CANDIDATES = 100


class ChatbotIntent(str, Enum):
    JOB_SEARCH = "JOB_SEARCH"
    PROFILE = "PROFILE"
    SMALLTALK = "SMALLTALK"


class SearchMode(str, Enum):
    SEARCH_ONLY = "SEARCH_ONLY"
    RECOMMENDATION = "RECOMMENDATION"
    HYBRID = "HYBRID"


class LocationMode(str, Enum):
    NONE = "NONE"
    EXPLICIT = "EXPLICIT"
    NEAR_ME = "NEAR_ME"
    RADIUS = "RADIUS"


class LocationSource(str, Enum):
    GPS = "GPS"
    PROFILE = "PROFILE"
    EXPLICIT = "EXPLICIT"


class ProfileStatus(str, Enum):
    PROFILE_AVAILABLE = "PROFILE_AVAILABLE"
    PROFILE_PARTIAL = "PROFILE_PARTIAL"
    PROFILE_UNAVAILABLE = "PROFILE_UNAVAILABLE"
    PROFILE_API_ERROR = "PROFILE_API_ERROR"


class SortMode(str, Enum):
    RELEVANCE = "RELEVANCE"
    RECOMMENDATION = "RECOMMENDATION"
    DISTANCE = "DISTANCE"
    SALARY = "SALARY"
    LATEST = "LATEST"


class SearchTarget(str, Enum):
    JOBS = "JOBS"
    PEOPLE_SEARCH = "PEOPLE_SEARCH"
    CANDIDATE_SEARCH = "CANDIDATE_SEARCH"
    UNKNOWN = "UNKNOWN"


class SearchQuery(BaseModel):
    intent: ChatbotIntent = ChatbotIntent.JOB_SEARCH
    search_target: SearchTarget = SearchTarget.JOBS
    search_mode: SearchMode = SearchMode.HYBRID
    profile_status: ProfileStatus = ProfileStatus.PROFILE_UNAVAILABLE

    title: Optional[str] = None
    skills: Optional[List[str]] = Field(default_factory=list)

    location_mode: LocationMode = LocationMode.NONE
    location_source: Optional[LocationSource] = None
    location: Optional[str] = None
    canonical_location: Optional[str] = None
    metro_cluster: Optional[List[str]] = Field(default_factory=list)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    radius_km: Optional[float] = None

    experience_level: Optional[str] = None

    salary_min: Optional[float] = None
    salary_max: Optional[float] = None

    work_mode: Optional[str] = None
    company: Optional[str] = None
    clear_filters: Optional[List[str]] = Field(default_factory=list)

    sort_by: SortMode = SortMode.RECOMMENDATION

    use_profile: bool = True
    use_recommendation: bool = True

    def compute_hash(self) -> str:
        """Generates a deterministic hash fingerprint of active query criteria."""
        data = {
            "title": (self.title or "").lower().strip(),
            "skills": sorted([s.lower().strip() for s in (self.skills or [])]),
            "location_mode": self.location_mode.value,
            "location": (self.location or "").lower().strip(),
            "canonical_location": (self.canonical_location or "").lower().strip(),
            "radius_km": self.radius_km,
            "experience_level": (self.experience_level or "").lower().strip(),
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "work_mode": (self.work_mode or "").lower().strip(),
            "company": (self.company or "").lower().strip(),
        }
        encoded = json.dumps(data, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]

    def to_search_criteria(self):
        """Converts SearchQuery to canonical SearchCriteria representation."""
        from backend.schemas.search_criteria import SearchCriteria
        return SearchCriteria.from_parsed_dict(self.model_dump())



class SearchSnapshot(BaseModel):
    snapshot_id: str
    session_id: str
    query_hash: str
    search_query: SearchQuery
    candidate_job_ids: List[int] = Field(default_factory=list)
    ranked_job_ids: List[int] = Field(default_factory=list)
    current_page: int = 1
    page_size: int = 5
    total_results: int = 0
    created_at: float = Field(default_factory=time.time)
    expires_at: float = Field(default_factory=lambda: time.time() + 3600.0)

    def is_valid(self, query_hash: str) -> bool:
        return (time.time() < self.expires_at) and (self.query_hash == query_hash)
