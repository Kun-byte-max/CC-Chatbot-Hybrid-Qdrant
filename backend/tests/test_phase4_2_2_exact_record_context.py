import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from backend.main import app
from backend.api.auth import verify_token
from backend.services.user_data_formatter import (
    _classify_employment_records,
    resolve_employment_selection_context,
    _generate_record_composite_key,
    build_selection_metadata
)
from backend.utils.session_state import (
    get_selected_employment_context,
    get_selected_employment_record_key,
    set_selected_employment_context,
    clear_selected_employment_context
)


class TestPhase422ExactRecordContext:

    @pytest.fixture
    def duplicate_google_data(self):
        """Synthetic employment data with two Google tenures and one Microsoft tenure."""
        history = [
            {
                "id": "g1",
                "company": "Google LLC",
                "is_verified": True,
                "lists": [{"designation": "Software Engineer", "joining_date": "2021-01-01", "worked_till_date": "2022-12-31", "still_working": "0", "salary": "1800000", "skill": [{"id": "s1", "name": "Python"}]}],
                "roles": [{"designation": "Software Engineer", "joining_date": "2021-01-01", "worked_till_date": "2022-12-31", "still_working": "0"}],
                "salary_details": [{"designation": "Software Engineer", "annual_ctc": "1800000"}],
                "skills": [{"name": "Python", "company": "Google LLC", "designation": "Software Engineer"}]
            },
            {
                "id": "g2",
                "company": "Google LLC",
                "is_verified": True,
                "lists": [{"designation": "Staff Engineer", "joining_date": "2023-01-01", "worked_till_date": "2024-12-31", "still_working": "0", "salary": "3500000", "skill": [{"id": "s2", "name": "Distributed Systems"}]}],
                "roles": [{"designation": "Staff Engineer", "joining_date": "2023-01-01", "worked_till_date": "2024-12-31", "still_working": "0"}],
                "salary_details": [{"designation": "Staff Engineer", "annual_ctc": "3500000"}],
                "skills": [{"name": "Distributed Systems", "company": "Google LLC", "designation": "Staff Engineer"}]
            },
            {
                "id": "ms1",
                "company": "Microsoft",
                "is_verified": True,
                "lists": [{"designation": "Backend Engineer", "joining_date": "2020-01-01", "worked_till_date": "present", "still_working": "1", "salary": "2500000", "skill": [{"id": "s3", "name": "C#"}]}],
                "roles": [{"designation": "Backend Engineer", "joining_date": "2020-01-01", "worked_till_date": "present", "still_working": "1"}],
                "salary_details": [{"designation": "Backend Engineer", "annual_ctc": "2500000"}],
                "skills": [{"name": "C#", "company": "Microsoft", "designation": "Backend Engineer"}]
            }
        ]
        return {
            "status": True,
            "data": history,
            "employment_history": history,
            "employement_history_new": history
        }

    # -------------------------------------------------------------------------
    # 1. Classification Tests
    # -------------------------------------------------------------------------
    def test_classification_explicit_current_and_previous(self):
        history = [
            {"company": "CompA", "roles": [{"still_working": "1"}]},
            {"company": "CompB", "roles": [{"still_working": "0", "worked_till_date": "2022-01-01"}]}
        ]
        curr, prev, unk = _classify_employment_records(history)
        assert len(curr) == 1
        assert len(prev) == 1
        assert len(unk) == 0

    def test_classification_missing_and_null_status(self):
        history = [
            {"company": "CompIncomplete", "roles": [{"designation": "Dev"}]}
        ]
        curr, prev, unk = _classify_employment_records(history)
        assert len(curr) == 0
        assert len(prev) == 0
        assert len(unk) == 1

    def test_classification_valid_past_and_future_end_dates(self):
        history = [
            {"company": "PastCorp", "roles": [{"worked_till_date": "2020-05-10"}]},
            {"company": "FutureCorp", "roles": [{"worked_till_date": "2030-05-10"}]}
        ]
        curr, prev, unk = _classify_employment_records(history)
        assert len(prev) == 1
        assert prev[0]["company"] == "PastCorp"
        assert len(unk) == 1
        assert unk[0]["company"] == "FutureCorp"

    def test_only_unknown_history_produces_clarification(self):
        history = [{"company": "UnknownCo", "roles": [{}]}]
        filtered, meta, matched = resolve_employment_selection_context("Show my previous employment", history)
        assert matched == "NO_PREVIOUS_RECORDS"
        assert meta is None
        assert filtered == []

    # -------------------------------------------------------------------------
    # 2. Exact-Record Context Tests & Session-Bound Option Key Mapping
    # -------------------------------------------------------------------------
    def test_two_google_tenures_first_selected_preserves_context(self, duplicate_google_data):
        session_id = "sess_g1_test"
        history = duplicate_google_data["employment_history"]

        # Turn 1: Generate selection metadata to build and store pending_option_mapping
        meta = build_selection_metadata(history, session_id=session_id)
        assert meta["options"][0]["selection_key"] == "emp_rec_1"

        # Turn 2: Select emp_rec_1 (Software Engineer 2021-2022)
        filtered, meta_sel, matched = resolve_employment_selection_context("emp_rec_1", history, session_id=session_id)
        assert len(filtered) == 1
        assert filtered[0]["id"] == "g1"
        assert get_selected_employment_context(session_id) == "Google LLC"
        assert get_selected_employment_record_key(session_id) == _generate_record_composite_key(filtered[0])

        # Follow-up query without explicit company
        filtered_fu, _, _ = resolve_employment_selection_context("What about Level 3?", history, session_id=session_id)
        assert len(filtered_fu) == 1
        assert filtered_fu[0]["id"] == "g1"

    def test_explicit_switch_to_second_google_tenure(self, duplicate_google_data):
        session_id = "sess_g2_test"
        history = duplicate_google_data["employment_history"]

        # Turn 1: Generate selection metadata and select emp_rec_1
        build_selection_metadata(history, session_id=session_id)
        resolve_employment_selection_context("emp_rec_1", history, session_id=session_id)

        # Turn 2: User asks to see options / switch, re-generating selection metadata for emp_rec_2
        build_selection_metadata(history, session_id=session_id)
        filtered, meta, matched = resolve_employment_selection_context("emp_rec_2", history, session_id=session_id)
        assert len(filtered) == 1
        assert filtered[0]["id"] == "g2"
        assert get_selected_employment_record_key(session_id) == _generate_record_composite_key(filtered[0])

        # Subsequent follow-up
        filtered_fu, _, _ = resolve_employment_selection_context("Show my salary", history, session_id=session_id)
        assert len(filtered_fu) == 1
        assert filtered_fu[0]["id"] == "g2"

    def test_api_record_order_changes_preserves_originally_displayed_tenure(self, duplicate_google_data):
        """
        Phase 4.2.2.2 Core Test:
        Turn 1 options rendered in order [g1, g2, ms1]. Key emp_rec_1 maps to g1.
        Turn 2 fresh GET returns re-ordered array [g2, g1, ms1].
        Submitting emp_rec_1 MUST select g1, not g2!
        """
        session_id = "sess_reorder_option_test"
        original_history = duplicate_google_data["employment_history"]

        # Turn 1: Build options for [g1, g2, ms1]. emp_rec_1 -> g1 composite key
        meta1 = build_selection_metadata(original_history, session_id=session_id)
        assert meta1["options"][0]["company_name"] == "Google LLC"

        # Fresh GET returns re-ordered array: [g2, g1, ms1]
        reordered_history = [original_history[1], original_history[0], original_history[2]]

        # Turn 2: User submits emp_rec_1 against re-ordered history
        filtered, meta2, matched = resolve_employment_selection_context("emp_rec_1", reordered_history, session_id=session_id)

        # Must select record g1 (Software Engineer), NOT re-ordered 0th element g2 (Staff Engineer)
        assert len(filtered) == 1
        assert filtered[0]["id"] == "g1"
        assert filtered[0]["roles"][0]["designation"] == "Software Engineer"

    def test_submit_unknown_selection_key(self, duplicate_google_data):
        session_id = "sess_unknown_key"
        history = duplicate_google_data["employment_history"]

        build_selection_metadata(history, session_id=session_id)
        filtered, meta, matched = resolve_employment_selection_context("emp_rec_99", history, session_id=session_id)
        assert filtered == []
        assert meta is not None
        assert "no longer valid" in meta["prompt"] or "choose" in meta["prompt"]

    def test_submit_key_from_replaced_previous_option_list(self, duplicate_google_data):
        session_id = "sess_replaced_list"
        history = duplicate_google_data["employment_history"]

        # List 1: 3 options (emp_rec_1, emp_rec_2, emp_rec_3)
        build_selection_metadata(history, session_id=session_id)

        # New option list generated with only Microsoft (1 option: emp_rec_1 -> ms1)
        single_ms_history = [history[2]]
        build_selection_metadata(single_ms_history, session_id=session_id)

        # Submitting emp_rec_3 from previous list should be rejected as invalid/expired
        filtered, meta, matched = resolve_employment_selection_context("emp_rec_3", single_ms_history, session_id=session_id)
        assert filtered == []
        assert meta is not None

    def test_submit_key_from_another_session(self, duplicate_google_data):
        sess_a = "session_A"
        sess_b = "session_B"
        history = duplicate_google_data["employment_history"]

        # Session A gets options generated
        build_selection_metadata(history, session_id=sess_a)

        # Session B attempts to submit emp_rec_1 without prior option mapping in Session B
        filtered, meta, matched = resolve_employment_selection_context("emp_rec_1", history, session_id=sess_b)
        assert filtered == []
        assert meta is not None

    def test_submit_key_whose_composite_identity_no_longer_exists(self, duplicate_google_data):
        session_id = "sess_disappeared_composite"
        history = duplicate_google_data["employment_history"]

        # Session gets options [g1, g2, ms1]
        build_selection_metadata(history, session_id=session_id)

        # Fresh GET returns history where g1 has been deleted from backend platform
        history_without_g1 = [history[1], history[2]]

        # Submitting emp_rec_1 (which mapped to g1) against history_without_g1
        filtered, meta, matched = resolve_employment_selection_context("emp_rec_1", history_without_g1, session_id=session_id)
        assert filtered == []
        assert meta is not None
        assert "could not be verified" in meta["prompt"]

    def test_selected_record_disappears_from_fresh_get(self, duplicate_google_data):
        session_id = "sess_disappear_test"
        history = duplicate_google_data["employment_history"]

        build_selection_metadata(history, session_id=session_id)
        resolve_employment_selection_context("emp_rec_1", history, session_id=session_id)

        # Fresh GET returns history without g1
        new_history = [history[2]] # Only Microsoft remains
        filtered, meta, matched = resolve_employment_selection_context("Show my verification", new_history, session_id=session_id)
        assert all(r["id"] != "g1" for r in filtered)

    def test_two_different_sessions_no_context_leakage(self, duplicate_google_data):
        sess1 = "sess_user_1"
        sess2 = "sess_user_2"
        history = duplicate_google_data["employment_history"]

        build_selection_metadata(history, session_id=sess1)
        build_selection_metadata(history, session_id=sess2)

        resolve_employment_selection_context("emp_rec_1", history, session_id=sess1)
        resolve_employment_selection_context("emp_rec_2", history, session_id=sess2)

        f1, _, _ = resolve_employment_selection_context("verification status", history, session_id=sess1)
        f2, _, _ = resolve_employment_selection_context("verification status", history, session_id=sess2)

        assert f1[0]["id"] == "g1"
        assert f2[0]["id"] == "g2"

    # -------------------------------------------------------------------------
    # 3. Phase 4.3.1 TTL and Cleanup Unit Tests
    # -------------------------------------------------------------------------
    def test_option_mapping_expires_after_ttl(self, duplicate_google_data):
        session_id = "sess_ttl_expiry_test"
        history = duplicate_google_data["employment_history"]

        # Build options metadata
        build_selection_metadata(history, session_id=session_id)

        # Fast-forward mock time past 15 minutes (901 seconds)
        import time
        with patch("time.time", return_value=time.time() + 905):
            filtered, meta, matched = resolve_employment_selection_context("emp_rec_1", history, session_id=session_id)
            assert filtered == []
            assert meta is not None
            assert "no longer valid" in meta["prompt"] or "choose" in meta["prompt"]

    def test_single_use_option_key_replay_rejected(self, duplicate_google_data):
        session_id = "sess_single_use_replay_test"
        history = duplicate_google_data["employment_history"]

        build_selection_metadata(history, session_id=session_id)

        # First resolution consumes the key
        filtered1, _, _ = resolve_employment_selection_context("emp_rec_1", history, session_id=session_id)
        assert len(filtered1) == 1
        assert filtered1[0]["id"] == "g1"

        # Immediate replay of the same key must be rejected (key consumed)
        filtered2, meta2, _ = resolve_employment_selection_context("emp_rec_1", history, session_id=session_id)
        assert filtered2 == []
        assert meta2 is not None

    def test_session_inactivity_evicts_ram_state(self, duplicate_google_data):
        session_id = "sess_inactivity_eviction_test"
        history = duplicate_google_data["employment_history"]

        build_selection_metadata(history, session_id=session_id)
        resolve_employment_selection_context("emp_rec_1", history, session_id=session_id)

        # Confirm selected record context stored
        assert get_selected_employment_context(session_id) == "Google LLC"

        # Fast-forward time past 4 hours (14401 seconds)
        import time
        with patch("time.time", return_value=time.time() + 14405):
            # Attempting to fetch selected context after inactivity TTL returns None (evicted)
            assert get_selected_employment_context(session_id) is None

    def test_touch_session_state_refreshes_activity(self):
        from backend.utils.session_state import touch_session_state, get_session_state, _SESSION_STATES
        session_id = "sess_touch_test"

        get_session_state(session_id)
        assert session_id in _SESSION_STATES

        import time
        t0 = _SESSION_STATES[session_id]["_last_active"]
        with patch("time.time", return_value=t0 + 100):
            touched = touch_session_state(session_id)
            assert touched is True
            assert _SESSION_STATES[session_id]["_last_active"] == t0 + 100

    def test_cleanup_stale_sessions_sweeps_abandoned_entries(self):
        from backend.utils.session_state import get_session_state, cleanup_stale_sessions, _SESSION_STATES
        s1 = "sess_sweep_active"
        s2 = "sess_sweep_stale"

        get_session_state(s1)
        get_session_state(s2)

        import time
        t0 = time.time()
        _SESSION_STATES[s2]["_last_active"] = t0 - 15000  # Older than 14400s

        purged = cleanup_stale_sessions(force=True)
        assert purged >= 1
        assert s2 not in _SESSION_STATES
        assert s1 in _SESSION_STATES

    # -------------------------------------------------------------------------
    # 3. HTTP /chat Multi-Turn Pipeline Test
    # -------------------------------------------------------------------------
    @patch("backend.services.user_data_service.fetch_endpoint_data")
    def test_http_chat_multiturn_pipeline(self, mock_fetch_api, duplicate_google_data):
        client = TestClient(app)
        app.dependency_overrides[verify_token] = lambda: {"uid": 1001, "email": "test@user.com", "role": "employee"}

        try:
            mock_fetch_api.return_value = {
                "status": True,
                "message": "Employment Data",
                "data": duplicate_google_data["employment_history"]
            }
            headers = {"X-User-Id": "1001", "Authorization": "Bearer fake_token_123"}
            session_id = "sess_http_multiturn_1001"

            # Turn 1: User asks for previous employment -> receives selection prompt
            resp1 = client.post("/chat", json={"messages": [{"role": "user", "content": "Show my previous employment verification status"}], "session_id": session_id}, headers=headers)
            assert resp1.status_code == 200
            data1 = resp1.json()
            assert data1.get("selection_metadata") is not None
            options = data1["selection_metadata"]["options"]
            assert len(options) == 2

            # Turn 2: User submits option key emp_rec_1 (Google Software Engineer)
            resp2 = client.post("/chat", json={"messages": [{"role": "user", "content": "emp_rec_1"}], "session_id": session_id}, headers=headers)
            assert resp2.status_code == 200

            # Turn 3: Follow-up question for Level 3 verification on selected Software Engineer tenure
            resp3 = client.post("/chat", json={"messages": [{"role": "user", "content": "What about my Level 3 verification?"}], "session_id": session_id}, headers=headers)
            assert resp3.status_code == 200
            data3 = resp3.json()
            reply3 = data3.get("reply") or data3.get("response") or ""
            assert "Google LLC" in reply3
            assert "Software Engineer" in reply3

            # Turn 4: Salary follow-up
            resp4 = client.post("/chat", json={"messages": [{"role": "user", "content": "What was my salary?"}], "session_id": session_id}, headers=headers)
            assert resp4.status_code == 200
            data4 = resp4.json()
            reply4 = data4.get("reply") or data4.get("response") or ""
            assert "1,800,000" in reply4 or "1800000" in reply4 or "Google" in reply4

            # Turn 5: Skills follow-up
            resp5 = client.post("/chat", json={"messages": [{"role": "user", "content": "What skills did I use?"}], "session_id": session_id}, headers=headers)
            assert resp5.status_code == 200
            data5 = resp5.json()
            reply5 = data5.get("reply") or data5.get("response") or ""
            assert len(reply5) > 0
        finally:
            app.dependency_overrides.clear()


class TestPhase5AOrdinalDisambiguation:
    """Phase 1 Step 5A: Focused regression tests for employment-history option mapping disambiguation."""

    @pytest.fixture
    def three_company_data(self):
        """Synthetic employment data with three distinct companies."""
        history = [
            {
                "id": "c1",
                "company": "Alpha Corp",
                "is_verified": True,
                "lists": [{"designation": "Lead Engineer", "joining_date": "2022-01-01", "worked_till_date": "2023-12-31", "still_working": "0", "salary": "2000000"}],
                "roles": [{"designation": "Lead Engineer", "joining_date": "2022-01-01", "worked_till_date": "2023-12-31", "still_working": "0"}],
                "salary_details": [{"designation": "Lead Engineer", "annual_ctc": "2000000"}],
            },
            {
                "id": "c2",
                "company": "Beta Systems",
                "is_verified": True,
                "lists": [{"designation": "Senior Developer", "joining_date": "2020-01-01", "worked_till_date": "2021-12-31", "still_working": "0", "salary": "1500000"}],
                "roles": [{"designation": "Senior Developer", "joining_date": "2020-01-01", "worked_till_date": "2021-12-31", "still_working": "0"}],
                "salary_details": [{"designation": "Senior Developer", "annual_ctc": "1500000"}],
            },
            {
                "id": "c3",
                "company": "Gamma Labs",
                "is_verified": True,
                "lists": [{"designation": "Research Scientist", "joining_date": "2018-01-01", "worked_till_date": "2019-12-31", "still_working": "0", "salary": "1200000"}],
                "roles": [{"designation": "Research Scientist", "joining_date": "2018-01-01", "worked_till_date": "2019-12-31", "still_working": "0"}],
                "salary_details": [{"designation": "Research Scientist", "annual_ctc": "1200000"}],
            }
        ]
        return history

    @patch("backend.services.user_data_service.fetch_endpoint_data")
    def test_numeric_selection_continues_to_resolve(self, mock_fetch_api, three_company_data):
        """1. '2' continues to resolve to option '2' (Beta Systems)."""
        client = TestClient(app)
        app.dependency_overrides[verify_token] = lambda: {"uid": 2001, "email": "test@user.com", "role": "employee"}
        mock_fetch_api.return_value = {"status": True, "data": three_company_data}
        headers = {"X-User-Id": "2001", "Authorization": "Bearer fake_token"}
        session_id = "sess_num_opt_2"

        try:
            # Turn 1: Trigger selection prompt
            r1 = client.post("/chat", json={"messages": [{"role": "user", "content": "Show my previous employment verification status"}], "session_id": session_id}, headers=headers)
            assert r1.status_code == 200
            assert r1.json().get("selection_metadata") is not None
            opts = r1.json()["selection_metadata"]["options"]
            assert len(opts) == 3
            assert opts[1]["company_name"] == "Beta Systems"

            # Turn 2: User sends "2"
            r2 = client.post("/chat", json={"messages": [{"role": "user", "content": "2"}], "session_id": session_id}, headers=headers)
            assert r2.status_code == 200
            reply2 = r2.json().get("reply", "")
            assert "Beta Systems" in reply2
            assert "Alpha Corp" not in reply2
        finally:
            app.dependency_overrides.clear()

    @patch("backend.services.user_data_service.fetch_endpoint_data")
    def test_what_about_the_second_company_resolves(self, mock_fetch_api, three_company_data):
        """2. 'What about the second company?' resolves to option '2' (Beta Systems)."""
        client = TestClient(app)
        app.dependency_overrides[verify_token] = lambda: {"uid": 2002, "email": "test@user.com", "role": "employee"}
        mock_fetch_api.return_value = {"status": True, "data": three_company_data}
        headers = {"X-User-Id": "2002", "Authorization": "Bearer fake_token"}
        session_id = "sess_ord_second"

        try:
            # Turn 1: Trigger selection prompt
            r1 = client.post("/chat", json={"messages": [{"role": "user", "content": "Show my previous employment verification status"}], "session_id": session_id}, headers=headers)
            assert r1.status_code == 200
            assert r1.json().get("selection_metadata") is not None

            # Turn 2: "What about the second company?"
            r2 = client.post("/chat", json={"messages": [{"role": "user", "content": "What about the second company?"}], "session_id": session_id}, headers=headers)
            assert r2.status_code == 200
            reply2 = r2.json().get("reply", "")
            assert "Beta Systems" in reply2
            assert "Alpha Corp" not in reply2
        finally:
            app.dependency_overrides.clear()

    @patch("backend.services.user_data_service.fetch_endpoint_data")
    def test_tell_me_about_the_first_one_resolves(self, mock_fetch_api, three_company_data):
        """3. 'Tell me about the first one' resolves to option '1' (Alpha Corp)."""
        client = TestClient(app)
        app.dependency_overrides[verify_token] = lambda: {"uid": 2003, "email": "test@user.com", "role": "employee"}
        mock_fetch_api.return_value = {"status": True, "data": three_company_data}
        headers = {"X-User-Id": "2003", "Authorization": "Bearer fake_token"}
        session_id = "sess_ord_first"

        try:
            r1 = client.post("/chat", json={"messages": [{"role": "user", "content": "Show my previous employment verification status"}], "session_id": session_id}, headers=headers)
            assert r1.status_code == 200
            assert r1.json().get("selection_metadata") is not None

            # Turn 2: "Tell me about the first one"
            r2 = client.post("/chat", json={"messages": [{"role": "user", "content": "Tell me about the first one"}], "session_id": session_id}, headers=headers)
            assert r2.status_code == 200
            reply2 = r2.json().get("reply", "")
            assert "Alpha Corp" in reply2
        finally:
            app.dependency_overrides.clear()

    @patch("backend.services.user_data_service.fetch_endpoint_data")
    def test_what_about_the_third_company_resolves(self, mock_fetch_api, three_company_data):
        """4. 'What about the third company?' resolves to option '3' (Gamma Labs) when option exists."""
        client = TestClient(app)
        app.dependency_overrides[verify_token] = lambda: {"uid": 2004, "email": "test@user.com", "role": "employee"}
        mock_fetch_api.return_value = {"status": True, "data": three_company_data}
        headers = {"X-User-Id": "2004", "Authorization": "Bearer fake_token"}
        session_id = "sess_ord_third"

        try:
            r1 = client.post("/chat", json={"messages": [{"role": "user", "content": "Show my previous employment verification status"}], "session_id": session_id}, headers=headers)
            assert r1.status_code == 200
            assert r1.json().get("selection_metadata") is not None

            # Turn 2: "What about the third company?"
            r2 = client.post("/chat", json={"messages": [{"role": "user", "content": "What about the third company?"}], "session_id": session_id}, headers=headers)
            assert r2.status_code == 200
            reply2 = r2.json().get("reply", "")
            assert "Gamma Labs" in reply2
        finally:
            app.dependency_overrides.clear()

    @patch("backend.services.user_data_service.fetch_endpoint_data")
    def test_nonexistent_ordinal_does_not_select_company(self, mock_fetch_api, three_company_data):
        """5. An ordinal referring to a nonexistent option ('fourth') does not select another company or fallback to option '1'."""
        client = TestClient(app)
        app.dependency_overrides[verify_token] = lambda: {"uid": 2005, "email": "test@user.com", "role": "employee"}
        mock_fetch_api.return_value = {"status": True, "data": three_company_data}
        headers = {"X-User-Id": "2005", "Authorization": "Bearer fake_token"}
        session_id = "sess_ord_nonexistent"

        try:
            r1 = client.post("/chat", json={"messages": [{"role": "user", "content": "Show my previous employment verification status"}], "session_id": session_id}, headers=headers)
            assert r1.status_code == 200
            assert r1.json().get("selection_metadata") is not None

            # Turn 2: "What about the fourth company?" (only 3 options exist)
            r2 = client.post("/chat", json={"messages": [{"role": "user", "content": "What about the fourth company?"}], "session_id": session_id}, headers=headers)
            assert r2.status_code == 200
            reply2 = r2.json().get("reply", "")
            # Must NOT select Alpha Corp (option 1) as a silent fallback
            from backend.utils.session_state import get_selected_employment_context
            selected = get_selected_employment_context(session_id)
            assert selected is None
        finally:
            app.dependency_overrides.clear()

    @patch("backend.services.user_data_service.fetch_endpoint_data")
    def test_unrelated_new_query_does_not_inherit_company_selection(self, mock_fetch_api, three_company_data):
        """6. An unrelated new query does not inherit a company selection merely because an opt_map exists."""
        client = TestClient(app)
        app.dependency_overrides[verify_token] = lambda: {"uid": 2006, "email": "test@user.com", "role": "employee"}
        mock_fetch_api.return_value = {"status": True, "data": three_company_data}
        headers = {"X-User-Id": "2006", "Authorization": "Bearer fake_token"}
        session_id = "sess_ord_unrelated"

        try:
            r1 = client.post("/chat", json={"messages": [{"role": "user", "content": "Show my previous employment verification status"}], "session_id": session_id}, headers=headers)
            assert r1.status_code == 200
            assert r1.json().get("selection_metadata") is not None

            # Turn 2: Unrelated query: "What is CollarCheck?"
            r2 = client.post("/chat", json={"messages": [{"role": "user", "content": "What is CollarCheck?"}], "session_id": session_id}, headers=headers)
            assert r2.status_code == 200
            reply2 = r2.json().get("reply", "")
            # Should NOT return verification results for any of the companies
            assert "Verification Level" not in reply2
            from backend.utils.session_state import get_selected_employment_context
            assert get_selected_employment_context(session_id) is None
        finally:
            app.dependency_overrides.clear()

    @patch("backend.services.user_data_service.fetch_endpoint_data")
    def test_company_name_matching_remains_unchanged(self, mock_fetch_api, three_company_data):
        """7. Existing company-name matching remains unchanged."""
        client = TestClient(app)
        app.dependency_overrides[verify_token] = lambda: {"uid": 2007, "email": "test@user.com", "role": "employee"}
        mock_fetch_api.return_value = {"status": True, "data": three_company_data}
        headers = {"X-User-Id": "2007", "Authorization": "Bearer fake_token"}
        session_id = "sess_comp_name_match"

        try:
            r1 = client.post("/chat", json={"messages": [{"role": "user", "content": "Show my previous employment verification status"}], "session_id": session_id}, headers=headers)
            assert r1.status_code == 200
            assert r1.json().get("selection_metadata") is not None

            # Turn 2: User mentions company name directly
            r2 = client.post("/chat", json={"messages": [{"role": "user", "content": "Tell me about Alpha Corp"}], "session_id": session_id}, headers=headers)
            assert r2.status_code == 200
            reply2 = r2.json().get("reply", "")
            assert "Alpha Corp" in reply2
        finally:
            app.dependency_overrides.clear()

    @patch("backend.services.user_data_service.fetch_endpoint_data")
    def test_explicit_record_key_resolves(self, mock_fetch_api, three_company_data):
        """8. Explicit record key 'emp_rec_2' resolves to Beta Systems."""
        client = TestClient(app)
        app.dependency_overrides[verify_token] = lambda: {"uid": 2008, "email": "test@user.com", "role": "employee"}
        mock_fetch_api.return_value = {"status": True, "data": three_company_data}
        headers = {"X-User-Id": "2008", "Authorization": "Bearer fake_token"}
        session_id = "sess_rec_key_2"

        try:
            r1 = client.post("/chat", json={"messages": [{"role": "user", "content": "Show my previous employment verification status"}], "session_id": session_id}, headers=headers)
            assert r1.status_code == 200
            assert r1.json().get("selection_metadata") is not None

            # Turn 2: Explicit record key "emp_rec_2"
            r2 = client.post("/chat", json={"messages": [{"role": "user", "content": "emp_rec_2"}], "session_id": session_id}, headers=headers)
            assert r2.status_code == 200
            reply2 = r2.json().get("reply", "")
            assert "Beta Systems" in reply2
        finally:
            app.dependency_overrides.clear()

    @patch("backend.services.user_data_service.fetch_endpoint_data")
    def test_job_search_not_intercepted_by_stale_pending_state(self, mock_fetch_api, three_company_data):
        """9. 'Search Python developer jobs in Delhi' is not intercepted by stale pending selection state or pending_lvl."""
        client = TestClient(app)
        app.dependency_overrides[verify_token] = lambda: {"uid": 2009, "email": "test@user.com", "role": "employee"}
        mock_fetch_api.return_value = {"status": True, "data": three_company_data}
        headers = {"X-User-Id": "2009", "Authorization": "Bearer fake_token"}
        session_id = "sess_job_search_stale"

        try:
            from backend.utils.session_state import set_pending_requested_level, set_session_option_mapping
            # Simulate stale pending state and option mapping
            set_session_option_mapping(session_id, {"emp_rec_1": "Alpha Corp::1", "emp_rec_2": "Beta Systems::2"})
            set_pending_requested_level(session_id, 3)

            # User sends job search query
            r = client.post("/chat", json={"messages": [{"role": "user", "content": "Search Python developer jobs in Delhi"}], "session_id": session_id}, headers=headers)
            assert r.status_code == 200
            data = r.json()
            # Must NOT be intercepted as employment verification
            assert "employment verification" not in data.get("reply", "").lower()
            from backend.utils.session_state import get_selected_employment_context
            assert get_selected_employment_context(session_id) is None
        finally:
            from backend.utils.session_state import clear_pending_requested_level
            clear_pending_requested_level(session_id)
            app.dependency_overrides.clear()

    @patch("backend.services.user_data_service.fetch_endpoint_data")
    def test_legitimate_pending_disambiguation_flow_with_pending_level(self, mock_fetch_api, three_company_data):
        """10. Legitimate pending disambiguation flow still works with the project's actual pending_lvl behavior."""
        client = TestClient(app)
        app.dependency_overrides[verify_token] = lambda: {"uid": 2010, "email": "test@user.com", "role": "employee"}
        mock_fetch_api.return_value = {"status": True, "data": three_company_data}
        headers = {"X-User-Id": "2010", "Authorization": "Bearer fake_token"}
        session_id = "sess_legit_pending_lvl"

        try:
            # Turn 1: User asks Level 3 question across ambiguous employment records
            r1 = client.post("/chat", json={"messages": [{"role": "user", "content": "How do I complete Level 3?"}], "session_id": session_id}, headers=headers)
            assert r1.status_code == 200
            assert r1.json().get("selection_metadata") is not None
            from backend.utils.session_state import get_pending_requested_level
            assert get_pending_requested_level(session_id, role="employee", user_id=2010) == 3

            # Turn 2: User responds with ordinal "second company"
            r2 = client.post("/chat", json={"messages": [{"role": "user", "content": "What about the second company?"}], "session_id": session_id}, headers=headers)
            assert r2.status_code == 200
            reply2 = r2.json().get("reply", "")
            assert "Beta Systems" in reply2
            assert r2.json().get("requested_level") == 3
        finally:
            from backend.utils.session_state import clear_pending_requested_level
            clear_pending_requested_level(session_id, role="employee", user_id=2010)
            app.dependency_overrides.clear()
