import logging
from typing import Dict, Any, Optional

try:
    from backend.schemas.search_query import SearchTarget
    from backend.api.employee import handle_employee_context
    from backend.api.employer import handle_employer_context
except ModuleNotFoundError:
    from schemas.search_query import SearchTarget  # type: ignore
    from api.employee import handle_employee_context  # type: ignore
    from api.employer import handle_employer_context  # type: ignore

log = logging.getLogger(__name__)


def build_clarification_response() -> Dict[str, Any]:
    """Returns safe conversational clarification response when query target is UNKNOWN."""
    return {
        "db_context": None,
        "results": None,
        "result_type": "clarification_prompt",
        "allowed_ids": [],
        "raw_hits": [],
        "explanation": "Could you clarify if you're looking for job openings, professionals/colleagues, or candidate resumes to hire?",
        "suggestion_chips": ["Search for Jobs", "Find Professionals", "Search Candidates"],
    }


async def dispatch_search_target(
    target: SearchTarget,
    role: str,
    last_user_msg: str,
    messages: Optional[list] = None,
    user_location: Optional[Any] = None,
    user_skills: Optional[list] = None,
    user_roles: Optional[list] = None,
    candidate_id: Optional[Any] = None,
    token: Optional[str] = None,
    user_slug: Optional[str] = None,
    user_profile_api_data: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = "default_session",
    **kwargs: Any
) -> Dict[str, Any]:
    """
    Pure Search Router Dispatcher.
    Answers: "Given an already-classified and authorized target, which handler should receive it?"
    Reuses existing handlers with ZERO code duplication.
    """
    clean_role = (role or "employee").lower().strip()

    if target == SearchTarget.UNKNOWN:
        return build_clarification_response()

    if target == SearchTarget.JOBS and clean_role == "employee":
        return await handle_employee_context(
            last_user_msg=last_user_msg,
            messages=messages,
            user_location=user_location,
            user_skills=user_skills,
            user_roles=user_roles,
            candidate_id=candidate_id,
            token=token,
            user_slug=user_slug,
            user_profile_api_data=user_profile_api_data,
            session_id=session_id,
            **kwargs
        )

    elif target == SearchTarget.PEOPLE_SEARCH and clean_role == "employee":
        # Dispatches to handle_employee_context which handles Phase 1 PEOPLE_SEARCH capability stub
        return await handle_employee_context(
            last_user_msg=last_user_msg,
            messages=messages,
            user_location=user_location,
            user_skills=user_skills,
            user_roles=user_roles,
            candidate_id=candidate_id,
            token=token,
            user_slug=user_slug,
            user_profile_api_data=user_profile_api_data,
            session_id=session_id,
            **kwargs
        )

    elif target == SearchTarget.CANDIDATE_SEARCH and clean_role == "employer":
        # Reuses existing employer handler execution pipeline directly
        return await handle_employer_context(
            last_user_msg=last_user_msg,
            messages=messages,
            user_location=user_location,
            candidate_id=candidate_id,
            token=token,
            user_slug=user_slug,
            user_profile_api_data=user_profile_api_data,
            session_id=session_id,
            **kwargs
        )

    # Safety fallback
    return build_clarification_response()
