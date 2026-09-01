"""
Mapper service translating raw Meilisearch documents or DB rows into StandardizedCandidateObject instances.
"""

from typing import Any, Dict, List, Optional
from backend.schemas.candidate_schema import StandardizedCandidateObject


def map_doc_to_standard_candidate(doc: Dict[str, Any]) -> StandardizedCandidateObject:
    """
    Maps a raw Meilisearch candidate document into a lean 9-domain StandardizedCandidateObject.
    Preserves None values for unpopulated fields to support MatchStatus.UNKNOWN evaluation.
    """
    candidate_id = doc.get("id") or doc.get("user_id") or doc.get("candidate_id") or ""
    
    # 1. Identity
    first_name = (doc.get("first_name") or "").strip()
    last_name = (doc.get("last_name") or "").strip()
    full_name_raw = (doc.get("full_name") or "").strip()
    
    if first_name or last_name:
        name = f"{first_name} {last_name}".strip()
    elif full_name_raw:
        name = full_name_raw
    elif doc.get("slug"):
        slug_parts = doc["slug"].split("-")
        clean_parts = [p.capitalize() for p in slug_parts if p and not p.lower().startswith("cce")]
        name = " ".join(clean_parts) or f"Candidate {candidate_id}"
    else:
        name = doc.get("name") or doc.get("username") or f"Candidate {candidate_id}"

    # 2. Title
    title = doc.get("current_position_name") or doc.get("designation") or doc.get("designation_name") or doc.get("title") or None


    # 3. Skills
    raw_skills = doc.get("skills") or doc.get("skill_names") or doc.get("exp_skills")
    skills = None
    if isinstance(raw_skills, list) and raw_skills:
        skills = [str(s).strip() for s in raw_skills if str(s).strip()]
    elif isinstance(raw_skills, str) and raw_skills.strip():
        skills = [s.strip() for s in raw_skills.split(",") if s.strip()]

    # 4. Experience
    exp_years = doc.get("total_experience_years")
    if exp_years is not None:
        try:
            exp_years = float(exp_years)
        except (ValueError, TypeError):
            exp_years = None

    # 5. Location
    city = doc.get("city_name") or (str(doc["city"]) if doc.get("city") is not None else None)
    state = doc.get("state_name") or (str(doc["state"]) if doc.get("state") is not None else None)
    country = doc.get("country_name") or (str(doc["country"]) if doc.get("country") is not None else None)

    # 6. Availability
    on_immediate = doc.get("on_immediate")
    if on_immediate is not None:
        on_immediate = bool(on_immediate)

    on_notice = doc.get("on_notice")
    if on_notice is not None:
        on_notice = bool(on_notice)

    # 7. Employment
    emp_type = doc.get("exp_employment_type_name") or doc.get("exp_employment_type") or doc.get("employment_type")
    if emp_type is not None:
        emp_type = str(emp_type)

    work_status = doc.get("work_status_name") or doc.get("work_status")
    if work_status is not None:
        work_status = str(work_status)

    # 8. Department
    department = doc.get("exp_department_name") or doc.get("exp_department") or doc.get("department")
    if department is not None:
        department = str(department)


    # 9. Rating & Verification
    rating = doc.get("rating")
    if rating is not None:
        try:
            rating = float(rating)
        except (ValueError, TypeError):
            rating = None

    verified_identity = bool(doc.get("verified_identity") or doc.get("is_verified") or False)
    verified_employment = bool(doc.get("verified_employment") or doc.get("emp_verified") or False)

    return StandardizedCandidateObject(
        candidate_id=candidate_id,
        name=name,
        title=title,
        skills=skills,
        experience_years=exp_years,
        city=city,
        state=state,
        country=country,
        on_immediate=on_immediate,
        on_notice=on_notice,
        employment_type=emp_type,
        work_status=work_status,
        department=department,
        rating=rating,
        verified_identity=verified_identity,
        verified_employment=verified_employment,
    )


def to_candidate_profile(row: Dict[str, Any]) -> Any:
    try:
        from backend.recommendation.models import CandidateProfile
    except ModuleNotFoundError:
        from recommendation.models import CandidateProfile  # type: ignore
    return CandidateProfile.from_dict(row)


def to_candidate_profile_from_api(doc: Dict[str, Any]) -> Any:
    try:
        from backend.recommendation.models import CandidateProfile
    except ModuleNotFoundError:
        from recommendation.models import CandidateProfile  # type: ignore
    return CandidateProfile.from_dict(doc)


def to_job_profile(row: Dict[str, Any]) -> Any:
    try:
        from backend.recommendation.models import JobProfile
    except ModuleNotFoundError:
        from recommendation.models import JobProfile  # type: ignore
    return JobProfile.from_dict(row)

