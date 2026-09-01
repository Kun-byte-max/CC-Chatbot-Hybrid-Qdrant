"""
Deterministic Normalization Service for Role Titles and Skill Aliases.
"""

import re
from typing import List, Optional, Tuple


def normalize_role_title(raw_title: Optional[str]) -> Optional[str]:
    """
    Normalizes surface variations of job titles (spelling, casing, hyphenation, 'dev' abbreviations).
    DOES NOT perform aggressive semantic role merging (e.g. Data Analyst != Data Scientist != Data Engineer).
    """
    if not raw_title:
        return None

    clean = raw_title.strip().lower()
    clean = re.sub(r"[\-_+]+", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()

    # Explicit Surface Variation Mappings ONLY
    SURFACE_ROLE_MAPPINGS = {
        "frontend dev": "Frontend Developer",
        "front end dev": "Frontend Developer",
        "front end developer": "Frontend Developer",
        "front end engineer": "Frontend Engineer",
        "backend dev": "Backend Developer",
        "back end dev": "Backend Developer",
        "back end developer": "Backend Developer",
        "full stack dev": "Full Stack Developer",
        "fullstack dev": "Full Stack Developer",
        "fullstack developer": "Full Stack Developer",
        "swe": "Software Engineer",
        "dev": "Developer",
    }

    if clean in SURFACE_ROLE_MAPPINGS:
        return SURFACE_ROLE_MAPPINGS[clean]

    # Return Title-Cased string for unmapped distinct titles without semantic collapsing
    return clean.title()


def normalize_skill_name(raw_skill: str) -> str:
    """
    Normalizes common tech skill alias variations.
    """
    if not raw_skill:
        return raw_skill

    clean = raw_skill.strip().lower()
    SKILL_ALIASES = {
        "js": "JavaScript",
        "javascript": "JavaScript",
        "reactjs": "React",
        "react.js": "React",
        "react": "React",
        "nodejs": "Node.js",
        "node.js": "Node.js",
        "node": "Node.js",
        "py": "Python",
        "python": "Python",
    }
    return SKILL_ALIASES.get(clean, raw_skill.strip())


def normalize_skills(skills: Optional[List[str]]) -> List[str]:
    """
    Normalizes a list of skill tokens.
    """
    if not skills:
        return []
    normalized = []
    for s in skills:
        norm = normalize_skill_name(s)
        if norm and norm not in normalized:
            normalized.append(norm)
    return normalized
