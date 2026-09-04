"""
user_data_formatter.py — Natural language response formatter for user-data and widget queries.

Phase 4 / Fix Scope:
  - Formats extracted JSON into clean, user-facing natural language text.
  - Complete support for LANGUAGES, PORTFOLIO, SALARY, CLOSING_SOON, NEARBY_ORGS, WIDGET_ALL.
  - Company-specific filtering for employment queries.
  - "Previous company" semantics resolution.
  - Zero raw JSON or credential leakage.
"""

import re
import logging
from typing import Dict, Any, List, Optional
from backend.services.user_data_intent_parser import UserIntent
from backend.services.user_data_extractor import FORBIDDEN_KEYS, scrub_sensitive_keys

log = logging.getLogger(__name__)


def _is_safe_public_url(url: Optional[str]) -> bool:
    """
    Validates whether a URL is a safe, user-facing public resource URL.
    Filters out internal API endpoints, localhost/IP addresses, admin panels, and microservice paths.
    """
    if not url or not isinstance(url, str):
        return False

    u_lower = url.strip().lower()

    # Must start with http:// or https://
    if not (u_lower.startswith("http://") or u_lower.startswith("https://")):
        return False

    # Block internal IP addresses, localhost, and internal microservice domains
    internal_hosts = ("localhost", "127.0.0.1", "0.0.0.0", "10.", "192.168.", "172.16.")
    if any(host in u_lower for host in internal_hosts):
        return False

    # Block internal API/admin path patterns
    forbidden_patterns = ("/wapi/", "/api/", "/v1/", "/v2/", "/admin/", "/internal/", "admin.collarcheck.com", ":8000", ":8001", ":8080", ":9000")
    if any(pat in u_lower for pat in forbidden_patterns):
        return False

    return True


def _parse_year_from_date(date_val: Any) -> Optional[int]:
    """Extracts a 4-digit year integer from various date format representations."""
    if not date_val:
        return None
    s = str(date_val).strip()
    match = re.search(r"\b(19\d\d|20\d\d)\b", s)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass
    return None


def _extract_company_name_from_query(
    query: str,
    available_companies: List[str],
    records: Optional[List[Dict[str, Any]]] = None
) -> Optional[str]:
    """
    Matches company names mentioned in prompt using deterministic matching hierarchy:
    1. Exact full containment match (e.g. prompt contains "Google LLC")
    2. Unambiguous partial match (single company matching token from prompt e.g. "Google" -> "Google LLC")
    3. Multiple partial matches -> Ambiguous; do not arbitrarily select one (returns None)
    4. Relative reference resolution ("previous", "last", "recent" company using verified dates)
    5. Explicit unlisted company extraction
    """
    if not query:
        return None

    q_lower = query.lower()

    # 1. Exact full containment match (e.g. prompt explicitly contains "Google LLC")
    if available_companies:
        exact_matches = [comp for comp in available_companies if comp and comp.lower() in q_lower]
        if len(exact_matches) == 1:
            return exact_matches[0]
        elif len(exact_matches) > 1:
            exact_matches.sort(key=len, reverse=True)
            return exact_matches[0]

    # 2. Extract explicit company token from query (e.g. "Google" from "salary at Google")
    match = re.search(r"\b(?:at|for|about|with|company)\s+([A-Za-z0-9_-]+)\b", query, re.IGNORECASE)
    extracted_token = match.group(1) if match else None

    # Fallback token extraction from non-stopword query terms if pattern not found
    if not extracted_token:
        words = [w.strip() for w in re.findall(r"\b[A-Za-z0-9_-]{3,}\b", query) if w.lower() not in {"what", "were", "used", "tell", "show", "that", "this", "from", "with", "about", "your", "have", "skills", "salary"}]
        for word in words:
            if available_companies:
                matching = [comp for comp in available_companies if comp and word.lower() in comp.lower()]
                if len(matching) == 1:
                    return matching[0]
                elif len(matching) > 1:
                    # Ambiguous: multiple companies match token -> do not arbitrarily pick one
                    return None

    if extracted_token and extracted_token.lower() not in {"my", "the", "a", "an", "this", "that", "your", "our", "his", "her", "their", "any", "all", "previous", "last", "recent", "latest"}:
        if available_companies:
            partial_matches = [comp for comp in available_companies if comp and extracted_token.lower() in comp.lower()]
            if len(partial_matches) == 1:
                return partial_matches[0]
            elif len(partial_matches) > 1:
                # Ambiguous: multiple companies match token -> do not arbitrarily pick one
                return None

        # Unlisted company explicitly requested in query
        return extracted_token

    # 3. Relative query resolution ("previous", "last", "prior", "recent", "latest" company)
    if re.search(r"\b(previous|last|prior|recent|latest)\s+company\b", q_lower):
        if records:
            prior_companies = []
            for idx, item in enumerate(records):
                if not isinstance(item, dict):
                    continue

                cname = item.get("company")
                if not cname:
                    continue

                roles = item.get("roles") if isinstance(item.get("roles"), list) else [item]

                is_active = False
                latest_year = 0

                for r in roles:
                    if not isinstance(r, dict):
                        continue

                    still_working = str(r.get("still_working") or "").strip()
                    worked_till = str(r.get("worked_till_date") or "").strip().lower()

                    if still_working in ("1", "true") or worked_till in ("present", "current"):
                        is_active = True

                    end_year = _parse_year_from_date(r.get("worked_till_date"))
                    start_year = _parse_year_from_date(r.get("joining_date"))
                    year_val = end_year or start_year or 0
                    if year_val > latest_year:
                        latest_year = year_val

                if not is_active:
                    prior_companies.append({
                        "company": cname,
                        "year": latest_year,
                        "original_index": idx
                    })

            if prior_companies:
                prior_companies.sort(key=lambda x: (x["year"], -x["original_index"]), reverse=True)
                return prior_companies[0]["company"]

            for item in reversed(records):
                if isinstance(item, dict) and item.get("company"):
                    return item.get("company")

        if available_companies:
            return available_companies[-1] if len(available_companies) > 1 else available_companies[0]

    return None


def format_single_intent(
    intent: UserIntent,
    extracted_data: Optional[Dict[str, Any]],
    api_response: Optional[Dict[str, Any]],
    query: str = ""
) -> str:
    """
    Formats extracted data for a single intent with explicit error vs empty data handling and company filtering.
    """
    if not isinstance(extracted_data, dict):
        extracted_data = {}

    # Recursively scrub Phase 3 FORBIDDEN_KEYS from input payload before any formatting or field interpolation
    extracted_data = scrub_sensitive_keys(extracted_data)
    if isinstance(api_response, dict):
        api_response = scrub_sensitive_keys(api_response)

    # Check for API / HTTP / Auth Failure
    if isinstance(api_response, dict):
        status_code = api_response.get("status_code")
        error_msg = str(api_response.get("error") or "").lower()

        if status_code == 401 or api_response.get("error") == "401" or "unauthorized" in error_msg:
            return "Please log in to view your profile details."
        if status_code == 403 or "forbidden" in error_msg:
            return "You do not have permission to view these profile details."
        if status_code == 404 or "not found" in error_msg:
            return "The requested profile information could not be found."
        if status_code == 500 or "internal" in error_msg:
            return "We experienced a temporary issue loading your profile details."
        if "timeout" in error_msg:
            return "The request timed out. Please try again in a moment."
        if api_response.get("status") is False and (api_response.get("error") or status_code):
            return "I couldn't retrieve your information right now due to a service connection issue."

    try:
        # 1. PROFILE_INCOMPLETE
        if intent == UserIntent.PROFILE_INCOMPLETE:
            percentage = extracted_data.get("profile_percentage")
            uncomplete = extracted_data.get("uncomplete") or []
            incomplete = extracted_data.get("incomplete") or []

            if percentage is None and not uncomplete and not incomplete:
                return "I couldn't find any profile completeness data recorded on your account."

            lines = []
            if percentage is not None:
                lines.append(f"Your profile is **{percentage}% complete**.")

            if uncomplete:
                lines.append("**Missing Sections:**")
                for item in uncomplete:
                    lines.append(f"• {item}")
            elif incomplete:
                lines.append("**Incomplete Details:**")
                for item in incomplete:
                    if isinstance(item, dict):
                        key = item.get("key")
                        val = item.get("value")
                        lines.append(f"• {key} ({val} impact)")
                    else:
                        lines.append(f"• {item}")
            else:
                lines.append("Great job! Your profile details appear to be fully complete.")

            return "\n".join(lines)

        # 2. USER_DETAIL_NOTICES
        elif intent == UserIntent.USER_DETAIL_NOTICES:
            notices = extracted_data.get("noticeEmployments") or []
            notice_period = extracted_data.get("notice_period_name")

            if not notices:
                return "You currently have no active employment notice records on file."

            count = len(notices)
            period_str = f" ({notice_period})" if notice_period else ""
            return f"You are currently on notice for **{count} company record(s)**{period_str} (Company IDs: {', '.join([str(n) for n in notices])})."

        # 3. USER_DETAIL_REMINDERS
        elif intent == UserIntent.USER_DETAIL_REMINDERS:
            has_reminder = extracted_data.get("reminderExperience")
            reminders = extracted_data.get("reminderExperienceList") or []

            if not has_reminder and not reminders:
                return "You have no pending experience reminders at this time."

            lines = ["**Pending Experience Reminders:**"]
            for r in reminders:
                if isinstance(r, dict):
                    comp = r.get("company") or "Company"
                    desig = r.get("designation") or "Position"
                    lines.append(f"• {desig} at Company #{comp}")
                else:
                    lines.append(f"• {r}")

            return "\n".join(lines)

        # 4. EMPLOYMENT_HISTORY
        elif intent == UserIntent.EMPLOYMENT_HISTORY:
            history = extracted_data.get("employment_history") or []
            if not history:
                return "I couldn't find any employment history recorded on your account."

            available_companies = [comp.get("company") for comp in history if isinstance(comp, dict) and comp.get("company")]
            target_company = _extract_company_name_from_query(query, available_companies, records=history)

            if target_company:
                history = [comp for comp in history if isinstance(comp, dict) and comp.get("company") and comp.get("company").lower() == target_company.lower()]

            if not history:
                return f"I couldn't find any employment history recorded for **{target_company}**."

            lines = ["**Your Work Experience:**"]
            for comp in history:
                if not isinstance(comp, dict):
                    continue
                name = comp.get("company") or "Company"
                roles = comp.get("roles") or []
                verified_badge = " ✓ (Verified)" if comp.get("is_verified") else ""
                lines.append(f"• **{name}**{verified_badge}")

                for r in roles:
                    if not isinstance(r, dict):
                        continue
                    desig = r.get("designation") or "Role"
                    j_date = r.get("joining_date") or ""
                    w_date = r.get("worked_till_date") or ("Present" if str(r.get("still_working")) == "1" else "")
                    date_str = f" ({j_date} to {w_date})" if j_date else ""
                    lines.append(f"   - {desig}{date_str}")

            return "\n".join(lines)

        # 5. EMPLOYMENT_SKILLS
        elif intent == UserIntent.EMPLOYMENT_SKILLS:
            emp_skills = extracted_data.get("employment_skills") or []
            if not emp_skills:
                return "I couldn't find any work skills recorded under your employment history."

            available_companies = list(dict.fromkeys([s.get("company") for s in emp_skills if isinstance(s, dict) and s.get("company")]))
            history = extracted_data.get("employment_history") or []
            records = history if history else emp_skills
            target_company = _extract_company_name_from_query(query, available_companies, records=records)

            if target_company:
                emp_skills = [s for s in emp_skills if isinstance(s, dict) and s.get("company") and s.get("company").lower() == target_company.lower()]

            if not emp_skills:
                target_name = target_company or "the specified company"
                return f"I couldn't find any work skills recorded for **{target_name}**."

            skills_by_comp = {}
            for item in emp_skills:
                if not isinstance(item, dict):
                    continue
                comp = item.get("company") or "Previous Employment"
                sname = item.get("name")
                if sname:
                    skills_by_comp.setdefault(comp, []).append(sname)

            lines = ["**Skills Used in Previous Employment:**"]
            for comp, sk_list in skills_by_comp.items():
                lines.append(f"• **{comp}**: {', '.join(sk_list)}")

            return "\n".join(lines)

        # 6. EMPLOYMENT_SALARY
        elif intent == UserIntent.EMPLOYMENT_SALARY:
            salaries = extracted_data.get("employment_salary") or []
            if not salaries:
                return "I couldn't find any salary details recorded under your employment history."

            available_companies = list(dict.fromkeys([s.get("company") for s in salaries if isinstance(s, dict) and s.get("company")]))
            history = extracted_data.get("employment_history") or []
            records = history if history else salaries
            target_company = _extract_company_name_from_query(query, available_companies, records=records)

            if target_company:
                salaries = [s for s in salaries if isinstance(s, dict) and s.get("company") and s.get("company").lower() == target_company.lower()]

            if not salaries:
                target_name = target_company or "the specified company"
                return f"I couldn't find any salary records listed for **{target_name}**."

            lines = ["**Employment Salary Details:**"]
            for item in salaries:
                if not isinstance(item, dict):
                    continue
                comp = item.get("company") or "Company"
                desig = item.get("designation") or "Role"
                sal = item.get("salary")
                mode = item.get("salary_mode") or "Per Annum"
                inhand = item.get("salary_inhand") or ""
                inhand_str = f" ({inhand})" if inhand else ""
                lines.append(f"• **{desig}** at **{comp}**: ₹{sal} {mode}{inhand_str}")

            return "\n".join(lines)

        # 7. EMPLOYMENT_VERIFICATION
        elif intent == UserIntent.EMPLOYMENT_VERIFICATION:
            history = extracted_data.get("employment_history") or []
            if not history:
                return "I couldn't find any employment verification records."

            lines = ["**Employment Verification Details:**"]
            for comp in history:
                if not isinstance(comp, dict):
                    continue
                name = comp.get("company") or "Company"
                is_verif = comp.get("is_verified")
                verif_str = "Verified ✓" if is_verif else "Pending Verification"
                lines.append(f"• **{name}**: {verif_str}")
            return "\n".join(lines)

        # 8. EMPLOYMENT_DOCUMENTS
        elif intent == UserIntent.EMPLOYMENT_DOCUMENTS:
            history = extracted_data.get("employment_history") or []
            if not history:
                return "No employment documents found."

            lines = ["**Work Documents:**"]
            for comp in history:
                if not isinstance(comp, dict):
                    continue
                name = comp.get("company") or "Company"
                roles = comp.get("roles") or []
                for r in roles:
                    if not isinstance(r, dict):
                        continue
                    v_proc = r.get("verificationProcess") if isinstance(r.get("verificationProcess"), dict) else {}
                    doc = v_proc.get("document") or v_proc.get("relieving_letter") or "Experience/Relieving Record"
                    lines.append(f"• **{name}**: {doc}")
            if len(lines) == 1:
                return "No employment documents found."
            return "\n".join(lines)

        # 9. WIDGET_CLOSING_SOON
        elif intent == UserIntent.WIDGET_CLOSING_SOON:
            widgets = extracted_data.get("widgets") or []
            closing_widget = next((w for w in widgets if isinstance(w, dict) and ("closing" in str(w.get("slug")).lower() or "closing" in str(w.get("heading")).lower() or w.get("api_slug") == "auth-all-job")), None)

            items = closing_widget.get("items") if isinstance(closing_widget, dict) and isinstance(closing_widget.get("items"), list) else []
            if not items:
                return "There are currently no positions closing soon on CollarCheck."

            lines = ["**Positions Closing Soon:**"]
            for item in items[:5]:
                if not isinstance(item, dict):
                    continue
                title = item.get("job_title") or "Position"
                comp = item.get("company") or "Employer"
                loc = item.get("location") or "India"
                url = item.get("url")
                link_str = f" - [View Job]({url})" if _is_safe_public_url(url) else ""
                lines.append(f"• **{title}** at **{comp}** ({loc}){link_str}")

            return "\n".join(lines)

        # 10. WIDGET_NEARBY_ORGS
        elif intent == UserIntent.WIDGET_NEARBY_ORGS:
            widgets = extracted_data.get("widgets") or []
            nearby_widget = next((w for w in widgets if isinstance(w, dict) and ("nearby" in str(w.get("slug")).lower() or "near" in str(w.get("heading")).lower() or w.get("api_slug") == "nearby-company")), None)

            items = nearby_widget.get("items") if isinstance(nearby_widget, dict) and isinstance(nearby_widget.get("items"), list) else []
            if not items:
                return "There are currently no nearby organizations listed for your location."

            lines = ["**Organizations Near You:**"]
            for item in items[:5]:
                if not isinstance(item, dict):
                    continue
                name = item.get("company") or item.get("name") or "Organization"
                city = item.get("city_name") or item.get("location") or ""
                dist = item.get("distance")
                dist_str = f" ({dist} km away)" if dist else ""
                ind = item.get("industry_name")
                ind_str = f" - {ind}" if ind else ""
                lines.append(f"• **{name}**{dist_str}{ind_str}")

            return "\n".join(lines)

        # 11. WIDGET_ALL
        elif intent == UserIntent.WIDGET_ALL:
            widgets = extracted_data.get("widgets") or []
            if not widgets:
                return "There are currently no platform widgets available on your dashboard."

            lines = ["**Platform Dashboard Widgets:**"]
            for w in widgets:
                if not isinstance(w, dict):
                    continue
                heading = w.get("heading") or w.get("slug") or "Widget"
                count = w.get("list_count") or 0
                lines.append(f"• **{heading}**: {count} active items")

            return "\n".join(lines)

        # 12. PROFILE_SKILLS
        elif intent == UserIntent.PROFILE_SKILLS:
            prof_skills = extracted_data.get("profile_skills") or []
            if not prof_skills:
                return "I couldn't find any skills listed on your profile."

            skill_names = [s.get("skill") for s in prof_skills if isinstance(s, dict) and s.get("skill")]
            if not skill_names:
                return "I couldn't find any skills listed on your profile."

            return f"**Your Profile Skills:** {', '.join(skill_names)}."

        # 13. PROFILE_EDUCATION
        elif intent == UserIntent.PROFILE_EDUCATION:
            edu_list = extracted_data.get("education") or []
            if not edu_list:
                return "I couldn't find any education background recorded on your profile."
            return "Here are your education details:"

        # 14. PROFILE_CERTIFICATES
        elif intent == UserIntent.PROFILE_CERTIFICATES:
            cert_list = extracted_data.get("certificates") or []
            if not cert_list:
                return "I couldn't find any certificates listed on your profile."

            lines = ["**Your Certifications:**"]
            for cert in cert_list:
                if not isinstance(cert, dict):
                    continue
                course = cert.get("course") or "Certificate"
                uni = cert.get("university") or ""
                uni_str = f" ({uni})" if uni else ""
                lines.append(f"• {course}{uni_str}")

            return "\n".join(lines)

        # 15. PROFILE_LANGUAGES
        elif intent == UserIntent.PROFILE_LANGUAGES:
            lang_list = extracted_data.get("languages") or []
            if not lang_list:
                return "I couldn't find any spoken languages listed on your profile."

            lines = ["**Spoken Languages:**"]
            for lang in lang_list:
                if not isinstance(lang, dict):
                    continue
                name = lang.get("name") or "Language"
                verbal = lang.get("verbal")
                written = lang.get("written")
                levels = []
                if verbal:
                    levels.append(f"Verbal Level {verbal}")
                if written:
                    levels.append(f"Written Level {written}")
                level_str = f" ({', '.join(levels)})" if levels else ""
                lines.append(f"• **{name}**{level_str}")

            return "\n".join(lines)

        # 16. PROFILE_PORTFOLIO
        elif intent == UserIntent.PROFILE_PORTFOLIO:
            port_list = extracted_data.get("portfolio") or []
            if not port_list:
                return "I couldn't find any portfolio entries listed on your profile."

            lines = ["**Your Portfolio & Projects:**"]
            for p in port_list:
                if not isinstance(p, dict):
                    continue
                title = p.get("title") or "Project"
                desc = p.get("description") or ""
                url = p.get("url")
                link_str = f" - [View Portfolio]({url})" if _is_safe_public_url(url) else ""
                desc_str = f": {desc}" if desc else ""
                lines.append(f"• **{title}**{link_str}{desc_str}")

            return "\n".join(lines)

        # 17. PROFILE_SUMMARY
        elif intent == UserIntent.PROFILE_SUMMARY:
            ps = extracted_data.get("profile_summary") if isinstance(extracted_data.get("profile_summary"), dict) else {}
            name = ps.get("name") or "User"
            email = ps.get("email")
            phone = ps.get("phone")
            dob = ps.get("dob")

            loc_parts = [p for p in [ps.get("city_name"), ps.get("state_name"), ps.get("country_name")] if p]
            location = ", ".join(loc_parts) if loc_parts else ps.get("state_name") or ps.get("country_name") or "Not provided"

            work_status = ps.get("work_status_name") or "Not provided"

            pos = ps.get("current_position_name")
            comp = ps.get("current_company_name")
            role_str = f"{pos} at {comp}" if pos and comp else (pos or comp or "Not provided")

            desc = ps.get("profile_description") or ""
            skills = ps.get("skills") or []

            lines = [
                f"Here is your profile information:\n",
                f"**Name:** {name}",
                f"**Email:** {email or 'Not provided'}",
                f"**Phone:** {phone or 'Not provided'}",
                f"**Date of Birth:** {dob or 'Not provided'}",
                f"**Location:** {location}",
                f"**Work Status:** {work_status}",
                f"**Current Role:** {role_str}",
                f"**Profile Description:** {desc}",
            ]

            if skills:
                lines.append("\n**Skills:**")
                for skill in skills:
                    lines.append(f"- {skill}")

            return "\n".join(lines)

    except Exception as exc:
        log.exception("Sanitized failure during single intent formatting: %s", exc)
        return "An unexpected error occurred while processing your request."

    return "Your requested information was processed."


def format_multi_intent_response(
    intents: List[UserIntent],
    extracted_data_by_intent: Dict[UserIntent, Dict[str, Any]],
    api_responses_by_endpoint: Dict[str, Any],
    query: str = ""
) -> str:
    if not intents:
        return "How can I assist you with your profile or employment details?"

    formatted_sections = []
    for intent in intents:
        data = extracted_data_by_intent.get(intent) or {}
        from backend.services.user_data_service import INTENT_ENDPOINT_MAP
        ep_key = INTENT_ENDPOINT_MAP.get(intent)
        api_resp = api_responses_by_endpoint.get(ep_key) or {}

        try:
            section_text = format_single_intent(intent, data, api_resp, query=query)
        except Exception as exc:
            log.exception("Section fault isolated for intent %s: %s", intent, exc)
            section_text = "An unexpected error occurred while processing this section."
        formatted_sections.append(section_text)

    return "\n\n".join(formatted_sections)
