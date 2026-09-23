"""
salary_matcher.py — Salary Matching Module for V1 Recommendation Engine.
"""

import re
from typing import Any, Optional
from backend.recommendation.models import CandidateProfile, JobProfile, SalaryMatchResult


class SalaryMatcher:
    """
    Evaluates financial expectation alignment between candidates and jobs.
    Normalizes different salary representations into standard annual amounts.
    """

    def _normalize_salary(self, value: Optional[Any]) -> Optional[float]:
        """
        Normalizes raw salary input into an annual float amount (in INR/base currency).
        Supports:
        - Numeric amounts / floats / ints (e.g. 10 -> 1,00,0000.0 if LPA, or 850000.0)
        - Strings with LPA/Lakh (e.g. '10 LPA', '12 lakh')
        - Strings with Monthly rate (e.g. '50000/month', '75000 per month')
        - Formatted strings with commas and symbols (e.g. '₹8,50,000')
        - Returns None if input is None, blank, or unparseable.
        - Never raises exceptions.

        :param value: Raw salary value.
        :return: Normalized annual float salary or None.
        """
        if value is None:
            return None

        if isinstance(value, (int, float)):
            num = float(value)
            if num <= 0:
                return None
            if num <= 100.0:
                return round(num * 100000.0, 2)
            return round(num, 2)

        try:
            s = str(value).strip().lower()
            if not s or s in ("none", "null", "unknown", "n/a"):
                return None

            s_clean = re.sub(r"[₹$,]", "", s).strip()

            is_monthly = False
            if any(k in s_clean for k in ["/month", "per month", "pm", "p.m.", "monthly"]):
                is_monthly = True
                s_clean = re.sub(r"/(month|m)|per\s+month|pm|p\.m\.|monthly", "", s_clean).strip()

            is_lpa = False
            if any(k in s_clean for k in ["lpa", "lakh", "lac"]):
                is_lpa = True
                s_clean = re.sub(r"lpa|lakhs|lakh|lacs|lac", "", s_clean).strip()

            match = re.search(r"[-+]?\d*\.?\d+", s_clean)
            if not match:
                return None

            num = float(match.group())
            if num <= 0:
                return None

            if is_monthly:
                return round(num * 12.0, 2)
            elif is_lpa or num <= 100.0:
                return round(num * 100000.0, 2)
            else:
                return round(num, 2)

        except Exception:
            return None

    def _match_salaries(
        self, candidate_salary: Optional[Any], job_salary: Optional[Any]
    ) -> SalaryMatchResult:
        """
        Internal evaluator calculating salary alignment score and difference percentage.

        :param candidate_salary: Candidate expected salary input.
        :param job_salary: Job offered salary input.
        :return: SalaryMatchResult dataclass.
        """
        norm_cand = self._normalize_salary(candidate_salary)
        norm_job = self._normalize_salary(job_salary)

        # Missing / Unknown Salary Rule
        if norm_cand is None or norm_job is None:
            return SalaryMatchResult(
                score=50.0,
                candidate_expected_salary=candidate_salary,
                job_offered_salary=job_salary,
                normalized_candidate_salary=norm_cand,
                normalized_job_salary=norm_job,
                salary_difference_percent=0.0,
                meets_expectation=False,
            )

        # Job Salary meets or exceeds expectation
        if norm_job >= norm_cand:
            return SalaryMatchResult(
                score=100.0,
                candidate_expected_salary=candidate_salary,
                job_offered_salary=job_salary,
                normalized_candidate_salary=norm_cand,
                normalized_job_salary=norm_job,
                salary_difference_percent=0.0,
                meets_expectation=True,
            )

        # Job Salary is below candidate expectation
        diff_pct = round(((norm_cand - norm_job) / norm_cand) * 100.0, 2)

        if diff_pct <= 10.0:
            score = 80.0
        elif diff_pct <= 20.0:
            score = 60.0
        elif diff_pct <= 30.0:
            score = 40.0
        else:
            score = 20.0

        return SalaryMatchResult(
            score=score,
            candidate_expected_salary=candidate_salary,
            job_offered_salary=job_salary,
            normalized_candidate_salary=norm_cand,
            normalized_job_salary=norm_job,
            salary_difference_percent=diff_pct,
            meets_expectation=False,
        )

    def evaluate_profiles(
        self, candidate: Optional[CandidateProfile], job: Optional[JobProfile]
    ) -> SalaryMatchResult:
        """
        Standardized public API to evaluate salary match between CandidateProfile and JobProfile.

        :param candidate: CandidateProfile dataclass instance or None.
        :param job: JobProfile dataclass instance or None.
        :return: SalaryMatchResult dataclass instance.
        """
        cand_sal = candidate.expected_salary if candidate else None
        job_sal = (
            job.offered_salary_max or job.offered_salary_min
            if job
            else None
        )
        return self._match_salaries(cand_sal, job_sal)

    def match_employer_salary(
        self,
        employer_budget: Optional[Any] = None,
        candidate_salary: Optional[Any] = None,
    ) -> Any:
        """
        Domain-safe salary matcher entry point for Phase 2 Employer Candidate Recommendation.
        Evaluates soft budget match with explicit salary missing, zero budget, and NULL candidate salary protections.
        """
        from backend.recommendation.types import MatchStatus, MatchResult, RecommendationDimension

        norm_budget = self._normalize_salary(employer_budget)
        if norm_budget is None or norm_budget <= 0.0:
            return MatchResult(
                dimension=RecommendationDimension.SALARY,
                status=MatchStatus.NOT_REQUESTED,
                score=None,
                details={"reason": "No employer budget requested or budget <= 0."},
            )

        norm_cand = self._normalize_salary(candidate_salary)
        if norm_cand is None or norm_cand <= 0.0:
            return MatchResult(
                dimension=RecommendationDimension.SALARY,
                status=MatchStatus.UNKNOWN,
                score=None,
                details={"reason": "Candidate profile has no populated or valid expected salary data."},
            )

        if norm_cand <= norm_budget:
            return MatchResult(
                dimension=RecommendationDimension.SALARY,
                status=MatchStatus.MATCH,
                score=100.0,
                details={
                    "reason": "Candidate expected salary meets or comes within employer budget.",
                    "normalized_budget": norm_budget,
                    "normalized_candidate_salary": norm_cand,
                },
            )

        over_pct = ((norm_cand - norm_budget) / norm_budget) * 100.0
        score = max(0.0, round(100.0 - over_pct, 2))
        status = MatchStatus.MATCH if score >= 70.0 else MatchStatus.MISMATCH

        return MatchResult(
            dimension=RecommendationDimension.SALARY,
            status=status,
            score=score,
            details={
                "reason": f"Candidate expected salary exceeds budget by {round(over_pct, 2)}%.",
                "normalized_budget": norm_budget,
                "normalized_candidate_salary": norm_cand,
                "over_budget_percent": round(over_pct, 2),
            },
        )

