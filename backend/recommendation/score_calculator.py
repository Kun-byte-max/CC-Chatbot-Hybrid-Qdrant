"""
score_calculator.py — Version 2 ScoreCalculator implementation.
Combines component matchers into a final recommendation score with dynamic profile-aware weighting.
"""

from typing import Optional, Dict
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

        raw_overall = (
            (skill_score * w_skill)
            + (experience_score * w_exp)
            + (title_score * w_title)
            + (salary_score * w_sal)
            + (location_score * w_loc)
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
            weight_skill=round(w_skill, 2),
            weight_experience=round(w_exp, 2),
            weight_location=round(w_loc, 2),
            weight_salary=round(w_sal, 2),
            weight_title=round(w_title, 2),
        )
