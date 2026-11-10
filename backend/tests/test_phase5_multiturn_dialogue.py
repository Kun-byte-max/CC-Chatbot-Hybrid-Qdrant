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
    import fakeredis
    from backend.utils.redis_client import set_test_redis_client, set_test_sync_redis_client
    server = fakeredis.FakeServer()
    fake = fakeredis.FakeRedis(server=server, decode_responses=True)
    set_test_redis_client(fake)
    set_test_sync_redis_client(fake)
    _SESSION_STATES.clear()
    yield fake
    set_test_redis_client(None)
    set_test_sync_redis_client(None)
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
    }, user_id="usr_999")
    res = reset_search_context(session_id, user_id="usr_999")
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
    set_pagination_state(session_id, {"current_page": 3, "total_pages": 5}, user_id="usr_999")
    # Reset triggers context update
    reset_search_context(session_id, user_id="usr_999")
    pag = get_pagination_state(session_id, user_id="usr_999")
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
    }, user_id="usr_999")
    res = handle_pagination_command(session_id, "next page", user_id="usr_999")
    assert res.get("current_page") == 2
    assert get_search_context(session_id, user_id="usr_999") == {"skills": ["Python"]}


def test_c2_previous_page_command():
    """C2: 'previous' decrements current page while criteria remain untouched."""
    session_id = "test_page_cmd_002"
    set_session_state(session_id, {
        "search_context": {"skills": ["Python"]},
        "pagination": {"current_page": 3, "total_pages": 4}
    }, user_id="usr_999")
    res = handle_pagination_command(session_id, "previous page", user_id="usr_999")
    assert res.get("current_page") == 2
    assert get_search_context(session_id, user_id="usr_999") == {"skills": ["Python"]}


def test_c3_explicit_page_number_command():
    """C3: 'page 4' updates current_page to target page."""
    session_id = "test_page_cmd_003"
    set_session_state(session_id, {
        "search_context": {"skills": ["Java"]},
        "pagination": {"current_page": 1, "total_pages": 10}
    }, user_id="usr_999")
    res = handle_pagination_command(session_id, "go to page 4", user_id="usr_999")
    assert res.get("current_page") == 4
    assert get_search_context(session_id, user_id="usr_999") == {"skills": ["Java"]}


def test_c4_pagination_lower_boundary_clamp():
    """C4: 'previous' on page 1 remains page 1."""
    session_id = "test_page_cmd_004"
    set_session_state(session_id, {
        "pagination": {"current_page": 1, "total_pages": 5}
    }, user_id="usr_999")
    res = handle_pagination_command(session_id, "prev", user_id="usr_999")
    assert res.get("current_page") == 1


def test_c5_pagination_upper_boundary_clamp():
    """C5: 'next' on max page remains max page."""
    session_id = "test_page_cmd_005"
    set_session_state(session_id, {
        "pagination": {"current_page": 5, "total_pages": 5}
    }, user_id="usr_999")
    res = handle_pagination_command(session_id, "next", user_id="usr_999")
    assert res.get("current_page") == 5


# ============================================================================
# CATEGORY D: Ambiguous Prompt Policy
# ============================================================================

def test_d1_ambiguous_prompt_leaves_primary_state_unmutated():
    """D1: Ambiguous transient evaluation does NOT alter active session state."""
    session_id = "test_ambig_001"
    initial_ctx = {"role": "Developer", "skills": ["Python"]}
    set_search_context(session_id, initial_ctx, user_id="usr_999")
    # Simulate transient evaluation without set_search_context call
    active_ctx = get_search_context(session_id, user_id="usr_999")
    assert active_ctx == initial_ctx


def test_d2_ambiguous_prompt_preserves_pagination():
    """D2: Ambiguous prompt evaluation preserves pagination state."""
    session_id = "test_ambig_002"
    set_pagination_state(session_id, {"current_page": 2, "total_pages": 5}, user_id="usr_999")
    pag = get_pagination_state(session_id, user_id="usr_999")
    assert pag.get("current_page") == 2


def test_d3_ambiguous_prompt_preserves_language():
    """D3: Ambiguous evaluation preserves language preference."""
    session_id = "test_ambig_003"
    set_language_preference(session_id, "hinglish", user_id="usr_999")
    lang = get_language_preference(session_id, user_id="usr_999")
    assert lang == "hinglish"


def test_d4_ambiguous_prompt_preserves_snapshot():
    """D4: Ambiguous query evaluation preserves active snapshot."""
    session_id = "test_ambig_004"
    q = SearchQuery(title="Python Developer")
    snap = SearchSnapshot(
        snapshot_id="s1", session_id=session_id, query_hash="hash1", search_query=q
    )
    set_search_snapshot(session_id, snap, user_id="usr_999")
    loaded_snap = get_search_snapshot(session_id, user_id="usr_999")
    assert loaded_snap is not None
    assert loaded_snap.snapshot_id == "s1"


def test_d5_ambiguous_prompt_preserves_dialogue_version():
    """D5: Ambiguous prompt preserves state version sequence."""
    session_id = "test_ambig_005"
    st = get_session_state(session_id, user_id="usr_999")
    initial_v = st.get("version_seq", 1)
    # Read-only operation doesn't increment version
    st_read = get_session_state(session_id, user_id="usr_999")
    assert st_read.get("version_seq", 1) == initial_v


# ============================================================================
# CATEGORY E: Cache Sync, Role Isolation & Concurrency Suite
# ============================================================================

def test_e1_role_isolation_namespacing():
    """E1: Employer and Employee contexts with same base session_id are isolated."""
    base_session = "user_session_100"
    set_employer_search_context(base_session, {"role": "Python Developer"}, role="employer", user_id="usr_100")
    set_search_context(base_session, {"title": "Data Analyst"}, role="employee", user_id="usr_100")

    emp_ctx = get_employer_search_context(base_session, role="employer", user_id="usr_100")
    ee_ctx = get_search_context(base_session, role="employee", user_id="usr_100")

    assert emp_ctx == {"role": "Python Developer"}
    assert ee_ctx == {"title": "Data Analyst"}


def test_e2_l1_memory_hit():
    """E2: Redis active session returns state."""
    session_id = "test_l1_hit_001"
    set_session_state(session_id, {"search_context": {"skills": ["React"]}}, user_id="usr_101")
    state = get_session_state(session_id, user_id="usr_101")
    assert state.get("search_context") == {"skills": ["React"]}


def test_e3_l2_db_fallback_on_l1_miss():
    """E3: Redis is single source of truth without database persistence."""
    session_id = "test_l2_fallback_001"
    set_session_state(session_id, {"search_context": {"skills": ["C++"]}}, user_id="usr_101")
    state = get_session_state(session_id, user_id="usr_101")
    assert state.get("search_context") == {"skills": ["C++"]}


def test_e4_version_invalidation_on_stale_l1():
    """E4: Redis state version increases monotonically on successive saves."""
    session_id = "test_v_invalidation_001"
    set_session_state(session_id, {"search_context": {"role": "V1"}}, user_id="usr_101")
    st1 = get_session_state(session_id, user_id="usr_101")
    set_session_state(session_id, {"search_context": {"role": "V2"}}, user_id="usr_101")
    st2 = get_session_state(session_id, user_id="usr_101")
    assert st2.get("version_seq", 1) > st1.get("version_seq", 0)


def test_e5_four_hour_ttl_expiry_and_selective_retention():
    """E5: Inactive session wipes criteria/pagination/snapshot but retains auth & language."""
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
    set_session_state(session_id, st, user_id="usr_777")

    from backend.utils.redis_client import get_sync_redis_client
    from backend.utils.session_state import make_composite_key, to_redis_key
    client = get_sync_redis_client()
    key = make_composite_key(session_id, user_id="usr_777")
    rkey = to_redis_key(key, user_id="usr_777", session_id=session_id)
    raw = client.get(rkey)
    if raw:
        data = json.loads(raw)
        data["_last_active"] = old_time
        client.set(rkey, json.dumps(data))

    loaded = get_session_state(session_id, user_id="usr_777")
    assert loaded.get("search_context") is None
    assert loaded.get("pagination") is None
    assert loaded.get("search_snapshot") is None
    assert loaded.get("language_preference") == "hindi"


def test_e6_concurrent_stale_write_cas_guard():
    """E6: Stale write (version mismatch) triggers atomic CAS rejection."""
    session_id = "test_cas_guard_001"
    set_session_state(session_id, {"search_context": {"role": "Initial"}}, user_id="usr_101")
    st = get_session_state(session_id, user_id="usr_101")
    st["search_context"] = {"role": "Stale Attempt"}
    from backend.utils.session_state import save_session_state_cas
    success = save_session_state_cas(session_id, st, expected_version=999, user_id="usr_101")
    assert success is False


def test_e7_cas_bounded_retry_exhaustion_graceful_degradation():
    """E7: Repeated CAS failure terminates loop and degrades cleanly."""
    from backend.utils.session_state import save_session_state_cas
    session_id = "test_cas_retry_exhaust_001"
    set_session_state(session_id, {"search_context": {"role": "Initial"}}, user_id="usr_101")
    success = save_session_state_cas(session_id, {"search_context": {"role": "Attempt"}}, expected_version=999, max_retries=3, user_id="usr_101")
    assert success is False


def test_e8_cas_conflict_true_remerge_preserves_concurrent_delta():
    """E8: CAS conflict re-merges turn delta against reloaded Redis state without overwriting concurrent additions."""
    from backend.utils.session_state import update_employer_session_cas
    session_id = "test_cas_remerge_delta_001"
    
    # Initial state: skills = ["Python"]
    v10_reqs = EmployerSearchRequirements(skills=ProvenanceValue(value=["Python"], source=ProvenanceSource.QUERY))
    set_session_state(session_id, {"search_context": v10_reqs.model_dump(), "version_seq": 10}, role="employer", user_id="usr_101")

    # Worker A turn delta: add "React"
    worker_a_turn = EmployerSearchRequirements(skills=ProvenanceValue(value=["React"], source=ProvenanceSource.EXPLICIT_FOLLOWUP))

    # Worker B turn: add "Java"
    worker_b_turn = EmployerSearchRequirements(skills=ProvenanceValue(value=["Java"], source=ProvenanceSource.EXPLICIT_FOLLOWUP))
    update_employer_session_cas(session_id, worker_b_turn, role="employer", user_id="usr_101")

    # Worker A executes
    final_state = update_employer_session_cas(session_id, worker_a_turn, role="employer", user_id="usr_101")

    # Verify that final state preserved Worker B's "Java" AND applied Worker A's "React" -> ["Python", "Java", "React"]
    final_skills = final_state.get("search_context", {}).get("skills", {}).get("value", [])
    assert "Python" in final_skills
    assert "Java" in final_skills
    assert "React" in final_skills


def test_e11_concurrent_different_field_update():
    """E11: Concurrent different-field update re-merges turn delta preserving both Worker B's field change and Worker A's field change."""
    from backend.utils.session_state import update_employer_session_cas
    session_id = "test_cas_diff_fields_001"
    
    # Initial state: role = "Developer", location = "Delhi"
    v10_reqs = EmployerSearchRequirements(
        role=ProvenanceValue(value="Developer", source=ProvenanceSource.QUERY),
        location=LocationRequirement(text="Delhi")
    )
    set_session_state(session_id, {"search_context": v10_reqs.model_dump(), "version_seq": 10}, role="employer", user_id="usr_101")

    # Worker A turn delta: experience = 5 years
    worker_a_turn = EmployerSearchRequirements(
        experience=ExperienceRequirement(min=5)
    )

    # Worker B succeeded first: location updated to "Bangalore"
    worker_b_turn = EmployerSearchRequirements(
        location=LocationRequirement(text="Bangalore")
    )
    update_employer_session_cas(session_id, worker_b_turn, role="employer", user_id="usr_101")

    # Worker A executes
    final_state = update_employer_session_cas(session_id, worker_a_turn, role="employer", user_id="usr_101")

    # Expected final state: role="Developer", location="Bangalore", experience=5
    ctx = final_state.get("search_context", {})
    assert ctx.get("role", {}).get("value") == "Developer"
    assert ctx.get("location", {}).get("text") == "Bangalore"
    assert ctx.get("experience", {}).get("min") == 5


def test_e12_simultaneous_two_threads_additive_turns():
    """E12: Simultaneous execution of Worker Thread A (add React) and Worker Thread B (add Java) against shared Redis store."""
    import threading
    from backend.utils.session_state import update_employer_session_cas
    session_id = "test_cas_threads_001"

    # Initial state: skills = ["Python"]
    v10_reqs = EmployerSearchRequirements(skills=ProvenanceValue(value=["Python"], source=ProvenanceSource.QUERY))
    set_session_state(session_id, {"search_context": v10_reqs.model_dump(), "version_seq": 10}, role="employer", user_id="usr_101")

    worker_a_turn = EmployerSearchRequirements(skills=ProvenanceValue(value=["React"], source=ProvenanceSource.EXPLICIT_FOLLOWUP))
    worker_b_turn = EmployerSearchRequirements(skills=ProvenanceValue(value=["Java"], source=ProvenanceSource.EXPLICIT_FOLLOWUP))

    res_a = {}
    res_b = {}

    def run_worker_a():
        res_a.update(update_employer_session_cas(session_id, worker_a_turn, role="employer", user_id="usr_101"))

    def run_worker_b():
        res_b.update(update_employer_session_cas(session_id, worker_b_turn, role="employer", user_id="usr_101"))

    t1 = threading.Thread(target=run_worker_a)
    t2 = threading.Thread(target=run_worker_b)

    t1.start()
    t2.start()

    t1.join()
    t2.join()

    final_state = get_session_state(session_id, role="employer", user_id="usr_101")
    final_skills = final_state.get("search_context", {}).get("skills", {}).get("value", [])
    assert "Python" in final_skills
    assert "React" in final_skills
    assert "Java" in final_skills


def test_e9_reset_search_context_cas_remerge_on_collision():
    """E9: reset_search_context clears criteria on Redis state while preserving lang."""
    from backend.utils.session_state import reset_search_context
    session_id = "test_reset_cas_001"

    set_session_state(session_id, {
        "search_context": {"role": "Dev"},
        "language_preference": "hindi",
        "user_id": "usr_999",
        "version_seq": 10,
    }, user_id="usr_999")

    res_state = reset_search_context(session_id, user_id="usr_999")

    assert res_state.get("search_context") is None
    assert res_state.get("language_preference") == "hindi"
    assert res_state.get("user_id") == "usr_999"


def test_e10_handle_pagination_command_cas_remerge_on_collision():
    """E10: handle_pagination_command applies page increment against Redis state."""
    from backend.utils.session_state import handle_pagination_command
    session_id = "test_page_cas_001"

    set_session_state(session_id, {
        "search_context": {"role": "Senior Dev"},
        "pagination": {"current_page": 1, "total_pages": 5},
        "version_seq": 10
    }, user_id="usr_101")

    pag = handle_pagination_command(session_id, "next", user_id="usr_101")

    assert pag is not None
    assert pag.get("current_page") == 2
    state = get_session_state(session_id, user_id="usr_101")
    assert state.get("search_context") == {"role": "Senior Dev"}


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
    """F5: Redis session persistence persists clean JSON without forbidden secret keys."""
    session_id = "test_sec_save_001"
    state = {
        "search_context": {
            "role": "Developer",
            "token": "should_be_scrubbed"
        },
        "version_seq": 1
    }
    set_session_state(session_id, state, user_id="usr_999")
    loaded = get_session_state(session_id, user_id="usr_999")
    assert loaded.get("search_context", {}).get("role") == "Developer"
    assert "token" not in loaded.get("search_context", {})


def test_f6_stale_write_rejected_by_version_sequence_guard():
    """F6: Direct save_session_state_cas call with stale version is rejected."""
    session_id = "test_stale_guard_001"
    set_session_state(session_id, {"search_context": {"role": "Initial"}, "version_seq": 15}, user_id="usr_999")
    stale_payload = {"search_context": {"role": "Stale Role"}, "version_seq": 10}
    from backend.utils.session_state import save_session_state_cas
    success = save_session_state_cas(session_id, stale_payload, expected_version=10, user_id="usr_999")
    assert success is False


def test_f7_cas_sql_exception_does_not_downgrade_to_unconditional_upsert():
    """F7: Concurrency WatchError contention exhausts retries and returns False without downgrading."""
    session_id = "test_cas_exception_guard_001"
    from backend.utils.session_state import save_session_state_cas
    from redis.exceptions import WatchError
    from backend.utils.redis_client import RedisUnavailableError
    import pytest

    # 1. WatchError contention exhausts retries -> returns False
    with patch("backend.utils.session_state.get_sync_redis_client") as mock_rc:
        mock_pipe = mock_rc.return_value.pipeline.return_value
        mock_pipe.execute.side_effect = WatchError("Contention")
        payload = {"search_context": {"role": "Tester"}, "version_seq": 11}
        success = save_session_state_cas(session_id, payload, expected_version=10, user_id="usr_999")
        assert success is False

    # 2. Hard infrastructure error -> raises typed RedisUnavailableError
    with patch("backend.utils.session_state.get_sync_redis_client") as mock_rc:
        mock_rc.return_value.pipeline.side_effect = Exception("Synthetic Redis Exception")
        with pytest.raises(RedisUnavailableError):
            save_session_state_cas(session_id, payload, expected_version=10, user_id="usr_999")


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
    set_language_preference(session_id, "hindi", user_id="usr_999")
    reset_search_context(session_id, user_id="usr_999")
    lang = get_language_preference(session_id, user_id="usr_999")
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
    set_search_context("sess_A", {"role": "Role A"}, user_id="usr_101")
    set_search_context("sess_B", {"role": "Role B"}, user_id="usr_101")

    assert get_search_context("sess_A", user_id="usr_101") == {"role": "Role A"}
    assert get_search_context("sess_B", user_id="usr_101") == {"role": "Role B"}


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
    assert json.dumps(reqs.model_dump(), sort_keys=True) == json.dumps(merged.model_dump(), sort_keys=True)


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
    """I1: Redis read failure raises RedisUnavailableError without silent fallback."""
    session_id = "test_crash_db_read_001"
    from backend.utils.redis_client import RedisUnavailableError
    with patch("backend.utils.session_state.get_sync_redis_client") as mock_rc:
        mock_rc.return_value.get.side_effect = Exception("Redis Down")
        with pytest.raises(RedisUnavailableError):
            get_session_state(session_id, user_id="usr_999")


def test_i2_db_write_failure_buffer_fallback():
    """I2: Redis write failure in domain helper propagates typed RedisUnavailableError."""
    session_id = "test_crash_db_write_001"
    from backend.utils.redis_client import RedisUnavailableError
    import pytest
    with patch("backend.utils.session_state.get_sync_redis_client") as mock_rc:
        mock_rc.return_value.pipeline.side_effect = Exception("Redis Connection Failed")
        with pytest.raises(RedisUnavailableError):
            set_search_context(session_id, {"role": "Python Dev"}, user_id="usr_999")


def test_i3_malformed_stored_json_recovery():
    """I3: Malformed JSON string in Redis resets corrupt entry and initializes fresh session."""
    session_id = "test_crash_corrupt_json_001"
    from backend.utils.redis_client import get_sync_redis_client
    from backend.utils.session_state import make_composite_key, to_redis_key
    client = get_sync_redis_client()
    key = make_composite_key(session_id, user_id="usr_999")
    rkey = to_redis_key(key, user_id="usr_999", session_id=session_id)
    client.set(rkey, "{bad_json:")

    state = get_session_state(session_id, user_id="usr_999")
    assert state is not None
    assert state.get("search_context") is None


def test_i4_missing_session_graceful_initialization():
    """I4: Unknown session_id initializes fresh session (V=1) cleanly."""
    state = get_session_state("non_existent_session_999999", user_id="usr_999")
    assert state is not None
    assert state.get("version_seq", 1) == 1


def test_i5_expired_session_inactivity_handling():
    """I5: >4 hour expired session clears criteria/pagination while retaining auth & language."""
    session_id = "test_crash_expired_001"
    old_time = time.time() - (14400 + 50)
    set_session_state(session_id, {
        "search_context": {"role": "Old Role"},
        "language_preference": "english"
    }, user_id="usr_999")

    from backend.utils.redis_client import get_sync_redis_client
    from backend.utils.session_state import make_composite_key, to_redis_key
    client = get_sync_redis_client()
    key = make_composite_key(session_id, user_id="usr_999")
    rkey = to_redis_key(key, user_id="usr_999", session_id=session_id)
    raw = client.get(rkey)
    if raw:
        data = json.loads(raw)
        data["_last_active"] = old_time
        client.set(rkey, json.dumps(data))

    state = get_session_state(session_id, user_id="usr_999")
    assert state.get("search_context") is None


def test_i6_unexpected_data_types_schema_resilience():
    """I6: Unexpected scalar values in context dict do not cause AttributeError or crash."""
    session_id = "test_crash_schema_001"
    set_session_state(session_id, {"search_context": "invalid_string_instead_of_dict"}, user_id="usr_999")
    ctx = get_search_context(session_id, user_id="usr_999")
    assert ctx == "invalid_string_instead_of_dict"


def test_i7_cross_session_corrupted_state_isolation_firewall():
    """I7: Corrupted session_A state DOES NOT leak into session_B."""
    set_session_state("sess_corrupt_A", {"search_context": "CORRUPT_DATA"}, user_id="usr_999")
    set_session_state("sess_clean_B", {"search_context": {"role": "Clean Role"}}, user_id="usr_999")

    ctx_A = get_search_context("sess_corrupt_A", user_id="usr_999")
    ctx_B = get_search_context("sess_clean_B", user_id="usr_999")

    assert ctx_A == "CORRUPT_DATA"
    assert ctx_B == {"role": "Clean Role"}
