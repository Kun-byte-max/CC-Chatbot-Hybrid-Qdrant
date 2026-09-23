"""
title_matcher.py — Job Title Matching Module for V1 Recommendation Engine.
"""

import re
from typing import Any, List, Optional, Set
from backend.recommendation.models import CandidateProfile, JobProfile, TitleMatchResult


class TitleMatcher:
    """
    Evaluates job title and role taxonomy alignment using deterministic V1 matching rules.
    """

    SENIORITY_WORDS: Set[str] = {
        "senior",
        "junior",
        "lead",
        "principal",
        "associate",
        "intern",
        "staff",
        "chief",
        "head",
        "sr",
        "jr",
    }

    GENERIC_ROLE_SUFFIXES: Set[str] = {
        "developer",
        "engineer",
        "programmer",
        "architect",
        "consultant",
        "analyst",
        "specialist",
        "manager",
        "lead",
        "executive",
        "officer",
        "assistant",
        "representative",
        "associate",
        "agent",
        "clerk",
        "admin",
        "administrator",
        "head",
        "director",
    }

    def _normalize_title(self, value: Optional[Any]) -> Optional[str]:
        """
        Normalizes job title strings:
        - Converts to lowercase.
        - Trims whitespace and collapses multiple spaces.
        - Removes unnecessary punctuation.
        - Treats None, empty strings, and 'unknown' as None.
        - Never raises exceptions.

        :param value: Raw title input.
        :return: Normalized title string or None.
        """
        if value is None:
            return None

        try:
            s = str(value).strip().lower()
            if not s or s in ("none", "null", "unknown", "n/a"):
                return None

            s_clean = re.sub(r"[^\w\s]", " ", s)
            s_clean = re.sub(r"\s+", " ", s_clean).strip()

            if not s_clean or s_clean in ("none", "null", "unknown", "n/a"):
                return None

            return s_clean

        except Exception:
            return None

    def _match_titles(
        self, candidate_title: Optional[Any], job_title: Optional[Any]
    ) -> TitleMatchResult:
        """
        Internal evaluator calculating title match score and match level.

        :param candidate_title: Candidate current or past job title.
        :param job_title: Job requirement title.
        :return: TitleMatchResult dataclass.
        """
        norm_cand = self._normalize_title(candidate_title)
        norm_job = self._normalize_title(job_title)

        # Rule 6 — Missing Titles
        if norm_cand is None or norm_job is None:
            return TitleMatchResult(
                score=50.0,
                candidate_title=str(candidate_title) if candidate_title else None,
                job_title=str(job_title) if job_title else None,
                normalized_candidate_title=norm_cand,
                normalized_job_title=norm_job,
                matched_keywords=[],
                match_level="unknown",
            )

        cand_words = norm_cand.split()
        job_words = norm_job.split()

        # Rule 1 — Exact Match
        if norm_cand == norm_job:
            return TitleMatchResult(
                score=100.0,
                candidate_title=str(candidate_title),
                job_title=str(job_title),
                normalized_candidate_title=norm_cand,
                normalized_job_title=norm_job,
                matched_keywords=cand_words,
                match_level="exact",
            )

        # Helper to get non-seniority words
        cand_base = [w for w in cand_words if w not in self.SENIORITY_WORDS]
        job_base = [w for w in job_words if w not in self.SENIORITY_WORDS]

        # Rule 2 & 3 — Seniority Contained Match (Difference is only seniority prefix/suffix)
        if " ".join(cand_base) == " ".join(job_base) and (
            norm_cand in norm_job or norm_job in norm_cand
        ):
            return TitleMatchResult(
                score=90.0,
                candidate_title=str(candidate_title),
                job_title=str(job_title),
                normalized_candidate_title=norm_cand,
                normalized_job_title=norm_job,
                matched_keywords=cand_base if cand_base else cand_words,
                match_level="contains",
            )

        # Extract domain keywords (excluding seniority and generic role words)
        cand_domain = [w for w in cand_base if w not in self.GENERIC_ROLE_SUFFIXES]
        job_domain = [w for w in job_base if w not in self.GENERIC_ROLE_SUFFIXES]

        common_domain = [w for w in cand_domain if w in job_domain]
        common_all_kw = [w for w in cand_base if w in job_base]

        # Rule 4 — Significant Keyword Overlap (Domain keywords match)
        if common_domain:
            return TitleMatchResult(
                score=70.0,
                candidate_title=str(candidate_title),
                job_title=str(job_title),
                normalized_candidate_title=norm_cand,
                normalized_job_title=norm_job,
                matched_keywords=common_all_kw,
                match_level="keyword_match",
            )

        # Rule 5 — No Meaningful Match (Different domain e.g. Java vs Python)
        return TitleMatchResult(
            score=20.0,
            candidate_title=str(candidate_title),
            job_title=str(job_title),
            normalized_candidate_title=norm_cand,
            normalized_job_title=norm_job,
            matched_keywords=[],
            match_level="different",
        )

    def evaluate_profiles(
        self, candidate: Optional[CandidateProfile], job: Optional[JobProfile]
    ) -> TitleMatchResult:
        """
        Standardized public API to evaluate title match between CandidateProfile and JobProfile.
        If primary candidate title yields low match, evaluates candidate profile skills against job title.
        """
        cand_t = candidate.current_title if candidate else None
        job_t = job.title if job else None

        res = self._match_titles(cand_t, job_t)
        if res.score < 70.0 and candidate and candidate.skills:
            for sk in candidate.skills:
                sk_res = self._match_titles(sk, job_t)
                if sk_res.score > res.score:
                    res = sk_res
        return res
