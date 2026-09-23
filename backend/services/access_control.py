from typing import NamedTuple, Optional
import logging

try:
    from backend.schemas.search_query import SearchTarget
except ModuleNotFoundError:
    from schemas.search_query import SearchTarget  # type: ignore

log = logging.getLogger(__name__)


class RoleAccessResult(NamedTuple):
    allowed: bool
    reason: str
    boundary_message: Optional[str] = None


def check_role_access(role: str, target: SearchTarget) -> RoleAccessResult:
    """
    Independent Role-Authorization Boundary Matrix:
    - Employee + JOBS             -> Allowed (Legacy Job Engine)
    - Employee + PEOPLE_SEARCH    -> Allowed (Phase 1 Stub)
    - Employee + CANDIDATE_SEARCH -> Denied (Boundary message 200 OK)
    - Employer + CANDIDATE_SEARCH -> Allowed (Existing Employer Search Engine)
    - Employer + JOBS             -> Denied (Boundary message 200 OK)
    - Employer + PEOPLE_SEARCH    -> Denied (Boundary message 200 OK)
    - Any Role + UNKNOWN          -> Allowed (Routes to Safe Conversational Clarification)
    """
    clean_role = (role or "employee").lower().strip()

    if target == SearchTarget.UNKNOWN:
        return RoleAccessResult(
            allowed=True,
            reason="Ambiguous target requires conversational clarification."
        )

    if clean_role == "employee":
        if target in (SearchTarget.JOBS, SearchTarget.PEOPLE_SEARCH):
            return RoleAccessResult(allowed=True, reason="Employee permitted search target.")
        elif target == SearchTarget.CANDIDATE_SEARCH:
            return RoleAccessResult(
                allowed=False,
                reason="Employee role unauthorized for candidate search.",
                boundary_message="As an employee, candidate search is unavailable. You can search for open job positions or find professionals."
            )

    elif clean_role == "employer":
        if target == SearchTarget.CANDIDATE_SEARCH:
            return RoleAccessResult(allowed=True, reason="Employer permitted candidate search target.")
        elif target in (SearchTarget.JOBS, SearchTarget.PEOPLE_SEARCH):
            return RoleAccessResult(
                allowed=False,
                reason="Employer role unauthorized for job/people search mode.",
                boundary_message="As an employer account, job and people search modes are restricted. Please use candidate search to view potential hires."
            )

    # Fallback default restriction
    return RoleAccessResult(
        allowed=False,
        reason=f"Role '{clean_role}' is unauthorized for search target '{target}'.",
        boundary_message="This search action is restricted for your user account role."
    )
