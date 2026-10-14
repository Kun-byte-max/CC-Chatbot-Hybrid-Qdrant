import logging
import re
import difflib
from typing import Optional, Tuple

log = logging.getLogger(__name__)

try:
    from backend.repositories.db import get_db
    from backend.config.config import EMPLOYER_TRIGGERS
except ModuleNotFoundError:
    from repositories.db import get_db
    from config.config import EMPLOYER_TRIGGERS

BLOCKED_PATTERNS = [
    r'\bsex\b', r'\bporn\b', r'\bnude\b', r'\berotic\b', r'\bsexual\b',
    r'\bdrug\b', r'\bcocaine\b', r'\bheroin\b', r'\bweed\b', r'\bmarijuana\b',
    r'\bmeth\b', r'\bnarcotics\b', r'\bsmuggle\b',
    r'\bbomb\b', r'\bterror\b', r'\bweapon\b', r'\bkill\b', r'\bmurder\b',
    r'\bhack\b', r'\bcyberattack\b', r'\bmalware\b',
    r'\bgamble\b', r'\bcasino\b', r'\bbetting\b',
]

OFF_TOPIC = [
    'recipe', 'weather', 'cricket', 'ipl', 'movie', 'bollywood',
    'politics', 'election', 'religion', 'astrology',
    'stock market', 'crypto', 'bitcoin', 'forex',
    'dating', 'girlfriend', 'boyfriend', 'love advice',
    'homework', 'essay', 'joke',
]

STOP_WORDS = {
    "find", "show", "me", "jobs", "job", "roles", "role", "openings", "opening",
    "available", "vacancies", "vacancy", "hiring", "position", "positions",
    "are", "there", "for", "in", "at", "on", "the", "a", "an", "can", "you",
    "please", "want", "need", "looking", "search", "get", "give", "list", "tell",
    "how", "many", "what", "which", "collarcheck", "currently", "right", "now",
    "related", "based", "all", "some", "latest", "new", "good", "best", "top",
    "i", "my", "us", "we", "it", "is", "to", "do", "have", "with", "about",
    "suggest", "recommend", "suitable", "matching", "relevant",
    "hi", "hello", "hey", "platform", "website", "portal", "today",
    "any", "could", "would", "should", "like", "work", "working",
    "open", "type", "help", "thanks", "okay", "sure", "yes", "no",
}

def check_guardrails(message: str) -> Optional[str]:
    msg_lower = message.lower()
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, msg_lower):
            return (
                "I'm CollarCheck's professional career assistant and can only help with "
                "employment, job search, and career topics. "
                "Ask me about jobs or your CollarCheck profile!"
            )
    for kw in OFF_TOPIC:
        if kw in msg_lower:
            return (
                "That's outside my area! I specialise in CollarCheck — India's professional "
                "verification platform. I can help you with:\n"
                "- Finding jobs on CollarCheck\n"
                "- Improving your profile rating\n"
                "- Understanding CC ID and verification\n\n"
                "What would you like to know about your career?"

            )
    return None

def smart_extract_terms(user_message: str) -> Tuple[str, str]:
    msg_lower = user_message.lower()

    try:
        conn = get_db()
        db_states = [r[0].lower() for r in conn.execute(
            "SELECT name FROM cyb_state WHERE name IS NOT NULL AND status = 1 AND country = 101 AND LOWER(name) NOT IN ('teststate', 'khushboonew')"
        ).fetchall()]
        conn.close()
    except Exception:
        log.exception("Failed to load states from database")
        db_states = []

    extra_cities = [
        "new delhi", "delhi ncr", "ncr", "navi mumbai", "pan india",
        "work from home", "wfh", "remote", "hybrid", "anywhere",
        "noida", "gurgaon", "gurugram", "thane", "faridabad",
        "prayagraj", "allahabad", "visakhapatnam",
    ]

    all_locations = list(set(db_states + extra_cities))
    all_locations.sort(key=len, reverse=True)

    location = ""
    matched_word_in_msg = ""
    for loc in all_locations:
        if loc in msg_lower:
            location = loc
            matched_word_in_msg = loc
            break

    if not location:
        words_in_msg = re.sub(r"[^\w\s]", "", msg_lower).split()
        for word in words_in_msg:
            if len(word) > 3:
                matches = difflib.get_close_matches(word, all_locations, n=1, cutoff=0.8)
                if matches:
                    location = matches[0]
                    matched_word_in_msg = word
                    break
        if location:
            cleaned = msg_lower.replace(matched_word_in_msg, "").strip()
        else:
            cleaned = msg_lower
    else:
        cleaned = msg_lower.replace(matched_word_in_msg, "").strip()

    words = re.sub(r"[^\w\s]", "", cleaned).split()
    role_words = [w for w in words if w not in STOP_WORDS and len(w) > 2]
    role_keyword = " ".join(role_words[:5]).strip()

    return role_keyword, location

def is_career_query(msg: str) -> bool:
    """
    Dynamically determines if incoming user text expresses a job seeker / career search intent
    (looking for jobs, openings, career opportunities, skill matching) using LLM Intent Classification.
    Falls back gracefully to keyword signal check if LLM API is unavailable.
    """
    if not msg or not str(msg).strip():
        return False

    msg_lower = str(msg).lower().strip()

    signals = [
        "job", "jobs", "role", "roles", "vacancy", "vacancies",
        "opening", "openings", "hiring", "position", "positions",
        "career", "opportunity", "opportunities", "apply",
        "find me", "show me", "get me", "search for", "looking for",
        "want to work", "want a job", "need a job",
        "how many", "are there any", "available",
        "profile", "rating", "cc id", "cc pro", "verification",
        "verified", "employer", "review", "star rating",
    ]

    # 1. Fast cache / direct keyword check for standard triggers
    if any(s in msg_lower for s in signals):
        return True

    # 2. Dynamic LLM Intent Classification
    try:
        from backend.config.config import OPENAI_API_KEY, OPENAI_API_URL, MODEL
    except ModuleNotFoundError:
        try:
            from config.config import OPENAI_API_KEY, OPENAI_API_URL, MODEL  # type: ignore
        except Exception:
            OPENAI_API_KEY = None

    if OPENAI_API_KEY:
        try:
            import requests
            prompt = (
                "You are an intent classifier for a job search platform.\n"
                "Classify whether the user input is a JOB SEEKER / CANDIDATE looking for job openings, career opportunities, or profile-matching jobs.\n"
                "Respond strictly with YES or NO.\n"
                f"User Input: \"{msg}\""
            )
            res = requests.post(
                OPENAI_API_URL,
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 5,
                    "temperature": 0.0,
                },
                timeout=2.5
            )
            if res.status_code == 200:
                answer = res.json()["choices"][0]["message"]["content"].strip().upper()
                if "YES" in answer:
                    return True
                elif "NO" in answer:
                    return False
        except Exception as e:
            log.warning("Dynamic LLM career intent classification fallback triggered: %s", e)

    return False

def is_faq_query(msg: str) -> bool:
    triggers = [
        "what is", "how does", "how do i", "how can i", "tell me about", "explain",
        "difference between", "is collarcheck", "does collarcheck",
        "free", "cost", "pricing", "linkedin", "signup", "register", "how to",
    ]
    return any(t in msg.lower() for t in triggers)

def is_employer_hiring_query(msg: str) -> bool:
    """
    Dynamically determines if incoming user text expresses an employer/recruiter hiring intent
    versus a job seeker search using LLM Intent Classification.
    Falls back gracefully to keyword trigger check if LLM API is unavailable or unconfigured.
    """
    if not msg or not str(msg).strip():
        return False

    msg_lower = str(msg).lower().strip()

    # 1. Fast cache / direct keyword check for standard triggers
    if any(t in msg_lower for t in EMPLOYER_TRIGGERS):
        return True

    # 2. Dynamic LLM Intent Classification
    try:
        from backend.config.config import OPENAI_API_KEY, OPENAI_API_URL, MODEL
    except ModuleNotFoundError:
        try:
            from config.config import OPENAI_API_KEY, OPENAI_API_URL, MODEL  # type: ignore
        except Exception:
            OPENAI_API_KEY = None

    if OPENAI_API_KEY:
        try:
            import requests
            prompt = (
                "You are an intent classifier for a recruitment platform.\n"
                "Classify whether the user input is an EMPLOYER trying to hire/recruit candidates or post a job requirement.\n"
                "Respond strictly with YES or NO.\n"
                f"User Input: \"{msg}\""
            )
            res = requests.post(
                OPENAI_API_URL,
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 5,
                    "temperature": 0.0,
                },
                timeout=2.5
            )
            if res.status_code == 200:
                answer = res.json()["choices"][0]["message"]["content"].strip().upper()
                if "YES" in answer:
                    return True
                elif "NO" in answer:
                    return False
        except Exception as e:
            log.warning("Dynamic LLM employer intent classification fallback triggered: %s", e)

    return False


