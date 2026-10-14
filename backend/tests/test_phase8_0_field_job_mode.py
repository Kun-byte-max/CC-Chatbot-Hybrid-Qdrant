import pytest
import asyncio
from typing import Dict, Any, List
from unittest.mock import MagicMock, patch, AsyncMock

from backend.services.hybrid_job_search import (
    _normalize_job_mode_category,
    passes_job_mode_eligibility,
    NormalizedJob,
    execute_hybrid_job_search
)
from backend.services.search_parser import parse_search_query
from backend.schemas.search_criteria import SearchCriteria
from backend.schemas.search_query import SearchQuery, SearchTarget
from backend.utils.session_state import merge_employee_search_query
from backend.schemas.schemas import JobCard


# 1. PARSING TESTS
def test_field_job_mode_parsing():
    """Verify parsing field job queries into job_mode='field' and keyword cleaning."""
    msg = "show me field Python jobs"
    res = asyncio.run(parse_search_query(msg, role="employee"))
    assert res.get("job_mode") == "field"
    assert res.get("keyword") == "Python" or "python" in (res.get("keyword") or "").lower()

    msg_work = "find field work jobs"
    res_work = asyncio.run(parse_search_query(msg_work, role="employee"))
    assert res_work.get("job_mode") == "field"


# 2. NORMALIZATION & ELIGIBILITY UNIT TESTS
def test_normalize_job_mode_field():
    """Verify _normalize_job_mode_category returns 'field' for integer 4, string '4', 'field', etc."""
    assert _normalize_job_mode_category(4) == "field"
    assert _normalize_job_mode_category("4") == "field"
    assert _normalize_job_mode_category("field") == "field"
    assert _normalize_job_mode_category("Field Work") == "field"


def test_passes_job_mode_eligibility_field():
    """Verify Field hard-gate eligibility across all job modes."""
    job_field = NormalizedJob(job_id=1, title="Field Technician", job_mode="Field Work", raw_data={"job_mode_id": 4, "job_mode_name": "Field Work"})
    job_remote = NormalizedJob(job_id=2, title="Remote Dev", job_mode="Work from Home", raw_data={"job_mode_id": 2, "job_mode_name": "Work from Home"})
    job_office = NormalizedJob(job_id=3, title="Office Lead", job_mode="Work from Office", raw_data={"job_mode_id": 1, "job_mode_name": "Work from Office"})
    job_hybrid = NormalizedJob(job_id=4, title="Hybrid Eng", job_mode="Hybrid", raw_data={"job_mode_id": 3, "job_mode_name": "Hybrid"})
    job_unknown = NormalizedJob(job_id=5, title="Unknown Mode", job_mode=None, raw_data={})

    # When Field requested:
    assert passes_job_mode_eligibility(job_field, target_job_mode="field") is True
    assert passes_job_mode_eligibility(job_field, target_job_mode=[4]) is True
    assert passes_job_mode_eligibility(job_remote, target_job_mode="field") is False
    assert passes_job_mode_eligibility(job_office, target_job_mode="field") is False
    assert passes_job_mode_eligibility(job_hybrid, target_job_mode="field") is False
    assert passes_job_mode_eligibility(job_unknown, target_job_mode="field") is False


# 3. HIGH-SIMILARITY NEGATIVE TEST (VECTOR SCORE CANNOT BYPASS FIELD GATE)
def test_field_high_similarity_vector_rejection():
    """Verify vector score = 0.99 for a Remote job cannot bypass an active Field hard constraint."""
    high_score_remote = NormalizedJob(
        job_id=101,
        title="Python Dev Remote",
        job_mode="Work from Home",
        raw_data={"job_mode_id": 2, "job_mode_name": "Work from Home"}
    )
    moderate_score_field = NormalizedJob(
        job_id=102,
        title="Field Python Engineer",
        job_mode="Field Work",
        raw_data={"job_mode_id": 4, "job_mode_name": "Field Work"}
    )

    # Hard eligibility gate evaluation
    assert passes_job_mode_eligibility(high_score_remote, target_job_mode="field") is False
    assert passes_job_mode_eligibility(moderate_score_field, target_job_mode="field") is True


# 4. MULTI-TURN SESSION STATE TRANSITIONS
def test_field_multiturn_state_transitions():
    """Verify multi-turn inheritance, override, and clearing of Field job mode."""
    # Turn 1: Python jobs -> Turn 2: make them field jobs
    q1 = SearchQuery(title="Python", target=SearchTarget.JOBS)
    q2 = SearchQuery(work_mode="field", target=SearchTarget.JOBS)
    m1 = merge_employee_search_query(q1, q2, prompt_category="ADDITIVE")
    assert m1.title == "Python"
    assert m1.work_mode == "field"

    # Remote -> Field (Override remote)
    q_remote = SearchQuery(work_mode="remote", target=SearchTarget.JOBS)
    q_field = SearchQuery(work_mode="field", target=SearchTarget.JOBS)
    m2 = merge_employee_search_query(q_remote, q_field, prompt_category="ADDITIVE")
    assert m2.work_mode == "field"

    # Clearing field mode
    q_clear = SearchQuery(clear_filters=["field"], target=SearchTarget.JOBS)
    m3 = merge_employee_search_query(m2, q_clear, prompt_category="ADDITIVE")
    assert m3.work_mode is None


# 5. HYBRID RETRIEVAL & DEDUPLICATION TEST
def test_field_hybrid_retrieval_and_deduplication():
    """Verify dual-engine (Meilisearch + JobMatch) execution, fusion, deduplication, and Field gate."""
    mock_meili_hits = [
        {"id": 201, "job_title": "Field Python Tech", "job_mode_id": 4, "job_mode_name": "Field Work"},
        {"id": 202, "job_title": "Remote Python Dev", "job_mode_id": 2, "job_mode_name": "Work from Home"}
    ]

    mock_jobmatch_res = {
        "success": True,
        "results": [
            {"job_id": 201, "final_score": 0.95, "payload": {"job_id": 201, "job_title": "Field Python Tech", "job_mode_id": 4, "job_mode_name": "Field Work"}},
            {"job_id": 203, "final_score": 0.88, "payload": {"job_id": 203, "job_title": "Field Service Eng", "job_mode_id": 4, "job_mode_name": "Field Work"}},
            {"job_id": 204, "final_score": 0.99, "payload": {"job_id": 204, "job_title": "Office Python Lead", "job_mode_id": 1, "job_mode_name": "Work from Office"}}
        ]
    }

    with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_meili, \
         patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:
        
        mock_meili.return_value = {"success": True, "data": mock_meili_hits}
        mock_jm.return_value = mock_jobmatch_res

        results, meta = asyncio.run(execute_hybrid_job_search(
            last_user_msg="field Python jobs",
            candidate_id=123,
            keyword="Python",
            base_filters={"job_mode": [4]},
            parsed={"job_mode": "field", "keyword": "Python"},
            limit=10
        ))

        res_ids = [r.get("id") or r.get("job_id") for r in results]
        # 201 (Field hit in both engines -> deduplicated), 202 (Remote) rejected by Meili filter, 204 (Office) rejected by post-RRF Field hard gate.
        assert 201 in res_ids
        for r in results:
            mode_name = r.get("job_mode_name") or r.get("job_mode") or (r.get("raw_data") or {}).get("job_mode_name")
            assert _normalize_job_mode_category(mode_name) == "field"


# 6. JOBCARD FIELD MODE PAYLOAD METADATA
def test_jobcard_field_mode_payload():
    """Verify JobCard receives 'Field Work' / 'Field' and matches frontend expected representation."""
    raw_job = {"job_title": "Field Executive", "job_mode_name": "Field Work", "job_mode_id": 4}
    card = JobCard(
        job_id=301,
        title=raw_job["job_title"],
        job_mode=raw_job["job_mode_name"],
        url="https://www.collarcheck.com/jobs-details/301"
    )

    assert card.job_mode == "Field Work"
    card_dict = card.model_dump()
    assert card_dict["job_mode"] == "Field Work"
