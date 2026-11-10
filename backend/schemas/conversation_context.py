from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, Union

class ConversationContext(BaseModel):
    """
    Typed, read-only facade representing an instantaneous snapshot of conversation
    session state. Does not own or persist state independently; all fields originate from
    backend.utils.session_state authoritative domains.
    """
    # 1. Identity & Provenance
    session_id: str
    role: str = "employee"
    user_id: Optional[Union[str, int]] = None
    composite_key: str
    context_version: int = 1
    last_active_at: Optional[float] = None
    
    # 2. Search Generation & Result-Set Provenance
    search_generation: int = 1
    has_active_search_context: bool = False
    active_search_criteria: Optional[Dict[str, Any]] = None
    
    # 3. Active Recommendation Pool (Job Results)
    has_active_job_results: bool = False
    active_job_count: int = 0
    last_referenced_job_id: Optional[Union[int, str]] = None
    
    # 4. Employment Disambiguation Context (Step 5A Isolation)
    pending_option_map: Optional[Dict[str, str]] = None
    pending_requested_level: Optional[int] = None
    selected_employment_context: Optional[str] = None
    selected_employment_record_key: Optional[str] = None
    
    # 5. Pagination Metadata Pointer
    has_active_pagination: bool = False
    current_page: int = 1
    total_pages: int = 0
    total_results: int = 0
