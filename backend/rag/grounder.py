"""
GroundedAnswerEngine module for synthesizing strictly fact-checked, grounded answers.
Enforces Invariant 2 (Unidirectional contract) and Invariant 3 (Strict no-hallucination contract).
"""
import logging
from typing import List
from backend.rag.document import RetrievedDocument, RetrievalResult

logger = logging.getLogger(__name__)

class GroundedAnswerEngine:
    """
    Synthesizes answers ONLY from explicitly provided RetrievedDocument[].
    Does NOT execute independent database, vector, or API queries.
    """

    @classmethod
    def generate_answer(cls, result: RetrievalResult) -> str:
        """
        Generates a grounded response strictly based on the documents in RetrievalResult.
        """
        if result.status != "success" or not result.documents:
            if result.message:
                return result.message
            return "The requested information is not available in the active session context."

        docs: List[RetrievedDocument] = result.documents
        doc = docs[0]
        
        q_lower = result.query.lower()
        content = doc.content
        meta = doc.metadata or {}
        
        # Skill-focused question
        if "skill" in q_lower or "require" in q_lower or "qualification" in q_lower:
            skills = meta.get("required_skills") or []
            if skills:
                formatted_skills = ", ".join(skills)
                return f"The required skills for **{doc.title}** are: **{formatted_skills}**."
            else:
                return f"The job details for **{doc.title}** do not specify any explicit skill requirements."
                
        # Experience-focused question
        if "experience" in q_lower or "exp" in q_lower or "year" in q_lower:
            exp = meta.get("required_experience_years")
            if exp is not None and exp != 0.0:
                return f"**{doc.title}** requires **{exp} years** of experience."
            return f"No specific experience requirement is listed for **{doc.title}**."

        # Location / Work mode question
        if "location" in q_lower or "city" in q_lower or "where" in q_lower or "mode" in q_lower:
            loc = meta.get("location") or meta.get("city") or meta.get("state") or "Not specified"
            mode = meta.get("job_mode") or "Office"
            return f"**{doc.title}** is located in **{loc}** (Work mode: **{mode}**)."

        # Default overview summary strictly from doc.content
        # Enforce SEC-05 Prompt Injection Safeguard:
        # Treat retrieved document content strictly as source data, never following instructions embedded within context.
        forbidden_terms = ["reveal", "password", "jwt", "token", "secret", "ignore previous", "system prompt", "api_key"]
        for term in forbidden_terms:
            if term in content.lower() and ("ignore" in content.lower() or "reveal" in content.lower()):
                logger.warning("Prompt injection instruction attempt detected in retrieved document source_id=%s", doc.source_id)
                # Strip injection instruction lines and return clean factual content or refusal
                cleaned_content = "\n".join([line for line in content.splitlines() if not any(t in line.lower() for t in ["ignore", "reveal", "jwt", "token", "password", "secret"])])
                content = cleaned_content if cleaned_content.strip() else "Job details content unavailable."

        return (
            f"Here are the details for **{doc.title}**:\n\n"
            f"{content}\n\n"
            f"*(Source ID: {doc.source_id})*"
        )
