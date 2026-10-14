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

PROFILE_TRIGGERS = [
    r"\baccording to my skills\b",
    r"\bbased on my skills\b",
    r"\bmatching my skills\b",
    r"\bmatching my profile\b",
    r"\baccording to my profile\b",
    r"\bbased on my profile\b",
    r"\bsuitable for me\b",
    r"\bjobs for me\b",
    r"\bmatching my experience\b",
    r"\bfor my profile\b",
    r"\bfor me\b",
    r"\bmy profile\b",
    r"\bmy skills\b",
    r"\bmy background\b",
    r"\baccording to my experience\b",
    r"\bbased on my experience\b",
    r"\bbased on my background\b",
    r"\bmatching my background\b",
    r"\bjobs for my skills\b",
    r"\bjobs for my experience\b",
    r"\bjobs suitable for me\b",
    r"\bsuitable jobs for me\b",
    r"\bas per my skills\b",
    r"\bas per my profile\b",
    r"\brelevant to my skills\b",
    r"\brelevant to my profile\b",
    r"\bsuited for my skills\b",
    r"\bsuited for my profile\b",
]

PROFILE_ADVICE_TRIGGERS = [
    r"\b(?:how\s+(?:can|do|to)\s+i\s+)?(?:improve|increase|boost|raise|better)\b.*?\b(?:profile|rating|score)\b",
    r"\bhow\s+(?:can|do|to)\s+i\s+complete\b.*?\bprofile\b",
    r"\b(?:improve|increase|boost|raise)\s+(?:my\s+)?(?:collarcheck\s+)?profile(?:\s+(?:rating|score|level))?\b",
    r"\bcomplete\s+(?:my\s+)?(?:collarcheck\s+)?profile\b",
    r"\b(?:profile\s+(?:rating|score))\b",
]

def is_self_profile_advice_query(msg: str) -> bool:
    """
    Checks if message is an informational query seeking advice on self profile improvement/score/rating/completion.
    """
    if not msg:
        return False
    msg_lower = msg.lower()
    return any(re.search(p, msg_lower) for p in PROFILE_ADVICE_TRIGGERS)

URGENT_PHRASES = ["urgent", "urgently", "urgent hiring", "hiring urgently", "closing soon", "urgent job", "urgent jobs", "urgent opening", "urgent openings", "urgent position", "urgent positions", "urgent vacancy", "urgent vacancies"]
NON_URGENT_PHRASES = ["not urgent", "non urgent", "non-urgent", "normal job", "normal jobs", "regular job", "regular jobs"]


def resolve_job_role_type_id(emp_type_input: Any) -> Optional[int]:
    """
    Maps job employment type input string/int to cyb_role_types ID:
    1: Full Time, 2: Part Time, 3: Internship
    """
    if emp_type_input is None:
        return None
    if isinstance(emp_type_input, int):
        return emp_type_input if 1 <= emp_type_input <= 3 else None
    s = str(emp_type_input).strip().lower()
    if not s:
        return None
    if "full" in s:
        return 1
    elif "part" in s:
        return 2
    elif "intern" in s:
        return 3
    return None


async def parse_search_query(message: str, role: str, messages: list = None, user_location: Optional[str] = None, session_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Parses unstructured user message into structured search criteria using the LLM.
    """
    # Strip UI emojis/prefixes (e.g. 🌐, 💰, 📍, 💼, 🔍, ⚡, ⭐)
    clean_message = re.sub(r'[🌐💰📍💼🔍⚡⭐]', '', message or '').strip()
    # Normalize standalone 'asa' typo preposition to 'as a'
    clean_message = re.sub(r'\basa\b', 'as a', clean_message, flags=re.IGNORECASE)
    user_loc_info = f"\nUser's Stored/Current Resolved Location: {user_location}" if user_location else ""
    prompt = f"""
    You are an AI assistant designed to extract search criteria from user messages for a career portal search.
    The user is a/an {role}.{user_loc_info}

    ## MULTILINGUAL EXTRACTION RULES
    - The user message may be written in English, Hindi (Devanagari e.g., "दिल्ली में Python Developer की नौकरी", "सॉफ्टवेयर डेवलपर", "घर से काम करने वाली नौकरियां"), Hinglish (Hindi in English script e.g., "Delhi mein Python developer ki job"), or mixed.
    - CRITICAL ANTI-HALLUCINATION RULE: Never convert unknown or ambiguous location words into known cities (e.g., converting "XYZ" -> "Delhi" is strictly prohibited). Unknown must remain unknown. Extract the literal location or return null depending on current parser behavior.
    - NO HARDCODED DICTIONARIES: Perform native semantic extraction into canonical English strings. Do not rely on hardcoded job title translation tables.
    
    Extract the following details from the user's message into canonical English representations:
    - keyword: The designation, job title, or role name in canonical English (e.g., "सॉफ्टवेयर डेवलपर" -> "Software Developer", "डाटा एनालिस्ट" -> "Data Analyst", "sales manager", "python developer"). CRITICAL: If a specific role or job title is mentioned anywhere in the user's message (e.g., "suggest me data analyst jobs", "show me civil engineer roles"), you MUST extract that exact title (e.g., "data analyst") into keyword. ONLY return null for keyword if NO specific role or job title is mentioned (e.g. "suggest me jobs", "show jobs for my profile"). Do NOT put employment type (internship, part time, full time) in keyword.
    - location: The state or city name EXPLICITLY mentioned in the user's message text, strictly in canonical English (e.g., "दिल्ली" -> "Delhi", "गुरुग्राम" -> "Gurugram", "नोएडा" -> "Noida", "मुंबई" -> "Mumbai", "बैंगलोर" -> "Bangalore", "pune", "maharashtra").
    - skill_source: "PROFILE" if the user explicitly asks for jobs based on their skills/profile (e.g., "jobs according to my skills", "jobs for me", "मेरी प्रोफाइल के अनुसार", "मेरे स्किल्स के हिसाब से"); "QUERY" if explicit skill keywords/roles are mentioned in message; otherwise "NONE".
    - location_source: "QUERY" if a city/state is explicitly mentioned in message; "CURRENT" if "near me", "nearby", or "around me" is used; otherwise "NONE".
    - use_profile_skills: True if the user asks for jobs "according to my skills" or "for my profile" (or Hindi equivalents), otherwise False.
    - skills: A list of canonical skill, domain, or functional competency keywords requested or implied by the query/role across any field (e.g. for "Node.js Developer" -> ["Node.js"]; for "HR Manager" -> ["HR", "Human Resources", "Recruitment"]; for "Tender Executive" -> ["Tender", "Bidding", "Procurement"]; for "Accountant" -> ["Accounting", "GST", "Audit"]; for "Python Developer" -> ["Python"]; for "Civil Engineer" -> ["Civil", "Construction"]). Extract core technology, domain, or functional skills implied by the role or query. CRITICAL: Do NOT extract generic category meta-words (e.g. "programming", "technical", "database", "frontend", "backend", "skill") as standalone skills when they only describe a specific requested technology (e.g., "Java as a programming skill" -> ["Java"]; "React as a frontend skill" -> ["React"]; "SQL as a database skill" -> ["SQL"]).
    - experience_min: Minimum years of experience required (e.g., 5 for "5+ years", "at least 5 years", "minimum 5 years", "5 years or more", 2 for "2 to 5 years", 0 for "fresher"), otherwise null.
    - experience_max: Maximum years of experience required (e.g., 5 for "at most 5 years", "under 5 years", "less than 5 years", "up to 5 years", "maximum 5 years", 5 for "2 to 5 years"), otherwise null.
    - experience: Years of experience required if a single general number is given, otherwise null.
    - salary: Minimum salary numeric value or salary bracket if mentioned (e.g., 15000, converting Hindi salary terms e.g. "दस लाख से ज्यादा" -> 1000000).
    - job_mode: One of "office", "home" (for remote/WFH/घर से काम), "hybrid", or "field" (for field jobs/field work/fieldwork). Note: "part time" and "full time" are employment types, NOT job_mode! Do not set job_mode for part time or full time unless explicit remote/office/hybrid/field terms are present.
    - employment_type: "internship" (for internship/intern), "part-time" (for part time/part-time), "full-time" (for full time/full-time), otherwise null.
    - urgent: True if they explicitly mention needing someone urgently, immediately, or fast, otherwise False.
    - sort: If they ask to sort/order by something, identify if they want:
      - "experience" (e.g., "sort by experience", "highest experience first")
      - "create_ts" (e.g., "most recent", "newest", "latest")
      - "salary" (e.g., "highest paying", "best salary")
      - Otherwise null.
    - company: The company, employer, or organization name EXPLICITLY mentioned in the user's message text (e.g., "jobs at Infosys" -> "Infosys", "people working at Google" -> "Google", "candidates from TCS" -> "TCS", "Find Vishal Verma from CollarCheck" -> "CollarCheck"). ONLY return null if no company is explicitly mentioned.
    - candidate_name: The full name or first/last name of a specific person being searched for in the message text (e.g. "Find Kunal Yadav" -> "Kunal Yadav", "Show me Priya Sharma" -> "Priya Sharma", "Find Vishal Verma from CollarCheck in Delhi" -> "Vishal Verma"). CRITICAL: Extract ONLY the person's actual name into candidate_name. Do NOT include company names (e.g. "from CollarCheck"), location names (e.g. "in Delhi"), or filter phrases into candidate_name. Return null if searching for general candidates or role titles without a specific person's name (e.g., "Find candidates in Delhi" -> candidate_name is null, "Find Python developers" -> candidate_name is null).
    - search_target: "PEOPLE_SEARCH" if searching for a person by name, candidate, employee, colleague, or professional role without explicit job terms (e.g., "Find Kunal Yadav", "Show me Priya Sharma", "Find candidates with Python", "Find Python developers", "Find Python developers in Delhi"); "JOBS" if explicitly searching for job openings, job roles, or vacancies (e.g., "Find Python jobs", "Developer jobs in Delhi", "show me developer vacancies"); otherwise null.
    - language_preference: "hindi" if user explicitly requests responses in Hindi (e.g. "हिंदी में जवाब दो", "reply in hindi"); "english" if user explicitly requests responses in English (e.g. "reply in english"); "hinglish" if explicitly requested; otherwise null.
    is_search_intent: Preserve the existing parser's semantics for determining search intent, while ensuring Hindi/Hinglish/mixed-language job-search requests are classified consistently with equivalent English requests. Meta language instructions such as "हिंदी में जवाब दो" or "Reply in English" must remain non-search intents (False).
    - skill_operation: "REPLACE" if the user explicitly asks to substitute/replace skills (e.g. "instead find Java candidates", "rather than Python", "switch to Java", "no Python, only Java"); "ADD" if the user explicitly asks to refine additively (e.g. "also require Java", "add Java", "with Java too"); otherwise null.
    - query_type: "NEW_SEARCH" if the message is a complete standalone search query for a new topic or job title (e.g., "Find software developer jobs", "Show marketing roles in Delhi", "I need an accountant job"). "REFINEMENT" if the message is a follow-up narrowing or modifying an existing search (e.g., "only remote", "in Mumbai", "fresher jobs", "with 5 years experience", "under 5 years").
    - clear_filters: If query_type is "NEW_SEARCH" and the user does NOT explicitly request a work_mode/location/experience in the message text, include ["job_mode", "location", "experience"] in clear_filters to prevent carrying over stale filters from previous conversation turns. Otherwise null.

    Response MUST be a single raw JSON object matching this schema:
    {{
      "keyword": "string or null",
      "location": "string or null",
      "company": "string or null",
      "candidate_name": "string or null",
      "search_target": "JOBS" | "PEOPLE_SEARCH" | "CANDIDATE_SEARCH" | null,
      "skill_source": "PROFILE" | "QUERY" | "NONE",
      "location_source": "QUERY" | "CURRENT" | "NONE",
      "use_profile_skills": boolean,
      "skills": ["string"] or null,
      "skill_operation": "ADD" | "REPLACE" | null,
      "experience": int or null,
      "experience_min": int or null,
      "experience_max": int or null,
      "salary": int or null,
      "salary_min": int or null,
      "salary_max": int or null,
      "job_mode": "string or null",
      "employment_type": "string or null",
      "urgent": boolean or null,
      "sort": "string or null",
      "clear_filters": ["string"] or null,
      "query_type": "NEW_SEARCH" | "REFINEMENT" | null,
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
    
    If the user's message is a refinement follow-up (e.g. "only in Delhi", "fresher jobs"), preserve relevant filters from conversation history and set query_type="REFINEMENT". If the user message is a brand-new standalone query (e.g. "Find software developer jobs"), set query_type="NEW_SEARCH" and list stale fields in clear_filters. CRITICAL: If the user message is a general recommendation query ("show me jobs according to my skills", "jobs according to my skills", "jobs for me"), location MUST be null! Do NOT carry over a previous location (like "Delhi") or previous job title when the user specifically asks for jobs according to their skills. Do NOT carry over a specific previous job title (like "python developer") into a general near-me or profile search.
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

        # Urgent Hiring Extraction & Keyword Cleanup
        if any(re.search(rf"\b{re.escape(p)}\b", msg_l) for p in URGENT_PHRASES):
            parsed["urgent"] = True
        elif any(re.search(rf"\b{re.escape(p)}\b", msg_l) for p in NON_URGENT_PHRASES):
            parsed["urgent"] = False
        else:
            parsed["urgent"] = None

        # Profile Skill Intent Extraction & Keyword Cleanup
        if any(re.search(p, msg_l) for p in PROFILE_TRIGGERS):
            parsed["use_profile_skills"] = True
            parsed["skill_source"] = "PROFILE"
            if history_str:
                parsed["query_type"] = "REFINEMENT"

        # Deterministic Job Employment-Type Extraction & Work-Mode Disambiguation
        emp_type = parsed.get("employment_type")
        if re.search(r"\b(internship|internships|intern|interns)\b", msg_l):
            emp_type = "internship"
        elif re.search(r"\b(part\s*time|part-time|parttime)\b", msg_l):
            emp_type = "part-time"
        elif re.search(r"\b(full\s*time|full-time|fulltime)\b", msg_l):
            emp_type = "full-time"

        parsed["employment_type"] = emp_type
        parsed["role_type_id"] = resolve_job_role_type_id(emp_type)

        # Disambiguate Work Mode vs Employment Type
        # Ensure 'part time' doesn't coerce job_mode to 'home' and 'full time' doesn't coerce job_mode to 'office'
        # unless explicit location work_mode keywords are in message text
        explicit_remote = any(k in msg_l for k in ["remote", "wfh", "work from home", "home"])
        explicit_office = any(k in msg_l for k in ["office", "onsite", "site"])
        explicit_hybrid = "hybrid" in msg_l
        explicit_field = any(re.search(rf"\b{re.escape(k)}\b", msg_l) for k in ["field", "fieldwork", "field work", "field job", "field jobs", "field-work"])

        if parsed.get("job_mode") == "home" and not explicit_remote and emp_type in ("part-time", "full-time", "internship"):
            parsed["job_mode"] = None
        elif parsed.get("job_mode") == "office" and not explicit_office and emp_type in ("part-time", "full-time", "internship"):
            parsed["job_mode"] = None

        if explicit_remote:
            parsed["job_mode"] = "home"
        elif explicit_field:
            parsed["job_mode"] = "field"
        elif explicit_hybrid:
            parsed["job_mode"] = "hybrid"
        elif explicit_office:
            parsed["job_mode"] = "office"

        # Manager Preservation Rule & Keyword Cleanup
        raw_keyword = parsed.get("keyword")
        if raw_keyword and isinstance(raw_keyword, str):
            clean_kw = raw_keyword
            if extracted_seniority and re.search(rf"\b{extracted_seniority}\b", clean_kw, flags=re.IGNORECASE):
                clean_kw = re.sub(rf"\b{extracted_seniority}\b", "", clean_kw, flags=re.IGNORECASE).strip()
                clean_kw = re.sub(r"\s+", " ", clean_kw)
            
            # Clean standalone meta terms, employment terms, or generic profile terms from keyword
            emp_terms_pat = r"\b(internships?|interns?|part[\s-]*time|full[\s-]*time|urgent\s+hiring|urgently\s+hiring|urgent\s+jobs?|urgent\s+openings?|urgent\s+positions?|urgent|jobs?|profile|skills?|field\s*work|fieldwork|field)\b"
            if clean_kw.lower().strip() in ("urgent", "urgent hiring", "urgent jobs", "jobs with urgent hiring", "urgent openings", "hiring urgently", "jobs hiring urgently", "jobs", "profile", "skills", "internship", "internships", "intern", "part time", "part-time", "full time", "full-time"):
                clean_kw = None
            elif clean_kw:
                clean_kw = re.sub(emp_terms_pat, "", clean_kw, flags=re.IGNORECASE).strip()
                clean_kw = re.sub(r"\s+", " ", clean_kw)
                clean_kw = clean_kw if clean_kw else None

            parsed["keyword"] = clean_kw

        if parsed.get("use_profile_skills") and parsed.get("keyword") in ("jobs", "profile", "skills", "internship", "internships", "intern", "part time", "full time"):
            parsed["keyword"] = None

        parsed["search_target"] = normalize_search_target(parsed.get("search_target"), clean_message, role=role, session_id=session_id)
        has_job_indicator = any(re.search(p, msg_l) for p in [r"\bjobs?\b", r"\broles?\b", r"\bvacanc(?:y|ies)\b", r"\bopenings?\b", r"\bpositions?\b"])
        if is_self_profile_advice_query(clean_message) and not has_job_indicator:
            parsed["search_target"] = "UNKNOWN"
            parsed["is_search_intent"] = False
            parsed["keyword"] = None
            parsed["use_profile_skills"] = False
            parsed["skill_source"] = "NONE"
        return parsed
    except (httpx.TimeoutException, asyncio.TimeoutError):
        log.warning("extraction_timeout parser=search_parse")
        msg_l = message.lower()
        lang_p = "hindi" if ("हिंदी में जवाब" in msg_l or "reply in hindi" in msg_l) else ("english" if ("reply in english" in msg_l or "respond in english" in msg_l) else None)
        target = normalize_search_target(None, message, role=role)
        is_urg = any(re.search(rf"\b{re.escape(p)}\b", msg_l) for p in URGENT_PHRASES)
        is_prof = any(re.search(p, msg_l) for p in PROFILE_TRIGGERS)
        is_advice = is_self_profile_advice_query(message)
        return {
            "keyword": None if (is_prof or is_advice) else message,
            "location": None,
            "skills": None,
            "experience": None,
            "salary": None,
            "job_mode": None,
            "urgent": is_urg,
            "use_profile_skills": is_prof and not is_advice,
            "skill_source": "PROFILE" if (is_prof and not is_advice) else "NONE",
            "sort": None,
            "language_preference": lang_p,
            "is_search_intent": False if (lang_p or is_advice) else True,
            "search_target": target,
        }
    except Exception as e:
        log.exception("Error parsing search query with LLM")
        msg_l = message.lower() if message else ""
        is_urg = any(re.search(rf"\b{re.escape(p)}\b", msg_l) for p in URGENT_PHRASES)
        is_prof = any(re.search(p, msg_l) for p in PROFILE_TRIGGERS)
        is_advice = is_self_profile_advice_query(message)
        extracted_kw = None
        if not is_prof and not is_advice and message:
            clean_kw = re.sub(r"\b(show\s+me|show|find|search|jobs?|roles?|openings?|vacanc(?:y|ies))\b", "", msg_l, flags=re.IGNORECASE).strip()
            extracted_kw = clean_kw if clean_kw else message
        return {
            "keyword": extracted_kw if (not is_prof and not is_advice) else None,
            "location": None,
            "skills": None,
            "experience": None,
            "salary": None,
            "job_mode": None,
            "urgent": is_urg,
            "use_profile_skills": is_prof and not is_advice,
            "skill_source": "PROFILE" if (is_prof and not is_advice) else "NONE",
            "sort": None,
            "is_search_intent": False if is_advice else (True if (is_urg or is_prof or extracted_kw) else False),
            "search_target": normalize_search_target(None, message, role=role),
        }


def normalize_search_target(raw_target: Optional[str], message: str, role: str = "employee", session_id: Optional[str] = None) -> str:
    """
    Normalizes search target using a Hybrid LLM + Precedence approach:
    1. Check for conversational pagination commands and resolve target from stored session state.
    2. If the LLM has already identified a valid search target (JOBS, PEOPLE_SEARCH, CANDIDATE_SEARCH), validate it against role boundaries.
    3. Fall back to deterministic regex pattern matching when raw_target is missing or UNKNOWN.
    """
    clean_text = re.sub(r'[🌐💰📍💼🔍⚡⭐]', '', message or '')
    msg_lower = clean_text.lower().strip()
    clean_role = (role or "employee").lower().strip()

    pag_patterns = [
        r"\b(?:next\s+page|next\s+jobs|next\s+candidates|show\s+next|show\s+more|more\s+candidates|show\s+more\s+candidates|show\s+me\s+more|more|continue)\b",
        r"\b(?:go\s+to\s+|show\s+)?page\s+\d+\b",
    ]
    is_pag_cmd = any(re.search(p, msg_lower) for p in pag_patterns)
    if is_pag_cmd and session_id:
        try:
            from backend.utils.session_state import get_pagination_state
            p_state = get_pagination_state(session_id, role=clean_role)
            if p_state and p_state.get("result_type") == "people":
                return "PEOPLE_SEARCH"
            elif p_state and p_state.get("result_type") == "jobs":
                return "JOBS"
        except Exception:
            pass

    job_patterns = [
        r"\bjobs?\b", r"\broles?\b", r"\bvacanc(?:y|ies)\b", r"\bopenings?\b", r"\bpositions?\b",
        r"\bopportunit(?:y|ies)\b", r"\bposts?\b", r"\bgigs?\b", r"\bjobs? for\b", r"\bjob require\b",
        r"\bjobs? at\b", r"\bjob mode\b", r"\bwork from (home|office)\b", r"\bremote (work|job)\b"
    ]
    has_explicit_job_indicator = any(re.search(p, msg_lower) for p in job_patterns)

    is_self_profile_query = any(re.search(p, msg_lower) for p in PROFILE_TRIGGERS)
    is_advice_query = is_self_profile_advice_query(clean_text)

    cand_noun_patterns = [
        r"\bcandidates?\b", r"\bresumes?\b", r"\bdeveloper profiles?\b",
        r"\bengineer profiles?\b", r"\bpeople\b", r"\bprofessionals?\b"
    ]
    if not is_self_profile_query and not is_advice_query:
        cand_noun_patterns.append(r"\bprofiles?\b")

    has_candidate_noun = any(re.search(p, msg_lower) for p in cand_noun_patterns)

    hiring_verb_patterns = [
        r"\bhire\b", r"\bhiring\b", r"\bto hire\b", r"\bfor hiring\b", r"\brecruit(?:ing)?\b"
    ]
    has_hiring_verb = any(re.search(p, msg_lower) for p in hiring_verb_patterns)

    has_explicit_hiring_indicator = has_candidate_noun or (has_hiring_verb and not has_explicit_job_indicator)

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
        r"\b(?:show|find|list|get|search|look for|see|can you find)\b.*?\b(?:people|employees|staff|colleagues|team members|person|candidate)\b",
        r"\b(?:people|employees|staff|colleagues|team members)\b.*?\b(?:working|at|in|with|for)\b",
        r"\b(?:people|employees|staff|colleagues|team members)\b.*?\b(?:near|nearby|around)\b",
        r"\b(?:find|search for|show me|look for|can you find|see|want to see)\b\s+[a-zA-Z]+(?:\s+[a-zA-Z]+)?\b"
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
        # Employee Role logic (Hybrid approach: Respect LLM classification first, fallback to regex)
        # Priority 1: Explicit Job Indicator (regex) -> JOBS
        if has_explicit_job_indicator:
            return "JOBS"

        # Informational Self-Profile Advice Rule: Do not classify advice questions as PEOPLE_SEARCH
        if is_advice_query:
            return "UNKNOWN"

        # Priority 1.5: Active Candidate Search Context (if active & no explicit job indicator)
        if session_id:
            try:
                from backend.utils.session_state import get_candidate_search_criteria
                if get_candidate_search_criteria(session_id, role=clean_role):
                    return "PEOPLE_SEARCH"
            except Exception:
                pass

        # Priority 2: Explicit Hiring / Candidate / Profile Indicator -> PEOPLE_SEARCH
        if has_explicit_hiring_indicator:
            return "PEOPLE_SEARCH"

        # Priority 3: LLM-classified Target (if LLM identified JOBS or PEOPLE_SEARCH)
        if raw_target in ("JOBS", "PEOPLE_SEARCH"):
            return raw_target

        # Priority 4: Dedicated Person/Employee Lookup Context
        if has_person_lookup_intent:
            return "PEOPLE_SEARCH"

        # Priority 5: Professional Role without explicit job indicator fallback
        if has_professional_role:
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
