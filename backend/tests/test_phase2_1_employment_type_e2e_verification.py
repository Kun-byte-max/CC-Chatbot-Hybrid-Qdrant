import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from backend.schemas.search_query import SearchQuery
from backend.schemas.search_criteria import SearchCriteria
from backend.services.search_parser import parse_search_query, resolve_job_role_type_id
from backend.utils.session_state import (
    merge_employee_search_query,
    get_search_context,
    set_search_context,
)
from backend.services.hybrid_job_search import (
    execute_hybrid_job_search,
    passes_employment_type_eligibility,
    NormalizedJob,
)


# ============================================================================
# VERIFICATION FIXTURES
# ============================================================================

INTERNSHIP_PYTHON = {
    "id": 101,
    "job_id": 101,
    "job_title": "Python Intern",
    "role_type": 3,
    "role_type_name": "Internship",
    "job_mode": 2,
    "job_mode_name": "Remote",
    "skills": ["Python"],
}

FULLTIME_PYTHON = {
    "id": 102,
    "job_id": 102,
    "job_title": "Python Backend Developer",
    "role_type": 1,
    "role_type_name": "Full Time",
    "job_mode": 1,
    "job_mode_name": "Office",
    "skills": ["Python"],
}

PARTTIME_PYTHON = {
    "id": 103,
    "job_id": 103,
    "job_title": "Part Time Python Developer",
    "role_type": 2,
    "role_type_name": "Part Time",
    "job_mode": 2,
    "job_mode_name": "Remote",
    "skills": ["Python"],
}

INTERNSHIP_REMOTE = {
    "id": 104,
    "job_id": 104,
    "job_title": "Remote Python Intern",
    "role_type": 3,
    "role_type_name": "Internship",
    "job_mode": 2,
    "job_mode_name": "Remote",
    "skills": ["Python"],
}

FULLTIME_REMOTE = {
    "id": 105,
    "job_id": 105,
    "job_title": "Remote Python Engineer",
    "role_type": 1,
    "role_type_name": "Full Time",
    "job_mode": 2,
    "job_mode_name": "Remote",
    "skills": ["Python"],
}

ALL_MOCK_JOBS = [
    INTERNSHIP_PYTHON,
    FULLTIME_PYTHON,
    PARTTIME_PYTHON,
    INTERNSHIP_REMOTE,
    FULLTIME_REMOTE,
]


# ============================================================================
# 7. VERIFICATION SCENARIO A — SINGLE-TURN INTERNSHIP
# ============================================================================

@pytest.mark.anyio
async def test_scenario_a_single_turn_internship():
    """Verify 'show me internship jobs' parses role_type_id=3 and rejects non-internships."""
    parsed = await parse_search_query("show me internship jobs", role="employee")
    assert parsed.get("employment_type") == "internship"
    assert parsed.get("role_type_id") == 3

    with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_search, \
         patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:

        mock_search.return_value = {"success": True, "data": ALL_MOCK_JOBS}
        mock_jm.return_value = {"success": True, "data": []}

        hits, telemetry = await execute_hybrid_job_search(
            last_user_msg="show me internship jobs",
            parsed=parsed,
            limit=10
        )

        hit_ids = [h["id"] for h in hits]
        assert 101 in hit_ids
        assert 104 in hit_ids
        assert 102 not in hit_ids  # Full Time REJECTED
        assert 103 not in hit_ids  # Part Time REJECTED
        assert 105 not in hit_ids  # Full Time Remote REJECTED

        # Response DTO contracts
        for h in hits:
            assert h["employment_type"] == "Internship"
            assert h["role_type"] == 3
            assert h["role_type_name"] == "Internship"


# ============================================================================
# 8. VERIFICATION SCENARIO B — PART-TIME
# ============================================================================

@pytest.mark.anyio
async def test_scenario_b_part_time_jobs():
    """Verify 'show me part time Python jobs' parses part-time/2 and does NOT coerce job_mode to home."""
    parsed = await parse_search_query("show me part time Python jobs", role="employee")
    assert parsed.get("employment_type") == "part-time"
    assert parsed.get("role_type_id") == 2
    assert parsed.get("job_mode") is None  # MUST NOT be coerced to home/remote

    with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_search, \
         patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:

        mock_search.return_value = {"success": True, "data": ALL_MOCK_JOBS}
        mock_jm.return_value = {"success": True, "data": []}

        hits, telemetry = await execute_hybrid_job_search(
            last_user_msg="show me part time Python jobs",
            parsed=parsed,
            limit=10
        )

        hit_ids = [h["id"] for h in hits]
        assert 103 in hit_ids
        assert 101 not in hit_ids
        assert 102 not in hit_ids


# ============================================================================
# 9. VERIFICATION SCENARIO C — FULL-TIME
# ============================================================================

@pytest.mark.anyio
async def test_scenario_c_full_time_jobs():
    """Verify 'show me full time Python jobs' parses full-time/1 and does NOT coerce job_mode to office."""
    parsed = await parse_search_query("show me full time Python jobs", role="employee")
    assert parsed.get("employment_type") == "full-time"
    assert parsed.get("role_type_id") == 1
    assert parsed.get("job_mode") is None  # MUST NOT be coerced to office

    with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_search, \
         patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:

        mock_search.return_value = {"success": True, "data": ALL_MOCK_JOBS}
        mock_jm.return_value = {"success": True, "data": []}

        hits, telemetry = await execute_hybrid_job_search(
            last_user_msg="show me full time Python jobs",
            parsed=parsed,
            limit=10
        )

        hit_ids = [h["id"] for h in hits]
        assert 102 in hit_ids
        assert 105 in hit_ids
        assert 101 not in hit_ids
        assert 103 not in hit_ids


# ============================================================================
# 10. VERIFICATION SCENARIO D — REMOTE INTERNSHIP
# ============================================================================

@pytest.mark.anyio
async def test_scenario_d_remote_internship():
    """Verify 'show me remote internship Python jobs' requires role_type_id=3 AND work_mode=home."""
    parsed = await parse_search_query("show me remote internship Python jobs", role="employee")
    assert parsed.get("employment_type") == "internship"
    assert parsed.get("role_type_id") == 3
    assert parsed.get("job_mode") == "home"

    with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_search, \
         patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:

        mock_search.return_value = {"success": True, "data": ALL_MOCK_JOBS}
        mock_jm.return_value = {"success": True, "data": []}

        hits, telemetry = await execute_hybrid_job_search(
            last_user_msg="show me remote internship Python jobs",
            parsed=parsed,
            limit=10
        )

        hit_ids = [h["id"] for h in hits]
        assert 104 in hit_ids  # Remote Internship PASS
        assert 105 not in hit_ids  # Remote Full Time REJECTED (employment_type mismatch)
        assert 102 not in hit_ids  # Office Full Time REJECTED


# ============================================================================
# 11. VERIFICATION SCENARIO E — REMOTE PART-TIME
# ============================================================================

@pytest.mark.anyio
async def test_scenario_e_remote_part_time():
    """Verify 'show me remote part time Python jobs' requires role_type_id=2 AND work_mode=home."""
    parsed = await parse_search_query("show me remote part time Python jobs", role="employee")
    assert parsed.get("employment_type") == "part-time"
    assert parsed.get("role_type_id") == 2
    assert parsed.get("job_mode") == "home"

    with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_search, \
         patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:

        mock_search.return_value = {"success": True, "data": ALL_MOCK_JOBS}
        mock_jm.return_value = {"success": True, "data": []}

        hits, telemetry = await execute_hybrid_job_search(
            last_user_msg="show me remote part time Python jobs",
            parsed=parsed,
            limit=10
        )

        hit_ids = [h["id"] for h in hits]
        assert 103 in hit_ids  # Remote Part Time PASS
        assert 105 not in hit_ids  # Remote Full Time REJECTED


# ============================================================================
# 12. VERIFICATION SCENARIO F — CONVERSATIONAL FOLLOW-UP (INTERNSHIP -> SKILLS)
# ============================================================================

def test_scenario_f_conversational_followup_internship_to_skills():
    """Verify Turn 1 'show me internship jobs' -> Turn 2 'according to my skills' retains role_type_id=3."""
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
    assert merged.use_profile is True


# ============================================================================
# 13. VERIFICATION SCENARIO G & H — PART-TIME & FULL-TIME FOLLOW-UP
# ============================================================================

def test_scenario_g_h_part_time_and_full_time_followups():
    """Verify part-time and full-time constraints survive follow-up turn merges."""
    pt_merged = merge_employee_search_query(
        SearchQuery(employment_type="part-time", role_type_id=2, location="Delhi"),
        SearchQuery(use_profile=True)
    )
    assert pt_merged.employment_type == "part-time"
    assert pt_merged.role_type_id == 2
    assert pt_merged.location == "Delhi"

    ft_merged = merge_employee_search_query(
        SearchQuery(employment_type="full-time", role_type_id=1),
        SearchQuery(use_profile=True)
    )
    assert ft_merged.employment_type == "full-time"
    assert ft_merged.role_type_id == 1


# ============================================================================
# 15. VERIFICATION SCENARIO I — RRF LEAKAGE REJECTION
# ============================================================================

@pytest.mark.anyio
async def test_scenario_i_rrf_leakage_prevented():
    """
    RRF Leakage Verification:
    Qdrant returns Full-Time Job 105 with top vector score (0.99).
    Meilisearch returns Internship Job 101 with lower score.
    Post-fusion gate MUST remove Job 105, returning ONLY Internship Job 101.
    """
    mock_meili = [INTERNSHIP_PYTHON]
    mock_jobmatch = {
        "success": True,
        "data": [
            {
                "id": 105,
                "job_id": 105,
                "final_score": 0.99,
                "payload": FULLTIME_REMOTE
            }
        ]
    }

    with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_search, \
         patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:

        mock_search.return_value = {"success": True, "data": mock_meili}
        mock_find_jobs = mock_jm
        mock_find_jobs.return_value = mock_jobmatch

        hits, telemetry = await execute_hybrid_job_search(
            last_user_msg="show me internship jobs",
            parsed={"role_type_id": 3, "employment_type": "internship"},
            limit=5
        )

        hit_ids = [h["id"] for h in hits]
        assert 101 in hit_ids
        assert 105 not in hit_ids  # Top RRF vector score Full-Time job MUST be rejected
        assert len(hits) == 1


# ============================================================================
# 16. VERIFICATION SCENARIO J — MISSING / UNKNOWN ROLE TYPE
# ============================================================================

def test_scenario_j_missing_or_zero_role_type_fails_closed():
    """Verify passes_employment_type_eligibility fails closed on role_type=None or role_type=0."""
    j_none = NormalizedJob(job_id=901, title="Job None", raw_data={})
    j_zero = NormalizedJob(job_id=902, title="Job Zero", raw_data={"role_type": 0})
    j_invalid = NormalizedJob(job_id=903, title="Job Invalid", raw_data={"role_type": "invalid"})

    assert passes_employment_type_eligibility(j_none, target_role_type_id=3) is False
    assert passes_employment_type_eligibility(j_zero, target_role_type_id=3) is False
    assert passes_employment_type_eligibility(j_invalid, target_role_type_id=3) is False


# ============================================================================
# 17. VERIFICATION SCENARIO K — UNCONSTRAINED SEARCH
# ============================================================================

def test_scenario_k_unconstrained_search_allows_all_role_types():
    """Verify when no employment type is requested (target_role_type_id=None), all role types pass."""
    j_ft = NormalizedJob(job_id=1, title="Full Time", raw_data={"role_type": 1})
    j_pt = NormalizedJob(job_id=2, title="Part Time", raw_data={"role_type": 2})
    j_intern = NormalizedJob(job_id=3, title="Internship", raw_data={"role_type": 3})

    assert passes_employment_type_eligibility(j_ft, target_role_type_id=None) is True
    assert passes_employment_type_eligibility(j_pt, target_role_type_id=None) is True
    assert passes_employment_type_eligibility(j_intern, target_role_type_id=None) is True


# ============================================================================
# 19. VERIFICATION SCENARIO M — RETRIEVAL ARGUMENT PASSING
# ============================================================================

@pytest.mark.anyio
async def test_scenario_m_retrieval_argument_passing():
    """Verify execute_hybrid_job_search passes role_type=[3] to search_jobs and filters={'role_type': 3} to JobMatch."""
    with patch("backend.services.hybrid_job_search.search_jobs", new_callable=AsyncMock) as mock_search, \
         patch("backend.services.hybrid_job_search.job_match_client.find_jobs", new_callable=AsyncMock) as mock_jm:

        mock_search.return_value = {"success": True, "data": []}
        mock_jm.return_value = {"success": True, "data": []}

        await execute_hybrid_job_search(
            last_user_msg="internship jobs",
            candidate_id=123,
            parsed={"role_type_id": 3, "employment_type": "internship"},
            limit=5
        )

        # Verify Meilisearch call received role_type
        assert mock_search.call_count >= 1
        search_args = mock_search.call_args.kwargs
        assert search_args.get("role_type") == 3

        # Verify JobMatch call received filters={'role_type': 3}
        assert mock_jm.call_count >= 1
        jm_args = mock_jm.call_args.kwargs
        assert jm_args.get("filters") == {"role_type": 3}


# ============================================================================
# 21. VERIFICATION SCENARIO N — TAXONOMY ISOLATION
# ============================================================================

def test_scenario_n_taxonomy_isolation():
    """Verify Job Internship is role_type_id=3 and NOT Candidate Internship ID 5."""
    assert resolve_job_role_type_id("internship") == 3
    assert resolve_job_role_type_id("internship") != 5


# ============================================================================
# 22. VERIFICATION SCENARIO O — SEARCH FINGERPRINT ISOLATION
# ============================================================================

def test_scenario_o_search_fingerprint_isolation():
    """Verify changing employment_type alters SearchQuery compute_hash fingerprint."""
    sq_intern = SearchQuery(title="Python", employment_type="internship", role_type_id=3)
    sq_fulltime = SearchQuery(title="Python", employment_type="full-time", role_type_id=1)

    assert sq_intern.compute_hash() != sq_fulltime.compute_hash()
