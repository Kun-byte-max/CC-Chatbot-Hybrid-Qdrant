"""
skill_matcher.py — Skill Matching Module for V1 Recommendation Engine.
"""

from typing import List, Optional, Set
from backend.recommendation.models import CandidateProfile, JobProfile, SkillMatchResult


SKILL_CANONICAL_MAP = {
    "data analyst": "Data Analysis",
    "data analytics": "Data Analysis",
    "data analysis": "Data Analysis",
    "next js": "Next.js",
    "nextjs": "Next.js",
    "next.js": "Next.js",
    "jquery": "jQuery",
    "frontend developer": "Frontend Developer",
    "frontend": "Frontend Developer",
    "front end": "Frontend Developer",
    "front-end": "Frontend Developer",
    "php": "PHP",
    "react": "React",
    "react.js": "React",
    "reactjs": "React",
    "node.js": "Node.js",
    "nodejs": "Node.js",
    "node js": "Node.js",
    "python": "Python",
    "java": "Java",
    "sql": "SQL",
    "mysql": "MySQL",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "recruiting": "Recruitment",
    "recruitment": "Recruitment",
    "recruiter": "Recruitment",
    "hr": "HR",
    "human resources": "HR",
    "payroll": "Payroll",
    "sales": "Sales",
    "sales executive": "Sales",
    "crm": "CRM",
    "accounting": "Accounting",
    "accountant": "Accounting",
    "gst": "GST",
}


def canonicalize_skill(skill: str) -> str:
    """
    Deterministically resolves skill variants and aliases to a single canonical skill name.
    E.g. 'Next Js', 'nextjs' -> 'Next.js'
         'Data Analyst' -> 'Data Analysis'
         'jquery' -> 'jQuery'
    """
    if not skill:
        return ""
    cleaned = str(skill).strip().lower()
    return SKILL_CANONICAL_MAP.get(cleaned, str(skill).strip())


class SkillMatcher:
    """
    Evaluates skill compatibility between candidate profiles and job requirements using exact string matching.
    """

    def _normalize_skills(self, skills: Optional[List[str]]) -> List[str]:
        """
        Normalizes a list of skill strings:
        - Converts strings to canonical skill form.
        - Trims leading and trailing whitespace.
        - Removes duplicates while preserving order.
        - Removes empty strings and None values.

        :param skills: Input list of raw skill strings or None.
        :return: List of clean, canonicalized skill strings.
        """
        if not skills:
            return []

        seen: Set[str] = set()
        normalized: List[str] = []

        for skill in skills:
            if skill is None:
                continue
            canon = canonicalize_skill(str(skill))
            key = canon.strip().lower()
            if key and key not in seen:
                seen.add(key)
                normalized.append(canon)

        return normalized


    def _match_skills(
        self, candidate_skills: Optional[List[str]], required_skills: Optional[List[str]]
    ) -> SkillMatchResult:
        """
        Internal evaluator calculating skill match metrics between skill lists.
        Supports exact matching as well as punctuation/whitespace-invariant matching (e.g. Next.js vs Next Js).
        """
        import re

        cand_list = self._normalize_skills(candidate_skills)
        req_list = self._normalize_skills(required_skills)

        cand_set = set(cand_list)
        req_set = set(req_list)

        # Zero required skills edge case
        if not req_list:
            score = 100.0 if not cand_list else 0.0
            return SkillMatchResult(
                score=score,
                matched_skills=[],
                missing_skills=[],
                extra_skills=cand_list,
                total_required=0,
                total_matched=0,
            )

        def _canon(s: str) -> str:
            return re.sub(r'[^a-z0-9]', '', s.lower())

        cand_canon_map = {_canon(s): s for s in cand_list if _canon(s)}

        matched = []
        missing = []

        for req in req_list:
            req_canon = _canon(req)
            if req in cand_set:
                matched.append(req)
            elif req_canon and req_canon in cand_canon_map:
                matched.append(req)
            else:
                missing.append(req)

        matched_canon_set = {_canon(m) for m in matched}
        extra = [s for s in cand_list if s not in req_set and _canon(s) not in matched_canon_set]

        total_required = len(req_list)
        total_matched = len(matched)

        base_ratio = (total_matched / total_required) * 100.0
        if cand_list and total_matched > 0:
            cand_cov = (total_matched / len(cand_list)) * 100.0
            score = round(0.85 * base_ratio + 0.15 * cand_cov, 2)
        else:
            score = round(base_ratio, 2)

        score = max(0.0, min(100.0, score))

        return SkillMatchResult(
            score=score,
            matched_skills=matched,
            missing_skills=missing,
            extra_skills=extra,
            total_required=total_required,
            total_matched=total_matched,
        )

    def evaluate_profiles(
        self, candidate: Optional[CandidateProfile], job: Optional[JobProfile]
    ) -> SkillMatchResult:
        """
        Standardized public API to evaluate skill match between CandidateProfile and JobProfile.

        :param candidate: CandidateProfile dataclass instance or None.
        :param job: JobProfile dataclass instance or None.
        :return: SkillMatchResult dataclass instance.
        """
        cand_skills = candidate.skills if candidate else []
        req_skills = job.required_skills if job else []
        return self._match_skills(cand_skills, req_skills)
