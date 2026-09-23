from typing import List, Dict, Any, Optional, Tuple, Set
import re
import logging

log = logging.getLogger(__name__)


class EmployeePeopleScoringEngine:
    """
    Dedicated Employee-Side Candidate & Professional Scoring Engine.
    Provides independent 5-tier title matching, skill normalization with false positive protection,
    numeric experience hard constraints, and deterministic score calculation for employee PEOPLE_SEARCH.
    Completely isolated from EmployerRecommendationEngine.
    """

    GENERIC_TITLE_WORDS: Set[str] = {
        "developer", "engineer", "analyst", "designer", "architect",
        "manager", "executive", "specialist", "officer", "assistant",
        "representative", "associate", "agent", "clerk", "admin",
        "administrator", "head", "director", "lead", "supervisor",
        "consultant", "coordinator", "professional", "technician",
    }

    SKILL_CANONICAL_MAP: Dict[str, str] = {
        "nodejs": "Node.js",
        "node js": "Node.js",
        "node.js": "Node.js",
        "node": "Node.js",
        "reactjs": "React",
        "react js": "React",
        "react.js": "React",
        "react": "React",
        "nextjs": "Next.js",
        "next js": "Next.js",
        "next.js": "Next.js",
        "vuejs": "Vue.js",
        "vue js": "Vue.js",
        "vue.js": "Vue.js",
        "vue": "Vue.js",
        "python": "Python",
        "java": "Java",
        "javascript": "JavaScript",
        "js": "JavaScript",
        "typescript": "TypeScript",
        "ts": "TypeScript",
        "php": "PHP",
        "django": "Django",
        "flask": "Flask",
        "express": "Express.js",
        "expressjs": "Express.js",
        "sql": "SQL",
        "mysql": "MySQL",
        "postgresql": "PostgreSQL",
        "postgres": "PostgreSQL",
        "mongodb": "MongoDB",
        "mongo": "MongoDB",
        "aws": "AWS",
        "docker": "Docker",
        "kubernetes": "Kubernetes",
        "hr": "HR",
        "human resources": "HR",
        "recruitment": "Recruitment",
        "recruiter": "Recruitment",
        "accounting": "Accounting",
        "accountant": "Accounting",
        "gst": "GST",
        "sales": "Sales",
    }

    SKILL_FALSE_POSITIVES: Dict[str, Set[str]] = {
        "java": {"javascript"},
        "react": {"react native"},
        "vue": {"vuex"},
        "node.js": {"node-red"},
    }

    @classmethod
    def normalize_skill(cls, skill: str) -> str:
        if not skill:
            return ""
        clean = skill.strip().lower()
        return cls.SKILL_CANONICAL_MAP.get(clean, skill.strip())

    TITLE_ALIAS_MAP: Dict[str, str] = {
        "frontend engineer": "frontend developer",
        "frontend developer": "frontend engineer",
        "backend engineer": "backend developer",
        "backend developer": "backend engineer",
        "fullstack engineer": "fullstack developer",
        "fullstack developer": "fullstack engineer",
        "software engineer": "software developer",
        "software developer": "software engineer",
        "react engineer": "react developer",
        "react developer": "react engineer",
    }

    @classmethod
    def match_title(cls, requested_title: Optional[str], candidate_title: Optional[str]) -> Tuple[float, str]:
        """
        Calculates 5-tier title relevance score (0.0 to 100.0) and tier label.
        - Exact Match: 100.0
        - Related Title: 85.0
        - Generic Role Only: 50.0
        - Weak Match: 25.0
        - Unrelated: 0.0
        """
        if not requested_title or not requested_title.strip():
            return 100.0, "NOT_REQUESTED"
        if not candidate_title or not candidate_title.strip():
            return 0.0, "UNRELATED"

        req_clean = requested_title.strip().lower()
        cand_clean = candidate_title.strip().lower()

        if req_clean == cand_clean:
            return 100.0, "EXACT"

        # Check conservative direct title aliases (interchangeable wording only)
        if cls.TITLE_ALIAS_MAP.get(req_clean) == cand_clean:
            return 100.0, "EXACT"

        req_words = set(re.findall(r"\b[a-z0-9\.\+#]+\b", req_clean))
        cand_words = set(re.findall(r"\b[a-z0-9\.\+#]+\b", cand_clean))

        if not req_words or not cand_words:
            return 0.0, "UNRELATED"

        common_words = req_words.intersection(cand_words)
        domain_req_words = req_words - cls.GENERIC_TITLE_WORDS

        if domain_req_words and domain_req_words.issubset(cand_words):
            return 85.0, "RELATED"

        if common_words and not domain_req_words:
            # Only generic role words matched (e.g. "Developer" matching "Web Developer")
            return 50.0, "GENERIC"

        if common_words:
            return 25.0, "WEAK"

        return 0.0, "UNRELATED"

    @classmethod
    def match_skills(
        cls, requested_skills: Optional[List[str]], candidate_skills: Optional[List[str]]
    ) -> Tuple[float, List[str], List[str]]:
        """
        Evaluates skill match percentage with alias normalization and false-positive guards.
        Returns (score, matched_skills, missing_skills).
        """
        if not requested_skills:
            return 100.0, [], []

        req_norm = [cls.normalize_skill(s) for s in requested_skills if s and s.strip()]
        if not req_norm:
            return 100.0, [], []

        cand_raw = [s.strip().lower() for s in (candidate_skills or []) if s and str(s).strip()]
        cand_norm = [cls.normalize_skill(s) for s in (candidate_skills or []) if s and str(s).strip()]

        matched = []
        missing = []

        for req in req_norm:
            req_l = req.lower()
            found = False
            # Check canonical match
            for c_norm, c_raw in zip(cand_norm, cand_raw):
                if req_l == c_norm.lower():
                    # False positive check
                    is_fp = False
                    if req_l in cls.SKILL_FALSE_POSITIVES:
                        for forbidden in cls.SKILL_FALSE_POSITIVES[req_l]:
                            if forbidden in c_raw and req_l != forbidden:
                                is_fp = True
                                break
                    if not is_fp:
                        found = True
                        break
            if found:
                matched.append(req)
            else:
                missing.append(req)

        score = round((len(matched) / len(req_norm)) * 100.0, 2)
        return score, matched, missing

    @classmethod
    def match_experience(
        cls, requested_exp: Optional[float], candidate_exp: Optional[float]
    ) -> Tuple[float, bool]:
        """
        Evaluates numeric experience eligibility.
        If requested_exp is 3.0+, candidate_exp < 3.0 returns (0.0, False) -> MISMATCH.
        """
        if requested_exp is None or requested_exp <= 0.0:
            return 100.0, True

        c_exp = candidate_exp if candidate_exp is not None else 0.0

        if c_exp < requested_exp:
            return 0.0, False

        return 100.0, True

    @classmethod
    def match_location(
        cls, requested_location: Optional[str], candidate_city: Optional[str], candidate_state: Optional[str]
    ) -> float:
        """
        Evaluates location match score.
        """
        if not requested_location or not requested_location.strip():
            return 100.0

        req_l = requested_location.strip().lower()
        c_city_l = (candidate_city or "").strip().lower()
        c_state_l = (candidate_state or "").strip().lower()

        if c_city_l and req_l == c_city_l:
            return 100.0
        if c_state_l and req_l == c_state_l:
            return 80.0
        if c_city_l and (req_l in c_city_l or c_city_l in req_l):
            return 90.0
        if c_state_l and (req_l in c_state_l or c_state_l in req_l):
            return 75.0

        return 0.0

    @classmethod
    def score_seniority_compatibility(
        cls, requested_seniority: Optional[str], candidate_title: Optional[str]
    ) -> float:
        """
        Evaluates Seniority Compatibility modifier (+15.0, 0.0, -20.0).
        Seniority is a secondary refinement and can NEVER overpower core title or skill matching.
        """
        if not requested_seniority or not requested_seniority.strip():
            return 0.0
        if not candidate_title or not candidate_title.strip():
            return 0.0

        req_sen = requested_seniority.strip().lower()
        cand_title_l = candidate_title.strip().lower()

        SENIOR_KEYWORDS = {"senior", "lead", "principal"}
        JUNIOR_KEYWORDS = {"junior", "fresher", "trainee", "intern"}

        cand_words = set(re.findall(r"\b[a-z0-9\.\+#]+\b", cand_title_l))

        has_senior = bool(cand_words.intersection(SENIOR_KEYWORDS))
        has_junior = bool(cand_words.intersection(JUNIOR_KEYWORDS))

        if req_sen in ("senior", "lead", "principal"):
            if has_senior:
                return 15.0
            elif has_junior:
                return -20.0
            else:
                return 0.0
        elif req_sen in ("junior", "fresher"):
            if has_junior:
                return 15.0
            elif has_senior:
                return -20.0
            else:
                return 0.0

        return 0.0

    @classmethod
    def score_candidate(
        cls,
        candidate: Dict[str, Any],
        requested_title: Optional[str] = None,
        requested_skills: Optional[List[str]] = None,
        requested_exp: Optional[float] = None,
        requested_location: Optional[str] = None,
        requested_seniority: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Calculates weighted overall score for a candidate dictionary.
        Core Relevance (Title & Skills) dominates.
        Seniority compatibility acts strictly as a secondary refinement.
        """
        title_score, title_tier = cls.match_title(requested_title, candidate.get("current_title"))
        skill_score, matched_skills, missing_skills = cls.match_skills(requested_skills, candidate.get("skills"))
        exp_score, exp_meets = cls.match_experience(requested_exp, candidate.get("total_experience_years"))
        loc_score = cls.match_location(requested_location, candidate.get("city"), candidate.get("state"))
        seniority_score = cls.score_seniority_compatibility(requested_seniority, candidate.get("current_title"))

        # Weighted Numerator / Denominator
        weights = {}
        if requested_title and requested_title.strip():
            weights["title"] = 0.40
        if requested_skills:
            weights["skill"] = 0.35
        if requested_exp is not None and requested_exp > 0:
            weights["exp"] = 0.15
        if requested_location and requested_location.strip():
            weights["location"] = 0.10

        if not weights:
            # Default scoring if no explicit search criteria specified
            base_score = 100.0
        else:
            total_w = sum(weights.values())
            weighted_sum = (
                weights.get("title", 0.0) * title_score
                + weights.get("skill", 0.0) * skill_score
                + weights.get("exp", 0.0) * exp_score
                + weights.get("location", 0.0) * loc_score
            )
            base_score = round(weighted_sum / total_w, 2)

        # Apply Seniority Refinement Score (secondary modifier)
        final_score = round(max(0.0, base_score + seniority_score), 2)

        is_eligible = exp_meets and (skill_score > 0.0 or not requested_skills)

        return {
            "candidate_id": candidate.get("id"),
            "final_score": final_score,
            "title_score": title_score,
            "title_tier": title_tier,
            "skill_score": skill_score,
            "matched_skills": matched_skills,
            "missing_skills": missing_skills,
            "exp_score": exp_score,
            "exp_meets": exp_meets,
            "loc_score": loc_score,
            "seniority_score": seniority_score,
            "is_eligible": is_eligible,
        }


def people_rank_key(candidate: Dict[str, Any], scoring: Dict[str, Any]) -> Tuple[Any, ...]:
    """
    Canonical deterministic rank key function for employee PEOPLE_SEARCH.
    Hierarchy:
    1. is_eligible (True=0, False=1)
    2. final_score (Descending via negative float)
    3. title_score (Descending)
    4. skill_score (Descending)
    5. exp_score (Descending)
    6. loc_score (Descending)
    7. seniority_score (Descending)
    8. candidate_id (Descending numeric tie-breaker)
    """
    elig_sort = 0 if scoring.get("is_eligible", True) else 1
    f_score = -round(scoring.get("final_score", 0.0), 2)
    t_score = -round(scoring.get("title_score", 0.0), 2)
    s_score = -round(scoring.get("skill_score", 0.0), 2)
    e_score = -round(scoring.get("exp_score", 0.0), 2)
    l_score = -round(scoring.get("loc_score", 0.0), 2)
    sen_score = -round(scoring.get("seniority_score", 0.0), 2)

    try:
        cand_id = -int(candidate.get("id", 0))
    except (ValueError, TypeError):
        cand_id = 0

    return (elig_sort, f_score, t_score, s_score, e_score, l_score, sen_score, cand_id)
