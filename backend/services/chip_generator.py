"""
chip_generator.py — Canonical Suggestion Chip Generation Service.
Generates suggestion chips for Employee and Employer search flows based on active search criteria,
user location, and result state, incorporating exact suppression, fallback, and formatting rules.
"""

from typing import List, Dict, Any, Optional


def generate_employee_chips(
    parsed_query: Dict[str, Any],
    total_results: int,
    user_location: Optional[str] = None
) -> List[str]:
    """
    Generates dynamic suggestion chips for Employee search context.
    """
    # 1. Zero-result fallback branch
    if total_results == 0:
        return [
            "🔍 Search All Jobs",
            "🌐 Work from Home",
            "📍 Jobs in Delhi",
            "💼 Fresher Jobs"
        ]

    chips: List[str] = []
    query_loc = parsed_query.get("location")
    has_location = bool(query_loc or parsed_query.get("location_mode") == "NEAR_ME")
    has_exp = parsed_query.get("experience") is not None
    has_mode = bool(parsed_query.get("job_mode"))
    has_salary = parsed_query.get("salary") is not None or parsed_query.get("salary_max") is not None

    if not has_location:
        loc_target = user_location.strip() if (user_location and isinstance(user_location, str) and user_location.strip()) else "Delhi"
        chips.append(f"📍 Jobs in {loc_target}")

    if not has_mode:
        chips.append("🌐 Work from Home")

    if not has_exp:
        chips.append("💼 2+ Years Exp")

    if not has_salary:
        chips.append("💰 Under ₹7.5 LPA")

    if has_location and query_loc and str(query_loc).lower().strip() != "india":
        chips.append("🌐 Search All India")

    return chips


def generate_employer_chips(
    employer_reqs: Any,
    total_results: int,
    user_location: Optional[str] = None
) -> List[str]:
    """
    Generates dynamic suggestion chips for Employer candidate search context.
    """
    # 1. Zero-result fallback branch
    if total_results == 0:
        return [
            "🔍 Search All Candidates",
            "📍 Filter by Delhi",
            "⚡ Immediate Joiners",
            "🌐 Search All India"
        ]

    chips: List[str] = []
    if isinstance(employer_reqs, dict):
        loc_val = employer_reqs.get("location")
        exp_val = employer_reqs.get("experience") or employer_reqs.get("min_exp")
        on_imm = employer_reqs.get("on_immediate")
    else:
        loc_val = getattr(employer_reqs, "location", None)
        exp_val = getattr(employer_reqs, "experience", None) or getattr(employer_reqs, "min_exp", None)
        on_imm = getattr(employer_reqs, "on_immediate", None)

    has_loc = False
    if loc_val:
        if isinstance(loc_val, str):
            has_loc = bool(loc_val.strip())
        elif hasattr(loc_val, "text") or hasattr(loc_val, "city_id"):
            has_loc = bool(getattr(loc_val, "text", None) or getattr(loc_val, "city_id", None) or getattr(loc_val, "state_id", None))

    has_exp = exp_val is not None
    if hasattr(exp_val, "min") or hasattr(exp_val, "max"):
        has_exp = (getattr(exp_val, "min", None) is not None) or (getattr(exp_val, "max", None) is not None)

    has_immediate = False
    if on_imm:
        if isinstance(on_imm, bool):
            has_immediate = on_imm
        elif hasattr(on_imm, "value"):
            has_immediate = bool(getattr(on_imm, "value", False))

    if not has_loc:
        loc_target = user_location.strip() if (user_location and isinstance(user_location, str) and user_location.strip()) else "Delhi"
        chips.append(f"📍 Filter by {loc_target}")

    if not has_exp:
        chips.append("💼 2+ Years Exp")

    if not has_immediate:
        chips.append("⚡ Immediate Joiners")

    chips.append("🌐 Search All India")
    return chips


def generate_suggestion_chips(
    context_type: str,
    search_state: Any,
    total_results: int,
    user_location: Optional[str] = None
) -> List[str]:
    """
    Canonical Entry Point for Suggestion Chip Generation.
    """
    norm_type = str(context_type).strip().lower()
    if norm_type == "employer":
        return generate_employer_chips(search_state, total_results, user_location=user_location)
    return generate_employee_chips(search_state if isinstance(search_state, dict) else {}, total_results, user_location=user_location)
