"""
Standardized Candidate Object & Search Metadata Schemas.
"""

from enum import Enum
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field


class MatchStatus(str, Enum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNKNOWN = "UNKNOWN"


class SearchMetadata(BaseModel):
    total_matches: int = 0
    returned: int = 0
    page: int = 1
    limit: int = 20

    def validate_invariants(self) -> bool:
        """
        Enforces mathematical invariants:
        1. returned <= limit
        2. returned <= total_matches
        3. If page == 1 and total_matches > 0: returned == min(total_matches, limit)
        """
        assert self.returned <= self.limit, f"returned ({self.returned}) > limit ({self.limit})"
        assert self.returned <= self.total_matches, f"returned ({self.returned}) > total_matches ({self.total_matches})"
        if self.page == 1 and self.total_matches > 0:
            expected = min(self.total_matches, self.limit)
            assert self.returned == expected, f"Page 1 returned ({self.returned}) != min(total_matches, limit) ({expected})"
        return True


class StandardizedCandidateObject(BaseModel):
    # 1. Identity
    candidate_id: Union[int, str]
    name: str

    # 2. Title
    title: Optional[str] = None

    # 3. Skills (Preserves None if missing to support MatchStatus.UNKNOWN)
    skills: Optional[List[str]] = None

    # 4. Experience
    experience_years: Optional[float] = None

    # 5. Location
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None

    # 6. Availability
    on_immediate: Optional[bool] = None
    on_notice: Optional[bool] = None

    # 7. Employment
    employment_type: Optional[str] = None
    work_status: Optional[str] = None

    # 8. Department
    department: Optional[str] = None

    # 9. Rating & Verification
    rating: Optional[float] = None
    verified_identity: bool = False
    verified_employment: bool = False
