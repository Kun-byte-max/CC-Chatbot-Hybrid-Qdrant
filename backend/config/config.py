import os
import math
import logging
from typing import Any
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

def validate_sample_rate(val: Any, default: float = 1.0) -> float:
    """
    Safely parses and validates LANGFUSE_SAMPLE_RATE.
    Preserves valid finite values in [0.0, 1.0] exactly.
    Malformed, empty, non-numeric, NaN, infinite, or out-of-range values fall back
    to the safe default (1.0) with a concise warning that does not expose secrets.
    Never silently clamps invalid or out-of-range values.
    """
    if val is None:
        return default
    try:
        parsed = float(val)
        if not math.isfinite(parsed):
            log.warning("LANGFUSE_SAMPLE_RATE must be finite; got non-finite value. Falling back to default %s.", default)
            return default
        if 0.0 <= parsed <= 1.0:
            return parsed
        log.warning("LANGFUSE_SAMPLE_RATE out of range [0.0, 1.0]; got %s. Falling back to default %s.", parsed, default)
        return default
    except (ValueError, TypeError):
        log.warning("LANGFUSE_SAMPLE_RATE invalid numeric format; got %r. Falling back to default %s.", str(val)[:20], default)
        return default

JWT_SECRET = os.getenv("JWT_SECRET", "collarcheck_prototype_secret_key_123")
JWT_ALGORITHM = "HS256"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("GROQ_API_KEY")
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
MODEL = "gpt-4o-mini"
try:
    from backend.utils.sse import validate_keepalive_interval
except ModuleNotFoundError:
    from utils.sse import validate_keepalive_interval  # type: ignore

SSE_KEEPALIVE_INTERVAL_SECONDS = validate_keepalive_interval(
    os.getenv("SSE_KEEPALIVE_INTERVAL_SECONDS", "15.0")
)

LANGFUSE_ENABLED = os.getenv("LANGFUSE_ENABLED", "false").lower() in ("true", "1", "yes")
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
LANGFUSE_RELEASE = os.getenv("LANGFUSE_RELEASE", "1.0.0")
LANGFUSE_SAMPLE_RATE = validate_sample_rate(os.getenv("LANGFUSE_SAMPLE_RATE", "1.0"))
LANGFUSE_SESSION_HMAC_SECRET = os.getenv("LANGFUSE_SESSION_HMAC_SECRET", "")

# Company Claim Status Strict Mode (Opt-in; defaults to false for legacy compatibility)
STRICT_CLAIM_STATUS_ENABLED = os.getenv("STRICT_CLAIM_STATUS_ENABLED", "false").lower() in ("true", "1", "yes")

# Redis Configuration (RAM-only session and conversation history store)
REDIS_ENABLED = os.getenv("REDIS_ENABLED", "true").lower() in ("true", "1", "yes")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_MAX_CONNECTIONS = int(os.getenv("REDIS_MAX_CONNECTIONS", "50"))
REDIS_CONNECT_TIMEOUT_SECONDS = float(os.getenv("REDIS_CONNECT_TIMEOUT_SECONDS", "2.0"))
REDIS_SOCKET_TIMEOUT_SECONDS = float(os.getenv("REDIS_SOCKET_TIMEOUT_SECONDS", "2.0"))
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "1800"))
MAX_SESSION_MESSAGES = int(os.getenv("MAX_SESSION_MESSAGES", "100"))
MAX_SESSION_BYTES = int(os.getenv("MAX_SESSION_BYTES", "524288"))  # 512 KB
MAX_MESSAGE_CHARS = int(os.getenv("MAX_MESSAGE_CHARS", "8000"))

SEARCH_API_URL = os.getenv("SEARCH_API_URL", "http://localhost:8000")
SEARCH_API_KEY = os.getenv("SEARCH_API_KEY", "")

JOB_MATCH_API_BASE_URL = os.getenv("JOB_MATCH_API_BASE_URL", "https://ai.collarcheck.com/jobmatch")
JOB_MATCH_API_KEY = os.getenv("JOB_MATCH_API_KEY", "")
JOB_MATCH_TIMEOUT_SECONDS = float(os.getenv("JOB_MATCH_TIMEOUT_SECONDS", "5.0"))
JOB_MATCH_ENABLED = os.getenv("JOB_MATCH_ENABLED", "false").lower() in ("true", "1", "yes")
JOB_MATCH_LIMIT = int(os.getenv("JOB_MATCH_LIMIT", "11"))

PLATFORM_MEDIA_BASE_URL = os.getenv("PLATFORM_MEDIA_BASE_URL", "https://admin.collarcheck.com")
ALLOWED_MEDIA_DOMAINS = set(
    os.getenv("ALLOWED_MEDIA_DOMAINS", "admin.collarcheck.com,www.collarcheck.com,collarcheck.com,ai.collarcheck.com").split(",")
)

HYBRID_SEARCH_ENABLED = os.getenv("HYBRID_SEARCH_ENABLED", "true").lower() in ("true", "1", "yes")
HYBRID_MEILISEARCH_WEIGHT = float(os.getenv("HYBRID_MEILISEARCH_WEIGHT", "0.70"))
HYBRID_JOBMATCH_WEIGHT = float(os.getenv("HYBRID_JOBMATCH_WEIGHT", "0.30"))
HYBRID_RRF_K = int(os.getenv("HYBRID_RRF_K", "60"))
HYBRID_RETRIEVAL_LIMIT = int(os.getenv("HYBRID_RETRIEVAL_LIMIT", "30"))

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "collarcheck.db"))

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "collarcheck")

def validate_positive_timeout(val: Any, default: float, param_name: str = "DB_TIMEOUT") -> float:
    """
    Safely parses and validates database socket timeouts.
    Preserves valid finite values > 0.0.
    Non-numeric, non-finite, or <= 0 values fall back to safe default with a warning.
    """
    if val is None:
        return default
    try:
        parsed = float(val)
        if not math.isfinite(parsed) or parsed <= 0:
            log.warning("%s must be positive finite float; got %r. Falling back to default %s.", param_name, val, default)
            return default
        return parsed
    except (ValueError, TypeError):
        log.warning("%s invalid numeric format; got %r. Falling back to default %s.", param_name, str(val)[:20], default)
        return default

DB_CONNECT_TIMEOUT_SECONDS = validate_positive_timeout(
    os.getenv("DB_CONNECT_TIMEOUT_SECONDS", "5.0"), default=5.0, param_name="DB_CONNECT_TIMEOUT_SECONDS"
)
DB_READ_TIMEOUT_SECONDS = validate_positive_timeout(
    os.getenv("DB_READ_TIMEOUT_SECONDS", "10.0"), default=10.0, param_name="DB_READ_TIMEOUT_SECONDS"
)
DB_WRITE_TIMEOUT_SECONDS = validate_positive_timeout(
    os.getenv("DB_WRITE_TIMEOUT_SECONDS", "10.0"), default=10.0, param_name="DB_WRITE_TIMEOUT_SECONDS"
)

# Bounded connection-establishment retry configuration
DB_CONNECT_MAX_RETRIES = max(1, min(10, int(os.getenv("DB_CONNECT_MAX_RETRIES", "3"))))
DB_RETRY_BASE_DELAY_SECONDS = validate_positive_timeout(
    os.getenv("DB_RETRY_BASE_DELAY_SECONDS", "0.1"), default=0.1, param_name="DB_RETRY_BASE_DELAY_SECONDS"
)
DB_RETRY_MAX_DELAY_SECONDS = validate_positive_timeout(
    os.getenv("DB_RETRY_MAX_DELAY_SECONDS", "1.0"), default=1.0, param_name="DB_RETRY_MAX_DELAY_SECONDS"
)

JOB_MODE_MAP = {1: "Work from Office", 2: "Work from Home", 3: "Hybrid"}

MAX_RESULTS = 5

class DynamicSkillsMap(dict):
    """
    Dynamic dictionary proxy for job profile skills mapping.
    Queries database dynamically via get_dynamic_job_profile_skills before falling back to initial default values.
    """
    def __getitem__(self, key: str):
        skills = self._get_dynamic(key)
        return skills if skills else super().get(key, ["Python", "SQL", "API Design", "System Design"])

    def get(self, key: str, default=None):
        skills = self._get_dynamic(key)
        if skills:
            return skills
        return super().get(key, default or ["Python", "SQL", "API Design", "System Design"])

    def _get_dynamic(self, key: str):
        try:
            from backend.repositories.job_repository import get_dynamic_job_profile_skills
            dyn = get_dynamic_job_profile_skills(str(key))
            if dyn:
                return dyn
        except Exception:
            pass
        return None


JOB_PROFILE_SKILLS_MAP = DynamicSkillsMap({
    "Backend Developer": ["Node.js", "Python", "SQL", "API Design", "System Design"],
    "Frontend Developer": ["React", "JavaScript", "HTML/CSS", "TypeScript", "Vue.js"],
    "Full Stack Developer": ["React", "Node.js", "Python", "SQL", "REST APIs"],
    "Data Scientist": ["Python", "SQL", "Machine Learning", "Pandas", "Statistics"],
    "Devops Engineer": ["Docker", "Kubernetes", "AWS", "CI/CD", "Linux"],
    "Mobile Developer": ["React Native", "Flutter", "iOS", "Android", "Swift"],
    "Ui/Ux Designer": ["Figma", "Wireframing", "Prototyping", "User Research", "Adobe XD"],
    "Software Engineer": ["Python", "Java", "C++", "SQL", "Git"],
    "Java Developer": ["Java", "Spring Boot", "SQL", "Microservices", "REST APIs"],
    "Python Developer": ["Python", "Django", "FastAPI", "SQL", "PostgreSQL"],
})


EMPLOYER_TRIGGERS = [
    "hire", "hiring", "recruit", "recruiting", "looking to hire",
    "want to hire", "need to hire", "show me profiles", "candidate",
    "candidates", "developer profiles", "engineer profiles","profile for", "profiles for", "suggest profile", "suggest profiles",
    "find candidate", "find candidates", "search candidate", "search candidates",
    "looking for candidate", "looking for candidates"
]
