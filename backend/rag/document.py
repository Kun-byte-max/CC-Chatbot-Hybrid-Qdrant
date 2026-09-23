"""
RAG document data structures for retrieval and grounding.
"""
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

@dataclass
class RetrievedDocument:
    source_type: str  # "job", "company", "faq", "candidate"
    source_id: str    # e.g., job_id, company_name
    title: str
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    score: float = 1.0

@dataclass
class RetrievalResult:
    query: str
    documents: List[RetrievedDocument] = field(default_factory=list)
    resolved_target_id: Optional[str] = None
    target_type: Optional[str] = None
    status: str = "success"  # "success", "unresolved_reference", "empty"
    message: str = ""
