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

import re
import json
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
def parse_optional_bool(val: Any) -> Optional[bool]:
    """
    Parses optional tri-state boolean for metadata presence flags.
    Preserves None for missing/unavailable fields.
    """
    if val is None:
        return None

    if isinstance(val, bool):
        return val

    if isinstance(val, (int, float)):
        if val == 1:
            return True
        if val == 0:
            return False
        return None

    value = str(val).strip().lower()

    if value in ("1", "true", "yes", "t", "y"):
        return True

    if value in ("0", "false", "no", "f", "n"):
        return False

    return None


def is_truthy(val: Any) -> bool:
    """
    Explicit boolean normalization for strings, ints, bools.
    Avoids python's generic bool("false") -> True bug.
    """
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    s = str(val).strip().lower()
    return s in ("1", "true", "yes", "t", "y")


def is_falsy(val: Any) -> bool:
    """
    Explicit boolean false normalization for strings, ints, bools.
    """
    if val is None:
        return False
    if isinstance(val, bool):
        return not val
    if isinstance(val, (int, float)):
        return val == 0
    s = str(val).strip().lower()
    return s in ("0", "false", "no", "f", "n")


def is_current_role(role: Dict[str, Any]) -> bool:
    """
    Checks if a role dictionary represents a current/active role based on
    still_working, worked_till_date ("present"/"current"), or employment_status/status ("current"/"active").
    A date alone does NOT establish a role as current.
    """
    if not isinstance(role, dict):
        return False
    still_w = role.get("still_working")
    worked_till = str(role.get("worked_till_date") or "").strip().lower()
    emp_st = str(role.get("employment_status") or role.get("status") or "").strip().lower()

    return (
        is_truthy(still_w) or
        worked_till in ("present", "current") or
        emp_st in ("current", "active")
    )


def is_previous_role(role: Dict[str, Any]) -> bool:
    """
    Checks if a role dictionary represents a previous/past role based on:
    - still_working is explicit falsy
    - employment_status/status is explicit previous/past/ended/completed
    - worked_till_date is a valid date in the past (year < current_year 2026) or explicitly not present/current
      WHEN not a future end-date without authoritative current status.
    """
    if not isinstance(role, dict):
        return False
    still_w = role.get("still_working")
    worked_till = str(role.get("worked_till_date") or "").strip().lower()
    emp_st = str(role.get("employment_status") or role.get("status") or "").strip().lower()

    if is_falsy(still_w) or emp_st in ("previous", "past", "ended", "completed"):
        return True

    if worked_till and worked_till not in ("present", "current"):
        # Parse year if worked_till_date is formatted as YYYY-MM-DD or YYYY
        m = re.search(r"\b(19\d\d|20\d\d)\b", worked_till)
        if m:
            end_year = int(m.group(1))
            from datetime import datetime
            current_year = datetime.now().year
            if end_year < current_year:
                return True
            # Future end year without still_working=1 or present is unknown, not previous
            return False
        return True

    return False



def _clean_val(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s if s != "" else None


def _is_level_active(val: Any) -> bool:
    if val is True or val == 1 or val == "1":
        return True
    if isinstance(val, str) and val.strip().lower() in ("1", "true"):
        return True
    return False


def normalize_employment_verification(
    raw_approved: Any,
    raw_status: Any,
    verif_process: Any,
    company_is_verified: Any = None,
    company_claim_status: Any = None
) -> Dict[str, Any]:
    """
    Normalizes employment verification status and level detection.

    Level Rules:
      - Level 1 (Work Identity): Record submitted and level1 flag is True in verificationProcess.
      - Level 2 (Company Domain Verification): Company domain verified on CollarCheck.
      - Level 3 (Employment Verification): Role confirmed by employer (approved=='1' & status=='1' or vp.level3=True).
      - Level 4 (Salary Verification): Salary verified (vp.level4=True).

    Claim vs Domain Verification:
      - claim_status == 1 -> Claimed; 0 -> Not Claimed; None -> Unknown
      - is_verified == True -> Domain Verified; False -> Domain Not Verified
    """
    str_app = _clean_val(raw_approved)
    str_stat = _clean_val(raw_status)

    if str_app is None or str_stat is None:
        status = "unknown"
        status_label = "Status Unknown"
        is_verified = False
    elif str_app == "1" and str_stat == "1":
        status = "verified"
        status_label = "Verified"
        is_verified = True
    else:
        status = "not_verified"
        status_label = "Not Verified"
        is_verified = False

    vp = verif_process if isinstance(verif_process, dict) else {}

    # Company Domain Verification state
    comp_verified = False
    if company_is_verified is True or company_is_verified == 1 or company_is_verified == "1":
        comp_verified = True
    elif isinstance(company_is_verified, str) and company_is_verified.strip().lower() in ("1", "true"):
        comp_verified = True
    elif company_is_verified is None:
        comp_verified = _is_level_active(vp.get("level2"))

    # Company Claim status state
    claim_st_val = parse_optional_bool(company_claim_status)
    if claim_st_val is True:
        claim_status_label = "Claimed"
    elif claim_st_val is False:
        claim_status_label = "Not Claimed"
    else:
        claim_status_label = "Unknown"

    # Level booleans:
    # 1. Independent level flags for breakdown:
    if verif_process is None:
        l1_indep = False
        l2_indep = comp_verified
        l3_indep = (str_app == "1" and str_stat == "1")
        l4_indep = False
    else:
        l1_indep = _is_level_active(vp.get("level1")) if vp.get("level1") is not None else False
        l2_indep = _is_level_active(vp.get("level2")) if "level2" in vp else comp_verified
        l3_indep = _is_level_active(vp.get("level3")) if "level3" in vp else (str_app == "1" and str_stat == "1")
        l4_indep = _is_level_active(vp.get("level4"))

    # 2. Sequential gating for progression tracking:
    l1_seq = l1_indep
    l2_seq = l1_seq and l2_indep
    l3_seq = l2_seq and l3_indep
    l4_seq = l3_seq and l4_indep

    highest_seq_level = None
    if l4_seq:
        highest_seq_level = 4
    elif l3_seq:
        highest_seq_level = 3
    elif l2_seq:
        highest_seq_level = 2
    elif l1_seq:
        highest_seq_level = 1

    # Highest independently verified level (highest active level flag in vp/contract)
    highest_indep_level = None
    if l4_indep:
        highest_indep_level = 4
    elif l3_indep:
        highest_indep_level = 3
    elif l2_indep:
        highest_indep_level = 2
    elif l1_indep:
        highest_indep_level = 1

    # Sequential progression gating: out-of-order levels cap sequential completion
    seq_val = highest_seq_level if highest_seq_level is not None else 0
    has_gap = (highest_indep_level is not None and highest_indep_level > seq_val)
    if has_gap:
        highest_completed_level = highest_seq_level
    else:
        highest_completed_level = highest_seq_level if highest_seq_level is not None else highest_indep_level

    highest_level = highest_completed_level








    level_labels = {
        1: "Work Identity",
        2: "Company Domain Verification",
        3: "Employment Verification",
        4: "Salary Verification"
    }

    level_label = f"Level {highest_level}" if highest_level else None
    highest_completed_label = level_labels.get(highest_level)
    highest_indep_label = level_labels.get(highest_indep_level)
    is_declined = (str_stat in ("2", "declined") or str_app in ("declined",))

    # Next Recommended Level: Lowest incomplete level index (1..4)
    # Explicitly declined L3 takes guidance precedence over generic unverified levels
    next_level = None
    if is_declined and not l3_indep:
        next_level = 3
    elif not l1_seq:
        next_level = 1
    elif not l2_seq:
        next_level = 2
    elif not l3_seq:
        next_level = 3
    elif not l4_seq:
        next_level = 4

    next_level_label = level_labels.get(next_level) if next_level else None

    # Determine action owner & guidance
    if next_level is None:
        next_action_owner = "none"
        next_action = "All verification levels (Level 1–4) are completed for this employment record."
        incomplete_reason = None
        guidance_status = "completed"
        next_step = next_action
    elif next_level == 1:
        next_action_owner = "employee"
        next_action = "Work identity verification details are incomplete."
        incomplete_reason = "Work identity verification details are incomplete."
        guidance_status = "action_required"
        next_step = next_action
    elif next_level == 2:
        next_action_owner = "company"
        next_action = "Complete Company Domain Verification. This requires the company/employer to verify its domain on CollarCheck."
        incomplete_reason = "Company domain verification on CollarCheck is pending."
        guidance_status = "waiting_on_company"
        next_step = f"To reach Level 2 (Company Domain Verification), {next_action.lower()}"
    elif next_level == 3:
        if is_declined:
            next_action_owner = "employee"
            next_action = "Employment verification was declined by employer. Contact your employer/HR or request re-verification."
            incomplete_reason = "Employment verification was declined by employer."
            guidance_status = "declined"
            next_step = next_action
        else:
            next_action_owner = "company"
            next_action = "Your employer needs to confirm your employment and designation."
            incomplete_reason = "Role employment confirmation from your employer is pending or not approved."
            guidance_status = "waiting_on_company"
            next_step = f"To reach Level 3 (Employment Verification), {next_action.lower()}"
    elif next_level == 4:
        next_action_owner = "employee"
        next_action = "Provide verified salary evidence for this role."
        incomplete_reason = "Salary verification evidence has not been provided or verified for this role."
        guidance_status = "action_required"
        next_step = f"To reach Level 4 (Salary Verification), {next_action.lower()}"
    else:
        next_action_owner = "employee"
        next_action = "Work identity verification details are incomplete."
        incomplete_reason = "Work identity verification details are incomplete."
        guidance_status = "action_required"
        next_step = next_action

    semantic_contract = {
        "level1": {
            "label": "Work Identity",
            "state": "COMPLETED" if l1_indep else "NOT_COMPLETED",
            "completion_evidence": "verification_process_level1" if vp.get("level1") is not None else "employment_record_present",
            "email_verification_observable": False,
            "employee_action": "Verify official work identity details",
            "company_action": None,
            "primary_actor": "employee",
            "guidance": "Work identity details are verified." if l1_indep else "Work identity verification details are incomplete.",
            "cta_label": "View Employment",
            "cta_url": "https://www.collarcheck.com/dashboard/user/experience",
            "supported_today": True
        },
        "level2": {
            "label": "Company Domain Verification",
            "state": "COMPLETED" if l2_indep else "NOT_COMPLETED",
            "completion_evidence": "company_is_verified" if comp_verified else ("verification_process_level2" if vp.get("level2") else "none"),
            "employee_action": "Request company domain verification on CollarCheck",
            "company_action": "Verify company domain on CollarCheck",
            "primary_actor": "company",
            "guidance": "Company domain is verified." if l2_indep else "Complete Company Domain Verification. This requires the company/employer to verify its domain on CollarCheck.",
            "cta_label": "Request Domain Verification",
            "cta_url": "https://www.collarcheck.com/dashboard/user/experience",
            "supported_today": False
        },
        "level3": {
            "label": "Employment Verification",
            "state": "COMPLETED" if l3_indep else ("DECLINED" if is_declined else "NOT_COMPLETED"),
            "completion_evidence": "role_approved_and_active" if (str_app == "1" and str_stat == "1") else ("verification_process_level3" if vp.get("level3") else ("employer_declined" if is_declined else "none")),
            "employee_action": "Contact employer/HR or request re-verification" if is_declined else "Request employment verification from employer",
            "company_action": "Confirm employment and designation",
            "primary_actor": "employee" if is_declined else "company",
            "guidance": "Employment verified by employer." if l3_indep else ("Employment verification was declined by employer." if is_declined else "Your employer needs to confirm your employment and designation."),
            "cta_label": "Resolve Rejection" if is_declined else "Request Verification",
            "cta_url": "https://www.collarcheck.com/dashboard/user/experience",
            "supported_today": False
        },
        "level4": {
            "label": "Salary Verification",
            "state": "COMPLETED" if l4_indep else "NOT_COMPLETED",
            "completion_evidence": "verification_process_level4" if vp.get("level4") else "none",
            "employee_action": "Provide verified salary evidence for this role",
            "company_action": "Verify salary details",
            "primary_actor": "employee",
            "guidance": "Salary verified." if l4_indep else "Provide verified salary evidence for this role.",
            "cta_label": "Verify Salary",
            "cta_url": "https://www.collarcheck.com/dashboard/user/skills",
            "supported_today": False
        }
    }


    progression = {
        "highest_completed_level": highest_level,
        "highest_completed_label": highest_completed_label,
        "highest_sequential_level": highest_seq_level,
        "highest_independent_verified_level": highest_indep_level,
        "highest_independent_verified_label": highest_indep_label,
        "next_level": next_level,
        "next_level_label": next_level_label,
        "next_action_owner": next_action_owner,
        "next_action": next_action,
        "incomplete_reason": incomplete_reason,
        "guidance_status": guidance_status,
        "semantic_contract": semantic_contract
    }

    return {
        "status": status,
        "status_label": status_label,
        "level": highest_level,
        "level_label": level_label,
        "is_verified": is_verified,
        "highest_completed_level": highest_level,
        "highest_completed_label": highest_completed_label,
        "highest_independent_verified_level": highest_indep_level,
        "highest_independent_verified_label": highest_indep_label,
        "next_level": next_level,
        "next_level_label": next_level_label,
        "next_action_owner": next_action_owner,
        "next_action": next_action,
        "incomplete_reason": incomplete_reason,
        "guidance_status": guidance_status,
        "company_domain_verified": comp_verified,
        "company_claim_status": claim_st_val,
        "company_claim_label": claim_status_label,
        "progression": progression,
        "semantic_contract": semantic_contract,
        "levels_breakdown": {
            "level1": {"completed": l1_indep, "status": "Completed" if l1_indep else "Not Completed"},
            "level2": {"completed": l2_indep, "status": "Completed" if l2_indep else "Not Completed"},
            "level3": {"completed": l3_indep, "status": "Completed" if l3_indep else ("Declined" if is_declined else "Not Completed")},
            "level4": {"completed": l4_indep, "status": "Completed" if l4_indep else "Not Completed"}
        },
        "next_step": next_step
    }



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
        company_claim_status = company_entry.get("claim_status")

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

            raw_approved = role.get("approved")
            raw_status = role.get("status")

            norm_v = normalize_employment_verification(raw_approved, raw_status, clean_verif, is_verified, company_claim_status)

            have_salary = parse_optional_bool(role.get("haveSalary"))
            have_document = parse_optional_bool(role.get("haveDocument"))
            have_review = parse_optional_bool(role.get("haveReview"))
            role_claim_status = role.get("claim_status")
            employment_status = role.get("employment_status")
            department = role.get("department")
            document = role.get("document")

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
                "approved": raw_approved,
                "status": raw_status,
                "status_label": norm_v["status_label"],
                "verificationProcess": clean_verif,
                "verification": norm_v,
                "have_salary": have_salary,
                "have_document": have_document,
                "have_review": have_review,
                "claim_status": role_claim_status,
                "employment_status": employment_status,
                "department": department,
                "document": document
            })

        clean_companies.append({
            "id": str(company_entry.get("id")) if company_entry.get("id") is not None else None,
            "company": company_name,
            "company_id": company_id,
            "is_verified": is_verified,
            "company_claim_status": company_claim_status,
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
            raw_skills_val = role.get("skill")
            if not raw_skills_val:
                continue

            parsed_items: List[Any] = []
            if isinstance(raw_skills_val, list):
                parsed_items = raw_skills_val
            elif isinstance(raw_skills_val, dict):
                parsed_items = [raw_skills_val]
            elif isinstance(raw_skills_val, str):
                s_str = raw_skills_val.strip()
                if s_str:
                    try:
                        parsed_json = json.loads(s_str)
                        if isinstance(parsed_json, list):
                            parsed_items = parsed_json
                        elif isinstance(parsed_json, dict):
                            parsed_items = [parsed_json]
                        elif isinstance(parsed_json, str):
                            parsed_items = [parsed_json]
                    except (json.JSONDecodeError, TypeError, ValueError):
                        parsed_items = [s_str]

            for item in parsed_items:
                extracted_pairs: List[Tuple[str, str]] = []
                if isinstance(item, dict):
                    name_val = str(item.get("name") or item.get("skill") or item.get("title") or item.get("skill_name") or "").strip()
                    skill_id = str(item.get("id")) if item.get("id") is not None else ""
                    if name_val:
                        extracted_pairs.append((skill_id, name_val))
                elif isinstance(item, str):
                    s_item = item.strip()
                    if s_item:
                        if s_item.startswith("[") or s_item.startswith("{"):
                            try:
                                sub_json = json.loads(s_item)
                                if isinstance(sub_json, list):
                                    for sj in sub_json:
                                        if isinstance(sj, dict):
                                            n = str(sj.get("name") or sj.get("skill") or "").strip()
                                            sid = str(sj.get("id")) if sj.get("id") is not None else ""
                                            if n:
                                                extracted_pairs.append((sid, n))
                                        elif isinstance(sj, str) and sj.strip():
                                            extracted_pairs.append(("", sj.strip()))
                                elif isinstance(sub_json, dict):
                                    n = str(sub_json.get("name") or sub_json.get("skill") or "").strip()
                                    sid = str(sub_json.get("id")) if sub_json.get("id") is not None else ""
                                    if n:
                                        extracted_pairs.append((sid, n))
                            except Exception:
                                extracted_pairs.append(("", s_item))
                        elif "," in s_item:
                            for sub in s_item.split(","):
                                sub_c = sub.strip()
                                if sub_c:
                                    extracted_pairs.append(("", sub_c))
                        else:
                            extracted_pairs.append(("", s_item))

                for sk_id, name in extracted_pairs:
                    if not name or name.lower() in {"none", "null", "undefined", "n/a", "na", "-", ""}:
                        continue
                    key = (name.lower(), company_name)
                    if key not in seen_skills:
                        seen_skills.add(key)
                        employment_skills.append({
                            "id": sk_id,
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
                    "salary_mode": role.get("salary_mode"),
                    "bonus": role.get("bonus")
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
    elif intent in (
        UserIntent.EMPLOYMENT_HISTORY,
        UserIntent.EMPLOYMENT_VERIFICATION,
        UserIntent.EMPLOYMENT_COMPANY_CLAIMED,
        UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS,
        UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS,
        UserIntent.EMPLOYMENT_VERIFICATION_WHY_INCOMPLETE,
        UserIntent.EMPLOYMENT_VERIFICATION_OWNERSHIP
    ):
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


def parse_employment_skills_raw(skill_raw_list: List[Any]) -> List[str]:
    """
    Safely parses raw skill entries from cyb_user_experience.skill columns.
    Handles JSON strings, Python dicts/lists, malformed JSON, plain strings, CSVs, missing fields, and whitespace.
    Returns a list of extracted skill display names.
    """
    extracted_names: List[str] = []

    if not skill_raw_list:
        return extracted_names

    for raw_entry in skill_raw_list:
        if raw_entry is None:
            continue

        parsed_items: List[Any] = []
        if isinstance(raw_entry, str):
            s_str = raw_entry.strip()
            if not s_str:
                continue
            try:
                parsed_json = json.loads(s_str)
                if isinstance(parsed_json, list):
                    parsed_items = parsed_json
                elif isinstance(parsed_json, dict):
                    parsed_items = [parsed_json]
                elif isinstance(parsed_json, str):
                    parsed_items = [parsed_json]
            except (json.JSONDecodeError, TypeError, ValueError):
                parsed_items = [s_str]
        elif isinstance(raw_entry, list):
            parsed_items = raw_entry
        elif isinstance(raw_entry, dict):
            parsed_items = [raw_entry]

        for item in parsed_items:
            if isinstance(item, dict):
                name_str = str(item.get("name") or item.get("skill") or item.get("title") or item.get("skill_name") or "").strip()
                if name_str and name_str.lower() not in {"none", "null", "undefined", "n/a", "na", "-", ""}:
                    extracted_names.append(name_str)
            elif isinstance(item, str):
                s_item = item.strip()
                if s_item:
                    if "," in s_item and not (s_item.startswith("[") or s_item.startswith("{")):
                        for sub in s_item.split(","):
                            sub_c = sub.strip()
                            if sub_c and sub_c.lower() not in {"none", "null", "undefined", "n/a", "na", "-", ""}:
                                extracted_names.append(sub_c)
                    else:
                        if s_item.lower() not in {"none", "null", "undefined", "n/a", "na", "-", ""}:
                            extracted_names.append(s_item)

    return extracted_names


def aggregate_user_skills(
    profile_skills: Optional[List[str]] = None,
    employment_skill_raw_list: Optional[List[Any]] = None,
) -> List[str]:
    """
    Pure in-memory aggregation of profile skills + employment skills.
    Preserves display casing of the first encountered skill name.
    Deduplicates case-insensitively while preserving order.
    """
    aggregated: List[str] = []
    seen_lower = set()

    # 1. Profile Skills first
    if profile_skills:
        for sk in profile_skills:
            if not sk:
                continue
            sk_clean = str(sk).strip()
            if not sk_clean:
                continue
            sk_lower = sk_clean.lower()
            if sk_lower not in seen_lower:
                seen_lower.add(sk_lower)
                aggregated.append(sk_clean)

    # 2. Employment Skills next
    if employment_skill_raw_list:
        emp_skill_names = parse_employment_skills_raw(employment_skill_raw_list)
        for emp_sk in emp_skill_names:
            sk_clean = emp_sk.strip()
            if not sk_clean:
                continue
            sk_lower = sk_clean.lower()
            if sk_lower not in seen_lower:
                seen_lower.add(sk_lower)
                aggregated.append(sk_clean)

    return aggregated
