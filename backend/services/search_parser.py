import asyncio
import json
import logging
import re
import httpx
from typing import Dict, Any, Optional

try:
    from backend.services.llm_service import LLMService
    from backend.repositories.db import get_db
except ModuleNotFoundError:
    from services.llm_service import LLMService  # type: ignore
    from repositories.db import get_db  # type: ignore

log = logging.getLogger(__name__)

async def parse_search_query(message: str, role: str, messages: list = None, user_location: Optional[str] = None) -> Dict[str, Any]:
    """
    Parses unstructured user message into structured search criteria using the LLM.
    """
    # Strip UI emojis/prefixes (e.g. 🌐, 💰, 📍, 💼, 🔍, ⚡, ⭐)
    clean_message = re.sub(r'[🌐💰📍💼🔍⚡⭐]', '', message or '').strip()
    user_loc_info = f"\nUser's Stored/Current Resolved Location: {user_location}" if user_location else ""
    prompt = f"""
    You are an AI assistant designed to extract search criteria from user messages for a career portal search.
    The user is a/an {role}.{user_loc_info}

    ## MULTILINGUAL EXTRACTION RULES
    - The user message may be written in English, Hindi (Devanagari e.g., "दिल्ली में Python Developer की नौकरी", "सॉफ्टवेयर डेवलपर", "घर से काम करने वाली नौकरियां"), Hinglish (Hindi in English script e.g., "Delhi mein Python developer ki job"), or mixed.
    - CRITICAL ANTI-HALLUCINATION RULE: Never convert unknown or ambiguous location words into known cities (e.g., converting "XYZ" -> "Delhi" is strictly prohibited). Unknown must remain unknown. Extract the literal location or return null depending on current parser behavior.
    - NO HARDCODED DICTIONARIES: Perform native semantic extraction into canonical English strings. Do not rely on hardcoded job title translation tables.
    
    Extract the following details from the user's message into canonical English representations:
    - keyword: The designation, job title, or role name in canonical English (e.g., "सॉफ्टवेयर डेवलपर" -> "Software Developer", "डाटा एनालिस्ट" -> "Data Analyst", "sales manager", "python developer"). CRITICAL: If a specific role or job title is mentioned anywhere in the user's message (e.g., "suggest me data analyst jobs", "show me civil engineer roles"), you MUST extract that exact title (e.g., "data analyst") into keyword. ONLY return null for keyword if NO specific role or job title is mentioned (e.g. "suggest me jobs", "show jobs for my profile").
    - location: The state or city name EXPLICITLY mentioned in the user's message text, strictly in canonical English (e.g., "दिल्ली" -> "Delhi", "गुरुग्राम" -> "Gurugram", "नोएडा" -> "Noida", "मुंबई" -> "Mumbai", "बैंगलोर" -> "Bangalore", "pune", "maharashtra").
    - skill_source: "PROFILE" if the user explicitly asks for jobs based on their skills/profile (e.g., "jobs according to my skills", "jobs for me", "मेरी प्रोफाइल के अनुसार", "मेरे स्किल्स के हिसाब से"); "QUERY" if explicit skill keywords/roles are mentioned in message; otherwise "NONE".
    - location_source: "QUERY" if a city/state is explicitly mentioned in message; "CURRENT" if "near me", "nearby", or "around me" is used; otherwise "NONE".
    - use_profile_skills: True if the user asks for jobs "according to my skills" or "for my profile" (or Hindi equivalents), otherwise False.
    - skills: A list of canonical skill, domain, or functional competency keywords requested or implied by the query/role across any field (e.g. for "Node.js Developer" -> ["Node.js"]; for "HR Manager" -> ["HR", "Human Resources", "Recruitment"]; for "Tender Executive" -> ["Tender", "Bidding", "Procurement"]; for "Accountant" -> ["Accounting", "GST", "Audit"]; for "Python Developer" -> ["Python"]; for "Civil Engineer" -> ["Civil", "Construction"]). Extract core technology, domain, or functional skills implied by the role or query.
    - experience: Years of experience required (e.g., 0 for "fresher" or "freshers" or "no experience", 2 for "2 years", 5 for "5 years", converting Hindi number words e.g. "दो" -> 2, "तीन" -> 3, "पांच" -> 5, "दस" -> 10, "3 साल से ज्यादा" -> 3, "पांच साल से अधिक" -> 5).
    - salary: Minimum salary numeric value or salary bracket if mentioned (e.g., 15000, converting Hindi salary terms e.g. "दस लाख से ज्यादा" -> 1000000).
    - job_mode: One of "office", "home" (for remote/WFH/घर से काम), or "hybrid".
    - urgent: True if they explicitly mention needing someone urgently, immediately, or fast, otherwise False.
    - sort: If they ask to sort/order by something, identify if they want:
      - "experience" (e.g., "sort by experience", "highest experience first")
      - "create_ts" (e.g., "most recent", "newest", "latest")
      - "salary" (e.g., "highest paying", "best salary")
      - Otherwise null.
    - company: The company, employer, or organization name EXPLICITLY mentioned in the user's message text (e.g., "jobs at Infosys" -> "Infosys", "people working at Google" -> "Google", "candidates from TCS" -> "TCS"). ONLY return null if no company is explicitly mentioned.
    - language_preference: "hindi" if user explicitly requests responses in Hindi (e.g. "हिंदी में जवाब दो", "reply in hindi"); "english" if user explicitly requests responses in English (e.g. "reply in english"); "hinglish" if explicitly requested; otherwise null.
    is_search_intent: Preserve the existing parser's semantics for determining search intent, while ensuring Hindi/Hinglish/mixed-language job-search requests are classified consistently with equivalent English requests. Meta language instructions such as "हिंदी में जवाब दो" or "Reply in English" must remain non-search intents (False).
    - is_knowledge_intent: True ONLY if the user's message is a knowledge, explanation, or detail inquiry (e.g. "What skills are required for the first job?", "Tell me more about this job", "What does the company do?"). Must be False for structured search/filter queries (e.g., "show me Python jobs in Delhi").

    Response MUST be a single raw JSON object matching this schema:
    {{
      "keyword": "string or null",
      "location": "string or null",
      "company": "string or null",
      "skill_source": "PROFILE" | "QUERY" | "NONE",
      "location_source": "QUERY" | "CURRENT" | "NONE",
      "use_profile_skills": boolean,
      "skills": ["string"] or null,
      "experience": int or null,
      "salary": int or null,
      "salary_min": int or null,
      "salary_max": int or null,
      "job_mode": "string or null",
      "urgent": boolean or null,
      "sort": "string or null",
      "clear_filters": ["string"] or null,
      "language_preference": "hindi" | "english" | "hinglish" | null,
      "is_search_intent": boolean,
      "is_recommendation_intent": boolean,
      "is_knowledge_intent": boolean
    }}
    """
    
    history_str = ""
    if messages:
        recent = messages[-5:]
        formatted_msgs = []
        for m in recent:
            if isinstance(m, dict):
                formatted_msgs.append(f"{str(m.get('role', 'user')).capitalize()}: {m.get('content', '')}")
            elif hasattr(m, 'role') and hasattr(m, 'content'):
                formatted_msgs.append(f"{str(getattr(m, 'role', 'user')).capitalize()}: {getattr(m, 'content', '')}")
            else:
                formatted_msgs.append(f"User: {str(m)}")
        history_str = "\n    Conversation History:\n    " + "\n    ".join(formatted_msgs) + "\n"

    prompt += f"""{history_str}
    User message: "{clean_message}"
    
    If the user's message is a refinement follow-up (e.g. "only in Delhi", "fresher jobs"), preserve relevant filters from conversation history. CRITICAL: If the user message is a general recommendation query ("show me jobs according to my skills", "jobs according to my skills", "jobs for me"), location MUST be null! Do NOT carry over a previous location (like "Delhi") or previous job title when the user specifically asks for jobs according to their skills. Do NOT carry over a specific previous job title (like "python developer") into a general near-me or profile search.
    JSON:
    """
    try:
        completion = await LLMService.get_chat_completion(prompt, [], timeout=12.0)
        cleaned_completion = completion.strip()
        if "```json" in cleaned_completion:
            cleaned_completion = cleaned_completion.split("```json")[1].split("```")[0].strip()
        elif "```" in cleaned_completion:
            cleaned_completion = cleaned_completion.split("```")[1].split("```")[0].strip()
        
        parsed = json.loads(cleaned_completion)
        msg_l = clean_message.lower()
        if "हिंदी में जवाब" in msg_l or "reply in hindi" in msg_l or "respond in hindi" in msg_l:
            parsed["language_preference"] = "hindi"
            parsed["is_search_intent"] = False
        elif "reply in english" in msg_l or "respond in english" in msg_l or "अंग्रेजी में जवाब" in msg_l:
            parsed["language_preference"] = "english"
            parsed["is_search_intent"] = False
        elif "reply in hinglish" in msg_l or "hinglish में जवाब" in msg_l:
            parsed["language_preference"] = "hinglish"
            parsed["is_search_intent"] = False

        # Phase 12A: Deterministic Seniority Level Extraction & Core Title Preservation
        seniority_terms = ["senior", "junior", "lead", "principal", "fresher"]
        extracted_seniority = None
        
        # Check explicit qualitative terms in clean message
        for term in seniority_terms:
            if re.search(rf"\b{term}\b", msg_l):
                extracted_seniority = term
                break
        
        parsed["seniority_level"] = extracted_seniority

        # Manager Preservation Rule: Do NOT strip Manager from core role titles like "HR Manager", "Product Manager", etc.
        raw_keyword = parsed.get("keyword")
        if raw_keyword and isinstance(raw_keyword, str):
            clean_kw = raw_keyword
            if extracted_seniority and re.search(rf"\b{extracted_seniority}\b", clean_kw, flags=re.IGNORECASE):
                clean_kw = re.sub(rf"\b{extracted_seniority}\b", "", clean_kw, flags=re.IGNORECASE).strip()
                clean_kw = re.sub(r"\s+", " ", clean_kw)
            parsed["keyword"] = clean_kw if clean_kw else raw_keyword

        parsed["search_target"] = normalize_search_target(parsed.get("search_target"), clean_message, role=role)
        return parsed
    except (httpx.TimeoutException, asyncio.TimeoutError):
        log.warning("extraction_timeout parser=search_parse")
        msg_l = message.lower()
        lang_p = "hindi" if ("हिंदी में जवाब" in msg_l or "reply in hindi" in msg_l) else ("english" if ("reply in english" in msg_l or "respond in english" in msg_l) else None)
        target = normalize_search_target(None, message, role=role)
        return {
            "keyword": message,
            "location": None,
            "skills": None,
            "experience": None,
            "salary": None,
            "job_mode": None,
            "urgent": False,
            "sort": None,
            "language_preference": lang_p,
            "is_search_intent": False if lang_p else True,
            "search_target": target,
        }
    except Exception as e:
        log.exception("Error parsing search query with LLM")
        return {
            "keyword": None,
            "location": None,
            "skills": None,
            "experience": None,
            "salary": None,
            "job_mode": None,
            "urgent": False,
            "sort": None,
            "is_search_intent": False,
            "search_target": normalize_search_target(None, message, role=role),
        }


def normalize_search_target(raw_target: Optional[str], message: str, role: str = "employee") -> str:
    """
    Normalizes search target using deterministic role-aware precedence rules:

    For EMPLOYER role:
    1. Explicit Job Indicators ("job", "jobs", "vacancy", "vacancies", "opening", "openings", "position", "positions", "opportunity", "opportunities") -> JOBS
    2. Explicit Candidate / Hiring Intent ("hire", "hiring", "recruit", "candidate", "candidates", "profile", "profiles", "resumes") -> CANDIDATE_SEARCH
    3. Professional Role / Title (technical or non-technical: e.g. "nodejs developer", "python engineer", "HR manager", "sales executive", "accountant") AND no explicit job indicator -> CANDIDATE_SEARCH
    4. Ambiguous / raw target fallback -> CANDIDATE_SEARCH if raw_target == "CANDIDATE_SEARCH" else UNKNOWN

    For EMPLOYEE role:
    1. Explicit Candidate / Hiring Intent -> CANDIDATE_SEARCH
    2. Explicit Job Terms or Professional Role Nouns -> JOBS
    3. Ambiguous -> UNKNOWN
    """
    clean_text = re.sub(r'[🌐💰📍💼🔍⚡⭐]', '', message or '')
    msg_lower = clean_text.lower().strip()
    clean_role = (role or "employee").lower().strip()

    job_patterns = [
        r"\bjobs?\b", r"\bvacanc(?:y|ies)\b", r"\bopenings?\b", r"\bpositions?\b",
        r"\bopportunit(?:y|ies)\b", r"\bjobs? for\b", r"\bjob require\b", r"\bjobs? at\b", r"\bjob mode\b"
    ]
    has_explicit_job_indicator = any(re.search(p, msg_lower) for p in job_patterns)

    hiring_patterns = [
        r"\bhire\b", r"\bhiring\b", r"\bcandidates?\b", r"\bto hire\b", r"\bfor hiring\b",
        r"\brecruit(?:ing)?\b", r"\bresumes?\b", r"\bprofiles?\b", r"\bdeveloper profiles?\b",
        r"\bengineer profiles?\b"
    ]
    has_explicit_hiring_indicator = any(re.search(p, msg_lower) for p in hiring_patterns)

    professional_role_patterns = [
        r"\bdevelopers?\b", r"\bengineers?\b", r"\banalysts?\b", r"\bdesigners?\b", r"\barchitects?\b",
        r"\bmanagers?\b", r"\bexecutives?\b", r"\bspecialists?\b", r"\baccountants?\b", r"\bofficers?\b",
        r"\bconsultants?\b", r"\bcoordinators?\b", r"\bprofessionals?\b", r"\bleads?\b", r"\bsupervisors?\b",
        r"\bhrs?\b", r"\bhuman resources?\b", r"\brecruiters?\b", r"\bresearchers?\b", r"\badministrators?\b",
        r"\btechnicians?\b", r"\bauditors?\b", r"\bprogrammers?\b", r"\bcoders?\b", r"\badvisors?\b",
        r"\bassociates?\b", r"\brepresentatives?\b", r"\bdirectors?\b"
    ]
    has_professional_role = any(re.search(p, msg_lower) for p in professional_role_patterns)

    person_lookup_patterns = [
        r"\b(?:show|find|list|get|search|look for|see)\b.*?\b(?:people|employees|staff|colleagues|team members)\b",
        r"\b(?:people|employees|staff|colleagues|team members)\b.*?\b(?:working|at|in|with|for)\b",
        r"\b(?:people|employees|staff|colleagues|team members)\b.*?\b(?:near|nearby|around)\b"
    ]
    has_person_lookup_intent = any(re.search(p, msg_lower) for p in person_lookup_patterns)

    if clean_role == "employer":
        if has_explicit_job_indicator and not has_explicit_hiring_indicator:
            return "JOBS"
        if has_explicit_hiring_indicator or has_professional_role:
            return "CANDIDATE_SEARCH"
        if raw_target in ("CANDIDATE_SEARCH", "JOBS", "PEOPLE_SEARCH"):
            return "CANDIDATE_SEARCH" if raw_target == "CANDIDATE_SEARCH" else raw_target
        return "UNKNOWN"

    else:
        # Employee Role logic
        # Priority 1: Explicit Job Indicator ALWAYS wins for Employee JOBS (Strict Precedence)
        if has_explicit_job_indicator:
            return "JOBS"

        # Priority 2: Explicit Hiring / Candidate / Profile Indicator
        if has_explicit_hiring_indicator:
            return "PEOPLE_SEARCH"

        # Priority 3: Professional Role / Person Title (without explicit job indicator)
        if has_professional_role:
            return "PEOPLE_SEARCH"

        # Priority 4: Dedicated Person/Employee Lookup Context (e.g. "show me people working at CollarCheck")
        if has_person_lookup_intent:
            return "PEOPLE_SEARCH"

        if raw_target in ("JOBS", "PEOPLE_SEARCH", "CANDIDATE_SEARCH", "UNKNOWN"):
            return raw_target
        return "UNKNOWN"

try:
    from backend.services.location_service import resolve_location_from_db, LocationResult
except ModuleNotFoundError:
    from services.location_service import resolve_location_from_db, LocationResult  # type: ignore

def resolve_state_id(state_name: str) -> Optional[int]:
    """
    Delegates to location_service to resolve state ID if applicable.
    """
    if not state_name:
        return None
    loc_res = resolve_location_from_db(state_name)
    return loc_res.state_id if loc_res.resolved and loc_res.location_type == "state" else None
