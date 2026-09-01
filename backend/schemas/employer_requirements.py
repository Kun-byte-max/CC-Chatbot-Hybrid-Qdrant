"""
Canonical Employer Search Requirements Schema with Filter Provenance Tracking.
"""

from enum import Enum
from typing import Any, Generic, List, Optional, TypeVar, Union
from pydantic import BaseModel, Field

T = TypeVar("T")


class ProvenanceSource(str, Enum):
    QUERY = "QUERY"
    EMPLOYER_PREFERENCE = "EMPLOYER_PREFERENCE"
    SESSION_CONTEXT = "SESSION_CONTEXT"
    EXPLICIT_FOLLOWUP = "EXPLICIT_FOLLOWUP"
    SYSTEM_DEFAULT = "SYSTEM_DEFAULT"


class ProvenanceValue(BaseModel, Generic[T]):
    value: Optional[T] = None
    source: ProvenanceSource = ProvenanceSource.QUERY


class LocationRequirement(BaseModel):
    text: Optional[str] = None
    type: Optional[str] = None  # "city", "state", "country"
    city_id: Optional[int] = None
    state_id: Optional[int] = None
    country_id: Optional[int] = None
    source: ProvenanceSource = ProvenanceSource.QUERY


class ExperienceRequirement(BaseModel):
    min: Optional[float] = None
    max: Optional[float] = None
    source: ProvenanceSource = ProvenanceSource.QUERY


class SalaryRequirement(BaseModel):
    min: Optional[float] = None
    max: Optional[float] = None
    currency: str = "INR"
    source: ProvenanceSource = ProvenanceSource.QUERY


class EmployerSearchRequirements(BaseModel):
    role: ProvenanceValue[str] = Field(default_factory=lambda: ProvenanceValue[str](value=None, source=ProvenanceSource.QUERY))
    skills: ProvenanceValue[List[str]] = Field(default_factory=lambda: ProvenanceValue[List[str]](value=[], source=ProvenanceSource.QUERY))
    location: Optional[LocationRequirement] = None
    experience: Optional[ExperienceRequirement] = None
    fresher: Optional[ProvenanceValue[bool]] = None
    employment_type: Optional[ProvenanceValue[str]] = None
    employment_type_id: Optional[ProvenanceValue[int]] = None
    department: Optional[ProvenanceValue[str]] = None
    department_id: Optional[ProvenanceValue[int]] = None
    on_immediate: Optional[ProvenanceValue[bool]] = None
    on_notice: Optional[ProvenanceValue[bool]] = None
    salary: Optional[SalaryRequirement] = None
    salary_filter_deferred: bool = True
    safe_failure: bool = False
    failure_reason: Optional[str] = None

    def to_meilisearch_keyword(self) -> str:
        """
        Builds transient Meilisearch text search keyword string.
        NOTE: This is ONLY a retrieval aid and DOES NOT mutate canonical role or skills attributes.
        """
        parts = []
        if self.role and self.role.value:
            parts.append(str(self.role.value))
        if self.skills and self.skills.value:
            parts.extend([str(s) for s in self.skills.value])
        return " ".join(parts).strip()
