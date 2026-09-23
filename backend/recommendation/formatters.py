"""
formatters.py — Response formatter for recommendation results.
Formats RecommendationResult objects into candidate-facing chatbot responses.
"""

from typing import List, Optional, Any
try:

    from backend.recommendation.models import RecommendationResult
except ModuleNotFoundError:
    from recommendation.models import RecommendationResult  # type: ignore


def format_recommendations_response(
    results: List[RecommendationResult],
    candidate_id: Optional[int] = None,
    candidate_exists: bool = True,
    profile_complete: bool = True,
    active_jobs_exist: bool = True
) -> str:
    """
    Formats recommendation engine results into structured chatbot response text.
    Handles error & edge cases according to Phase 11 specifications.
    """
    if not candidate_exists or candidate_id is None:
        return "No candidate profile found. Please log in or register to get job recommendations."

    if not profile_complete:
        return "Your profile is incomplete. Please update your skills and experience details to get personalized job recommendations."

    if not active_jobs_exist:
        return "No active jobs were found."

    if not results:
        return "No suitable jobs were found."

    lines = ["Here are your best matching jobs:\n"]
    divider = "-" * 50

    for res in results:
        job = res.title_result.job_title or "Job Title"
        # Extract job metadata from score_result / explanation_result
        exp_gen = res.explanation_result
        score_percent = int(round(res.overall_score * 100)) if res.overall_score <= 1.0 else int(round(res.overall_score))

        # Check job profile title & company if attached in raw details
        comp_name = "Company N/A"
        if hasattr(res, "job_id") and res.job_id:
            comp_name = getattr(res.explanation_result, "company_name", None) or "ABC Technologies"

        lines.append(divider)
        lines.append("")
        lines.append(f"{res.title_result.job_title or 'Job'}")
        lines.append("")
        lines.append("Company:")
        lines.append(f"{res.explanation_result.summary.split('at ')[-1] if 'at ' in res.explanation_result.summary else 'Company'}")
        lines.append("")
        lines.append("Overall Match:")
        lines.append(f"{score_percent}%")
        lines.append("")
        lines.append("Recommendation:")
        lines.append(f"{res.recommendation_level}")
        lines.append("")
        lines.append("Summary:")
        lines.append(f"{res.explanation_result.summary}")
        lines.append("")

        if res.explanation_result.strengths:
            lines.append("Strengths")
            lines.append("")
            for st in res.explanation_result.strengths:
                lines.append(f"✓ {st}")
            lines.append("")

        if res.explanation_result.weaknesses:
            lines.append("Weaknesses")
            lines.append("")
            for wk in res.explanation_result.weaknesses:
                lines.append(f"• {wk}")
            lines.append("")

    lines.append(divider)
    return "\n".join(lines)


class EmployerResponseFormatter:
    """
    Formatter for Employer Candidate Recommendation responses.
    Formats EmployerCandidateRecommendationResult objects into candidate cards and chatbot responses.
    """

    @staticmethod
    def format_candidate_cards(
        recs: List[Any],
        candidates_by_id: dict,
    ) -> List[dict]:
        """
        Formats EmployerCandidateRecommendationResult objects into API candidate_cards DTO format.
        """
        cards = []
        for rec in recs:
            cand = candidates_by_id.get(str(rec.candidate_id)) or candidates_by_id.get(rec.candidate_id)
            if not cand:
                cand = {}

            if isinstance(cand, dict):
                fname = cand.get("fname") or cand.get("first_name") or ""
                lname = cand.get("lname") or cand.get("last_name") or ""
                full_name_raw = cand.get("full_name") or ""
                if fname or lname:
                    full_name = f"{fname} {lname}".strip()
                elif full_name_raw:
                    full_name = full_name_raw
                else:
                    full_name = f"Candidate {rec.candidate_id}"

                city = cand.get("city_name") or cand.get("city") or ""
                state = cand.get("state_name") or cand.get("state") or ""
                skills = cand.get("skills") or cand.get("skill_names") or []
                title = cand.get("current_title") or cand.get("current_position_name") or cand.get("designation") or "Candidate Profile"
                exp_yrs = cand.get("experience_years") or cand.get("total_experience_years") or 0.0
                rating = cand.get("star_rating_avg") or cand.get("rating")
                slug = cand.get("slug") or str(rec.candidate_id)
            else:
                fname = getattr(cand, "fname", None) or getattr(cand, "first_name", None) or ""
                lname = getattr(cand, "lname", None) or getattr(cand, "last_name", None) or ""
                full_name_raw = getattr(cand, "full_name", None) or ""
                if fname or lname:
                    full_name = f"{fname} {lname}".strip()
                elif full_name_raw:
                    full_name = full_name_raw
                else:
                    full_name = f"Candidate {rec.candidate_id}"

                city = getattr(cand, "city_name", None) or getattr(cand, "city", None) or ""
                state = getattr(cand, "state_name", None) or getattr(cand, "state", None) or ""
                skills = getattr(cand, "skills", None) or getattr(cand, "skill_names", None) or []
                title = getattr(cand, "current_title", None) or getattr(cand, "current_position_name", None) or getattr(cand, "designation", None) or "Candidate Profile"
                exp_yrs = getattr(cand, "experience_years", None) or getattr(cand, "total_experience_years", None) or 0.0
                rating = getattr(cand, "star_rating_avg", None) or getattr(cand, "rating", None)
                slug = getattr(cand, "slug", None) or str(rec.candidate_id)

            # Sanitize location: if city/state are raw integer IDs (e.g. 154257, 4017), resolve DB names
            city_str = str(city).strip() if city and str(city) != "N/A" else ""
            state_str = str(state).strip() if state and str(state) != "N/A" else ""

            if city_str.isdigit() or state_str.isdigit():
                try:
                    from backend.repositories.db import get_db
                    conn = get_db()
                    c = conn.cursor()
                    if city_str.isdigit():
                        c.execute("SELECT name FROM cyb_cities WHERE id = %s LIMIT 1", (int(city_str),))
                        r = c.fetchone()
                        if r:
                            city_str = r["name"] if isinstance(r, dict) else (r[0] if r else "")
                    if state_str.isdigit():
                        c.execute("SELECT name FROM cyb_state WHERE id = %s LIMIT 1", (int(state_str),))
                        r = c.fetchone()
                        if r:
                            state_str = r["name"] if isinstance(r, dict) else (r[0] if r else "")
                    conn.close()
                except Exception:
                    pass

            city_clean = city_str if not city_str.isdigit() else ""
            state_clean = state_str if not state_str.isdigit() else ""
            loc_parts = [p for p in (city_clean, state_clean) if p]
            loc_str = ", ".join(loc_parts) or "Location not specified"


            if float(exp_yrs) == 0.0:
                exp_str = "Fresher (0 years)"
            elif float(exp_yrs) > 0 and float(exp_yrs) < 1.0:
                months = int(round(float(exp_yrs) * 12))
                exp_str = f"{months} month{'s' if months != 1 else ''}"
            else:
                exp_str = f"{round(float(exp_yrs), 1)} year{'s' if round(float(exp_yrs), 1) != 1.0 else ''}"

            score_val = rec.final_score if rec.final_score is not None else 0.0
            cand_url = f"https://www.collarcheck.com/candidate-details/{slug}"

            cards.append({
                "candidate_id": str(rec.candidate_id),
                "cc_id": str(rec.candidate_id),
                "name": full_name,
                "position": title,
                "title": title,
                "city": city_clean or None,
                "state": state_clean or None,
                "location": loc_str,
                "experience_label": exp_str,
                "experience_years": exp_yrs,
                "skills": skills if isinstance(skills, list) else [str(skills)],
                "url": cand_url,
                "rating": rating,
                "match_reason": f"Match Score: {round(score_val)}% | {exp_str} | {loc_str}",
                "overall_score": score_val,
                "overall_status": rec.overall_status.value if hasattr(rec.overall_status, "value") else str(rec.overall_status),
                "confidence_score": rec.confidence_score,
                "explanation": rec.explanation,
            })
        return cards


    @staticmethod
    def format_employer_response_text(
        recs: List[Any],
        candidates_by_id: dict,
        total_results: int,
        current_page: int,
        total_pages: int,
    ) -> str:
        """
        Formats EmployerCandidateRecommendationResult objects into natural language chatbot prompt context.
        """
        if total_results == 0 or not recs:
            return "No candidates matching your search criteria were found. Try adjusting your role, skill, or location requirements."

        lines = [f"Found {total_results} matching candidate(s) (Page {current_page} of {total_pages}):\n"]
        divider = "-" * 50

        for rec in recs:
            cand = candidates_by_id.get(str(rec.candidate_id)) or candidates_by_id.get(rec.candidate_id)
            full_name = f"{getattr(cand, 'fname', '')} {getattr(cand, 'lname', '')}".strip() if cand else f"Candidate {rec.candidate_id}"
            score_str = f"{rec.final_score}%" if rec.final_score is not None else "INSUFFICIENT_DATA"

            lines.append(divider)
            lines.append(f"Candidate: {full_name} (ID: {rec.candidate_id})")
            lines.append(f"Overall Match: {score_str}")
            lines.append(f"Confidence: {int(rec.confidence_score * 100)}%")
            if rec.explanation:
                lines.append(f"Summary: {rec.explanation}")
            lines.append("")

        lines.append(divider)
        return "\n".join(lines)

