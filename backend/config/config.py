import os
from dotenv import load_dotenv

load_dotenv()

JWT_SECRET = os.getenv("JWT_SECRET", "collarcheck_prototype_secret_key_123")
JWT_ALGORITHM = "HS256"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("GROQ_API_KEY")
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
MODEL = "gpt-4o-mini"

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
