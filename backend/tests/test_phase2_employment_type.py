import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from backend.services.search_parser import parse_search_query, resolve_job_role_type_id
from backend.schemas.search_query import SearchQuery
from backend.schemas.search_criteria import SearchCriteria
from backend.utils.session_state import merge_employee_search_query
from backend.services.hybrid_job_search import (
    execute_hybrid_job_search,
    passes_employment_type_eligibility,
    NormalizedJob,
)


# ============================================================================
# A. Resolver Tests
# ============================================================================

def test_resolve_job_role_type_id_valid():
    """Verify job role_type resolver maps job taxonomy IDs: 1=Full Time, 2=Part Time, 3=Internship."""
    assert resolve_job_role_type_id("full time") == 1
    assert resolve_job_role_type_id("full-time") == 1
    assert resolve_job_role_type_id("fulltime") == 1

    assert resolve_job_role_type_id("part time") == 2
    assert resolve_job_role_type_id("part-time") == 2
    assert resolve_job_role_type_id("parttime") == 2

    assert resolve_job_role_type_id("internship") == 3
    assert resolve_job_role_type_id("intern") == 3


def test_resolve_job_role_type_id_unknown_and_candidate_isolation():
    """Verify unknown input returns None and candidate Internship ID (5) is NOT used for jobs."""
    assert resolve_job_role_type_id("unknown") is None
    assert resolve_job_role_type_id(None) is None
    assert resolve_job_role_type_id("") is None
    # Candidate ID 5 (Internship in cyb_employement_type) must resolve to None or 3 in job taxonomy
    assert resolve_job_role_type_id("internship") != 5
    assert resolve_job_role_type_id("internship") == 3


# ============================================================================
# B. Parser Extraction Tests
# ============================================================================

@pytest.mark.anyio
async def test_parser_extracts_internship_jobs():
    """Verify 'internship jobs' extracts employment_type='internship', role_type_id=3, and work_mode=None."""
    res = await parse_search_query("internship jobs", role="employee")
    assert res.get("employment_type") == "internship"
    assert res.get("role_type_id") == 3
    assert res.get("job_mode") is None
    assert res.get("keyword") is None


@pytest.mark.anyio
async def test_parser_part_time_python_no_work_mode_coercion():
    """Verify 'part time Python jobs' extracts part-time/2, skills/title Python, and work_mode=None (not home)."""
    res = await parse_search_query("part time Python jobs", role="employee")
    assert res.get("employment_type") == "part-time"
    assert res.get("role_type_id") == 2
    assert res.get("job_mode") is None  # Must NOT be coerced to home/remote
    assert "python" in (res.get("keyword") or "").lower() or "Python" in (res.get("skills") or [])


@pytest.mark.anyio
async def test_parser_full_time_python_no_office_coercion():
    """Verify 'full time Python jobs' extracts full-time/1 and work_mode=None (not office)."""
    res = await parse_search_query("full time Python jobs", role="employee")
    assert res.get("employment_type") == "full-time"
    assert res.get("role_type_id") == 1
    assert res.get("job_mode") is None  # Must NOT be coerced to office


@pytest.mark.anyio
async def test_parser_remote_part_time_python():
    """Verify 'remote part time Python' extracts employment_type=part-time/2 AND work_mode=home."""
    res = await parse_search_query("remote part time Python", role="employee")
    assert res.get("employment_type") == "part-time"
    assert res.get("role_type_id") == 2
    assert res.get("job_mode") == "home"


@pytest.mark.anyio
async def test_parser_remote_internship_python():
    """Verify 'remote internship Python' extracts employment_type=internship/3 AND work_mode=home."""
    res = await parse_search_query("remote internship Python", role="employee")
    assert res.get("employment_type") == "internship"
    assert res.get("role_type_id") == 3
    assert res.get("job_mode") == "home"


# ============================================================================
# C. DTO Tests
# ============================================================================

def test_search_query_and_criteria_carry_employment_type():
    """Verify SearchQuery and SearchCriteria schema models store and hash employment_type and role_type_id."""
    sq = SearchQuery(
        title="Python Developer",
        employment_type="internship",
        role_type_id=3
    )
    assert sq.employment_type == "internship"
    assert sq.role_type_id == 3
    h = sq.compute_hash()
    assert isinstance(h, str) and len(h) == 16

    sc = SearchCriteria.from_parsed_dict({
        "keyword": "Python Developer",
        "employment_type": "internship",
        "role_type_id": 3,
    })
    assert sc.employment_type == "internship"
    assert sc.role_type_id == 3


# ============================================================================
# D. Session State Persistence Tests
# ============================================================================

def test_session_state_merges_and_retains_employment_type():
    """Verify follow-up queries (Turn 2: 'according to my skills') preserve role_type_id from Turn 1."""
    turn1_q = SearchQuery(
        employment_type="internship",
        role_type_id=3
    )
    turn2_q = SearchQuery(
        use_profile=True
    )
    merged = merge_employee_search_query(turn1_q, turn2_q)
    assert merged.employment_type == "internship"
    assert merged.role_type_id == 3


def test_session_state_clears_employment_type():
    """Verify explicit filter removal clears employment_type and role_type_id."""
    prev_q = SearchQuery(
        employment_type="part-time",
        role_type_id=2
    )
    curr_q = SearchQuery(
        clear_filters=["employment_type"]
    )
    merged = merge_employee_search_query(prev_q, curr_q)
    assert merged.employment_type is None
    assert merged.role_type_id is None


# ============================================================================
# E. Hard Post-Fusion Eligibility Gate Tests
# ============================================================================

def test_passes_employment_type_eligibility_target_internship():
    """Verify passes_employment_type_eligibility for Internship (target=3) fails non-internship and missing/0."""
    job_internship = NormalizedJob(job_id=1, title="Python Intern", raw_data={"role_type": 3, "role_type_name": "Internship"})
    job_full_time = NormalizedJob(job_id=2, title="Python Engineer", raw_data={"role_type": 1, "role_type_name": "Full Time"})
    job_part_time = NormalizedJob(job_id=3, title="Python Assistant", raw_data={"role_type": 2, "role_type_name": "Part Time"})
    job_zero = NormalizedJob(job_id=4, title="Unknown Job", raw_data={"role_type": 0})
    job_missing = NormalizedJob(job_id=5, title="Missing Job", raw_data={})

    assert passes_employment_type_eligibility(job_internship, target_role_type_id=3) is True
    assert passes_employment_type_eligibility(job_full_time, target_role_type_id=3) is False
    assert passes_employment_type_eligibility(job_part_time, target_role_type_id=3) is False
    assert passes_employment_type_eligibility(job_zero, target_role_type_id=3) is False
    assert passes_employment_type_eligibility(job_missing, target_role_type_id=3) is False


def test_passes_employment_type_eligibility_target_part_time():
    """Verify passes_employment_type_eligibility for Part-time (target=2)."""
    job_part_time = NormalizedJob(job_id=1, title="Part time Dev", raw_data={"role_type": 2, "role_type_name": "Part Time"})
    job_full_time = NormalizedJob(job_id=2, title="Full time Dev", raw_data={"role_type": 1, "role_type_name": "Full Time"})

    assert passes_employment_type_eligibility(job_part_time, target_role_type_id=2) is True
    assert passes_employment_type_eligibility(job_full_time, target_role_type_id=2) is False


def test_passes_employment_type_eligibility_target_none_unconstrained():
    """Verify when target_role_type_id is None, all jobs pass."""
    job_any = NormalizedJob(job_id=1, title="Dev", raw_data={"role_type": 1})
    assert passes_employment_type_eligibility(job_any, target_role_type_id=None) is True


# ============================================================================
# F. RRF Leakage & Fusion Integration Tests
# ============================================================================

@pytest.mark.anyio
async def test_rrf_leakage_prevented_by_hard_gate():
    """
    RRF Leakage Test:
    Job Match returns Full-Time Job B with extremely high vector score (0.95).
    Meilisearch returns Internship Job A with lower score.
    Post-fusion gate MUST remove Job B, returning ONLY Job A.
    """
    mock_meili = [
        {
            "id": 101,
            "job_title": "Python Intern",
            "role_type": 3,
            "role_type_name": "Internship",
            "location": "Delhi"
        }
    ]
    mock_jobmatch = {
        "success": True,
        "data": [
            {
                "id": 102,
                "job_id": 102,
                "final_score": 0.99,
                "payload": {
                    "job_title": "Senior Full-Time Python Architect",
                    "role_type": 1,
                    "role_type_name": "Full Time",
                    "location": "Delhi"
                }
            }
        ]
    }

    with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_search_jobs, \
         patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_find_jobs:

        mock_search_jobs.return_value = {"success": True, "data": mock_meili}
        mock_find_jobs.return_value = mock_jobmatch

        hits, telemetry = await execute_hybrid_job_search(
            last_user_msg="show internship jobs",
            keyword="Python",
            parsed={"role_type_id": 3, "employment_type": "internship"},
            limit=5
        )

        # Assert Full-Time Job 102 was rejected despite top vector rank
        hit_ids = [h["id"] for h in hits]
        assert 101 in hit_ids
        assert 102 not in hit_ids
        assert len(hits) == 1
        assert hits[0]["employment_type"] == "Internship"


# ============================================================================
# G. Frontend DTO Normalization Tests
# ============================================================================

def test_frontend_dto_normalization_exposes_employment_type():
    """Verify NormalizedJob.to_dict() populates employment_type from role_type_name (not job_mode)."""
    nj = NormalizedJob(
        job_id=1,
        title="Software Intern",
        job_mode="Office",
        raw_data={"role_type": 3, "role_type_name": "Internship", "job_mode_name": "Office"}
    )
    d = nj.to_dict()
    assert d["employment_type"] == "Internship"
    assert d["role_type"] == 3
    assert d["role_type_name"] == "Internship"
    assert d["job_mode_name"] == "Office"
    assert d["employment_type"] != "Office"
