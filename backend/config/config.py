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

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "collarcheck.db"))

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "collarcheck")

JOB_MODE_MAP = {1: "Work from Office", 2: "Work from Home", 3: "Hybrid"}

MAX_RESULTS = 5

JOB_PROFILE_SKILLS_MAP = {
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
}

EMPLOYER_TRIGGERS = [
    "hire", "hiring", "recruit", "recruiting", "looking to hire",
    "want to hire", "need to hire", "show me profiles", "candidate",
    "candidates", "developer profiles", "engineer profiles","profile for", "profiles for", "suggest profile", "suggest profiles",
    "find candidate", "find candidates", "search candidate", "search candidates",
    "looking for candidate", "looking for candidates"
]
