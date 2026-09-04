"""
explanation_generator.py — Version 2 Dual Explanation Generator implementation.
Generates distinct Search Relevance reasons (Why this job appeared) and Recommendation Ranking reasons (Why it ranked highly).
"""

from typing import List, Optional, Dict, Any
try:
    from backend.recommendation.models import (
        ExplanationResult,
        RecommendationReason,
        ScoreResult,
        SkillMatchResult,
        ExperienceMatchResult,
        LocationMatchResult,
        SalaryMatchResult,
        TitleMatchResult,
    )
except ModuleNotFoundError:
    from recommendation.models import (  # type: ignore
        ExplanationResult,
        RecommendationReason,
        ScoreResult,
        SkillMatchResult,
        ExperienceMatchResult,
        LocationMatchResult,
        SalaryMatchResult,
        TitleMatchResult,
    )


def _format_skills_list(skills: List[str]) -> str:
    """Formats a list of skill strings naturally with 'and' for grammar."""
    if not skills:
        return ""
    clean = [str(s).strip() for s in skills if s]
    if len(clean) == 1:
        return clean[0]
    if len(clean) == 2:
        return f"{clean[0]} and {clean[1]}"
    return f"{', '.join(clean[:-1])}, and {clean[-1]}"


def _format_years(years: float) -> str:
    """Formats experience years cleanly."""
    val = int(years) if isinstance(years, (int, float)) and float(years).is_integer() else round(float(years), 1)
    if val == 1:
        return "1 year"
    return f"{val} years"


class ExplanationGenerator:
    """
    Generates dual explanations:
    1. search_reasons: Why this job appeared (Hard query & filter matches).
    2. recommendation_reasons: Why this job ranked highly (Soft profile fit & signals).
    """

    SUMMARY_MAPPINGS = {
        "Excellent Match": "This job is an excellent match for your profile.",
        "Very Strong Match": "This job closely matches your profile with only minor gaps.",
        "Good Match": "This job is a good match for your profile.",
        "Fair Match": "This job partially matches your profile.",
        "Weak Match": "This job has several gaps compared to your profile.",
        "Not Recommended": "This job currently does not sufficiently match your profile.",
    }

    def generate_explanation(
        self,
        score_result: ScoreResult,
        skill_result: SkillMatchResult,
        experience_result: ExperienceMatchResult,
        location_result: LocationMatchResult,
        salary_result: SalaryMatchResult,
        title_result: TitleMatchResult,
        search_query_title: Optional[str] = None,
        search_query_location: Optional[str] = None,
        search_query_work_mode: Optional[str] = None,
    ) -> ExplanationResult:
        """
        Constructs separate search_reasons and recommendation_reasons.
        """
        match_results = {
            "score_result": score_result,
            "skill_result": skill_result,
            "experience_result": experience_result,
            "location_result": location_result,
            "salary_result": salary_result,
            "title_result": title_result,
        }
        for name, res in match_results.items():
            if res is None:
                raise ValueError(f"Argument '{name}' cannot be None.")

        summary = self.SUMMARY_MAPPINGS.get(
            score_result.recommendation_level,
            "Match evaluation completed.",
        )

        search_reasons: List[str] = []
        strengths: List[str] = []
        weaknesses: List[str] = []
        rec_reasons: List[RecommendationReason] = []

        # 1. SEARCH RELEVANCE REASONS ("Why this job appeared")
        job_title_name = getattr(title_result, "job_title", None) or "role"
        if search_query_title:
            search_reasons.append(f"Matches your search for \"{search_query_title}\"")
        elif title_result.score >= 70.0:
            search_reasons.append(f"Matches requested {job_title_name} role")

        work_mode = getattr(location_result, "work_mode", "") or ""
        match_level = getattr(location_result, "match_level", "") or ""
        job_city = getattr(location_result, "job_city", None)

        if search_query_location:
            search_reasons.append(f"Located in {search_query_location}")
        elif job_city:
            search_reasons.append(f"Located in {job_city}")

        if search_query_work_mode or work_mode == "Remote":
            search_reasons.append(f"Work Mode: {work_mode or search_query_work_mode}")

        # Default search reason if empty
        if not search_reasons:
            search_reasons.append(f"Matched search query criteria for {job_title_name}")

        # 2. RECOMMENDATION RELEVANCE REASONS ("Why it ranked highly")

        # Skill Matcher Evidence
        matched_skills = skill_result.matched_skills or []
        missing_skills = skill_result.missing_skills or []

        if matched_skills:
            formatted_matched = _format_skills_list(matched_skills)
            strengths.append(f"Matched skills: {', '.join(matched_skills)}")
            rec_reasons.append(RecommendationReason(
                type="skill",
                label="Skills",
                message=f"Matches {len(matched_skills)} of your skills ({formatted_matched}).",
                evidence={"matched_skills": list(matched_skills)}
            ))
        if missing_skills:
            weaknesses.append(f"Missing skills: {', '.join(missing_skills)}")

        # Experience Matcher Evidence
        cand_exp = getattr(experience_result, "candidate_experience", 0.0) or 0.0
        req_exp = getattr(experience_result, "required_experience", 0.0) or 0.0
        meets_exp = getattr(experience_result, "meets_requirement", False)

        if meets_exp or experience_result.score >= 70.0:
            strengths.append("Meets experience requirement.")
            if cand_exp == 0:
                exp_msg = "Your entry-level experience matches the role requirement."
            else:
                exp_msg = f"Your {_format_years(cand_exp)} of experience matches the role requirement."
            rec_reasons.append(RecommendationReason(
                type="experience",
                label="Experience",
                message=exp_msg,
                evidence={"candidate_experience": float(cand_exp), "required_experience": float(req_exp)}
            ))
        else:
            weaknesses.append("Experience is below the required level.")

        # Location Matcher Evidence
        if work_mode == "Remote" or match_level == "Remote":
            strengths.append("Eligible for remote work.")
            rec_reasons.append(RecommendationReason(
                type="location",
                label="Location",
                message="Offers Remote work option.",
                evidence={"work_mode": work_mode}
            ))
        elif match_level in ("Exact City", "Exact"):
            strengths.append("Located in the same city.")
            rec_reasons.append(RecommendationReason(
                type="location",
                label="Location",
                message=f"Located in {job_city or 'your target city'}, matching your location.",
                evidence={"job_city": job_city}
            ))

        # Salary Matcher Evidence
        meets_sal = getattr(salary_result, "meets_expectation", False)
        if meets_sal or salary_result.score >= 70.0:
            strengths.append("Salary expectations are satisfied.")
            rec_reasons.append(RecommendationReason(
                type="salary",
                label="Salary",
                message="Offered salary satisfies your expectations.",
                evidence={"meets_expectation": bool(meets_sal)}
            ))

        # Role Matcher Evidence
        if title_result.score >= 70.0:
            strengths.append("Job title closely matches profile.")
            rec_reasons.append(RecommendationReason(
                type="title",
                label="Role Match",
                message=f"Profile closely aligns with the {job_title_name} role.",
                evidence={"job_title": job_title_name}
            ))

        full_explanation_text = " ".join([r.message for r in rec_reasons]) if rec_reasons else summary

        return ExplanationResult(
            summary=full_explanation_text,
            strengths=strengths,
            weaknesses=weaknesses,
            recommendation_level=score_result.recommendation_level,
            overall_score=score_result.overall_score,
            search_reasons=search_reasons,
            recommendation_reasons=rec_reasons,
        )
