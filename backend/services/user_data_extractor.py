"""
user_data_extractor.py — Granular field extraction layer with strict security allowlists.

Phase 3 / Fix Scope:
  - Built strictly against verified Phase 0 API schemas.
  - Profile skills: user-profile.data.all_Skill[].skill
  - Employment skills: allEmployementNew.data[].lists[].skill[].name
  - Languages: user-profile.data.all_languages[]
  - Portfolio: user-profile.data.portfolio[]
  - Missing labels: user-detail.data.uncomplete[]
  - Completion impact: user-detail.data.incomplete[]
  - Notice employments: user-detail.data.noticeEmployments[] (company IDs array)
  - Strict field allowlists to guarantee zero token/credential exposure.
"""

import logging
from typing import Dict, Any, List, Optional
from backend.services.user_data_intent_parser import UserIntent

log = logging.getLogger(__name__)

# Sensitive Keys Blocklist (Extra safety layer on top of strict allowlist)
FORBIDDEN_KEYS = {
    "token", "jwt", "access_token", "refresh_token", "password", "passwd",
    "loginauth", "secret", "cookie", "session_id", "session", "auth_token",
    "x-auth-token", "api_key", "hash"
}


def _is_safe_key(key: str) -> bool:
    if not isinstance(key, str):
        return True
    k_lower = key.lower().strip()
    return not any(forbidden in k_lower for forbidden in FORBIDDEN_KEYS)


def scrub_sensitive_keys(obj: Any) -> Any:
    """
    Recursively scrubs forbidden sensitive keys across dictionaries, nested dictionaries,
    lists, and lists containing dictionaries without mutating the input object in-place.
    """
    if isinstance(obj, dict):
        return {k: scrub_sensitive_keys(v) for k, v in obj.items() if _is_safe_key(str(k))}
    elif isinstance(obj, list):
        return [scrub_sensitive_keys(item) for item in obj]
    return obj


# -----------------------------------------------------------------------
# LEVEL 1 EXTRACTORS: user-detail
# -----------------------------------------------------------------------
def extract_profile_incomplete(user_detail_data: Dict[str, Any]) -> Dict[str, Any]:
    data = user_detail_data.get("data") if isinstance(user_detail_data, dict) else {}
    if not isinstance(data, dict):
        data = {}

    percentage = data.get("profile_percentage")
    uncomplete_labels = data.get("uncomplete") if isinstance(data.get("uncomplete"), list) else []
    clean_uncomplete = [str(lbl) for lbl in uncomplete_labels if lbl is not None]

    raw_incomplete = data.get("incomplete") if isinstance(data.get("incomplete"), list) else []
    clean_incomplete = []
    for item in raw_incomplete:
        if isinstance(item, dict):
            key = item.get("key")
            val = item.get("value")
            if key is not None:
                clean_incomplete.append({"key": str(key), "value": str(val) if val else ""})

    return {
        "profile_percentage": percentage,
        "uncomplete": clean_uncomplete,
        "incomplete": clean_incomplete
    }


def extract_user_detail_notices(user_detail_data: Dict[str, Any]) -> Dict[str, Any]:
    data = user_detail_data.get("data") if isinstance(user_detail_data, dict) else {}
    if not isinstance(data, dict):
        data = {}

    notice_ids = data.get("noticeEmployments") if isinstance(data.get("noticeEmployments"), list) else []
    clean_ids = [str(nid) for nid in notice_ids if nid is not None]

    return {
        "noticeEmployments": clean_ids,
        "on_notice": data.get("on_notice"),
        "notice_period_name": data.get("notice_period_name")
    }


def extract_user_detail_reminders(user_detail_data: Dict[str, Any]) -> Dict[str, Any]:
    data = user_detail_data.get("data") if isinstance(user_detail_data, dict) else {}
    if not isinstance(data, dict):
        data = {}

    has_reminder = bool(data.get("reminderExperience"))
    raw_reminders = data.get("reminderExperienceList") if isinstance(data.get("reminderExperienceList"), list) else []

    clean_reminders = []
    for r in raw_reminders:
        if isinstance(r, dict):
            clean_reminders.append({
                "id": str(r.get("id")) if r.get("id") is not None else None,
                "company": str(r.get("company")) if r.get("company") is not None else None,
                "designation": str(r.get("designation")) if r.get("designation") is not None else None,
                "joining_date": r.get("joining_date"),
                "worked_till_date": r.get("worked_till_date")
            })

    return {
        "reminderExperience": has_reminder,
        "reminderExperienceList": clean_reminders
    }


# -----------------------------------------------------------------------
# LEVEL 2 EXTRACTORS: allEmployementNew
# -----------------------------------------------------------------------
def extract_employment_history(employment_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = employment_data.get("data") if isinstance(employment_data, dict) else []
    if not isinstance(data, list):
        return []

    clean_companies = []
    for company_entry in data:
        if not isinstance(company_entry, dict):
            continue

        company_name = company_entry.get("company")
        company_id = company_entry.get("company_id")
        is_verified = company_entry.get("is_verified")
        total_exp = company_entry.get("totalExperienceMonths")

        raw_lists = company_entry.get("lists") if isinstance(company_entry.get("lists"), list) else []
        clean_roles = []
        for role in raw_lists:
            if not isinstance(role, dict):
                continue

            raw_skills = role.get("skill") if isinstance(role.get("skill"), list) else []
            clean_skills = [
                {"id": str(s.get("id")), "name": str(s.get("name"))}
                for s in raw_skills if isinstance(s, dict) and s.get("name")
            ]

            verification = role.get("verificationProcess")
            clean_verif = verification if isinstance(verification, dict) else {}

            clean_roles.append({
                "id": str(role.get("id")) if role.get("id") is not None else None,
                "designation": role.get("designation"),
                "employment_type": role.get("employment_type"),
                "joining_date": role.get("joining_date"),
                "worked_till_date": role.get("worked_till_date"),
                "still_working": str(role.get("still_working")) if role.get("still_working") is not None else "0",
                "salary": role.get("salary"),
                "salary_inhand": role.get("salary_inhand"),
                "salary_mode": role.get("salary_mode"),
                "skill": clean_skills,
                "verificationProcess": clean_verif
            })

        clean_companies.append({
            "id": str(company_entry.get("id")) if company_entry.get("id") is not None else None,
            "company": company_name,
            "company_id": company_id,
            "is_verified": is_verified,
            "totalExperienceMonths": total_exp,
            "roles": clean_roles
        })

    return clean_companies


def extract_employment_skills(employment_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = employment_data.get("data") if isinstance(employment_data, dict) else []
    if not isinstance(data, list):
        return []

    seen_skills = set()
    employment_skills = []

    for company_entry in data:
        if not isinstance(company_entry, dict):
            continue
        company_name = company_entry.get("company")
        raw_lists = company_entry.get("lists") if isinstance(company_entry.get("lists"), list) else []

        for role in raw_lists:
            if not isinstance(role, dict):
                continue
            designation = role.get("designation")
            raw_skills = role.get("skill") if isinstance(role.get("skill"), list) else []

            for s in raw_skills:
                if isinstance(s, dict) and s.get("name"):
                    name = str(s.get("name")).strip()
                    skill_id = str(s.get("id")) if s.get("id") is not None else ""
                    key = (name.lower(), company_name)
                    if key not in seen_skills:
                        seen_skills.add(key)
                        employment_skills.append({
                            "id": skill_id,
                            "name": name,
                            "company": company_name,
                            "designation": designation
                        })

    return employment_skills


def extract_employment_salary(employment_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extracts salary information from allEmployementNew.data[].lists[]
    """
    data = employment_data.get("data") if isinstance(employment_data, dict) else []
    if not isinstance(data, list):
        return []

    salary_list = []
    for company_entry in data:
        if not isinstance(company_entry, dict):
            continue
        company_name = company_entry.get("company")
        raw_lists = company_entry.get("lists") if isinstance(company_entry.get("lists"), list) else []

        for role in raw_lists:
            if not isinstance(role, dict):
                continue
            sal = role.get("salary")
            if sal:
                salary_list.append({
                    "company": company_name,
                    "designation": role.get("designation"),
                    "salary": str(sal),
                    "salary_inhand": role.get("salary_inhand"),
                    "salary_mode": role.get("salary_mode")
                })

    return salary_list


# -----------------------------------------------------------------------
# LEVEL 3 EXTRACTORS: random-widget
# -----------------------------------------------------------------------
def extract_widget_data(widget_data: Dict[str, Any], target_slug: Optional[str] = None) -> List[Dict[str, Any]]:
    data = widget_data.get("data") if isinstance(widget_data, dict) else []
    if not isinstance(data, list):
        return []

    results = []
    for w in data:
        if not isinstance(w, dict):
            continue
        slug = str(w.get("slug") or w.get("api_slug") or "").lower()
        heading = str(w.get("heading") or "")
        raw_list = w.get("list") if isinstance(w.get("list"), list) else []

        if target_slug:
            if target_slug.lower() not in slug and target_slug.lower() not in heading.lower():
                continue

        clean_items = []
        for item in raw_list:
            if isinstance(item, dict):
                clean_items.append({
                    "id": item.get("id"),
                    "job_title": item.get("job_title"),
                    "company": item.get("company_name") or item.get("company") or item.get("name"),
                    "location": item.get("city_name") or item.get("location") or item.get("state_name"),
                    "city_name": item.get("city_name"),
                    "state_name": item.get("state_name"),
                    "industry_name": item.get("industry_name"),
                    "distance": item.get("distance"),
                    "salary": item.get("salary"),
                    "url": f"https://www.collarcheck.com/jobs-details/{item.get('id')}" if item.get("id") else None
                })

        results.append({
            "slug": w.get("slug"),
            "api_slug": w.get("api_slug"),
            "heading": heading,
            "list_count": len(clean_items),
            "items": clean_items
        })

    return results


# -----------------------------------------------------------------------
# LEVEL 4 EXTRACTORS: user-profile/{slug}
# -----------------------------------------------------------------------
def extract_profile_skills(user_profile_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = user_profile_data.get("data") if isinstance(user_profile_data, dict) else {}
    if not isinstance(data, dict):
        data = {}

    all_skill = data.get("all_Skill") if isinstance(data.get("all_Skill"), list) else []
    clean_skills = []

    for s in all_skill:
        if isinstance(s, dict) and s.get("skill"):
            clean_skills.append({
                "id": str(s.get("id")) if s.get("id") is not None else None,
                "skill": str(s.get("skill")),
                "rating": str(s.get("rating")) if s.get("rating") is not None else None
            })

    return clean_skills


def extract_profile_education(user_profile_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = user_profile_data.get("data") if isinstance(user_profile_data, dict) else {}
    if not isinstance(data, dict):
        data = {}

    raw_edu = data.get("all_education") if isinstance(data.get("all_education"), list) else []
    clean_edu = []

    for edu in raw_edu:
        if isinstance(edu, dict):
            clean_edu.append({
                "id": str(edu.get("id")) if edu.get("id") is not None else None,
                "university": edu.get("university"),
                "course": edu.get("course"),
                "course_type": edu.get("course_type"),
                "starting_date": edu.get("starting_date"),
                "ending_date": edu.get("ending_date"),
                "ongoing": bool(edu.get("ongoing")),
                "city": edu.get("city"),
                "state": edu.get("state"),
                "country": edu.get("country"),
                "ishighest": bool(edu.get("ishighest"))
            })

    return clean_edu


def extract_profile_certificates(user_profile_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = user_profile_data.get("data") if isinstance(user_profile_data, dict) else {}
    if not isinstance(data, dict):
        data = {}

    raw_cert = data.get("all_certificate") if isinstance(data.get("all_certificate"), list) else []
    clean_cert = []

    for cert in raw_cert:
        if isinstance(cert, dict):
            clean_cert.append({
                "id": str(cert.get("id")) if cert.get("id") is not None else None,
                "university": cert.get("university"),
                "course": cert.get("course"),
                "start_date": cert.get("start_date"),
                "end_date": cert.get("end_date")
            })

    return clean_cert


def extract_profile_languages(user_profile_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extracts languages from user-profile.data.all_languages[]
    """
    data = user_profile_data.get("data") if isinstance(user_profile_data, dict) else {}
    if not isinstance(data, dict):
        data = {}

    raw_lang = data.get("all_languages") if isinstance(data.get("all_languages"), list) else []
    clean_lang = []

    for lang in raw_lang:
        if isinstance(lang, dict) and lang.get("name"):
            name_clean = str(lang.get("name")).strip().rstrip(",")
            clean_lang.append({
                "id": str(lang.get("id")) if lang.get("id") is not None else None,
                "name": name_clean,
                "verbal": str(lang.get("verbal")) if lang.get("verbal") is not None else None,
                "written": str(lang.get("written")) if lang.get("written") is not None else None
            })

    return clean_lang


def extract_profile_portfolio(user_profile_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extracts portfolio items from user-profile.data.portfolio[]
    """
    data = user_profile_data.get("data") if isinstance(user_profile_data, dict) else {}
    if not isinstance(data, dict):
        data = {}

    raw_port = data.get("portfolio") if isinstance(data.get("portfolio"), list) else []
    clean_port = []

    for port in raw_port:
        if isinstance(port, dict):
            clean_port.append({
                "id": str(port.get("id")) if port.get("id") is not None else None,
                "title": port.get("title"),
                "description": port.get("description"),
                "url": port.get("url"),
                "image": port.get("image")
            })

    return clean_port


def extract_profile_summary(user_profile_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extracts profile summary overview from user-profile.data
    """
    data = user_profile_data.get("data") if isinstance(user_profile_data, dict) else {}
    if not isinstance(data, dict):
        data = {}

    return {
        "name": f"{data.get('fname', '')} {data.get('lname', '')}".strip(),
        "profile_description": data.get("profile_description"),
        "current_position_name": data.get("still_working_position_name"),
        "current_company_name": data.get("still_working_company_name"),
        "city_name": data.get("city_name"),
        "state_name": data.get("state_name"),
        "country_name": data.get("country_name"),
        "email": data.get("email"),
        "phone": data.get("phone"),
        "dob": data.get("dob"),
        "work_status_name": data.get("work_status_name"),
        "skills": [s.get("skill") for s in extract_profile_skills(user_profile_data) if s.get("skill")]
    }


# Main Extraction Router Enforcing Field Allowlist
def extract_by_intent(intent: UserIntent, api_data: Dict[str, Any]) -> Dict[str, Any]:
    if intent == UserIntent.PROFILE_INCOMPLETE:
        extracted = extract_profile_incomplete(api_data)
    elif intent == UserIntent.USER_DETAIL_NOTICES:
        extracted = extract_user_detail_notices(api_data)
    elif intent == UserIntent.USER_DETAIL_REMINDERS:
        extracted = extract_user_detail_reminders(api_data)
    elif intent == UserIntent.EMPLOYMENT_HISTORY:
        extracted = {"employment_history": extract_employment_history(api_data)}
    elif intent == UserIntent.EMPLOYMENT_SKILLS:
        extracted = {"employment_skills": extract_employment_skills(api_data)}
    elif intent == UserIntent.EMPLOYMENT_SALARY:
        extracted = {"employment_salary": extract_employment_salary(api_data)}
    elif intent in (UserIntent.WIDGET_CLOSING_SOON, UserIntent.WIDGET_NEARBY_ORGS, UserIntent.WIDGET_ALL):
        extracted = {"widgets": extract_widget_data(api_data)}
    elif intent == UserIntent.PROFILE_SKILLS:
        extracted = {"profile_skills": extract_profile_skills(api_data)}
    elif intent == UserIntent.PROFILE_EDUCATION:
        extracted = {"education": extract_profile_education(api_data)}
    elif intent == UserIntent.PROFILE_CERTIFICATES:
        extracted = {"certificates": extract_profile_certificates(api_data)}
    elif intent == UserIntent.PROFILE_LANGUAGES:
        extracted = {"languages": extract_profile_languages(api_data)}
    elif intent == UserIntent.PROFILE_PORTFOLIO:
        extracted = {"portfolio": extract_profile_portfolio(api_data)}
    elif intent == UserIntent.PROFILE_SUMMARY:
        extracted = {"profile_summary": extract_profile_summary(api_data)}
    else:
        extracted = {}

    # Final Security Scrubbing Guard
    return scrub_sensitive_keys(extracted)
