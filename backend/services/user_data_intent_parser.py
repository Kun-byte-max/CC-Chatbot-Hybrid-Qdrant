"""
user_data_intent_parser.py — Centralized deterministic intent classifier for user-data and widget queries.

Phase 1 / Fix Scope:
  - 100% deterministic regex and keyword parsing without LLM fallback.
  - Multi-intent support returning List[UserIntent].
  - Typo tolerance and stemmed keyword matching for education, skills, employment, etc.
  - Strict 4-level Contextual Precedence Hierarchy (Level 1 > Level 2 > Level 3 > Level 4).
"""

import re
from enum import Enum
from typing import List, Set, Optional


def detect_requested_level(text: str) -> Optional[int]:
    """
    Deterministically extracts requested verification level integer (1-4) from query text.
    Returns 1, 2, 3, 4, or None.
    """
    if not text or not text.strip():
        return None

    q_lower = text.lower()
    m_lvl = re.search(
        r"\b(?:complete|how\s+(?:do|can)\s+i\s+complete|how\s+to\s+complete|how\s+do\s+i\s+verify|how\s+to\s+verify)\s+(?:level\s*([1-4])|work\s+identity|company\s+domain|employment\s+verification|salary\s+verification)\b",
        q_lower
    )
    if m_lvl:
        if m_lvl.group(1):
            return int(m_lvl.group(1))
        elif "work identity" in q_lower:
            return 1
        elif "domain" in q_lower:
            return 2
        elif "employment verification" in q_lower:
            return 3
        elif "salary verification" in q_lower:
            return 4

    m_direct = re.search(r"\blevel\s*([1-4])\b", q_lower)
    if m_direct and re.search(r"\b(verify|verification|complete|status|how|check|reach|get)\b", q_lower):
        return int(m_direct.group(1))

    return None



class UserIntent(str, Enum):
    # Level 1 — User Detail Context
    PROFILE_INCOMPLETE = "PROFILE_INCOMPLETE"
    USER_DETAIL_NOTICES = "USER_DETAIL_NOTICES"
    USER_DETAIL_REMINDERS = "USER_DETAIL_REMINDERS"

    # Level 2 — Employment / Company-Specific Context
    EMPLOYMENT_HISTORY = "EMPLOYMENT_HISTORY"
    EMPLOYMENT_SKILLS = "EMPLOYMENT_SKILLS"
    EMPLOYMENT_SALARY = "EMPLOYMENT_SALARY"
    EMPLOYMENT_VERIFICATION = "EMPLOYMENT_VERIFICATION"
    EMPLOYMENT_COMPANY_CLAIMED = "EMPLOYMENT_COMPANY_CLAIMED"
    EMPLOYMENT_VERIFICATION_PROGRESS = "EMPLOYMENT_VERIFICATION_PROGRESS"
    EMPLOYMENT_VERIFICATION_NEXT_STEPS = "EMPLOYMENT_VERIFICATION_NEXT_STEPS"
    EMPLOYMENT_VERIFICATION_WHY_INCOMPLETE = "EMPLOYMENT_VERIFICATION_WHY_INCOMPLETE"
    EMPLOYMENT_VERIFICATION_OWNERSHIP = "EMPLOYMENT_VERIFICATION_OWNERSHIP"
    VERIFICATION_LEVEL_DEFINITION = "VERIFICATION_LEVEL_DEFINITION"
    EMPLOYMENT_DOCUMENTS = "EMPLOYMENT_DOCUMENTS"
    EMPLOYMENT_ACTION_ADD = "EMPLOYMENT_ACTION_ADD"
    EMPLOYMENT_ACTION_INVITE_EMPLOYER = "EMPLOYMENT_ACTION_INVITE_EMPLOYER"
    EMPLOYMENT_ACTION_UPLOAD_SALARY = "EMPLOYMENT_ACTION_UPLOAD_SALARY"

    # Level 3 — Widget Context
    WIDGET_CLOSING_SOON = "WIDGET_CLOSING_SOON"
    WIDGET_NEARBY_ORGS = "WIDGET_NEARBY_ORGS"
    WIDGET_ALL = "WIDGET_ALL"

    # Level 4 — Generic Profile Context
    PROFILE_SKILLS = "PROFILE_SKILLS"
    PROFILE_EDUCATION = "PROFILE_EDUCATION"
    PROFILE_CERTIFICATES = "PROFILE_CERTIFICATES"
    PROFILE_LANGUAGES = "PROFILE_LANGUAGES"
    PROFILE_PORTFOLIO = "PROFILE_PORTFOLIO"
    PROFILE_SUMMARY = "PROFILE_SUMMARY"

import logging

log = logging.getLogger(__name__)


async def parse_user_data_intents_llm(text: str) -> List[UserIntent]:
    """
    LLM-driven intent classifier for complex / conversational user-data & profile queries.
    Uses LLMService to extract matching UserIntent enum values with precision.
    """
    if not text or not text.strip():
        return []

    try:
        import json
        from backend.services.llm_service import LLMService

        system_prompt = """You are an intent classifier for user profile & employment verification queries.
Classify the user query into matching intent strings from this exact list:
- PROFILE_INCOMPLETE: asking about missing info, completion status/percentage
- USER_DETAIL_NOTICES: asking about notice period or notice status
- USER_DETAIL_REMINDERS: asking about pending reminders
- EMPLOYMENT_HISTORY: asking about work history, past companies, employment records
- EMPLOYMENT_SKILLS: asking about skills used at a specific company or past employment
- EMPLOYMENT_SALARY: asking about salary/pay at past or current employment
- EMPLOYMENT_VERIFICATION: asking about verification status of employment or company
- EMPLOYMENT_VERIFICATION_PROGRESS: asking about verification progress
- EMPLOYMENT_VERIFICATION_NEXT_STEPS: asking how to verify or next steps
- EMPLOYMENT_DOCUMENTS: asking about payslips, offer letters, form 16
- PROFILE_SKILLS: asking about overall skills listed on their profile
- PROFILE_EDUCATION: asking about education, degrees, college, courses
- PROFILE_CERTIFICATES: asking about certifications
- PROFILE_LANGUAGES: asking about languages
- PROFILE_PORTFOLIO: asking about portfolio, projects
- PROFILE_SUMMARY: asking for profile summary or overview

If the query is seeking to SEARCH FOR THIRD-PARTY CANDIDATES, EMPLOYEES, OR JOBS (e.g. "Find React developers", "Show jobs in Mumbai", "Hiring candidates"), return an empty list [].
Return ONLY a raw JSON list of matching intent strings, e.g. ["EMPLOYMENT_HISTORY", "EMPLOYMENT_SALARY"]."""

        raw_res = await LLMService.get_chat_completion(
            system_prompt, [{"role": "user", "content": text}], timeout=4.0
        )

        clean_json = raw_res.strip()
        if "```json" in clean_json:
            clean_json = clean_json.split("```json")[1].split("```")[0].strip()
        elif "```" in clean_json:
            clean_json = clean_json.split("```")[1].split("```")[0].strip()

        parsed = json.loads(clean_json)
        if isinstance(parsed, list):
            res_intents = []
            for item in parsed:
                try:
                    res_intents.append(UserIntent(item))
                except Exception:
                    pass
            return res_intents

    except Exception as e:
        log.warning("LLM user data intent parsing skipped: %s", e)

    return []


async def parse_user_data_intents_async(text: str) -> List[UserIntent]:
    """
    Native async intent parser.
    First runs ultra-fast deterministic intent parsing; if no match and text is conversational,
    awaits LLM-driven intent parsing.
    """
    res = parse_user_data_intents_deterministic(text)
    if res:
        return res

    if text and len(text.strip()) > 5:
        llm_res = await parse_user_data_intents_llm(text)
        if llm_res:
            return llm_res

    return []


def parse_user_data_intents(text: str) -> List[UserIntent]:
    """
    Centralized Hybrid Intent Parser (Sync interface).
    First runs ultra-fast deterministic intent parsing.
    If no match and an event loop is running, returns deterministic result to avoid nesting event loops;
    otherwise executes asyncio.run for non-async callers.
    """
    import asyncio
    res = parse_user_data_intents_deterministic(text)
    if res:
        return res

    if text and len(text.strip()) > 5:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            return []

        try:
            return asyncio.run(parse_user_data_intents_llm(text))
        except Exception as e:
            log.warning("LLM user data intent parsing skipped: %s", e)
            return []

    return []


def parse_user_data_intents_deterministic(text: str) -> List[UserIntent]:
    """
    Parses a user query string deterministically and returns matching intents ordered by precedence.
    Supports multi-intent extraction for compound prompts and fuzzy typo tolerance.
    """

    if not text or not text.strip():
        return []

    raw = text.lower().strip()

    # Pre-normalize high-value candidate/people entity typos before applying intent rules
    raw = re.sub(r"\bcandidtes?\b", "candidates", raw)
    raw = re.sub(r"\bcandidte\b", "candidate", raw)
    raw = re.sub(r"\bemplyees?\b", "employees", raw)
    raw = re.sub(r"\bemplyee\b", "employee", raw)
    raw = re.sub(r"\bprofesionals?\b", "professionals", raw)
    raw = re.sub(r"\bprofesional\b", "professional", raw)
    raw = re.sub(r"\bdevelprs?\b", "developers", raw)
    raw = re.sub(r"\bdevelpr\b", "developer", raw)
    raw = re.sub(r"\benginees?\b", "engineers", raw)
    raw = re.sub(r"\bl([1-4])\b", r"level \1", raw)

    # Check if prompt explicitly targets third-party people entities (candidates, developers, employees, users, etc.)
    has_people_entity = bool(re.search(
        r"\b(employees?|candidates?|developers?|engineers?|people|person|profiles?|professionals?|recruits?|applicants?|staff|workers?|users?|who|someone|anyone)\b",
        raw
    ))

    # Contextual self-reference: distinguishing imperative delivery phrasing ("give me", "show me", "find me", "send me", "get me", "list me", "fetch me", "suggest me", "recommend me", "search for me", "look for me") from self-profile ownership ("my", "mine", "myself", "i", "do i have")
    is_imperative_delivery = bool(re.search(r"\b(give|show|find|send|get|list|fetch|suggest|recommend|search(\s+for)?|look(\s+for)?)\s+me\b", raw))
    
    has_explicit_my = bool(re.search(r"\b(my|mine|myself|i)\b", raw))
    has_self_ownership = has_explicit_my or (bool(re.search(r"\bme\b", raw)) and not is_imperative_delivery and not has_people_entity)

    # Dynamic third-person candidate / people search detection:
    # 1. Possessive name query (e.g., "Kunal's profile", "Priya's skills")
    # 2. Third-person subject (employees, candidates, developers, engineers, etc.) with filter/skill context
    # 3. Direct search verbs targeting a third-person entity (non-job, non-self)
    is_possessive_name_query = bool(re.search(r"\b[a-z]+['’]s\s+(profile|skil*s?|educat(io|ion)?|summary|history|experience)\b", raw))
    
    is_people_entity_query = has_people_entity and bool(re.search(
        r"\b(employees?|candidates?|developers?|engineers?|people|person|profiles?|professionals?|recruits?|applicants?|staff|workers?|users?|who|someone|anyone)\b.*?\b(with|wth|wit|skilled|having|knowing|expert|proficient|specializing|using|as\s+a\s+skill|asa?\s+programming\s+skill|experience|skills?)\b",
        raw
    )) and not has_explicit_my

    is_third_person_search_verb = (
        bool(re.search(r"\b(find|search(\s+for)?|show(\s+me)?|give(\s+me)?|look\s+for|get(\s+me)?|list|fetch|suggest(\s+me)?|recommend(\s+me)?)\s+(?:all\s+)?(employees?|candidates?|developers?|engineers?|people|person|profiles?|professionals?|users?|[a-z]+)\b", raw))
        and not has_explicit_my
        and not bool(re.search(r"\b(jobs?|roles?|vacanc|openings?)\b", raw))
    )

    is_third_person_candidate_query = is_possessive_name_query or is_people_entity_query or is_third_person_search_verb
    if is_third_person_candidate_query:
        return []

    # Exclude past/previous employment job queries from job search
    is_past_jobs_query = bool(re.search(r"\b(previous|past|last|old|prior)\s+jobs?\b", raw)) or bool(re.search(r"\b(work\s+experience|employment\s+history)\b", raw))

    # CRITICAL REGRESSION GUARD: Check if user is asking to SEARCH, FIND, RECOMMEND, or SHOW OPEN JOBS
    is_job_search_intent = False
    if not is_past_jobs_query:
        if re.search(r"\b(jobs?|roles?|positions?|vacancies|openings?)\b.*?\b(according\s+to|as\s+per|based\s+on|matching|relevant\s+to|suited\s+for|suitable\s+for|fit\s+for|for|with)\b.*?\b(skills?|profile|experience)\b", raw):
            is_job_search_intent = True
        elif re.search(r"\b(find|search|recommend)\b.*?\b(jobs?|roles?|positions?|vacancies|openings?)\b", raw):
            is_job_search_intent = True
        elif re.search(r"\b(show|get|list)\b.*?\b(remote|active|available|open|hiring|tech|developer|engineer|python|java|react)\s+(jobs?|roles?|vacancies)\b", raw):
            is_job_search_intent = True

    if is_job_search_intent and not re.search(r"\b(what|list|tell|show)\s+(are\s+)?(my|profile)\s+skil*s?\b", raw):
        return []

    intents: Set[UserIntent] = set()

    # -------------------------------------------------------------------
    # LEVEL 1: Missing / Incomplete / Notice / Reminder Context (USER_DETAIL)
    # -------------------------------------------------------------------
    is_verification_level_prompt = bool(re.search(r"\b(level\s*[1-4]|employ?ment\s+verification|verification)\b", raw))
    if re.search(r"\b(missing\s+profile|profile\s+incomplete|incomplete\s+profile|profile\s+complete(ness)?|how\s+complete\s+is\s+my\s+profile|what\s+is\s+missing\s+from\s+my\s+profile|profile\s+completion)\b", raw):
        intents.add(UserIntent.PROFILE_INCOMPLETE)
    elif re.search(r"\b(missing|incomplete|complete(ness)?|percentage|how\s+complete|what\s+is\s+missing)\b", raw) and not is_verification_level_prompt:
        intents.add(UserIntent.PROFILE_INCOMPLETE)

    if re.search(r"\b(notice|notice period|notices|notice employments|company on notice|on notice)\b", raw):
        intents.add(UserIntent.USER_DETAIL_NOTICES)

    if re.search(r"\b(reminder|reminders|experience reminder|pending reminder)\b", raw):
        intents.add(UserIntent.USER_DETAIL_REMINDERS)

    # -------------------------------------------------------------------
    # LEVEL 2: Employment / Company-Specific Context (EMPLOYMENT_*)
    # -------------------------------------------------------------------
    # Employment Skills
    if re.search(r"\bskil*s?.*?\b(used|applied|at|in|during)\b.*?\b(previous|past|last|company|work|job|employ?ment)\b", raw) or re.search(r"\bskil*s?.*?\bfor\b.*?\b(previous|past|last|company|work|job|employ?ment)\b", raw) or "work skill" in raw or "company skill" in raw:
        if not re.search(r"\b(profile\s+skil*s?|skil*s?\s+on\s+my\s+profile)\b", raw) or re.search(r"\b(previous|past|last|company|zenith|work|job)\b", raw):
            intents.add(UserIntent.EMPLOYMENT_SKILLS)

    # Employment Salary
    if re.search(r"\b(sal[a-e]ry|pay|ctc)\b.*?\b(at|in|during|company|previous|last|employ?ment)\b", raw) or re.search(r"\b(my|current|present|remaining)\s+.*?\b(sal[a-e]ry|pay|ctc)\b", raw):
        if not re.search(r"\b(upload|submit|provide|attach|where|how\s+do\s+i\s+complete\s+level\s*4|how\s+can\s+i\s+verify)\b", raw):
            intents.add(UserIntent.EMPLOYMENT_SALARY)

    # Check if query is an instructional / procedural question (e.g. "how can I...", "how to...", "how does...", "what are the ways...")
    is_procedural_question = bool(re.search(
        r"\b(how\s+(can|do|to|should|would|does)|what\s+(is|are)\s+(the\s+)?(process|processes|step|steps|procedure|procedures|way|ways|method|methods|option|options)|ways?\s+to|methods?\s+to|steps?\s+to)\b", 
        raw
    ))

    # Phase 3.1 Action Intents — Employment Addition
    if re.search(
        r"\b(how\s+(do|can|should|to)\s+i\s+)?(add|register|submit|enter|include|put)\s+(my\s+)?(previous|current|past|last|new)?\s*(employ?ment|company|employer|job\s+details|work\s+experience|job|work)\b",
        raw
    ) or re.search(
        r"\b(where\s+(can|do)\s+i\s+)?(add|register|submit)\s+(my\s+)?(employ?ment|company|employer|job\s+details|work\s+experience)\b",
        raw
    ) or re.search(
        r"\bi\s+(want|need)\s+to\s+add\s+(a\s+|(my\s+))?(previous|current|past|new)?\s*(company|employer|employ?ment|job)\b",
        raw
    ):
        intents.add(UserIntent.EMPLOYMENT_ACTION_ADD)

    # Phase 3.1 Action Intents — Invite Employer / Request Employer Verification
    if re.search(
        r"\b(how\s+(can|do|should|to)\s+i\s+)?(ask|invite|get|request|send)\s+(my\s+)?(employer|company|hr|manager)\s+(to\s+)?(verify|approve|confirm)\b",
        raw
    ) or re.search(
        r"\b(how\s+(do|can|to)\s+i\s+)?invite\s+(my\s+)?(hr|employer|company)\b",
        raw
    ) or re.search(
        r"\bi\s+want\s+(my\s+)?(company|employer|hr)\s+to\s+verify\b",
        raw
    ) or re.search(
        r"\bwhere\s+can\s+i\s+request\s+employ?ment\s+verification\b",
        raw
    ) or re.search(
        r"\bhow\s+can\s+i\s+request\s+employ?ment\s+verification\b",
        raw
    ):
        intents.add(UserIntent.EMPLOYMENT_ACTION_INVITE_EMPLOYER)

    # Phase 3.1 Action Intents — Upload Salary Documents / Evidence / Complete Level 4
    if re.search(
        r"\b(where|how)\s+(can|do|should|to)\s+i\s+(upload|submit|provide|attach)\s+.*?\b(sal[a-e]ry|pay|payslip|pay\s+slip|ctc|income)\s+(proof|document|documents|evidence|slips?|details?)\b",
        raw
    ) or re.search(
        r"\b(where|how)\s+(can|do|should|to)\s+i\s+(upload|submit|provide|attach|complete|finish)\s+(sal[a-e]ry|payslip|pay\s+slip|income)\s*(proof|document|documents|evidence|verification)?\b",
        raw
    ) or re.search(
        r"\bhow\s+(do|can|should|to)\s+i\s+complete\s+level\s*4\b",
        raw
    ) or re.search(
        r"\bhow\s+(can|do|should|to)\s+i\s+verify\s+my\s+sal[a-e]ry\b",
        raw
    ) or re.search(
        r"\bwhere\s+(can|do)\s+i\s+submit\s+sal[a-e]ry\s+verification\b",
        raw
    ):
        intents.add(UserIntent.EMPLOYMENT_ACTION_UPLOAD_SALARY)

    # Category B: Next-Step Guidance Questions
    if re.search(
        r"\b(what\s+(should|can|do)\s+i\s+do\s+next|next\s+steps?|next\s+verification\s+step|what\s+is\s+my\s+next\s+step|"
        r"how\s+(can|do|to)\s+i?\s*(reach|complete|get|verify)\s+(level\s*[1-4]|my\s+employ?ment|employ?ment)|"
        r"how\s+(can|do|to)\s+(i\s+)?get\s+verified|how\s+can\s+i\s+complete\s+my\s+verification|next\s+steps?\s+to\s+(reach|complete|get)\s+level\s*[1-4]|"
        r"what\s+are\s+the\s+(ways|steps|methods|options)\s+to\s+verify|ways?\s+to\s+verify\s+(my\s+)?employ?ment|"
        r"how\s+to\s+verify\s+(my\s+)?employ?ment)\b",
        raw
    ):
        intents.add(UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS)

    # Category C: Incomplete-Level Explanation Questions
    if re.search(r"\b(why\s+(am\s+i|is\s+my|is\s+it|are\s+we)\s+.*?\b(still\s+at|pending|declined|incomplete|not\s+verified|preventing)\b|why\s+is\s+level\s*[1-4]\s+(pending|not\s+completed|incomplete)|why\s+is\s+my\s+(salary|company|employment)\s+not\s+verified|what\s+is\s+preventing)\b", raw):
        intents.add(UserIntent.EMPLOYMENT_VERIFICATION_WHY_INCOMPLETE)

    # Category D: Verification Ownership Questions
    if re.search(r"\b(who\s+needs\s+to|do\s+i\s+need\s+to|does\s+my\s+company\s+need\s+to|who\s+is\s+responsible|is\s+there\s+anything\s+pending\s+from\s+my\s+employer)\b", raw):
        intents.add(UserIntent.EMPLOYMENT_VERIFICATION_OWNERSHIP)

    # Category A: Verification Level Definitions (General / Procedural Questions)
    is_general_definition_query = (
        bool(re.search(r"\b(what\s+is|explain|meaning\s+of|tell\s+me\s+about|what\s+does|how\s+does)\b.*?\b(level\s*[1-4]|salary\s+verification|company\s+domain\s+verification|employment\s+verification|verification\s+levels?|verification\s+work)\b", raw))
        or bool(re.search(r"\b(level\s*[1-4]\s+verification|all\s+four\s+(employment\s+)?verification\s+levels|what\s+does\s+level\s*[1-4]\s+(mean|is|represent)|purpose\s+of\s+level\s*[1-4])\b", raw))
        or bool(re.search(r"\bhow\s+does\s+.*?\s+(work|function)\b", raw) and "verification" in raw)
    )
    if is_general_definition_query and not has_explicit_my and not re.search(r"\b(my\s+level|my\s+status|have\s+i|did\s+i|my\s+salary|my\s+company)\b", raw):
        intents.add(UserIntent.VERIFICATION_LEVEL_DEFINITION)

    # Category C: Verification Progress Questions
    if re.search(
        r"\b(verification\s+progress|display\s+my\s+verification\s+progress|show\s+my\s+verification\s+progress|"
        r"progress\s+breakdown|next\s+verification\s+level|steps?\s+remaining|remaining\s+steps?|remaining\s+verification|"
        r"remaining\s+levels?|levels?\s+remain(ing)?|what\s+verification\s+steps\s+remain|what\s+levels?\s+remain|"
        r"what\s+do\s+i\s+need|what\s+level\s+have\s+i\s+completed|how\s+far\s+along|how\s+to\s+(reach|complete|get)\s+level\s*[1-4]|"
        r"level\s*1\s+to\s+level\s*4\s+progress|verification\s+steps)\b",
        raw
    ):
        intents.add(UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS)

    # Claimed / Unclaimed Company Status Intent
    if re.search(r"\b(claimed|unclaimed|is\s+claimed|not\s+claimed|claimed\s+company|unclaimed\s+company)\b", raw):
        intents.add(UserIntent.EMPLOYMENT_COMPANY_CLAIMED)

    has_specialized_verif_intent = any(i in intents for i in (
        UserIntent.EMPLOYMENT_COMPANY_CLAIMED,
        UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS,
        UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS,
        UserIntent.EMPLOYMENT_VERIFICATION_WHY_INCOMPLETE,
        UserIntent.EMPLOYMENT_VERIFICATION_OWNERSHIP,
        UserIntent.EMPLOYMENT_ACTION_ADD,
        UserIntent.EMPLOYMENT_ACTION_INVITE_EMPLOYER,
        UserIntent.EMPLOYMENT_ACTION_UPLOAD_SALARY,
    ))

    # Category B: Employment Verification (Personal status lookup)
    if UserIntent.VERIFICATION_LEVEL_DEFINITION not in intents and not has_specialized_verif_intent:
        if re.search(r"\b(verification\s+status|claim\s+status|level\s*[1-4]\s+status|my\s+level\s*[1-4]|is\s+my\s+level\s*[1-4]\s+(completed|verified|done)|has\s+my\s+level\s*[1-4]|tell\s+me\s+my\s+level\s*[1-4]|is\s+my\s+.*?\s+verified|check\s+verification|my\s+verification\s+level|have\s+i\s+reached\s+level\s*[1-4]|is\s+my\s+company\s+verified|is\s+my\s+salary\s+verified|is\s+my\s+employment\s+verified|how\s+much\s+of\s+my\s+employment\s+is\s+verified)\b", raw):
            intents.add(UserIntent.EMPLOYMENT_VERIFICATION)
        elif re.search(r"\b(verify|check)\s+(my\s+)?employ?ment(\s+(for|at|in)\s+.*)?\b", raw) or re.search(r"\bemploy?ment\s+verification(\s+(for|at|in)\s+.*)?\b", raw):
            intents.add(UserIntent.EMPLOYMENT_VERIFICATION)
        elif re.search(r"\b(verification|claim\s+status)\b", raw) and not is_procedural_question and not is_general_definition_query:
            intents.add(UserIntent.EMPLOYMENT_VERIFICATION)

    # Employment Documents
    if re.search(r"\b(employ?ment\s+documents?|work\s+documents?|relieving\s+letter|experience\s+letter|pay\s+slip|documents?)\b", raw):
        intents.add(UserIntent.EMPLOYMENT_DOCUMENTS)

    # Check if query is looking for job openings/roles matching experience (exclude self-referential 'my job/jobs' queries)
    is_job_search_context = bool(re.search(r"\b(open\s+jobs?|vacanc(?:y|ies)|roles?|positions?|opportunities|hiring)\b", raw)) or (
        bool(re.search(r"\bjobs?\b", raw)) and not re.search(r"\bmy\s+(?:current\s+|old\s+|previous\s+|past\s+)?jobs?\b", raw) and not re.search(r"\bwhat\s+is\s+my\b", raw)
    )

    # Employment History (General)
    if not has_specialized_verif_intent:
        if re.search(r"\bemp_rec_\d+\b", raw):
            intents.add(UserIntent.EMPLOYMENT_HISTORY)
        elif re.search(
            r"employ?ment\s+history|work\s+history|work\s+experience|employ?ment\s+experience|"
            r"past\s+jobs|previous\s+jobs|old\s+jobs|my\s+jobs|current\s+job|current\s+employ?ment|"
            r"previous\s+compani(?:es|y)|previous\s+employer|last\s+compani(?:es|y)|last\s+employer|past\s+compani(?:es|y)|past\s+employer|"
            r"compani(?:es|y)\s+history|what\s+compani(?:es|y)\s+(?:did\s+i|have\s+i|do\s+i)\s+work\s*(?:for|at)?|"
            r"where\s+(?:did\s+i|have\s+i|i\s+have|was\s+i|am\s+i|do\s+i)\s+work(ed)?|"
            r"what\s+(?:company|companies)\s+do\s+i\s+work\s*(?:for|at)?|what\s+is\s+my\s+(?:current\s+)?(?:job|employ?ment)|"
            r"my\s+(?:previous|past|current|old)\s+(?:work|employ?ment|job\s+details|work\s+details)|"
            r"my\s+employ?ment\s+details|my\s+work\s+details",
            raw
        ) and not is_procedural_question and not is_job_search_context:
            intents.add(UserIntent.EMPLOYMENT_HISTORY)
        elif re.search(r"\b(my\s+experience|employ?ment\s+experience|work\s+experience|work\s+history)\b", raw) and not is_procedural_question and not is_job_search_context:
            intents.add(UserIntent.EMPLOYMENT_HISTORY)
        elif re.search(r"\bemploy?ment\b", raw) and not re.search(r"\b(notice|notice period|notices|salary|pay|document|documents|verification)\b", raw) and not is_procedural_question and not is_job_search_context:
            intents.add(UserIntent.EMPLOYMENT_HISTORY)

    # -------------------------------------------------------------------
    # LEVEL 3: Widget Context (WIDGET_*)
    # -------------------------------------------------------------------
    if re.search(r"\b(closing\s+soon|urgent\s+jobs?|deadline\s+approaching|expiring\s+jobs?|urgent\s+position)\b", raw):
        intents.add(UserIntent.WIDGET_CLOSING_SOON)

    if re.search(r"\b(companies\s+near\s+me|organizations?\s+near(by)?|local\s+employers?|employers?\s+near(by)?)\b", raw):
        intents.add(UserIntent.WIDGET_NEARBY_ORGS)

    if re.search(r"\b(show\s+widgets?|all\s+widgets?|random\s+widgets?|dashboard\s+widgets?)\b", raw):
        intents.add(UserIntent.WIDGET_ALL)

    # -------------------------------------------------------------------
    # LEVEL 4: Generic Profile Context (PROFILE_*)
    # -------------------------------------------------------------------
    if re.search(r"\b(profile\s+skil*s?|my\s+skil*s?|skil*s?\s+(for|on)\s+my\s+(profile|resume)|what\s+skil*s?\s+(do\s+i\s+have|i\s+have|are\s+on\s+my\s+profile)|skil*s?\s+list)\b", raw):
        intents.add(UserIntent.PROFILE_SKILLS)
    elif ("skill" in raw or "skil" in raw) and UserIntent.EMPLOYMENT_SKILLS not in intents and not is_job_search_intent and (has_self_ownership or raw in ("skills", "my skills", "skill list", "profile skills")):
        intents.add(UserIntent.PROFILE_SKILLS)

    if re.search(r"\b(educat(io|ion)?|degree|university|college|course|qualification|academic)\b", raw) or raw.startswith("what is my educat"):
        intents.add(UserIntent.PROFILE_EDUCATION)

    if re.search(r"\b(certif(icate|ication)?s?|licenses?)\b", raw):
        intents.add(UserIntent.PROFILE_CERTIFICATES)

    if re.search(r"\b(languag(e|es)?|languages\s+i\s+speak|spoken\s+languages)\b", raw):
        intents.add(UserIntent.PROFILE_LANGUAGES)

    if re.search(r"\b(portfol(io|ios)?|projects?|links?)\b", raw):
        intents.add(UserIntent.PROFILE_PORTFOLIO)

    if re.search(r"\b(profile\s+summary|about\s+me|full\s+profile|overview\s+of\s+my\s+profile|tell\s+me\s+about\s+my\s+profile|profile\s+info(rmation)?|profile\s+details|what\s+is\s+my\s+profile|my\s+profile|show\s+my\s+profile)\b", raw):
        intents.add(UserIntent.PROFILE_SUMMARY)

    # Order results deterministically according to precedence hierarchy
    precedence_order = [
        # Level 1
        UserIntent.PROFILE_INCOMPLETE,
        UserIntent.USER_DETAIL_NOTICES,
        UserIntent.USER_DETAIL_REMINDERS,
        # Level 2
        UserIntent.VERIFICATION_LEVEL_DEFINITION,
        UserIntent.EMPLOYMENT_ACTION_ADD,
        UserIntent.EMPLOYMENT_ACTION_INVITE_EMPLOYER,
        UserIntent.EMPLOYMENT_ACTION_UPLOAD_SALARY,
        UserIntent.EMPLOYMENT_VERIFICATION_NEXT_STEPS,
        UserIntent.EMPLOYMENT_VERIFICATION_WHY_INCOMPLETE,
        UserIntent.EMPLOYMENT_VERIFICATION_OWNERSHIP,
        UserIntent.EMPLOYMENT_VERIFICATION_PROGRESS,
        UserIntent.EMPLOYMENT_SKILLS,
        UserIntent.EMPLOYMENT_SALARY,
        UserIntent.EMPLOYMENT_COMPANY_CLAIMED,
        UserIntent.EMPLOYMENT_VERIFICATION,
        UserIntent.EMPLOYMENT_DOCUMENTS,
        UserIntent.EMPLOYMENT_HISTORY,
        # Level 3
        UserIntent.WIDGET_CLOSING_SOON,
        UserIntent.WIDGET_NEARBY_ORGS,
        UserIntent.WIDGET_ALL,
        # Level 4
        UserIntent.PROFILE_SKILLS,
        UserIntent.PROFILE_EDUCATION,
        UserIntent.PROFILE_CERTIFICATES,
        UserIntent.PROFILE_LANGUAGES,
        UserIntent.PROFILE_PORTFOLIO,
        UserIntent.PROFILE_SUMMARY,
    ]

    sorted_intents = [intent for intent in precedence_order if intent in intents]
    return sorted_intents
