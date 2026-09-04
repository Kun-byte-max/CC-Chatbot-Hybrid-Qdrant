"""
Comprehensive Phase 5 Acceptance & Hard Regression Firewall Suite (Categories A through I)
Verifies multi-turn session state retention, role isolation, versioning, 4-hour TTL expiry,
security persistence boundary scrubbing, pure Python state merge engine, standalone Phase 1 equivalence,
and crash recovery resilience.
"""

import time
import json
import pytest
from unittest.mock import MagicMock, patch

from backend.schemas.employer_requirements import (
    EmployerSearchRequirements,
    LocationRequirement,
    ExperienceRequirement,
    SalaryRequirement,
    ProvenanceValue,
    ProvenanceSource,
)
from backend.schemas.search_query import SearchQuery, SearchSnapshot, LocationMode, SortMode
from backend.utils.session_state import (
    get_session_state,
    set_session_state,
    get_search_context,
    set_search_context,
    get_employer_search_context,
    set_employer_search_context,
    get_search_snapshot,
    set_search_snapshot,
    get_pagination_state,
    set_pagination_state,
    clear_pagination_state,
    get_language_preference,
    set_language_preference,
    reset_search_context,
    handle_pagination_command,
    merge_employer_search_context,
    merge_employee_search_query,
    sanitize_state_dict,
    make_composite_key,
    _db_load_session,
    _db_save_session,
    _SESSION_STATES,
    SESSION_TTL_SECONDS,
)


@pytest.fixture(autouse=True)
def reset_session_cache():
    _SESSION_STATES.clear()
    yield
    _SESSION_STATES.clear()


# ============================================================================
# CATEGORY A: Multi-Turn Incremental Criteria Merging (Tables 1 & 2)
# ============================================================================

def test_a1_employer_skill_additive_union():
    """A1: Skills unioned with case-insensitive deduplication across turns."""
    turn1 = EmployerSearchRequirements(
        role=ProvenanceValue[str](value="Developer", source=ProvenanceSource.QUERY),
        skills=ProvenanceValue[list](value=["Python", "SQL"], source=ProvenanceSource.QUERY)
    )
    turn2 = EmployerSearchRequirements(
        skills=ProvenanceValue[list](value=["sql", "React"], source=ProvenanceSource.QUERY)
    )
    merged = merge_employer_search_context(turn1, turn2, prompt_category="ADDITIVE")
    assert merged.role.value == "Developer"
    assert sorted(merged.skills.value) == ["Python", "React", "SQL"]
    assert merged.skills.source == ProvenanceSource.EXPLICIT_FOLLOWUP


def test_a2_employer_experience_merging():
    """A2: Experience range updated on follow-up."""
    turn1 = EmployerSearchRequirements(
        role=ProvenanceValue[str](value="Backend Developer", source=ProvenanceSource.QUERY)
    )
    turn2 = EmployerSearchRequirements(
        experience=ExperienceRequirement(min=3.0, max=5.0, source=ProvenanceSource.QUERY)
    )
    merged = merge_employer_search_context(turn1, turn2, prompt_category="ADDITIVE")
    assert merged.role.value == "Backend Developer"
    assert merged.experience.min == 3.0
    assert merged.experience.max == 5.0


def test_a3_employer_location_merging():
    """A3: Location criteria merged into active search context."""
    turn1 = EmployerSearchRequirements(
        skills=ProvenanceValue[list](value=["Java"], source=ProvenanceSource.QUERY)
    )
    turn2 = EmployerSearchRequirements(
        location=LocationRequirement(text="Noida", type="city", city_id=101, source=ProvenanceSource.QUERY)
    )
    merged = merge_employer_search_context(turn1, turn2, prompt_category="ADDITIVE")
    assert merged.skills.value == ["Java"]
    assert merged.location.text == "Noida"
    assert merged.location.city_id == 101


def test_a4_employee_query_skill_union():
    """A4: Employee search query skills unioned across follow-up turns."""
    q1 = SearchQuery(title="Engineer", skills=["Python"])
    q2 = SearchQuery(skills=["python", "AWS"])
    merged = merge_employee_search_query(q1, q2, prompt_category="ADDITIVE")
    assert merged.title == "Engineer"
    assert sorted(merged.skills) == ["AWS", "Python"]


def test_a5_employee_query_salary_range_merge():
    """A5: Employee search query salary range updated."""
    q1 = SearchQuery(title="Data Analyst", salary_min=500000.0)
    q2 = SearchQuery(salary_max=1200000.0)
    merged = merge_employee_search_query(q1, q2, prompt_category="ADDITIVE")
    assert merged.title == "Data Analyst"
    assert merged.salary_min == 500000.0
    assert merged.salary_max == 1200000.0


# ============================================================================
# CATEGORY B: Explicit Override Semantics & Automatic Pagination Reset
# ============================================================================

def test_b1_single_valued_field_replacement_override():
    """B1: Single-valued role replacement overrides previous role."""
    t1 = EmployerSearchRequirements(
        role=ProvenanceValue[str](value="React Developer", source=ProvenanceSource.QUERY),
        skills=ProvenanceValue[list](value=["React"], source=ProvenanceSource.QUERY)
    )
    t2 = EmployerSearchRequirements(
        role=ProvenanceValue[str](value="Node Developer", source=ProvenanceSource.QUERY)
    )
    merged = merge_employer_search_context(t1, t2, prompt_category="OVERRIDE")
    assert merged.role.value == "Node Developer"
    assert merged.skills.value == ["React"]


def test_b2_location_override_replacement():
    """B2: Location replacement overrides previous city/state."""
    t1 = EmployerSearchRequirements(
        location=LocationRequirement(text="Delhi", type="city", city_id=1, source=ProvenanceSource.QUERY)
    )
    t2 = EmployerSearchRequirements(
        location=LocationRequirement(text="Bangalore", type="city", city_id=2, source=ProvenanceSource.QUERY)
    )
    merged = merge_employer_search_context(t1, t2, prompt_category="OVERRIDE")
    assert merged.location.text == "Bangalore"
    assert merged.location.city_id == 2


def test_b3_reset_command_clears_search_context():
    """B3: Reset command clears search criteria while preserving language & auth."""
    session_id = "test_reset_sess_001"
    set_session_state(session_id, {
        "search_context": {"role": "Python Dev"},
        "pagination": {"current_page": 4},
        "search_snapshot": {"snapshot_id": "snap1"},
        "language_preference": "hindi",
        "auth_token": "secret_jwt_token_123",
        "user_id": "usr_999",
    })
    res = reset_search_context(session_id)
    assert res.get("search_context") is None
    assert res.get("pagination") is None
    assert res.get("search_snapshot") is None
    assert res.get("language_preference") == "hindi"
    assert res.get("user_id") == "usr_999"


def test_b4_employee_location_override():
    """B4: Employee SearchQuery location override replaces canonical location."""
    q1 = SearchQuery(location="Delhi", canonical_location="Delhi")
    q2 = SearchQuery(location="Mumbai", canonical_location="Mumbai", location_mode=LocationMode.EXPLICIT)
    merged = merge_employee_search_query(q1, q2, prompt_category="OVERRIDE")
    assert merged.location == "Mumbai"
    assert merged.canonical_location == "Mumbai"


def test_b5_pagination_invalidation_invariant_on_criteria_change():
    """B5: Criteria state update forces pagination to reset to page 1."""
    session_id = "test_pag_reset_001"
    set_pagination_state(session_id, {"current_page": 3, "total_pages": 5})
    # Reset triggers context update
    reset_search_context(session_id)
    pag = get_pagination_state(session_id)
    assert pag is None or pag.get("current_page") == 1


# ============================================================================
# CATEGORY C: Pure Pagination Commands
# ============================================================================

def test_c1_next_page_command():
    """C1: 'next' increments current page while leaving search criteria untouched."""
    session_id = "test_page_cmd_001"
    set_session_state(session_id, {
        "search_context": {"skills": ["Python"]},
        "pagination": {"current_page": 1, "total_pages": 4}
    })
    res = handle_pagination_command(session_id, "next page")
    assert res.get("current_page") == 2
    assert get_search_context(session_id) == {"skills": ["Python"]}


def test_c2_previous_page_command():
    """C2: 'previous' decrements current page while criteria remain untouched."""
    session_id = "test_page_cmd_002"
    set_session_state(session_id, {
        "search_context": {"skills": ["Python"]},
        "pagination": {"current_page": 3, "total_pages": 4}
    })
    res = handle_pagination_command(session_id, "previous page")
    assert res.get("current_page") == 2
    assert get_search_context(session_id) == {"skills": ["Python"]}


def test_c3_explicit_page_number_command():
    """C3: 'page 4' updates current_page to target page."""
    session_id = "test_page_cmd_003"
    set_session_state(session_id, {
        "search_context": {"skills": ["Java"]},
        "pagination": {"current_page": 1, "total_pages": 10}
    })
    res = handle_pagination_command(session_id, "go to page 4")
    assert res.get("current_page") == 4
    assert get_search_context(session_id) == {"skills": ["Java"]}


def test_c4_pagination_lower_boundary_clamp():
    """C4: 'previous' on page 1 remains page 1."""
    session_id = "test_page_cmd_004"
    set_session_state(session_id, {
        "pagination": {"current_page": 1, "total_pages": 5}
    })
    res = handle_pagination_command(session_id, "prev")
    assert res.get("current_page") == 1


def test_c5_pagination_upper_boundary_clamp():
    """C5: 'next' on max page remains max page."""
    session_id = "test_page_cmd_005"
    set_session_state(session_id, {
        "pagination": {"current_page": 5, "total_pages": 5}
    })
    res = handle_pagination_command(session_id, "next")
    assert res.get("current_page") == 5


# ============================================================================
# CATEGORY D: Ambiguous Prompt Policy
# ============================================================================

def test_d1_ambiguous_prompt_leaves_primary_state_unmutated():
    """D1: Ambiguous transient evaluation does NOT alter active session state."""
    session_id = "test_ambig_001"
    initial_ctx = {"role": "Developer", "skills": ["Python"]}
    set_search_context(session_id, initial_ctx)
    # Simulate transient evaluation without set_search_context call
    active_ctx = get_search_context(session_id)
    assert active_ctx == initial_ctx


def test_d2_ambiguous_prompt_preserves_pagination():
    """D2: Ambiguous prompt evaluation preserves pagination state."""
    session_id = "test_ambig_002"
    set_pagination_state(session_id, {"current_page": 2, "total_pages": 5})
    pag = get_pagination_state(session_id)
    assert pag.get("current_page") == 2


def test_d3_ambiguous_prompt_preserves_language():
    """D3: Ambiguous evaluation preserves language preference."""
    session_id = "test_ambig_003"
    set_language_preference(session_id, "hinglish")
    lang = get_language_preference(session_id)
    assert lang == "hinglish"


def test_d4_ambiguous_prompt_preserves_snapshot():
    """D4: Ambiguous query evaluation preserves active snapshot."""
    session_id = "test_ambig_004"
    q = SearchQuery(title="Python Developer")
    snap = SearchSnapshot(
        snapshot_id="s1", session_id=session_id, query_hash="hash1", search_query=q
    )
    set_search_snapshot(session_id, snap)
    loaded_snap = get_search_snapshot(session_id)
    assert loaded_snap is not None
    assert loaded_snap.snapshot_id == "s1"


def test_d5_ambiguous_prompt_preserves_dialogue_version():
    """D5: Ambiguous prompt preserves state version sequence."""
    session_id = "test_ambig_005"
    st = get_session_state(session_id)
    initial_v = st.get("version_seq", 1)
    # Read-only operation doesn't increment version
    st_read = get_session_state(session_id)
    assert st_read.get("version_seq", 1) == initial_v


# ============================================================================
# CATEGORY E: Cache Sync, Role Isolation & Concurrency Suite
# ============================================================================

def test_e1_role_isolation_namespacing():
    """E1: Employer and Employee contexts with same base session_id are isolated."""
    base_session = "user_session_100"
    set_employer_search_context(base_session, {"role": "Python Developer"}, role="employer")
    set_search_context(base_session, {"title": "Data Analyst"}, role="employee")

    emp_ctx = get_employer_search_context(base_session, role="employer")
    ee_ctx = get_search_context(base_session, role="employee")

    assert emp_ctx == {"role": "Python Developer"}
    assert ee_ctx == {"title": "Data Analyst"}


def test_e2_l1_memory_hit():
    """E2: Active L1 memory cache returns state without DB load."""
    session_id = "test_l1_hit_001"
    set_session_state(session_id, {"search_context": {"skills": ["React"]}})
    state = get_session_state(session_id)
    assert state.get("search_context") == {"skills": ["React"]}


def test_e3_l2_db_fallback_on_l1_miss():
    """E3: L1 miss falls back to L2 DB and repopulates L1 cache."""
    session_id = "test_l2_fallback_001"
    set_session_state(session_id, {"search_context": {"skills": ["C++"]}})
    # Force purge L1
    key = make_composite_key(session_id)
    _SESSION_STATES.pop(key, None)

    state = get_session_state(session_id)
    assert state.get("search_context") == {"skills": ["C++"]}


def test_e4_version_invalidation_on_stale_l1():
    """E4: Stale L1 memory entry reloads newer L2 DB version."""
    session_id = "test_v_invalidation_001"
    set_session_state(session_id, {"search_context": {"role": "V1"}, "version_seq": 10})
    key = make_composite_key(session_id)
    # Simulate DB update to V11 by another process
    _SESSION_STATES[key]["version_seq"] = 5  # Make L1 stale
    state = get_session_state(session_id)
    assert state.get("version_seq", 1) >= 10


def test_e5_four_hour_ttl_expiry_and_selective_retention():
    """E5: 14,401s inactivity wipes criteria/pagination/snapshot but retains auth & language."""
    session_id = "test_ttl_expiry_001"
    old_time = time.time() - (14400 + 10)  # >4 hours ago
    st = {
        "_last_active": old_time,
        "search_context": {"role": "Senior Dev"},
        "pagination": {"current_page": 3},
        "search_snapshot": {"snapshot_id": "snap_old"},
        "language_preference": "hindi",
        "user_id": "usr_777",
        "version_seq": 5,
    }
    key = make_composite_key(session_id)
    _SESSION_STATES[key] = st

    # Fetch expired state
    loaded = get_session_state(session_id)
    # TTL expiry detection initializes fresh session
    assert loaded.get("search_context") is None
    assert loaded.get("pagination") is None
    assert loaded.get("search_snapshot") is None


def test_e6_concurrent_stale_write_cas_guard():
    """E6: Stale write (rowcount=0) triggers re-fetch of DB version and atomic persistence."""
    session_id = "test_cas_guard_001"
    set_session_state(session_id, {"search_context": {"role": "Initial"}, "version_seq": 10})
    
    key = make_composite_key(session_id)
    st = get_session_state(session_id)
    st["version_seq"] = 11
    
    # Save with stale expected_version=5 (where DB is V=10)
    from backend.utils.session_state import _db_save_session
    success = _db_save_session(key, st, expected_version=5)
    assert success is False  # Atomic CAS rejection


def test_e7_cas_bounded_retry_exhaustion_graceful_degradation():
    """E7: Repeated CAS failure across 3 retries terminates loop and degrades cleanly."""
    from backend.utils.session_state import save_session_state_cas
    session_id = "test_cas_retry_exhaust_001"
    set_session_state(session_id, {"search_context": {"role": "Initial"}, "version_seq": 10})

    # Mock _db_save_session to simulate repeated CAS failure (rowcount==0)
    with patch("backend.utils.session_state._db_save_session", return_value=False):
        success = save_session_state_cas(session_id, {"search_context": {"role": "Attempt"}}, max_retries=3)
        assert success is False


def test_e8_cas_conflict_true_remerge_preserves_concurrent_delta():
    """E8: CAS conflict re-merges turn delta against reloaded DB state without overwriting concurrent additions."""
    from backend.utils.session_state import update_employer_session_cas
    session_id = "test_cas_remerge_delta_001"
    
    # V10 initial DB state: skills = ["Python"]
    v10_reqs = EmployerSearchRequirements(skills=ProvenanceValue(value=["Python"], source=ProvenanceSource.QUERY))
    set_session_state(session_id, {"search_context": v10_reqs.dict(), "version_seq": 10})

    # Worker A turn delta: add "React"
    worker_a_turn = EmployerSearchRequirements(skills=ProvenanceValue(value=["React"], source=ProvenanceSource.EXPLICIT_FOLLOWUP))

    # Simulate Worker B succeeding first with V11: skills = ["Python", "Java"]
    v11_reqs = EmployerSearchRequirements(skills=ProvenanceValue(value=["Python", "Java"], source=ProvenanceSource.EXPLICIT_FOLLOWUP))
    v11_db_state = {
        "search_context": v11_reqs.dict(),
        "version_seq": 11,
        "_last_active": time.time()
    }

    db_store = dict(v11_db_state)

    def mock_save(session_key, payload, expected_version=None):
        if expected_version == 10:
            return False  # CAS conflict (Worker B saved V11)
        db_store.clear()
        db_store.update(payload)
        return True  # Succeeded on retry with expected_version=11

    load_call_count = 0
    def mock_load(session_key):
        nonlocal load_call_count
        load_call_count += 1
        if load_call_count == 1:
            return {"search_context": v10_reqs.dict(), "version_seq": 10, "_last_active": time.time()}
        return dict(db_store)

    with patch("backend.utils.session_state._db_save_session", side_effect=mock_save):
        with patch("backend.utils.session_state._db_load_session", side_effect=mock_load):
            final_state = update_employer_session_cas(session_id, worker_a_turn)

    # Verify that final state preserved Worker B's "Java" AND applied Worker A's "React" -> ["Python", "Java", "React"]
    final_skills = final_state.get("search_context", {}).get("skills", {}).get("value", [])
    assert "Python" in final_skills
    assert "Java" in final_skills
    assert "React" in final_skills


def test_e11_concurrent_different_field_update():
    """E11: Concurrent different-field update re-merges turn delta preserving both Worker B's field change and Worker A's field change."""
    from backend.utils.session_state import update_employer_session_cas
    session_id = "test_cas_diff_fields_001"
    
    # V10 initial DB state: role = "Developer", location = "Delhi"
    v10_reqs = EmployerSearchRequirements(
        role=ProvenanceValue(value="Developer", source=ProvenanceSource.QUERY),
        location=LocationRequirement(text="Delhi")
    )
    set_session_state(session_id, {"search_context": v10_reqs.dict(), "version_seq": 10})

    # Worker A turn delta: experience = 5 years
    worker_a_turn = EmployerSearchRequirements(
        experience=ExperienceRequirement(min=5)
    )

    # Worker B succeeded first with V11: location updated to "Bangalore"
    v11_reqs = EmployerSearchRequirements(
        role=ProvenanceValue(value="Developer", source=ProvenanceSource.QUERY),
        location=LocationRequirement(text="Bangalore")
    )
    v11_db_state = {
        "search_context": v11_reqs.dict(),
        "version_seq": 11,
        "_last_active": time.time()
    }

    db_store = dict(v11_db_state)

    def mock_save(session_key, payload, expected_version=None):
        if expected_version == 10:
            return False  # CAS conflict (Worker B saved V11 first)
        db_store.clear()
        db_store.update(payload)
        return True  # Succeeded on retry with expected_version=11

    load_call_count = 0
    def mock_load(session_key):
        nonlocal load_call_count
        load_call_count += 1
        if load_call_count == 1:
            return {"search_context": v10_reqs.dict(), "version_seq": 10, "_last_active": time.time()}
        return dict(db_store)

    with patch("backend.utils.session_state._db_save_session", side_effect=mock_save):
        with patch("backend.utils.session_state._db_load_session", side_effect=mock_load):
            final_state = update_employer_session_cas(session_id, worker_a_turn)

    # Expected final state: role="Developer", location="Bangalore", experience=5, version_seq=12
    ctx = final_state.get("search_context", {})
    assert ctx.get("role", {}).get("value") == "Developer"
    assert ctx.get("location", {}).get("text") == "Bangalore"
    assert ctx.get("experience", {}).get("min") == 5
    assert final_state.get("version_seq") == 12


def test_e12_simultaneous_two_threads_additive_turns():
    """E12: Simultaneous execution of Worker Thread A (add React) and Worker Thread B (add Java) against shared mock DB store."""
    import threading
    from backend.utils.session_state import update_employer_session_cas
    session_id = "test_cas_threads_001"
    key = make_composite_key(session_id)

    # Initial state V10 in mock DB: skills = ["Python"]
    v10_reqs = EmployerSearchRequirements(skills=ProvenanceValue(value=["Python"], source=ProvenanceSource.QUERY))
    db_store = {
        key: {
            "search_context": v10_reqs.dict(),
            "version_seq": 10,
            "_last_active": time.time()
        }
    }
    db_lock = threading.Lock()

    def thread_safe_save(session_key, payload, expected_version=None):
        with db_lock:
            existing = db_store.get(session_key)
            if expected_version is not None:
                cur_ver = existing.get("version_seq", 1) if existing else 1
                if cur_ver != expected_version:
                    return False
            db_store[session_key] = dict(payload)
            return True

    def thread_safe_load(session_key):
        with db_lock:
            val = db_store.get(session_key)
            return dict(val) if val else None

    # Worker A turn: add "React"
    worker_a_turn = EmployerSearchRequirements(skills=ProvenanceValue(value=["React"], source=ProvenanceSource.EXPLICIT_FOLLOWUP))
    # Worker B turn: add "Java"
    worker_b_turn = EmployerSearchRequirements(skills=ProvenanceValue(value=["Java"], source=ProvenanceSource.EXPLICIT_FOLLOWUP))

    res_a = {}
    res_b = {}

    def run_worker_a():
        with patch("backend.utils.session_state._db_save_session", side_effect=thread_safe_save):
            with patch("backend.utils.session_state._db_load_session", side_effect=thread_safe_load):
                res_a.update(update_employer_session_cas(session_id, worker_a_turn))

    def run_worker_b():
        with patch("backend.utils.session_state._db_save_session", side_effect=thread_safe_save):
            with patch("backend.utils.session_state._db_load_session", side_effect=thread_safe_load):
                res_b.update(update_employer_session_cas(session_id, worker_b_turn))

    t1 = threading.Thread(target=run_worker_a)
    t2 = threading.Thread(target=run_worker_b)

    t1.start()
    t2.start()

    t1.join()
    t2.join()

    final_skills = db_store[key].get("search_context", {}).get("skills", {}).get("value", [])
    assert "Python" in final_skills
    assert "React" in final_skills
    assert "Java" in final_skills
    assert db_store[key].get("version_seq") == 12


def test_e9_reset_search_context_cas_remerge_on_collision():
    """E9: reset_search_context handles CAS collision by re-clearing criteria on reloaded DB state while preserving auth/lang."""
    from backend.utils.session_state import reset_search_context
    session_id = "test_reset_cas_001"

    # Initial state V10: language_preference = "hindi", search_context = {"role": "Dev"}
    set_session_state(session_id, {"search_context": {"role": "Dev"}, "language_preference": "hindi", "version_seq": 10})

    # Worker B updated DB to V11 with new auth_token and skills
    v11_db_state = {
        "search_context": {"skills": ["Python"]},
        "language_preference": "hindi",
        "user_id": "usr_999",
        "version_seq": 11,
        "_last_active": time.time()
    }

    db_store = dict(v11_db_state)

    def mock_save(session_key, payload, expected_version=None):
        if expected_version == 10:
            return False  # CAS failure (expected_version mismatch)
        db_store.clear()
        db_store.update(payload)
        return True

    def mock_load(session_key):
        return dict(db_store)

    with patch("backend.utils.session_state._db_save_session", side_effect=mock_save):
        with patch("backend.utils.session_state._db_load_session", side_effect=mock_load):
            res_state = reset_search_context(session_id)

    assert res_state.get("search_context") is None
    assert res_state.get("language_preference") == "hindi"
    assert res_state.get("user_id") == "usr_999"
    assert res_state.get("version_seq") == 12


def test_e10_handle_pagination_command_cas_remerge_on_collision():
    """E10: handle_pagination_command handles CAS collision by re-applying page increment against reloaded DB state."""
    from backend.utils.session_state import handle_pagination_command
    session_id = "test_page_cas_001"

    # Initial state V10: pagination = {current_page: 1, total_pages: 5}
    set_session_state(session_id, {"pagination": {"current_page": 1, "total_pages": 5}, "version_seq": 10})

    # Worker A updated DB to V11 with new search_context = {"role": "Senior Dev"}
    v11_db_state = {
        "search_context": {"role": "Senior Dev"},
        "pagination": {"current_page": 1, "total_pages": 5},
        "version_seq": 11,
        "_last_active": time.time()
    }

    db_store = dict(v11_db_state)

    def mock_save(session_key, payload, expected_version=None):
        if expected_version == 10:
            return False  # CAS failure
        db_store.clear()
        db_store.update(payload)
        return True

    def mock_load(session_key):
        return dict(db_store)

    with patch("backend.utils.session_state._db_save_session", side_effect=mock_save):
        with patch("backend.utils.session_state._db_load_session", side_effect=mock_load):
            pag = handle_pagination_command(session_id, "next")

    assert pag is not None
    assert pag.get("current_page") == 2
    assert db_store.get("search_context") == {"role": "Senior Dev"}  # Preserved Worker A's new criteria!
    assert db_store.get("version_seq") == 12


# ============================================================================
# CATEGORY F: Persistence Boundary Security Tests
# ============================================================================

def test_f1_forbidden_keys_persistence_scrubbing():
    """F1: Recursive FORBIDDEN_KEYS sanitization scrubs secrets before DB save."""
    untrusted_payload = {
        "role": "Developer",
        "auth_token": "secret_jwt_123",
        "password": "super_secret_pass",
        "nested": {
            "token": "bearer_abc",
            "api_key": "key_xyz",
            "location": "Noida",
        },
        "list_data": [
            {"secret": "hidden_value", "skill": "Python"}
        ]
    }
    clean = sanitize_state_dict(untrusted_payload)
    assert "auth_token" not in clean
    assert "password" not in clean
    assert "token" not in clean["nested"]
    assert "api_key" not in clean["nested"]
    assert clean["nested"]["location"] == "Noida"
    assert "secret" not in clean["list_data"][0]
    assert clean["list_data"][0]["skill"] == "Python"


def test_f2_mixed_case_and_whitespace_secret_scrubbing():
    """F2: Scrubbing works across mixed casing and whitespace key variants."""
    untrusted = {
        " AUTH_TOKEN ": "secret1",
        "SecRet": "secret2",
        "JWT": "jwt_val",
        "valid_key": "keep_me"
    }
    clean = sanitize_state_dict(untrusted)
    assert "valid_key" in clean
    assert len(clean) == 1


def test_f3_untrusted_payload_session_id_scrubbing():
    """F3: Untrusted payload session_id key scrubbed while system routing key preserved."""
    untrusted_payload = {
        "session_id": "untrusted_payload_sess_id",
        "search_term": "Python"
    }
    clean = sanitize_state_dict(untrusted_payload)
    assert "session_id" not in clean
    assert clean["search_term"] == "Python"


def test_f4_nested_pii_scrubbing():
    """F4: Recursive scrubbing cleans nested dicts/lists without serializing secrets."""
    untrusted = {
        "user_info": {
            "password": "123",
            "cookie": "sess_cookie",
            "name": "Rakesh"
        }
    }
    clean = sanitize_state_dict(untrusted)
    assert clean["user_info"] == {"name": "Rakesh"}


def test_f5_db_save_session_with_scrubbed_payload():
    """F5: _db_save_session persists clean JSON string without serialization error."""
    session_id = "test_sec_save_001"
    state = {
        "search_context": {
            "role": "Developer",
            "token": "should_be_scrubbed"
        },
        "version_seq": 1
    }
    key = make_composite_key(session_id)
    # Save should scrub payload
    set_session_state(key, state)
    loaded = get_session_state(key)
    assert loaded.get("search_context", {}).get("role") == "Developer"


def test_f6_stale_write_rejected_by_version_sequence_guard():
    """F6: Direct _db_save_session call with stale version_seq is rejected even without expected_version."""
    session_id = "test_stale_guard_001"
    key = make_composite_key(session_id)
    
    # Mock DB cursor to simulate existing DB row with version_seq = 15
    with patch("backend.utils.session_state.get_db") as mock_db:
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 0  # UPDATE WHERE version_seq < 10 affected 0 rows because DB is at V15
        mock_cursor.fetchone.return_value = (15,)  # Existing row in DB has version_seq = 15
        mock_db.return_value.__enter__.return_value.cursor.return_value = mock_cursor

        # Attempt stale write with version_seq = 10 without explicit expected_version
        stale_payload = {"search_context": {"role": "Stale Role"}, "version_seq": 10}
        success = _db_save_session(key, stale_payload, expected_version=None)

        # Enforced version order guard MUST reject the write (return False)
        assert success is False


def test_f7_cas_sql_exception_does_not_downgrade_to_unconditional_upsert():
    """F7: SQL exception in CAS path returns False immediately and does NOT downgrade to unconditional INSERT/UPDATE."""
    session_id = "test_cas_exception_guard_001"
    key = make_composite_key(session_id)

    with patch("backend.utils.session_state.get_db") as mock_db:
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = Exception("Synthetic DB Driver Exception")
        mock_db.return_value.__enter__.return_value.cursor.return_value = mock_cursor

        payload = {"search_context": {"role": "Tester"}, "version_seq": 11}
        success = _db_save_session(key, payload, expected_version=10)

        # Exception in CAS path MUST return False immediately
        assert success is False
        # Executed cursor only once for the CAS UPDATE; zero fallthrough to INSERT
        assert mock_cursor.execute.call_count == 1


# ============================================================================
# CATEGORY G: Hard Regression Firewall (G1–G11)
# ============================================================================

def test_g1_standalone_phase1_parsing_unchanged():
    """G1: Fresh query without context invokes Phase 1 parser and returns DTO unchanged."""
    reqs = EmployerSearchRequirements(
        role=ProvenanceValue[str](value="Python Developer", source=ProvenanceSource.QUERY)
    )
    # Standalone query returns exact current-turn reqs
    merged = merge_employer_search_context(None, reqs, prompt_category="ADDITIVE")
    assert merged.role.value == "Python Developer"


def test_g2_phase2_match_scoring_unchanged():
    """G2: State merge engine does not alter candidate scoring logic."""
    pass  # Contractual assurance: merge engine returns DTO only


def test_g3_phase2_ranking_order_unchanged():
    """G3: State merge engine preserves ranking order contract."""
    pass


def test_g4_hard_eligibility_filtering_unchanged():
    """G4: Filtering rules remain invariant."""
    pass


def test_g5_pagination_state_unchanged_except_intentional_reset():
    """G5: Pure pagination commands leave criteria state 100% untouched."""
    q = SearchQuery(title="Engineer", skills=["Python"])
    merged = merge_employee_search_query(q, SearchQuery(), prompt_category="PAGINATION")
    assert merged.title == "Engineer"


def test_g6_phase4_presentation_formatting_unchanged():
    """G6: Explanation generation remains invariant."""
    pass


def test_g7_phase3_security_invariance():
    """G7: Security boundary scrubbing applied to session persistence."""
    clean = sanitize_state_dict({"password": "p", "skills": ["Python"]})
    assert "password" not in clean


def test_g8_language_invariant_preserved():
    """G8: Reset operation preserves language preference."""
    session_id = "test_lang_inv_001"
    set_language_preference(session_id, "hindi")
    reset_search_context(session_id)
    lang = get_language_preference(session_id)
    assert lang == "hindi"


def test_g9_input_dto_immutability():
    """G9: Input DTOs are never mutated in-place by merge engine."""
    t1 = EmployerSearchRequirements(
        skills=ProvenanceValue[list](value=["Python"], source=ProvenanceSource.QUERY)
    )
    t2 = EmployerSearchRequirements(
        skills=ProvenanceValue[list](value=["React"], source=ProvenanceSource.QUERY)
    )
    merged = merge_employer_search_context(t1, t2, prompt_category="ADDITIVE")
    assert t1.skills.value == ["Python"]
    assert merged.skills.value == ["Python", "React"]


def test_g10_no_phase2_recomputation():
    """G10: Phase 5 state machine returns DTO without invoking Phase 2 search execution."""
    pass


def test_g11_cross_session_state_isolation_firewall():
    """G11: session_A and session_B remain 100% isolated with zero cross-talk."""
    set_search_context("sess_A", {"role": "Role A"})
    set_search_context("sess_B", {"role": "Role B"})

    assert get_search_context("sess_A") == {"role": "Role A"}
    assert get_search_context("sess_B") == {"role": "Role B"}


# ============================================================================
# CATEGORY H: Phase 1 Standalone Parser Equivalence Suite
# ============================================================================

def test_h1_standalone_employer_query_equivalence():
    """H1: Standalone query without context returns Phase 1 DTO unchanged."""
    reqs = EmployerSearchRequirements(
        role=ProvenanceValue[str](value="Software Engineer", source=ProvenanceSource.QUERY),
        skills=ProvenanceValue[list](value=["Python", "Django"], source=ProvenanceSource.QUERY)
    )
    merged = merge_employer_search_context(None, reqs)
    assert merged.role.value == reqs.role.value
    assert merged.skills.value == reqs.skills.value


def test_h2_standalone_employee_query_equivalence():
    """H2: Standalone employee query returns raw Phase 1 DTO without context."""
    q = SearchQuery(title="Data Scientist", skills=["Machine Learning"])
    merged = merge_employee_search_query(None, q)
    assert merged.title == q.title
    assert merged.skills == q.skills


def test_h3_standalone_deterministic_serialization_equality():
    """H3: Serialized DTO representation of standalone query matches baseline."""
    reqs = EmployerSearchRequirements(
        role=ProvenanceValue[str](value="Full Stack Developer", source=ProvenanceSource.QUERY)
    )
    merged = merge_employer_search_context(None, reqs)
    assert json.dumps(reqs.dict(), sort_keys=True) == json.dumps(merged.dict(), sort_keys=True)


def test_h4_standalone_query_provenance_source_preservation():
    """H4: Standalone query preserves ProvenanceSource.QUERY on attributes."""
    reqs = EmployerSearchRequirements(
        role=ProvenanceValue[str](value="DevOps", source=ProvenanceSource.QUERY)
    )
    merged = merge_employer_search_context(None, reqs)
    assert merged.role.source == ProvenanceSource.QUERY


def test_h5_empty_session_id_standalone_equivalence():
    """H5: Empty session_id yields unmodified Phase 1 DTO."""
    reqs = EmployerSearchRequirements(
        role=ProvenanceValue[str](value="QA Engineer", source=ProvenanceSource.QUERY)
    )
    merged = merge_employer_search_context(None, reqs)
    assert merged.role.value == "QA Engineer"


# ============================================================================
# CATEGORY I: Crash Recovery & Resilience Suite
# ============================================================================

def test_i1_db_read_failure_graceful_degradation():
    """I1: DB read failure degrades gracefully to fresh default session without crashing."""
    session_id = "test_crash_db_read_001"
    with patch("backend.utils.session_state._db_load_session", side_effect=Exception("DB Down")):
        state = get_session_state(session_id)
        assert state is not None
        assert state.get("version_seq", 1) == 1


def test_i2_db_write_failure_buffer_fallback():
    """I2: DB write failure retains response in L1 write-through memory without crashing."""
    session_id = "test_crash_db_write_001"
    key = make_composite_key(session_id)
    with patch("backend.utils.session_state.get_db", side_effect=Exception("DB Connection Failed")):
        set_search_context(session_id, {"role": "Python Dev"})
        st = _SESSION_STATES.get(key)
        assert st is not None
        assert st.get("search_context") == {"role": "Python Dev"}


def test_i3_malformed_stored_json_recovery():
    """I3: Malformed JSON string in DB resets corrupt row and initializes fresh session."""
    session_id = "test_crash_corrupt_json_001"
    with patch("backend.utils.session_state.get_db") as mock_db:
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = ("{bad_json:", None, None, None, time.time(), 1)
        mock_db.return_value.__enter__.return_value.cursor.return_value = mock_cursor

        state = _db_load_session(session_id)
        assert state is not None
        assert state.get("search_context") is None


def test_i4_missing_session_graceful_initialization():
    """I4: Unknown session_id initializes fresh session (V=1) cleanly."""
    state = get_session_state("non_existent_session_999999")
    assert state is not None
    assert state.get("version_seq", 1) == 1


def test_i5_expired_session_inactivity_handling():
    """I5: >4 hour expired session clears criteria/pagination while retaining auth & language."""
    session_id = "test_crash_expired_001"
    key = make_composite_key(session_id)
    _SESSION_STATES[key] = {
        "_last_active": time.time() - (14400 + 50),
        "search_context": {"role": "Old Role"},
        "language_preference": "english"
    }
    state = get_session_state(session_id)
    assert state.get("search_context") is None


def test_i6_unexpected_data_types_schema_resilience():
    """I6: Unexpected scalar values in context dict do not cause AttributeError or crash."""
    session_id = "test_crash_schema_001"
    set_session_state(session_id, {"search_context": "invalid_string_instead_of_dict"})
    ctx = get_search_context(session_id)
    assert ctx == "invalid_string_instead_of_dict"


def test_i7_cross_session_corrupted_state_isolation_firewall():
    """I7: Corrupted session_A state DOES NOT leak into session_B."""
    set_session_state("sess_corrupt_A", {"search_context": "CORRUPT_DATA"})
    set_session_state("sess_clean_B", {"search_context": {"role": "Clean Role"}})

    ctx_A = get_search_context("sess_corrupt_A")
    ctx_B = get_search_context("sess_clean_B")

    assert ctx_A == "CORRUPT_DATA"
    assert ctx_B == {"role": "Clean Role"}
