import re
import logging
from typing import Dict, Any, Optional, Tuple
from backend.schemas.search_query import (
    SearchQuery,
    SearchSnapshot,
    ChatbotIntent,
    SearchMode,
    LocationMode,
    LocationSource,
    ProfileStatus,
    SortMode,
    MAX_SNAPSHOT_CANDIDATES,
)
from backend.utils.session_state import (
    get_search_context,
    set_search_context,
    get_search_snapshot,
    set_search_snapshot,
)
from backend.utils.location_resolver import LocationResolver

log = logging.getLogger(__name__)


class SearchPlanner:
    """
    SearchPlanner determines WHAT to search for:
    - Maintains conversational search context across turns.
    - Merges filter additions, removals, role replacements, and radius updates.
    - Manages SearchSnapshot creation, lookup, and invalidation using query fingerprinting.
    """

    PAGINATION_PATTERNS = [
        r"^(next|more|next page|show more|more jobs|load more)$",
        r"^(page\s*\d+)$",
        r"^(previous|prev|prev page|previous page)$",
    ]

    REMOVE_FILTER_PATTERNS = [
        r"\b(remove|clear)\s+remote(\s+filter)?\b",
        r"\b(remove|clear)\s+location(\s+filter)?\b",
        r"\b(remove|clear)\s+experience(\s+filter)?\b",
        r"\b(remove|clear)\s+salary(\s+filter)?\b",
        r"\b(remove|clear)\s+all\s+filters?\b",
    ]

    ROLE_SUBSTITUTION_PATTERNS = [
        re.compile(r"^\s*change\s+(?P<old_role>.+?)\s+to\s+(?P<new_role>.+?)\s*$", re.IGNORECASE),
        re.compile(r"^\s*switch\s+(?P<old_role>.+?)\s+to\s+(?P<new_role>.+?)\s*$", re.IGNORECASE),
        re.compile(r"^\s*replace\s+(?P<old_role>.+?)\s+with\s+(?P<new_role>.+?)\s*$", re.IGNORECASE),
    ]

    RESERVED_ROLE_TERMS = {
        "location", "salary", "experience", "remote", "wfh", "profile", "filter", "filters",
        "the salary filter", "the location filter", "the remote filter", "the experience filter"
    }

    CHANGE_ROLE_PATTERNS = [
        r"change\s+to\s+([a-zA-Z0-9\+\#\.\s]+)",
        r"search\s+for\s+([a-zA-Z0-9\+\#\.\s]+)\s+instead",
    ]

    CHANGE_RADIUS_PATTERNS = [
        r"change\s+(\d+)\s*km\s+to\s+(\d+)\s*km",
        r"within\s+(\d+)\s*km",
        r"radius\s+(\d+)\s*km",
    ]

    @classmethod
    def is_pagination_command(cls, message: str) -> Tuple[bool, int]:
        """
        Checks if a message is a pagination command.
        Returns (is_pagination, requested_page_delta_or_number).
        """
        msg = (message or "").strip().lower()
        if not msg:
            return False, 0

        for pattern in cls.PAGINATION_PATTERNS:
            match = re.search(pattern, msg)
            if match:
                matched_str = match.group(1)
                if "prev" in matched_str:
                    return True, -1
                elif "page" in matched_str and matched_str != "next page" and matched_str != "prev page":
                    page_num_match = re.search(r"\d+", matched_str)
                    if page_num_match:
                        return True, int(page_num_match.group(0))
                return True, 1  # Next page

        return False, 0

    @classmethod
    def build_or_refine_query(
        cls,
        session_id: str,
        user_message: str,
        parsed_query_dict: Dict[str, Any],
        request_lat: Optional[float] = None,
        request_lon: Optional[float] = None,
        profile_data: Optional[Dict[str, Any]] = None,
    ) -> Tuple[SearchQuery, bool]:
        """
        Builds a new SearchQuery or refines existing search context.
        Returns (SearchQuery, is_context_modified).
        """
        msg = (user_message or "").strip().lower()

        # Load existing search context
        existing_context = get_search_context(session_id) or {}
        context_modified = False

        # Evaluate profile status
        profile_status = ProfileStatus.PROFILE_UNAVAILABLE
        profile_location = None
        if profile_data:
            if profile_data.get("status") == "API_ERROR":
                profile_status = ProfileStatus.PROFILE_API_ERROR
            elif profile_data.get("skills"):
                profile_status = ProfileStatus.PROFILE_AVAILABLE
            else:
                profile_status = ProfileStatus.PROFILE_PARTIAL

            # Extract profile location
            profile_location = (
                profile_data.get("city")
                or profile_data.get("city_name")
                or profile_data.get("location")
            )

        # 1. EVALUATE EXPLICIT ROLE SUBSTITUTION FIRST (IF VALID & ACTIVE SEARCH CONTEXT EXISTS)
        role_substituted = False
        if existing_context and (existing_context.get("title") or existing_context.get("location") or existing_context.get("work_mode") or existing_context.get("experience_level")):
            for pattern in cls.ROLE_SUBSTITUTION_PATTERNS:
                match = pattern.search(user_message or "")
                if match:
                    extracted_new_role = match.group("new_role").strip()
                    # Strip terminal role suffixes ONLY (jobs, job, roles, role, positions, position)
                    clean_new_role = re.sub(r"\s+(?:jobs?|roles?|positions?)$", "", extracted_new_role, flags=re.IGNORECASE).strip()

                    # Validate new_role is not a reserved filter keyword or compound query
                    is_reserved = clean_new_role.lower() in cls.RESERVED_ROLE_TERMS
                    is_compound = bool(re.search(r"\b(and|or|plus|also|remove|clear)\b", clean_new_role, re.IGNORECASE))
                    if clean_new_role and not is_reserved and not is_compound:
                        old_role = match.group("old_role").strip()
                        if existing_context.get("title") != clean_new_role:
                            existing_context["title"] = clean_new_role
                            context_modified = True
                            role_substituted = True
                            log.info(
                                "Role substitution detected | Old role: %s | New role: %s | Updated title: %s | Preserved location: %s | Preserved experience: %s",
                                old_role, clean_new_role, existing_context.get("title"), existing_context.get("location"), existing_context.get("experience_level")
                            )
                        break

        # 2. HANDLE CONVERSATIONAL FILTER REMOVALS (IF NOT ROLE SUBSTITUTED)
        if not role_substituted:
            if re.search(r"\b(remove|clear)\s+remote(\s+filter)?\b", msg):
                if "work_mode" in existing_context:
                    existing_context.pop("work_mode", None)
                    context_modified = True

            if re.search(r"\b(remove|clear)\s+location(\s+filter)?\b", msg):
                for k in ["location", "canonical_location", "metro_cluster", "location_mode", "location_source", "latitude", "longitude", "radius_km"]:
                    if k in existing_context:
                        existing_context.pop(k, None)
                        context_modified = True

            if re.search(r"\b(remove|clear)\s+experience(\s+filter)?\b", msg):
                if "experience_level" in existing_context:
                    existing_context.pop("experience_level", None)
                    context_modified = True

            if re.search(r"\b(remove|clear)\s+salary(\s+filter)?\b", msg):
                for k in ["salary_min", "salary_max"]:
                    if k in existing_context:
                        existing_context.pop(k, None)
                        context_modified = True

        # 3. HANDLE RADIUS CHANGE ("change 5 km to 10 km" / "within 10 km")
        radius_match = re.search(r"(\d+)\s*km", msg)
        if radius_match and ("within" in msg or "radius" in msg or "change" in msg):
            new_radius = float(radius_match.group(1))
            if existing_context.get("radius_km") != new_radius:
                existing_context["radius_km"] = new_radius
                context_modified = True

        # 4. CONDITIONAL PAGINATION RESET (ONLY IF CONTEXT MODIFIED)
        if context_modified:
            try:
                from backend.utils.session_state import clear_pagination_state
            except ModuleNotFoundError:
                from utils.session_state import clear_pagination_state  # type: ignore
            clear_pagination_state(session_id)

        # Extract parsed intent & search mode
        raw_intent = parsed_query_dict.get("intent", "JOB_SEARCH")
        raw_search_mode = parsed_query_dict.get("search_mode", "HYBRID")

        intent = ChatbotIntent.JOB_SEARCH
        if raw_intent == "PROFILE":
            intent = ChatbotIntent.PROFILE
        elif raw_intent == "SMALLTALK":
            intent = ChatbotIntent.SMALLTALK

        search_mode = SearchMode.HYBRID
        if raw_search_mode == "SEARCH_ONLY":
            search_mode = SearchMode.SEARCH_ONLY
        elif raw_search_mode == "RECOMMENDATION":
            search_mode = SearchMode.RECOMMENDATION

        # Resolve fields from new parsed query
        parsed_title = parsed_query_dict.get("title") or parsed_query_dict.get("keyword")
        parsed_location = parsed_query_dict.get("location")
        parsed_loc_mode = parsed_query_dict.get("location_mode")
        parsed_radius = parsed_query_dict.get("radius_km")
        parsed_exp = parsed_query_dict.get("experience_level")
        parsed_work_mode = parsed_query_dict.get("work_mode")
        parsed_company = parsed_query_dict.get("company")
        parsed_salary_min = parsed_query_dict.get("salary_min")
        parsed_salary_max = parsed_query_dict.get("salary_max")
        parsed_skills = parsed_query_dict.get("skills") or []

        # Update context fields if parsed in query
        if parsed_title and not role_substituted:
            if existing_context.get("title") != parsed_title:
                existing_context["title"] = parsed_title
                context_modified = True

        if parsed_exp and existing_context.get("experience_level") != parsed_exp:
            existing_context["experience_level"] = parsed_exp
            context_modified = True

        if parsed_work_mode and existing_context.get("work_mode") != parsed_work_mode:
            existing_context["work_mode"] = parsed_work_mode
            context_modified = True

        if parsed_company and existing_context.get("company") != parsed_company:
            existing_context["company"] = parsed_company
            context_modified = True

        if parsed_skills and existing_context.get("skills") != parsed_skills:
            existing_context["skills"] = parsed_skills
            context_modified = True

        if parsed_salary_min:
            existing_context["salary_min"] = float(parsed_salary_min)
            context_modified = True

        if parsed_salary_max:
            existing_context["salary_max"] = float(parsed_salary_max)
            context_modified = True

        # Resolve location parameters via LocationResolver
        loc_res = LocationResolver.resolve_location(
            parsed_location=parsed_location or existing_context.get("location"),
            parsed_location_mode=parsed_loc_mode or existing_context.get("location_mode"),
            parsed_radius_km=parsed_radius or existing_context.get("radius_km"),
            request_lat=request_lat or existing_context.get("latitude"),
            request_lon=request_lon or existing_context.get("longitude"),
            profile_location=profile_location,
        )

        existing_context.update({
            "location_mode": loc_res["location_mode"].value if isinstance(loc_res["location_mode"], LocationMode) else str(loc_res["location_mode"]),
            "location_source": loc_res["location_source"].value if isinstance(loc_res["location_source"], LocationSource) else (str(loc_res["location_source"]) if loc_res["location_source"] else None),
            "location": loc_res["location"],
            "canonical_location": loc_res["canonical_location"],
            "metro_cluster": loc_res["metro_cluster"],
            "latitude": loc_res["latitude"],
            "longitude": loc_res["longitude"],
            "radius_km": loc_res["radius_km"],
        })

        # Save updated context
        set_search_context(session_id, existing_context)

        # Build SearchQuery model
        query = SearchQuery(
            intent=intent,
            search_mode=search_mode,
            profile_status=profile_status,
            title=existing_context.get("title"),
            skills=existing_context.get("skills", []),
            location_mode=LocationMode(existing_context["location_mode"]),
            location_source=LocationSource(existing_context["location_source"]) if existing_context.get("location_source") else None,
            location=existing_context.get("location"),
            canonical_location=existing_context.get("canonical_location"),
            metro_cluster=existing_context.get("metro_cluster", []),
            latitude=existing_context.get("latitude"),
            longitude=existing_context.get("longitude"),
            radius_km=existing_context.get("radius_km"),
            experience_level=existing_context.get("experience_level"),
            salary_min=existing_context.get("salary_min"),
            salary_max=existing_context.get("salary_max"),
            work_mode=existing_context.get("work_mode"),
            company=existing_context.get("company"),
            sort_by=SortMode.RECOMMENDATION,
            use_profile=(profile_status in [ProfileStatus.PROFILE_AVAILABLE, ProfileStatus.PROFILE_PARTIAL]),
            use_recommendation=(search_mode != SearchMode.SEARCH_ONLY),
        )

        return query, context_modified
