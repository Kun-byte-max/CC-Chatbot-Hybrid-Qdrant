import time
import pytest
from unittest.mock import patch

from backend.schemas.conversation_context import ConversationContext
from backend.utils.redis_client import RedisSessionOwnershipError
from backend.utils.session_state import (
    get_conversation_context,
    set_session_state,
    reset_search_context,
    set_session_option_mapping,
    set_pending_requested_level,
    set_selected_employment_context,
    set_recommendation_session,
    get_recommendation_session,
    update_last_referenced_job_id,
    begin_search_generation,
    get_search_generation,
    PENDING_OPTION_MAPPING_TTL_SECONDS,
)


class TestConversationContextFacade:
    """Focused regression tests for Phase 6B Canonical Conversation Context Facade."""

    @pytest.fixture(autouse=True)
    def setup_fakeredis(self):
        """Sets up a shared in-memory fakeredis instance for sync and async operations."""
        import fakeredis
        import fakeredis.aioredis
        from backend.utils.redis_client import set_test_redis_client, set_test_sync_redis_client
        from backend.utils.session_state import _SESSION_STATES
        server = fakeredis.FakeServer()
        fake_sync = fakeredis.FakeRedis(server=server, decode_responses=True)
        fake_async = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
        set_test_redis_client(fake_async)
        set_test_sync_redis_client(fake_sync)
        _SESSION_STATES.clear()
        yield fake_sync
        set_test_redis_client(None)
        set_test_sync_redis_client(None)
        _SESSION_STATES.clear()

    def test_missing_session_context_returns_valid_empty_representation(self):
        """1. Missing or brand-new session context returns a valid representation without inventing active results."""
        sess_id = f"test_ctx_missing_{time.time()}"
        ctx = get_conversation_context(sess_id, role="employee")

        assert isinstance(ctx, ConversationContext)
        assert ctx.session_id == sess_id
        assert ctx.role == "employee"
        assert ctx.has_active_search_context is False
        assert ctx.has_active_job_results is False
        assert ctx.active_job_count == 0
        assert ctx.last_referenced_job_id is None
        assert ctx.pending_option_map is None
        assert ctx.pending_requested_level is None
        assert ctx.has_active_pagination is False

    def test_role_and_user_isolation(self):
        """2. Employee and Employer contexts, as well as distinct users, remain strictly isolated."""
        sess_id = f"test_ctx_isolation_{time.time()}"
        uid = 101

        # Populate Employee state
        set_session_state(sess_id, {"search_context": {"keyword": "python dev"}}, role="employee", user_id=uid)

        # Populate Employer state
        set_session_state(sess_id, {"employer_search_context": {"role": "react architect"}}, role="employer", user_id=uid)

        ctx_emp = get_conversation_context(sess_id, role="employee", user_id=uid)
        ctx_empr = get_conversation_context(sess_id, role="employer", user_id=uid)

        assert ctx_emp.role == "employee"
        assert ctx_emp.active_search_criteria == {"keyword": "python dev"}

        assert ctx_empr.role == "employer"
        assert ctx_empr.active_search_criteria == {"role": "react architect"}

        # Distinct user ID isolation
        ctx_u1 = get_conversation_context(sess_id, role="employee", user_id=101)
        ctx_u2 = get_conversation_context(sess_id, role="employee", user_id=202)
        assert ctx_u1.composite_key != ctx_u2.composite_key
        assert "101" in ctx_u1.composite_key
        assert "202" in ctx_u2.composite_key

    def test_active_search_generation_and_result_metadata_consistency(self):
        """3. Search generation and recommendation context metadata reflect authoritative state accurately."""
        sess_id = f"test_ctx_gen_{time.time()}"
        uid = 101

        # Initialize generation and jobs
        gen = begin_search_generation(sess_id, role="employee", user_id=uid)
        assert gen >= 1

        mock_jobs = [
            {"job_id": 1, "title": "Dev 1"},
            {"job_id": 2, "title": "Dev 2"},
            {"job_id": 3, "title": "Dev 3"},
        ]
        set_recommendation_session(
            session_id=sess_id,
            query_key="python",
            ranked_jobs=mock_jobs,
            current_page=1,
            page_size=5,
            total_jobs=3,
            role="employee",
            user_id=uid,
        )
        update_last_referenced_job_id(sess_id, 2, role="employee", user_id=uid)

        ctx = get_conversation_context(sess_id, role="employee", user_id=uid)
        assert ctx.search_generation == gen
        assert ctx.has_active_job_results is True
        assert ctx.active_job_count == 3
        assert ctx.last_referenced_job_id == 2

    def test_superseded_or_reset_search_is_not_exposed_as_active(self):
        """4. A reset search clears search and recommendation context while preserving version and language."""
        sess_id = f"test_ctx_reset_{time.time()}"
        uid = 101

        mock_jobs = [{"job_id": 10, "title": "Analyst"}]
        set_recommendation_session(
            session_id=sess_id,
            query_key="analyst",
            ranked_jobs=mock_jobs,
            current_page=1,
            page_size=5,
            total_jobs=1,
            role="employee",
            user_id=uid,
        )
        ctx_before = get_conversation_context(sess_id, role="employee", user_id=uid)
        assert ctx_before.has_active_job_results is True
        assert ctx_before.active_job_count == 1

        # Reset search context
        reset_search_context(sess_id, role="employee", user_id=uid)

        ctx_after = get_conversation_context(sess_id, role="employee", user_id=uid)
        assert ctx_after.has_active_job_results is False
        assert ctx_after.active_job_count == 0
        assert ctx_after.has_active_search_context is False

    def test_employment_option_map_and_ttl_expiration(self):
        """5. Employment option-map and pending-level are represented, and expired maps are safely cleared."""
        sess_id = f"test_ctx_opt_{time.time()}"
        uid = 101

        # 1. Active option map and pending level
        set_session_option_mapping(sess_id, {"emp_rec_1": "Google::123"}, role="employee", user_id=uid)
        set_pending_requested_level(sess_id, 2, role="employee", user_id=uid)
        set_selected_employment_context(sess_id, "Google", record_key="123", role="employee", user_id=uid)

        ctx = get_conversation_context(sess_id, role="employee", user_id=uid)
        assert ctx.pending_option_map == {"emp_rec_1": "Google::123"}
        assert ctx.pending_requested_level == 2
        assert ctx.selected_employment_context == "Google"
        assert ctx.selected_employment_record_key == "123"

        # 2. TTL Expiration (Simulate expired created_at beyond 15 min TTL)
        with patch("time.time", return_value=time.time() + PENDING_OPTION_MAPPING_TTL_SECONDS + 60):
            ctx_expired = get_conversation_context(sess_id, role="employee", user_id=uid)
            assert ctx_expired.pending_option_map is None
            # selected_employment_context is persistent across turns and not subject to the 15-min option map TTL
            assert ctx_expired.selected_employment_context == "Google"

    def test_read_does_not_mutate_state(self):
        """6. Reading conversation context is strictly non-mutating."""
        sess_id = f"test_ctx_nomutate_{time.time()}"
        uid = 101
        begin_search_generation(sess_id, role="employee", user_id=uid)

        ctx1 = get_conversation_context(sess_id, role="employee", user_id=uid)
        ctx2 = get_conversation_context(sess_id, role="employee", user_id=uid)

        assert ctx1.context_version == ctx2.context_version
        assert ctx1.search_generation == ctx2.search_generation

    def test_anonymous_session_handling_backward_compatible(self):
        """7. Anonymous sessions without role or user_id default to employee role property while preserving legacy key."""
        sess_id = f"test_ctx_anon_{time.time()}"
        ctx = get_conversation_context(sess_id)
        assert ctx.session_id == sess_id
        assert ctx.role == "employee"
        assert ctx.user_id is None
        # When role and user_id are None, make_composite_key returns raw session_id
        assert ctx.composite_key == sess_id

    def test_facade_and_legacy_accessors_use_same_composite_key(self):
        """8. Facade and legacy accessors resolve against the identical composite key."""
        from backend.utils.session_state import make_composite_key, get_session_state
        sess_id = f"test_ctx_keys_{time.time()}"
        role = "employer"
        user_id = 999

        expected_key = make_composite_key(sess_id, role=role, user_id=user_id)
        ctx = get_conversation_context(sess_id, role=role, user_id=user_id)
        assert ctx.composite_key == expected_key

        # Verify underlying session dict accessed by get_session_state matches
        legacy_state = get_session_state(expected_key)
        assert ctx.context_version == int(legacy_state.get("version_seq", 1))

    def test_request_entry_context_used_in_pipeline(self):
        """9. TestClient request entry loads context snapshot with authenticated identity."""
        from fastapi.testclient import TestClient
        from backend.main import app, verify_token

        client = TestClient(app)
        app.dependency_overrides[verify_token] = lambda: {"uid": 4001, "email": "test6c@user.com", "role": "employee"}
        sess_id = f"test_sess_entry_{time.time()}"

        try:
            r = client.post("/chat", json={
                "messages": [{"role": "user", "content": "hello"}],
                "session_id": sess_id,
            })
            assert r.status_code == 200
            # Verify session state was touched under the composite key with uid 4001
            ctx = get_conversation_context(sess_id, role="employee", user_id=4001)
            assert ctx.last_active_at is not None
            assert ctx.composite_key == f"4001:{sess_id}:employee"
        finally:
            app.dependency_overrides.clear()

    def test_canonical_precedence_over_raw_session_fallback(self):
        """10. Canonical composite-key state takes precedence and unauthenticated writes are rejected."""
        sess_id = f"test_ctx_precedence_{time.time()}"
        uid = 5001
        role = "employee"

        # Unauthenticated raw session writes are rejected under Phase 2B.1
        with pytest.raises(RedisSessionOwnershipError):
            set_session_option_mapping(sess_id, {"emp_rec_1": "RawLegacy::10"})
        with pytest.raises(RedisSessionOwnershipError):
            set_pending_requested_level(sess_id, 1)

        # Set canonical composite-key option map and pending level
        set_session_option_mapping(sess_id, {"emp_rec_1": "CanonicalUser::20"}, role=role, user_id=uid)
        set_pending_requested_level(sess_id, 3, role=role, user_id=uid)

        # Context for authenticated user should strictly observe canonical value
        ctx = get_conversation_context(sess_id, role=role, user_id=uid)
        assert ctx.pending_option_map == {"emp_rec_1": "CanonicalUser::20"}
        assert ctx.pending_requested_level == 3

    def test_role_and_user_fallback_isolation(self):
        """11. Employer requests or different users preserve role and identity isolation."""
        sess_id = f"test_ctx_fallback_iso_{time.time()}"
        uid1 = 6001
        uid2 = 6002

        # User 1 sets employment selection on canonical key
        set_session_option_mapping(sess_id, {"emp_rec_1": "User1Company::1"}, role="employee", user_id=uid1)
        set_selected_employment_context(sess_id, "User1Company", record_key="key1", role="employee", user_id=uid1)

        # Employer context for same session must NOT leak employee employment options
        ctx_employer = get_conversation_context(sess_id, role="employer", user_id=uid1)
        assert ctx_employer.role == "employer"
        assert ctx_employer.pending_option_map is None
        assert ctx_employer.selected_employment_context is None

        # User 2 context for same session must have distinct composite key
        ctx_u1 = get_conversation_context(sess_id, role="employee", user_id=uid1)
        ctx_u2 = get_conversation_context(sess_id, role="employee", user_id=uid2)
        assert ctx_u1.composite_key != ctx_u2.composite_key
        assert ctx_u1.composite_key == f"{uid1}:{sess_id}:employee"
        assert ctx_u2.composite_key == f"{uid2}:{sess_id}:employee"

    def test_two_authenticated_employees_same_session_id_isolation(self):
        """12. Two authenticated employee users sharing the same raw session ID remain strictly isolated."""
        sess_id = f"test_shared_sess_{time.time()}"
        uid_a = 7001
        uid_b = 7002

        # User A has job results in session scoped to User A
        set_recommendation_session(
            session_id=sess_id,
            query_key="cand_a",
            ranked_jobs=[{"id": 101, "title": "Job A"}],
            current_page=1,
            page_size=5,
            total_jobs=1,
            role="employee",
            user_id=uid_a,
        )
        update_last_referenced_job_id(sess_id, 101, role="employee", user_id=uid_a)

        # User A's generation advances
        gen_a = begin_search_generation(sess_id, role="employee", user_id=uid_a)

        # Verify User A can read their recommendation session
        rec_a = get_recommendation_session(sess_id, role="employee", user_id=uid_a)
        assert rec_a is not None
        assert rec_a["ranked_jobs"][0]["id"] == 101
        assert rec_a["last_referenced_job_id"] == 101

        # Verify User B cannot read User A's recommendation session
        rec_b = get_recommendation_session(sess_id, role="employee", user_id=uid_b)
        assert rec_b is None

        # Verify ConversationContext agrees
        ctx_a = get_conversation_context(sess_id, role="employee", user_id=uid_a)
        ctx_b = get_conversation_context(sess_id, role="employee", user_id=uid_b)

        assert ctx_a.composite_key == f"{uid_a}:{sess_id}:employee"
        assert ctx_b.composite_key == f"{uid_b}:{sess_id}:employee"
        assert ctx_a.composite_key != ctx_b.composite_key

        assert ctx_a.has_active_job_results is True
        assert ctx_a.active_job_count == 1
        assert ctx_a.last_referenced_job_id == 101
        assert ctx_a.search_generation == gen_a

        assert ctx_b.has_active_job_results is False
        assert ctx_b.active_job_count == 0
        assert ctx_b.last_referenced_job_id is None

        # Reset by User B must not clear User A's state
        reset_search_context(sess_id, role="employee", user_id=uid_b)
        rec_a_after = get_recommendation_session(sess_id, role="employee", user_id=uid_a)
        assert rec_a_after is not None
        assert rec_a_after["ranked_jobs"][0]["id"] == 101

        # Advance User B's search generation; User A's generation must remain unchanged
        gen_b = begin_search_generation(sess_id, role="employee", user_id=uid_b)
        assert gen_b == 1
        assert get_search_generation(sess_id, role="employee", user_id=uid_a) == gen_a
        assert get_search_generation(sess_id, role="employee", user_id=uid_b) == gen_b
        ctx_a_after_gen = get_conversation_context(sess_id, role="employee", user_id=uid_a)
        ctx_b_after_gen = get_conversation_context(sess_id, role="employee", user_id=uid_b)
        assert ctx_a_after_gen.search_generation == gen_a
        assert ctx_b_after_gen.search_generation == gen_b

    @pytest.mark.anyio
    async def test_step5b_metadata_empty_pool_handling(self):
        """13. Context metadata indicating no active results vs active results while pool empty."""
        from backend.api.employee import handle_employee_context
        sess_id = f"test_empty_pool_{time.time()}"
        uid = 8000

        # Turn with no active rec context
        res = await handle_employee_context("What about the second one?", session_id=sess_id, user_id=uid)
        # When no active rec context exists and query is asking for follow-up details on second one,
        # it returns error gracefully
        assert res is not None
        assert res.get("result_type") == "job_detail_error"

    def test_authenticated_user_cannot_inherit_other_user_legacy_option_map(self):
        """14. Authenticated user A cannot read a legacy option map created by authenticated user B."""
        sess_id = f"test_cross_user_legacy_{time.time()}"
        uid_a = 8001
        uid_b = 8002

        # User B sets employment selection under canonical user B key
        set_session_option_mapping(sess_id, {"emp_rec_1": "UserBCompany::100"}, role="employee", user_id=uid_b)
        set_session_state(sess_id, {"pending_option_mapping": {"created_at": time.time(), "mapping": {"emp_rec_1": "LegacyB::100"}}}, role="employee", user_id=uid_b)

        # Authenticated User A context MUST NOT inherit User B's legacy or role-scoped option map
        ctx_a = get_conversation_context(sess_id, role="employee", user_id=uid_a)
        assert ctx_a.pending_option_map is None
        assert ctx_a.selected_employment_context is None

    def test_authenticated_request_cannot_inherit_anonymous_raw_session_state(self):
        """15. Authenticated requests cannot inherit unauthenticated/anonymous state."""
        sess_id = f"test_auth_no_anon_inherit_{time.time()}"
        uid = 8003

        # Anonymous state write is rejected under Phase 2B.1
        with pytest.raises(RedisSessionOwnershipError):
            set_session_option_mapping(sess_id, {"emp_rec_1": "AnonCorp::999"})

        # Context for an authenticated employee must NOT inherit this anonymous state
        ctx_auth = get_conversation_context(sess_id, role="employee", user_id=uid)
        assert ctx_auth.pending_option_map is None
        assert ctx_auth.pending_requested_level is None
        assert ctx_auth.selected_employment_context is None

    def test_anonymous_legacy_fallback_remains_functional(self):
        """16. Unauthenticated requests reject state writes and return safe empty context."""
        sess_id = f"test_anon_fallback_func_{time.time()}"

        # Anonymous caller attempting to write state is rejected
        with pytest.raises(RedisSessionOwnershipError):
            set_session_option_mapping(sess_id, {"emp_rec_1": "AnonSafeCorp::1"})

        # Calling get_conversation_context with no user_id returns safe empty context
        ctx_anon = get_conversation_context(sess_id, role="employee", user_id=None)
        assert ctx_anon.has_active_search_context is False
        assert ctx_anon.pending_option_map is None
        assert ctx_anon.selected_employment_context is None

    def test_employment_selection_fields_cannot_be_combined_from_different_scopes(self):
        """17. Employment-selection fields cannot be combined from different state sources/scopes."""
        sess_id = f"test_no_mixed_scope_{time.time()}"
        uid = 8004

        # User writes company context without option map
        set_selected_employment_context(sess_id, "UserCompany", record_key="key_1", role="employee", user_id=uid)

        ctx = get_conversation_context(sess_id, role="employee", user_id=uid)
        assert ctx.selected_employment_context == "UserCompany"
        assert ctx.pending_option_map is None

    def test_authenticated_option_map_write_read_and_consume_end_to_end(self):
        """18. Authenticated option map write targets canonical key, and can be read and consumed by same user."""
        from backend.services.user_data_formatter import build_selection_metadata, resolve_employment_selection_context
        sess_id = f"test_auth_prop_e2e_{time.time()}"
        uid = 9001
        role = "employee"

        history = [
            {"company": "CanonicalCorp", "roles": [{"id": "1", "designation": "Engineer", "joining_date": "2020", "worked_till_date": "2022"}]},
            {"company": "CanonicalCorp", "roles": [{"id": "2", "designation": "Lead", "joining_date": "2022", "worked_till_date": "2024"}]}
        ]

        # 1. build_selection_metadata writes option mapping with user_id
        meta = build_selection_metadata(history, session_id=sess_id, user_id=uid, role=role)
        assert meta is not None
        assert len(meta["options"]) == 2

        # 2. get_conversation_context reads canonical option mapping
        ctx = get_conversation_context(sess_id, role=role, user_id=uid)
        assert ctx.pending_option_map is not None
        assert "emp_rec_1" in ctx.pending_option_map
        assert "emp_rec_2" in ctx.pending_option_map

        # 3. resolve_employment_selection_context consumes emp_rec_1 for same user
        filtered, sel_meta, target_name = resolve_employment_selection_context(
            "emp_rec_1", history, session_id=sess_id, consume_key=True, user_id=uid, role=role
        )
        assert sel_meta is None
        assert target_name == "CanonicalCorp"
        assert len(filtered) == 1

        # 4. Context now reflects consumed map and selected company context
        ctx_after = get_conversation_context(sess_id, role=role, user_id=uid)
        assert ctx_after.pending_option_map is None
        assert ctx_after.selected_employment_context == "CanonicalCorp"

    def test_two_authenticated_users_same_session_id_cannot_see_or_consume_each_other(self):
        """19. Two authenticated users sharing the same raw session ID are strictly isolated and cannot mutate each other."""
        from backend.services.user_data_formatter import build_selection_metadata, resolve_employment_selection_context
        sess_id = f"test_shared_sess_iso_{time.time()}"
        uid_a = 9002
        uid_b = 9003
        role = "employee"

        hist_a = [{"company": "AlphaWorks", "roles": [{"id": "a1", "designation": "Dev", "joining_date": "2021"}]}]
        hist_b = [{"company": "BetaWorks", "roles": [{"id": "b1", "designation": "Dev", "joining_date": "2022"}]}]

        # User A writes option map and sets pending level
        build_selection_metadata(hist_a, session_id=sess_id, user_id=uid_a, role=role)
        set_pending_requested_level(sess_id, 2, role=role, user_id=uid_a)

        # User B writes option map and sets pending level
        build_selection_metadata(hist_b, session_id=sess_id, user_id=uid_b, role=role)
        set_pending_requested_level(sess_id, 3, role=role, user_id=uid_b)

        # Context for User A sees only User A's data
        ctx_a = get_conversation_context(sess_id, role=role, user_id=uid_a)
        assert ctx_a.pending_requested_level == 2
        assert "alphaworks" in str(ctx_a.pending_option_map).lower()
        assert "betaworks" not in str(ctx_a.pending_option_map).lower()

        # Context for User B sees only User B's data
        ctx_b = get_conversation_context(sess_id, role=role, user_id=uid_b)
        assert ctx_b.pending_requested_level == 3
        assert "betaworks" in str(ctx_b.pending_option_map).lower()
        assert "alphaworks" not in str(ctx_b.pending_option_map).lower()

        # User A consumes their key:
        resolve_employment_selection_context("emp_rec_1", hist_a, session_id=sess_id, consume_key=True, user_id=uid_a, role=role)

        # User A's option map is consumed, but User B's option map is completely undisturbed
        ctx_a_after = get_conversation_context(sess_id, role=role, user_id=uid_a)
        ctx_b_after = get_conversation_context(sess_id, role=role, user_id=uid_b)
        assert ctx_a_after.pending_option_map is None
        assert ctx_a_after.selected_employment_context == "AlphaWorks"

        assert ctx_b_after.pending_option_map is not None
        assert "betaworks" in str(ctx_b_after.pending_option_map).lower()
        assert ctx_b_after.pending_requested_level == 3

    @pytest.mark.anyio
    async def test_authenticated_handle_query_propagates_user_id_end_to_end(self):
        """20. user_data_service.handle_query propagates user_id and sets canonical state correctly."""
        from backend.services import user_data_service
        from unittest.mock import patch
        sess_id = f"test_handle_query_prop_{time.time()}"
        uid = 9004

        # Calling handle_query for level 3 question across ambiguous records
        api_payload = {
            "status": True,
            "data": [
                {
                    "company": "CompOne",
                    "lists": [{"id": "1", "designation": "Role 1", "joining_date": "2020", "still_working": "1"}]
                },
                {
                    "company": "CompTwo",
                    "lists": [{"id": "2", "designation": "Role 2", "joining_date": "2022", "still_working": "1"}]
                }
            ]
        }
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=api_payload):
            uds_res = await user_data_service.handle_query(
                "How do I complete Level 3?",
                token="fake_token",
                user_id=uid,
                session_id=sess_id
            )
            assert uds_res.handled is True
            # Context for this user should have pending_requested_level 3 and pending_option_map
            ctx = get_conversation_context(sess_id, role="employee", user_id=uid)
            assert ctx.pending_requested_level == 3
            assert ctx.pending_option_map is not None
            assert len(ctx.pending_option_map) == 2

    def test_authenticated_http_chat_step5a_ordinal_disambiguation_end_to_end(self):
        """21. End-to-end HTTP /chat Step 5A ordinal disambiguation writes to canonical context and routes Turn 2 correctly."""
        from fastapi.testclient import TestClient
        from backend.main import app
        from backend.api.auth import verify_token
        from unittest.mock import patch

        client = TestClient(app)
        uid = 9005
        sess_id = f"sess_chat_5a_canonical_{time.time()}"
        app.dependency_overrides[verify_token] = lambda: {"uid": uid, "email": "auth5a@user.com", "role": "employee"}

        three_comp_payload = {
            "status": True,
            "data": [
                {
                    "id": "c1",
                    "company": "Alpha Corp",
                    "is_verified": True,
                    "lists": [{"id": "1", "designation": "Lead Engineer", "joining_date": "2020-01-01", "worked_till_date": "present", "still_working": "1"}]
                },
                {
                    "id": "c2",
                    "company": "Beta Systems",
                    "is_verified": True,
                    "lists": [{"id": "2", "designation": "Senior Architect", "joining_date": "2021-06-01", "worked_till_date": "present", "still_working": "1"}]
                }
            ]
        }

        try:
            with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=three_comp_payload):
                headers = {"Authorization": "Bearer fake_auth_tok"}

                # Turn 1: Level 3 query -> triggers disambiguation prompt
                r1 = client.post(
                    "/chat",
                    json={"messages": [{"role": "user", "content": "How do I complete Level 3?"}], "session_id": sess_id},
                    headers=headers
                )
                assert r1.status_code == 200
                data1 = r1.json()
                assert data1.get("selection_metadata") is not None

                # Canonical ConversationContext facade must show pending option map and pending level
                ctx1 = get_conversation_context(sess_id, role="employee", user_id=uid)
                assert ctx1.pending_requested_level == 3
                assert ctx1.pending_option_map is not None
                assert len(ctx1.pending_option_map) == 2

                # Turn 2: Ordinal selection "second company"
                r2 = client.post(
                    "/chat",
                    json={"messages": [{"role": "user", "content": "What about the second company?"}], "session_id": sess_id},
                    headers=headers
                )
                assert r2.status_code == 200
                data2 = r2.json()
                assert "Beta Systems" in data2.get("reply", "")
                assert data2.get("requested_level") == 3

                # Canonical context after Turn 2 has consumed option map and set selected company
                ctx2 = get_conversation_context(sess_id, role="employee", user_id=uid)
                assert ctx2.pending_option_map is None
                assert ctx2.selected_employment_context == "Beta Systems"
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.anyio
    async def test_clear_pending_requested_level_isolation_between_shared_session_users(self):
        """22. Clearing pending requested level for User A does not clear User B's pending level on shared session."""
        from backend.services import user_data_service
        from backend.services.user_data_formatter import build_selection_metadata
        from unittest.mock import patch
        sess_id = f"test_clear_lvl_iso_{time.time()}"
        uid_a = 9010
        uid_b = 9011

        # Pre-set pending requested level and pending option map for User A and User B
        set_pending_requested_level(sess_id, 3, role="employee", user_id=uid_a)
        set_pending_requested_level(sess_id, 4, role="employee", user_id=uid_b)

        # Set pending option map for User A on their canonical key
        build_selection_metadata(
            [
                {"company": "CompA", "roles": [{"id": "1", "designation": "Dev", "joining_date": "2020"}]},
                {"company": "CompB", "roles": [{"id": "2", "designation": "Lead", "joining_date": "2021"}]}
            ],
            session_id=sess_id,
            user_id=uid_a,
            role="employee"
        )

        two_comp_payload = {
            "status": True,
            "data": [
                {
                    "company": "CompA",
                    "lists": [{"id": "1", "designation": "Dev", "joining_date": "2020", "still_working": "1"}]
                },
                {
                    "company": "CompB",
                    "lists": [{"id": "2", "designation": "Lead", "joining_date": "2021", "still_working": "1"}]
                }
            ]
        }

        # User A selects "emp_rec_1", resolving the disambiguation:
        # selection_metadata is None, effective_requested_level is 3, len(results_list) == 1
        with patch("backend.services.user_data_service.fetch_endpoint_data", return_value=two_comp_payload):
            uds_res = await user_data_service.handle_query(
                "emp_rec_1",
                token="fake_token",
                user_id=uid_a,
                session_id=sess_id
            )
            assert uds_res.handled is True

        # User A's pending level should now be cleared
        ctx_a = get_conversation_context(sess_id, role="employee", user_id=uid_a)
        assert ctx_a.pending_requested_level is None

        # User B's pending level MUST remain undisturbed at 4
        ctx_b = get_conversation_context(sess_id, role="employee", user_id=uid_b)
        assert ctx_b.pending_requested_level == 4

    def test_concurrent_writer_l1_consistency_on_cas_conflict(self):
        """23. Overlapping writers to the same session properly remerge or reject on CAS conflict in Redis."""
        import threading
        from backend.utils.session_state import save_session_state_cas, get_session_state

        sess_id = f"test_concurrent_writers_{time.time()}"
        role = "employee"
        uid = 9901

        # Setup initial state in Redis
        initial_state = {
            "search_context": {"keyword": "Initial Search"},
            "version_seq": 1,
            "_last_active": time.time(),
        }
        save_session_state_cas(sess_id, initial_state, role=role, user_id=uid)

        barrier = threading.Barrier(2)
        results = {}

        def remerge_writer_1(base_state):
            res = dict(base_state)
            sc = dict(res.get("search_context") or {})
            sc["writer1"] = "done"
            res["search_context"] = sc
            return res

        def remerge_writer_2(base_state):
            res = dict(base_state)
            sc = dict(res.get("search_context") or {})
            sc["writer2"] = "done"
            res["search_context"] = sc
            return res

        def worker_1():
            barrier.wait()
            # Writer 1 proposes updating search_context with writer1
            base = get_session_state(sess_id, role=role, user_id=uid)
            req_state = remerge_writer_1(base)
            ok = save_session_state_cas(sess_id, req_state, role=role, user_id=uid, remerge_fn=remerge_writer_1, max_retries=3)
            results["writer_1"] = ok

        def worker_2():
            barrier.wait()
            # Writer 2 proposes updating search_context with writer2
            base = get_session_state(sess_id, role=role, user_id=uid)
            req_state = remerge_writer_2(base)
            ok = save_session_state_cas(sess_id, req_state, role=role, user_id=uid, remerge_fn=remerge_writer_2, max_retries=3)
            results["writer_2"] = ok

        t1 = threading.Thread(target=worker_1)
        t2 = threading.Thread(target=worker_2)

        t1.start()
        t2.start()

        t1.join(timeout=5)
        t2.join(timeout=5)

        assert results.get("writer_1") is True
        assert results.get("writer_2") is True

        final = get_session_state(sess_id, role=role, user_id=uid)

        # Both increments must be reflected (initial save to v2, writer 1 to v3, writer 2 to v4)
        assert final.get("version_seq") == 4

        # Assert that neither writer's changes were silently lost
        sc = final.get("search_context", {})
        assert sc.get("writer1") == "done"
        assert sc.get("writer2") == "done"

    def test_stale_writer_rejected_and_does_not_corrupt_l1(self):
        """24. Stale writer whose CAS fails cannot corrupt or regress Redis session state."""
        import threading
        from backend.utils.session_state import save_session_state_cas, get_session_state

        sess_id = f"test_stale_writer_{time.time()}"
        role = "employee"
        uid = 9902

        authoritative_state = {
            "search_context": {"keyword": "Authoritative V5"},
            "version_seq": 5,
            "_last_active": time.time(),
        }
        save_session_state_cas(sess_id, authoritative_state, role=role, user_id=uid)

        # Stale writer attempts to write stale data with an older version and no remerge_fn
        stale_payload = {"search_context": {"keyword": "Stale Snapshot"}, "version_seq": 2}
        success = save_session_state_cas(sess_id, stale_payload, role=role, user_id=uid, max_retries=2)
        assert success is False

        # Verify Redis state was NOT overwritten with stale data (preserved at version 6 from initial save)
        current = get_session_state(sess_id, role=role, user_id=uid)
        assert current["search_context"]["keyword"] == "Authoritative V5"
        assert current["version_seq"] == 6

    def test_concurrent_writes_isolated_sessions(self):
        """25. Concurrent writers to different sessions execute independently without cross-session lock blocking."""
        import threading
        from backend.utils.session_state import save_session_state_cas, get_session_state

        sess_id1 = f"test_iso_sess1_{time.time()}"
        sess_id2 = f"test_iso_sess2_{time.time()}"
        uid1 = 9903
        uid2 = 9904

        save_session_state_cas(sess_id1, {"search_context": {"session": 1}, "version_seq": 1}, role="employee", user_id=uid1)
        save_session_state_cas(sess_id2, {"search_context": {"session": 2}, "version_seq": 1}, role="employee", user_id=uid2)

        t1 = threading.Thread(target=lambda: save_session_state_cas(sess_id1, {"search_context": {"updated": 1}}, role="employee", user_id=uid1))
        t2 = threading.Thread(target=lambda: save_session_state_cas(sess_id2, {"search_context": {"updated": 2}}, role="employee", user_id=uid2))

        t1.start()
        t2.start()

        t1.join(timeout=5)
        t2.join(timeout=5)

        assert get_session_state(sess_id1, role="employee", user_id=uid1)["search_context"] == {"updated": 1}
        assert get_session_state(sess_id2, role="employee", user_id=uid2)["search_context"] == {"updated": 2}






