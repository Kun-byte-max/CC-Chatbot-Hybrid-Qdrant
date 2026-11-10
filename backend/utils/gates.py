import logging
import re
from typing import Optional, Tuple

try:
    from backend.services.ranking_service import EMPLOYER_SESSIONS
except ModuleNotFoundError:
    from services.ranking_service import EMPLOYER_SESSIONS  # type: ignore

log = logging.getLogger(__name__)

PROFILE_HINTS = (
    "i am", "i'm", "my name", "my skill", "i know", "i work", "i worked",
    "add ", "remove ", "update my", "i have", "years of experience",
    "i studied", "graduated", "my email", "my phone", "my location",
    "i live", "based in", "certification", "certified", "resume", "cv",
)

SMALLTALK_EXACT = {
    "hi", "hello", "hey", "yo", "hola",
    "thanks", "thank you", "thanks!", "ty",
    "ok", "okay", "k", "cool", "nice", "great",
    "yes", "no", "yeah", "yep", "nope",
    "bye", "goodbye", "good morning", "good evening", "good night",
}

JOB_NOUNS_PATTERN = r"\b(jobs?|openings?|vacanc(?:y|ies)|roles?|positions?|opportunities|gigs?)\b"
PROFILE_CONNECTIVES_PATTERN = (
    r"\b(?:according\s+to|as\s+per|based\s+on|matching|relevant\s+to|suited\s+for|suitable\s+for|fit\s+for|for)\s+"
    r"(?:my\s+)?(?:skills?|profile|experience|background)\b"
)
PROFILE_JOB_DIRECT_PATTERN = r"\b(?:jobs?\s+for\s+me|suitable\s+jobs?\s+for\s+me|jobs?\s+suitable\s+for\s+me)\b"
PROFILE_UPDATE_ACTIONS_PATTERN = r"\b(add|remove|delete|update|modify|change|edit|set)\b"


def is_widget_relevant_query(msg: str) -> bool:
    """
    Checks if user query pertains to widget-enriched content such as urgent or closing-soon positions.
    """
    if not msg:
        return False
    return bool(re.search(r'\b(closing|soon|widget|urgent)\b', msg, re.I))


def is_profile_job_search(msg: str) -> bool:
    """
    Checks if a query is a clear job search referencing the user's profile/skills/experience.
    Returns False if the query contains profile update action verbs (e.g. add, remove, update)
    to ensure genuine profile edits and mixed-intent queries are not suppressed.
    """
    m = (msg or "").lower().strip()
    if not m:
        return False
    if re.search(PROFILE_UPDATE_ACTIONS_PATTERN, m):
        return False
    if re.search(PROFILE_JOB_DIRECT_PATTERN, m):
        return True
    return bool(re.search(JOB_NOUNS_PATTERN, m)) and bool(re.search(PROFILE_CONNECTIVES_PATTERN, m))


def might_be_profile_update(msg: str) -> bool:
    m = (msg or "").lower().strip()
    if len(m) < 8:
        return False
    return any(h in m for h in PROFILE_HINTS)


def is_definitely_not_search(msg: str) -> bool:
    m = (msg or "").lower().strip().rstrip("!.?")
    return m in SMALLTALK_EXACT


def should_run_profile_parse(role: str, user_message: str) -> Tuple[bool, str]:
    if role != "employee":
        return False, "skip_role"
    m = (user_message or "").strip()
    if len(m) < 8:
        return False, "skip_too_short"
    if is_profile_job_search(m):
        return False, "skip_profile_job_search"
    if not might_be_profile_update(m):
        return False, "skip_no_hint"
    return True, "ran_hint_matched"

def should_run_profile_parse_safe(role: str, user_message: str) -> Tuple[bool, str]:
    try:
        return should_run_profile_parse(role, user_message)
    except Exception:
        log.exception("Gate function failed, running profile_parse fail-open")
        return True, "ran_gate_error"

def should_run_search_parse(role: str, user_message: str, session_id: Optional[str] = None) -> Tuple[bool, str]:
    # B3 Employer Session Override check (side-effect free direct lookup)
    if role == "employer" and session_id:
        sid = f"employer_{session_id}"
        session = EMPLOYER_SESSIONS.get(sid)
        if session and session.get("current_step", "idle") != "idle":
            return True, "ran_employer_session_override"

    if is_definitely_not_search(user_message):
        return False, "skip_exact_smalltalk"

    return True, "ran_default"

def should_run_search_parse_safe(role: str, user_message: str, session_id: Optional[str] = None) -> Tuple[bool, str]:
    try:
        return should_run_search_parse(role, user_message, session_id=session_id)
    except Exception:
        log.exception("Gate function failed, running search_parse fail-open")
        return True, "ran_gate_error"
