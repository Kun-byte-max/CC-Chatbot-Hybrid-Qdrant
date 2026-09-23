from typing import List, Dict, Any, Optional
import logging

try:
    from backend.repositories.people_repository import PeopleRepository
    from backend.schemas.search_criteria import SearchCriteria
    from backend.services.location_service import resolve_location_from_db, LocationResult
    from backend.schemas.search_query import LocationMode
except ModuleNotFoundError:
    from repositories.people_repository import PeopleRepository  # type: ignore
    from schemas.search_criteria import SearchCriteria  # type: ignore
    from services.location_service import resolve_location_from_db, LocationResult  # type: ignore
    from schemas.search_query import LocationMode  # type: ignore

log = logging.getLogger(__name__)


class PeopleSearchService:
    """
    Service Layer for Phase 3 Employee -> People Search.
    Consumes canonical SearchCriteria and delegates to PeopleRepository.
    Formats safe employee result cards and returns structured conversational responses.
    """

    @staticmethod
    def search_people(
        criteria: SearchCriteria,
        current_user_id: Optional[int] = None,
        user_location: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Executes People Search using criteria, resolving NEAR_ME and EXPLICIT locations.
        """
        city_id = None
        state_id = None
        resolved_loc_name = criteria.location

        # Location Resolution logic using existing location infrastructure
        if criteria.location_mode == LocationMode.NEAR_ME:
            resolved_raw_loc = None
            if user_location and isinstance(user_location, dict):
                resolved_raw_loc = user_location.get("state") or user_location.get("city")
            elif user_location and isinstance(user_location, str) and user_location.strip():
                resolved_raw_loc = user_location.strip()

            if not resolved_raw_loc:
                return {
                    "success": True,
                    "status": "LOCATION_UNAVAILABLE",
                    "db_context": (
                        "## LOCATION UNAVAILABLE\n"
                        "Your location details are not available in your profile or browser.\n\n"
                        "[INSTRUCTION]\n"
                        "Inform the user in one sentence that their location could not be determined and ask them to specify a city."
                    ),
                    "results": [],
                    "result_type": "people_location_unavailable",
                }

            loc_res: LocationResult = resolve_location_from_db(resolved_raw_loc)
            if loc_res.resolved:
                city_id = loc_res.city_id
                state_id = loc_res.state_id
                resolved_loc_name = loc_res.city_name or loc_res.state_name or resolved_raw_loc

        elif criteria.location_mode == LocationMode.EXPLICIT and criteria.location:
            loc_res: LocationResult = resolve_location_from_db(criteria.location)
            if loc_res.resolved:
                city_id = loc_res.city_id
                state_id = loc_res.state_id
                resolved_loc_name = loc_res.city_name or loc_res.state_name or criteria.location
            else:
                # Unresolved explicit location query (Zero hits to prevent location leakage)
                return PeopleSearchService._format_empty_response(criteria, unresolved_location=criteria.location)

        # Query database repository for evaluation pool (Limit 100)
        raw_people = PeopleRepository.search_people(
            skills=criteria.skills,
            title=criteria.title,
            city_id=city_id,
            state_id=state_id,
            company=criteria.company,
            exclude_user_id=current_user_id,
            limit=100,
        )

        if not raw_people:
            return PeopleSearchService._format_empty_response(criteria, resolved_location=resolved_loc_name)

        from backend.services.people_scoring_engine import EmployeePeopleScoringEngine, people_rank_key

        # Score candidates using EmployeePeopleScoringEngine
        scored_pairs = []
        for p in raw_people:
            scoring = EmployeePeopleScoringEngine.score_candidate(
                candidate=p,
                requested_title=criteria.title,
                requested_skills=criteria.skills,
                requested_exp=float(criteria.experience_min) if criteria.experience_min is not None else None,
                requested_location=resolved_loc_name or criteria.location,
                requested_seniority=criteria.seniority_level,
            )
            scored_pairs.append((p, scoring))

        # Deterministic sorting using people_rank_key
        scored_pairs.sort(key=lambda pair: people_rank_key(pair[0], pair[1]))

        # Filter candidates for result cards strictly by canonical candidate ID (cyb_user.id)
        people_cards = []
        seen_card_ids = set()
        for p, scoring in scored_pairs:
            p_id = p["id"]
            if p_id in seen_card_ids:
                continue

            # Skip candidates who fail explicit numeric experience or explicit hard filters if experience was requested
            if criteria.experience_min is not None and not scoring.get("is_eligible", True):
                continue

            seen_card_ids.add(p_id)

            people_cards.append({
                "id": p["id"],
                "candidate_id": p["id"],
                "cc_id": f"CC-{p['id']}",
                "name": p["name"],
                "title": p["current_title"],
                "company": p["company"],
                "location": p["location"],
                "skills": p["skills"],
                "experience_years": p.get("total_experience_years", 0.0),
                "match_reason": f"Match Score: {scoring.get('final_score', 0.0)}%" if scoring.get('final_score') else "Matched request criteria",
            })

            if len(people_cards) >= 50:
                break

        if not people_cards:
            return PeopleSearchService._format_empty_response(criteria, resolved_location=resolved_loc_name)

        count = len(people_cards)
        loc_label = f" in {resolved_loc_name}" if resolved_loc_name else ""
        comp_label = f" at {criteria.company}" if criteria.company else ""

        db_context_str = (
            f"## PEOPLE SEARCH RESULT SUMMARY\n"
            f"Found {count} professional(s){comp_label}{loc_label}.\n"
            f"The top {count} are displayed below.\n\n"
            "[MANDATORY AI INSTRUCTION]\n"
            f"Write ONE short framing sentence: 'I found {count} professionals matching your request.' Do NOT list raw JSON."
        )

        import math
        per_page = 5
        total_pages = max(1, math.ceil(count / per_page))
        pagination_payload = {
            "current_page": 1,
            "per_page": per_page,
            "page_size": per_page,
            "total_results": count,
            "total_pages": total_pages,
            "start_index": 1 if count > 0 else 0,
            "end_index": min(per_page, count),
            "has_next": count > per_page,
            "has_prev": False,
        }

        return {
            "success": True,
            "status": "SUCCESS",
            "db_context": db_context_str,
            "results": people_cards,
            "result_type": "people",
            "pagination": pagination_payload,
            "raw_hits": [pair[0] for pair in scored_pairs[:count]],
        }

    @staticmethod
    def _format_empty_response(
        criteria: SearchCriteria,
        resolved_location: Optional[str] = None,
        unresolved_location: Optional[str] = None
    ) -> Dict[str, Any]:
        """Formats conversational empty result response without returning raw []."""
        search_terms = []
        if criteria.skills:
            search_terms.append(", ".join(criteria.skills))
        if criteria.title:
            search_terms.append(criteria.title)
        if criteria.company:
            search_terms.append(f"at {criteria.company}")
        if unresolved_location or resolved_location or criteria.location:
            search_terms.append(f"in {unresolved_location or resolved_location or criteria.location}")

        query_str = " ".join(search_terms) if search_terms else "your criteria"

        db_context_str = (
            f"## PEOPLE SEARCH RESULT SUMMARY\n"
            f"No professionals were found matching {query_str}.\n\n"
            "[MANDATORY AI INSTRUCTION]\n"
            "Inform the user in one sentence that no professionals were found matching their request and suggest broadening their search criteria."
        )

        return {
            "success": True,
            "status": "NO_HITS",
            "db_context": db_context_str,
            "results": [],
            "result_type": "people_search_empty",
            "suggestion_chips": ["Search Jobs", "Companies Near Me", "Clear Filters"],
        }
