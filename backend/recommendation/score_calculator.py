"""
score_calculator.py — Version 2 ScoreCalculator implementation.
Combines component matchers into a final recommendation score with dynamic profile-aware weighting.
"""

from typing import Optional, Dict, Any
from backend.recommendation.models import ScoreResult

from backend.schemas.search_query import ProfileStatus, SearchMode


class ScoreCalculator:
    """
    Combines individual component match scores into a final weighted recommendation score.
    Supports dynamic weights based on ProfileStatus and SearchMode.
    """

    DEFAULT_WEIGHTS = {
        "skill": 0.40,
        "experience": 0.20,
        "title": 0.15,
        "salary": 0.15,
        "location": 0.10,
    }

    UNAVAILABLE_PROFILE_WEIGHTS = {
        "title": 0.55,
        "location": 0.25,
        "experience": 0.20,
        "skill": 0.0,
        "salary": 0.0,
    }

    def _get_weights_for_context(
        self,
        profile_status: Optional[ProfileStatus] = None,
        search_mode: Optional[SearchMode] = None,
    ) -> Dict[str, float]:
        """Returns dynamic weight dictionary based on profile status and search mode."""
        if profile_status in (ProfileStatus.PROFILE_UNAVAILABLE, ProfileStatus.PROFILE_API_ERROR):
            return self.UNAVAILABLE_PROFILE_WEIGHTS
        elif search_mode == SearchMode.SEARCH_ONLY:
            return {
                "title": 0.60,
                "location": 0.20,
                "experience": 0.10,
                "skill": 0.10,
                "salary": 0.0,
            }
        return self.DEFAULT_WEIGHTS

    def _determine_recommendation_level(self, score: float) -> str:
        """Determines string recommendation level from overall score."""
        if score >= 90.0:
            return "Excellent Match"
        elif score >= 80.0:
            return "Very Strong Match"
        elif score >= 70.0:
            return "Good Match"
        elif score >= 60.0:
            return "Fair Match"
        elif score >= 50.0:
            return "Weak Match"
        else:
            return "Not Recommended"

    def calculate_score(
        self,
        skill_result,
        experience_result,
        location_result,
        salary_result,
        title_result,
        profile_status: Optional[ProfileStatus] = None,
        search_mode: Optional[SearchMode] = None,
    ) -> ScoreResult:
        """
        Calculates weighted composite score and recommendation level from MatchResult objects.
        """
        match_results = {
            "skill_result": skill_result,
            "experience_result": experience_result,
            "location_result": location_result,
            "salary_result": salary_result,
            "title_result": title_result,
        }

        for name, result in match_results.items():
            if result is None:
                raise ValueError(f"Match result '{name}' cannot be None.")

        weights = self._get_weights_for_context(profile_status=profile_status, search_mode=search_mode)

        w_skill = weights.get("skill", 0.0)
        w_exp = weights.get("experience", 0.0)
        w_loc = weights.get("location", 0.0)
        w_sal = weights.get("salary", 0.0)
        w_title = weights.get("title", 0.0)

        skill_score = float(skill_result.score)
        experience_score = float(experience_result.score)
        location_score = float(location_result.score)
        salary_score = float(salary_result.score)
        title_score = float(title_result.score)

        # Dynamic Renormalization: Exclude dimensions where data is missing (match_level == 'unknown' or score is default 50.0 without evidence)
        active_weights = {
            "skill": w_skill if getattr(skill_result, "match_level", "") != "unknown" else 0.0,
            "experience": w_exp,
            "title": w_title if getattr(title_result, "match_level", "") != "unknown" else 0.0,
            "salary": w_sal if getattr(salary_result, "normalized_candidate_salary", None) is not None or getattr(salary_result, "normalized_job_salary", None) is not None else 0.0,
            "location": w_loc if getattr(location_result, "match_level", "") != "unknown" else 0.0,
        }

        total_weight = sum(active_weights.values())
        if total_weight > 0:
            norm_w_skill = active_weights["skill"] / total_weight
            norm_w_exp = active_weights["experience"] / total_weight
            norm_w_title = active_weights["title"] / total_weight
            norm_w_sal = active_weights["salary"] / total_weight
            norm_w_loc = active_weights["location"] / total_weight
        else:
            norm_w_skill, norm_w_exp, norm_w_title, norm_w_sal, norm_w_loc = w_skill, w_exp, w_title, w_sal, w_loc

        raw_overall = (
            (skill_score * norm_w_skill)
            + (experience_score * norm_w_exp)
            + (title_score * norm_w_title)
            + (salary_score * norm_w_sal)
            + (location_score * norm_w_loc)
        )

        overall_score = round(raw_overall, 2)
        rec_level = self._determine_recommendation_level(overall_score)

        return ScoreResult(
            overall_score=overall_score,
            recommendation_level=rec_level,
            skill_score=round(skill_score, 2),
            experience_score=round(experience_score, 2),
            location_score=round(location_score, 2),
            salary_score=round(salary_score, 2),
            title_score=round(title_score, 2),
            weight_skill=round(norm_w_skill, 2),
            weight_experience=round(norm_w_exp, 2),
            weight_location=round(norm_w_loc, 2),
            weight_salary=round(norm_w_sal, 2),
            weight_title=round(norm_w_title, 2),
        )

    def calculate_employer_candidate_score(
        self,
        candidate_id: str,
        match_results: Dict[Any, Any],
        explanation: str = "",
    ) -> Any:

        """
        Calculates weighted composite candidate score with dynamic renormalization,
        zero-denominator protection, and confidence score calculation.
        """
        from backend.recommendation.types import (
            MatchStatus,
            RecommendationStatus,
            RecommendationDimension,
            EmployerCandidateRecommendationResult,
        )

        base_weights = {
            RecommendationDimension.TITLE: 0.35,
            RecommendationDimension.SKILL: 0.35,
            RecommendationDimension.EXPERIENCE: 0.15,
            RecommendationDimension.LOCATION: 0.15,
            RecommendationDimension.SALARY: 0.10,
        }

        weighted_numerator = 0.0
        applicable_denominator = 0.0
        scored_criteria_count = 0
        total_requested_criteria_count = 0
        total_requested_weight = 0.0
        evaluated_weight = 0.0

        for dim, base_w in base_weights.items():
            match_res = match_results.get(dim)
            if not match_res or match_res.status == MatchStatus.NOT_REQUESTED:
                continue

            total_requested_criteria_count += 1
            total_requested_weight += base_w

            if match_res.status in (MatchStatus.MATCH, MatchStatus.MISMATCH) and match_res.score is not None:
                evaluated_weight += base_w
                weighted_numerator += float(match_res.score) * base_w
                applicable_denominator += base_w
                scored_criteria_count += 1
            elif match_res.status == MatchStatus.UNKNOWN:
                # Include requested UNKNOWN dimensions in denominator to avoid artificially inflating missing-data profiles
                applicable_denominator += base_w

        confidence_score = (
            round(evaluated_weight / total_requested_weight, 4) if total_requested_weight > 0.0 else 0.0
        )

        if applicable_denominator == 0.0 or scored_criteria_count == 0:
            return EmployerCandidateRecommendationResult(
                candidate_id=str(candidate_id),
                overall_status=RecommendationStatus.INSUFFICIENT_DATA,
                final_score=None,
                confidence_score=confidence_score,
                match_results=match_results,
                explanation=explanation or "Insufficient data to score candidate across requested criteria.",
                scored_criteria_count=scored_criteria_count,
                total_requested_criteria_count=total_requested_criteria_count,
            )

        final_score = round(weighted_numerator / applicable_denominator, 2)
        final_score = max(0.0, min(100.0, final_score))

        return EmployerCandidateRecommendationResult(
            candidate_id=str(candidate_id),
            overall_status=RecommendationStatus.CALCULATED,
            final_score=final_score,
            confidence_score=confidence_score,
            match_results=match_results,
            explanation=explanation,
            scored_criteria_count=scored_criteria_count,
            total_requested_criteria_count=total_requested_criteria_count,
        )

