"""
KnowledgeRetriever module for extracting authoritative job and company context.
Enforces strict reference resolution rules (current-page position vs DB primary keys)
and role-isolated session context access.
"""
import re
from typing import Optional, Dict, Any, List
from backend.rag.document import RetrievedDocument, RetrievalResult
from backend.utils.session_state import get_recommendation_session
from backend.repositories.job_repository import JobRepository
from backend.recommendation.recommendation_service import recommendation_service

def _get_job_dict_from_session(rec_session: Any, target_id_str: str) -> Optional[Dict[str, Any]]:
    """Helper to resolve job dict from RecommendationSession."""
    if not rec_session or not target_id_str:
        return None
    # 1. Check all_recs / cached dicts
    all_recs = getattr(rec_session, "all_recs", []) or []
    for r in all_recs:
        j_id = str(getattr(r, "job_id", None) if hasattr(r, "job_id") else (r.get("id") or r.get("job_id") if isinstance(r, dict) else ""))
        if j_id == target_id_str:
            if isinstance(r, dict):
                return r
            raw_job = getattr(r, "raw_data", {}) if hasattr(r, "raw_data") else {}
            title = getattr(r.title_result, "job_title", None) if hasattr(r, "title_result") else None
            skills = getattr(r.skill_result, "matched_skills", []) if hasattr(r, "skill_result") else []
            return {
                "id": j_id,
                "title": title or raw_job.get("job_title") or "Job",
                "company_name": raw_job.get("company_name") or "Company",
                "required_skills": skills or (raw_job.get("required_skills") if isinstance(raw_job, dict) else []),
                "required_experience_years": getattr(r.exp_result, "required_exp", 0.0) if hasattr(r, "exp_result") else 0.0,
                "location": raw_job.get("location") or raw_job.get("city_name"),
                "job_mode": raw_job.get("job_mode") or "Office",
                "raw_data": raw_job
            }
    # 2. Query via JobRepository if missing from in-memory snapshot
    try:
        from backend.repositories.job_repository import JobRepository
        conn = JobRepository.get_active_jobs(limit=100)
        for j in conn:
            if str(j.get("id")) == target_id_str:
                return j
    except Exception:
        pass
    return None

class KnowledgeRetriever:
    """
    Authoritative Knowledge Retriever over MySQL / JobRepository data.
    """
    
    @staticmethod
    def _parse_ordinal_or_rank(query: str) -> Optional[int]:
        """
        Parses ordinal references ("first", "second", "3rd", "position 1", "job #1")
        returning 1-indexed display position on current page.
        """
        q_lower = query.lower().strip()
        
        ordinals = {
            "first": 1,
            "1st": 1,
            "second": 2,
            "2nd": 2,
            "third": 3,
            "3rd": 3,
            "fourth": 4,
            "4th": 4,
            "fifth": 5,
            "5th": 5
        }
        
        for word, val in ordinals.items():
            if re.search(r"\b" + re.escape(word) + r"\b", q_lower):
                return val
                
        # Check explicit rank match e.g. "job #1", "job 1", "position 2"
        match = re.search(r"(?:job|position|result)\s*#?\s*(\d+)", q_lower)
        if match:
            return int(match.group(1))
            
        return None

    @staticmethod
    def _is_relative_reference(query: str) -> bool:
        """Checks for 'this job', 'that job', 'previous job', 'the job'."""
        q_lower = query.lower()
        patterns = [r"\bthis job\b", r"\bthat job\b", r"\bthe job\b", r"\bprevious job\b", r"\bthe role\b"]
        return any(re.search(p, q_lower) for p in patterns)

    @classmethod
    def retrieve_job_knowledge(
        cls,
        user_id: str,
        session_id: str,
        query: str,
        role: str = "employee"
    ) -> RetrievalResult:
        """
        Retrieves authoritative job document based on session reference or query criteria.
        Role-isolated: employee uses (user_id, session_id), employer uses (employer_id, session_id).
        """
        # Ensure role-isolated composite session key
        rec_ctx = get_recommendation_session(session_id, role=role) or get_recommendation_session(user_id, role=role)
        if not rec_ctx:
            # Fall back to checking session_state directly if composite key formatted
            comp_key = f"{role}:{user_id}:{session_id}" if user_id and session_id else (session_id or user_id or "default")
            rec_ctx = get_recommendation_session(comp_key) or {}

        target_job_dict: Optional[Dict[str, Any]] = None
        ordinal_pos = cls._parse_ordinal_or_rank(query)
        is_relative = cls._is_relative_reference(query)
        
        curr_page_ids = rec_ctx.get("last_recommendation_ids") or [] if isinstance(rec_ctx, dict) else (getattr(rec_ctx, "last_recommendation_ids", []) or [])
        last_ref_id = rec_ctx.get("last_referenced_job_id") if isinstance(rec_ctx, dict) else getattr(rec_ctx, "last_referenced_job_id", None)

        if ordinal_pos is not None:
            # Current-page ordinal resolution
            if not curr_page_ids:
                return RetrievalResult(
                    query=query,
                    status="unresolved_reference",
                    message="No recent job recommendations found in this session. Please perform a job search first."
                )
            if 1 <= ordinal_pos <= len(curr_page_ids):
                target_id = curr_page_ids[ordinal_pos - 1]
                target_job_dict = _get_job_dict_from_session(rec_ctx, str(target_id))
            else:
                return RetrievalResult(
                    query=query,
                    status="unresolved_reference",
                    message=f"Display position #{ordinal_pos} is out of bounds for the current page ({len(curr_page_ids)} jobs displayed)."
                )
        elif is_relative:
            # Resolve most recently referenced job ID
            if last_ref_id:
                target_job_dict = _get_job_dict_from_session(rec_ctx, str(last_ref_id))
            elif curr_page_ids:
                # Fall back to display position 1 of active page if last_referenced_job_id not explicitly set
                target_id = curr_page_ids[0]
                target_job_dict = _get_job_dict_from_session(rec_ctx, str(target_id))
            else:
                return RetrievalResult(
                    query=query,
                    status="unresolved_reference",
                    message="No active job reference found in this session. Please search or select a job first."
                )
        else:
            # Fall back to active referenced job if session exists
            if last_ref_id:
                target_job_dict = _get_job_dict_from_session(rec_ctx, str(last_ref_id))
            elif curr_page_ids:
                target_id = curr_page_ids[0]
                target_job_dict = _get_job_dict_from_session(rec_ctx, str(target_id))

        if not target_job_dict:
            return RetrievalResult(
                query=query,
                status="unresolved_reference",
                message="Could not resolve a specific job context from your request."
            )

        # Build authoritative RetrievedDocument from target_job_dict
        job_id = str(target_job_dict.get("id", ""))
        title = target_job_dict.get("title") or target_job_dict.get("job_title") or "Job Details"
        company = target_job_dict.get("company_name") or "Company"
        skills = target_job_dict.get("required_skills") or []
        exp = target_job_dict.get("required_experience_years", "Not specified")
        loc = target_job_dict.get("location") or target_job_dict.get("city") or target_job_dict.get("state") or "Not specified"
        mode = target_job_dict.get("job_mode", "Office")
        raw = target_job_dict.get("raw_data") or {}
        desc = raw.get("job_description") or target_job_dict.get("preview") or ""
        
        # Clean HTML from description
        desc_clean = re.sub(r"<[^>]+>", " ", str(desc))
        desc_clean = re.sub(r"\s+", " ", desc_clean).strip()

        content_parts = [
            f"Job Title: {title}",
            f"Company: {company}",
            f"Required Skills: {', '.join(skills) if skills else 'Not specified'}",
            f"Required Experience: {exp} years",
            f"Location: {loc}",
            f"Work Mode: {mode}",
            f"Job Description: {desc_clean}"
        ]
        
        doc = RetrievedDocument(
            source_type="job",
            source_id=job_id,
            title=f"{title} at {company}",
            content="\n".join(content_parts),
            metadata=target_job_dict
        )

        return RetrievalResult(
            query=query,
            documents=[doc],
            resolved_target_id=job_id,
            target_type="job",
            status="success"
        )
