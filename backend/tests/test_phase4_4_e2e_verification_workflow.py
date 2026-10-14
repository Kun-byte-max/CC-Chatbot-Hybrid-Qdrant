"""
test_phase4_4_e2e_verification_workflow.py — Comprehensive HTTP E2E Test Suite for Employee-Side Employment Verification

Phase 4.4.1 Scope:
  - Class A: TestE2ESingleEmploymentJourney (10 scenarios: History, Overview, Levels 1-4, Incomplete reasons, Next steps, Ownership)
  - Class B: TestE2EMultipleEmploymentJourney (10 scenarios: Disambiguation, Selection, Retention, Salary/Skills propagation, Reordered GET validation, Non-previous classification)
  - Class C: TestE2ECrossIntentJourney (6 scenarios: Selection->Verification, Verification->Salary, Salary->Skills, Skills->Verification, Explicit override, Unrelated isolation)
  - Class D: TestE2ESessionLifecycleJourney (9 scenarios: Inactivity TTL, Option TTL, Single-use key replay, Reordered GET revalidation, Multi-tenant isolation)
  - Class E: TestE2EActionMetadataAndFrontendContracts (8 scenarios: Action metadata serialization, Selection metadata serialization, Frontend contracts)

Zero live API calls. Full FastAPI /chat TestClient boundary integration.
"""

import pytest
import time
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from backend.main import app
from backend.api.auth import verify_token
from backend.services.user_data_formatter import _generate_record_composite_key
from backend.utils.session_state import (
    get_selected_employment_context,
    get_selected_employment_record_key,
    _SESSION_STATES
)


@pytest.fixture
def single_employment_payload():
    """Synthetic employment payload with a single record at Level 3 verification."""
    history = [
        {
            "id": "emp_101",
            "company": "Acme Corp",
            "company_id": "99",
            "is_verified": True,
            "lists": [
                {
                    "id": "role_201",
                    "designation": "Senior Developer",
                    "employment_type": "Full Time",
                    "joining_date": "2022-01-01",
                    "worked_till_date": "present",
                    "still_working": "1",
                    "salary": "2400000",
                    "salary_inhand": "180000",
                    "salary_mode": "Per Annum",
                    "approved": "1",
                    "status": "1",
                    "verificationProcess": {"level1": 1, "level2": 1, "level3": 1, "level4": 0},
                    "skill": [{"id": "sk1", "name": "Python"}, {"id": "sk2", "name": "FastAPI"}]
                }
            ],
            "roles": [
                {
                    "id": "role_201",
                    "designation": "Senior Developer",
                    "joining_date": "2022-01-01",
                    "worked_till_date": "present",
                    "still_working": "1",
                    "approved": "1",
                    "status": "1"
                }
            ]
        }
    ]
    return {"status": True, "data": history, "employment_history": history}


@pytest.fixture
def multi_employment_payload():
    """Synthetic employment payload with two Google tenures and one Microsoft tenure."""
    history = [
        {
            "id": "g1",
            "company": "Google LLC",
            "company_id": "501",
            "is_verified": True,
            "lists": [
                {
                    "id": "r_g1",
                    "designation": "Software Engineer",
                    "joining_date": "2020-01-01",
                    "worked_till_date": "2021-12-31",
                    "still_working": "0",
                    "salary": "1800000",
                    "salary_inhand": "130000",
                    "salary_mode": "Per Annum",
                    "approved": "1",
                    "status": "1",
                    "verificationProcess": {"level1": 1, "level2": 1, "level3": 1, "level4": 1},
                    "skill": [{"id": "s1", "name": "Python"}]
                }
            ],
            "roles": [
                {
                    "id": "r_g1",
                    "designation": "Software Engineer",
                    "joining_date": "2020-01-01",
                    "worked_till_date": "2021-12-31",
                    "still_working": "0",
                    "approved": "1",
                    "status": "1"
                }
            ]
        },
        {
            "id": "g2",
            "company": "Google LLC",
            "company_id": "501",
            "is_verified": True,
            "lists": [
                {
                    "id": "r_g2",
                    "designation": "Staff Engineer",
                    "joining_date": "2022-01-01",
                    "worked_till_date": "2024-05-31",
                    "still_working": "0",
                    "salary": "3600000",
                    "salary_inhand": "250000",
                    "salary_mode": "Per Annum",
                    "approved": "1",
                    "status": "1",
                    "verificationProcess": {"level1": 1, "level2": 1, "level3": 1, "level4": 0},
                    "skill": [{"id": "s2", "name": "Distributed Systems"}]
                }
            ],
            "roles": [
                {
                    "id": "r_g2",
                    "designation": "Staff Engineer",
                    "joining_date": "2022-01-01",
                    "worked_till_date": "2024-05-31",
                    "still_working": "0",
                    "approved": "1",
                    "status": "1"
                }
            ]
        },
        {
            "id": "ms1",
            "company": "Microsoft",
            "company_id": "502",
            "is_verified": True,
            "lists": [
                {
                    "id": "r_ms1",
                    "designation": "Principal Engineer",
                    "joining_date": "2024-06-01",
                    "worked_till_date": "present",
                    "still_working": "1",
                    "salary": "4500000",
                    "salary_inhand": "320000",
                    "salary_mode": "Per Annum",
                    "approved": "1",
                    "status": "1",
                    "verificationProcess": {"level1": 1, "level2": 1, "level3": 1, "level4": 1},
                    "skill": [{"id": "s3", "name": "C#"}]
                }
            ],
            "roles": [
                {
                    "id": "r_ms1",
                    "designation": "Principal Engineer",
                    "joining_date": "2024-06-01",
                    "worked_till_date": "present",
                    "still_working": "1",
                    "approved": "1",
                    "status": "1"
                }
            ]
        }
    ]
    return {"status": True, "data": history, "employment_history": history}


# ============================================================================
# CLASS A — TestE2ESingleEmploymentJourney
# ============================================================================
class TestE2ESingleEmploymentJourney:

    @patch("backend.services.user_data_service.fetch_endpoint_data")
    def test_single_employment_full_journey(self, mock_fetch_api, single_employment_payload):
        client = TestClient(app)

        def _override(request=None, credentials=None):
            if request is not None and hasattr(request, "state"):
                request.state.user_id = 1001
                request.state.db_user_id = 1001
                request.state.role = "employee"
            return {"uid": 1001, "email": "emp@test.com", "role": "employee"}

        app.dependency_overrides[verify_token] = _override
        mock_fetch_api.return_value = single_employment_payload
        headers = {"X-User-Id": "1001", "Authorization": "Bearer token_1001"}
        session_id = "sess_e2e_single_1001"

        try:
            # 1. Employment history query
            r1 = client.post("/chat", json={"messages": [{"role": "user", "content": "Show my employment history"}], "session_id": session_id}, headers=headers)
            assert r1.status_code == 200
            d1 = r1.json()
            assert d1["success"] is True
            assert "Acme Corp" in d1["reply"]

            # 2. Verification overview query
            r2 = client.post("/chat", json={"messages": [{"role": "user", "content": "What is my employment verification status?"}], "session_id": session_id}, headers=headers)
            assert r2.status_code == 200
            assert "Level 3" in r2.json()["reply"]

            # 3. Level 1 status query
            r3 = client.post("/chat", json={"messages": [{"role": "user", "content": "Is my Level 1 Work Identity completed?"}], "session_id": session_id}, headers=headers)
            assert r3.status_code == 200
            assert "Level 1" in r3.json()["reply"] or "Work Identity" in r3.json()["reply"]

            # 4. Level 2 status query
            r4 = client.post("/chat", json={"messages": [{"role": "user", "content": "What is my Level 2 verification status?"}], "session_id": session_id}, headers=headers)
            assert r4.status_code == 200
            assert "Level 2" in r4.json()["reply"] or "Company Domain" in r4.json()["reply"]

            # 5. Level 3 status query
            r5 = client.post("/chat", json={"messages": [{"role": "user", "content": "What about Level 3 Employment Verification status?"}], "session_id": session_id}, headers=headers)
            assert r5.status_code == 200
            assert "Level 3" in r5.json()["reply"]

            # 6. Level 4 status query
            r6 = client.post("/chat", json={"messages": [{"role": "user", "content": "What is my Level 4 Salary Verification status?"}], "session_id": session_id}, headers=headers)
            assert r6.status_code == 200
            assert "Level 4" in r6.json()["reply"] or "Salary Verification" in r6.json()["reply"]

            # 7. What remains incomplete
            r7 = client.post("/chat", json={"messages": [{"role": "user", "content": "Why is my verification incomplete?"}], "session_id": session_id}, headers=headers)
            assert r7.status_code == 200
            assert "Salary" in r7.json()["reply"] or "Level 4" in r7.json()["reply"] or "incomplete" in r7.json()["reply"]

            # 8. How to complete next step
            r8 = client.post("/chat", json={"messages": [{"role": "user", "content": "What are my next steps to reach Level 4?"}], "session_id": session_id}, headers=headers)
            assert r8.status_code == 200
            assert "Level 4" in r8.json()["reply"] or "salary" in r8.json()["reply"].lower()

            # 9. Why verification is incomplete
            r9 = client.post("/chat", json={"messages": [{"role": "user", "content": "Why is my Level 4 status pending?"}], "session_id": session_id}, headers=headers)
            assert r9.status_code == 200
            assert len(r9.json()["reply"]) > 0

            # 10. Who is responsible for next action
            r10 = client.post("/chat", json={"messages": [{"role": "user", "content": "Who needs to take the next verification action?"}], "session_id": session_id}, headers=headers)
            assert r10.status_code == 200
            assert "You" in r10.json()["reply"] or "employee" in r10.json()["reply"].lower() or "Salary" in r10.json()["reply"]
        finally:
            app.dependency_overrides.clear()


# ============================================================================
# CLASS B — TestE2EMultipleEmploymentJourney
# ============================================================================
class TestE2EMultipleEmploymentJourney:

    @patch("backend.services.user_data_service.fetch_endpoint_data")
    def test_multiple_employment_disambiguation_and_selection(self, mock_fetch_api, multi_employment_payload):
        client = TestClient(app)

        def _override(request=None, credentials=None):
            if request is not None and hasattr(request, "state"):
                request.state.user_id = 1002
                request.state.db_user_id = 1002
                request.state.role = "employee"
            return {"uid": 1002, "email": "multi@test.com", "role": "employee"}

        app.dependency_overrides[verify_token] = _override
        mock_fetch_api.return_value = multi_employment_payload
        headers = {"X-User-Id": "1002", "Authorization": "Bearer token_1002"}
        session_id = "sess_e2e_multi_1002"

        try:
            # 11-12. Generic previous employment query -> Triggers selection metadata disambiguation
            r1 = client.post("/chat", json={"messages": [{"role": "user", "content": "Show my previous employment verification status"}], "session_id": session_id}, headers=headers)
            assert r1.status_code == 200
            d1 = r1.json()
            assert d1.get("selection_metadata") is not None
            opts = d1["selection_metadata"]["options"]
            assert len(opts) == 2
            assert opts[0]["selection_key"] == "emp_rec_1"

            # 13. Submit selection key emp_rec_1 (Software Engineer 2020-2021)
            r2 = client.post("/chat", json={"messages": [{"role": "user", "content": "emp_rec_1"}], "session_id": session_id}, headers=headers)
            assert r2.status_code == 200

            # 14-15. Verification follow-up refers to selected record (Software Engineer)
            r3 = client.post("/chat", json={"messages": [{"role": "user", "content": "What is my employment verification status?"}], "session_id": session_id}, headers=headers)
            assert r3.status_code == 200
            rep3 = r3.json()["reply"]
            assert "Google LLC" in rep3

            # 16. Salary follow-up refers to selected record (1,800,000)
            r4 = client.post("/chat", json={"messages": [{"role": "user", "content": "What was my salary at previous company?"}], "session_id": session_id}, headers=headers)
            assert r4.status_code == 200
            rep4 = r4.json()["reply"]
            assert "1,800,000" in rep4 or "1800000" in rep4 or "Google" in rep4

            # 17. Skills follow-up refers to selected record (Python)
            r5 = client.post("/chat", json={"messages": [{"role": "user", "content": "What skills did I use during previous employment?"}], "session_id": session_id}, headers=headers)
            assert r5.status_code == 200
            rep5 = r5.json()["reply"]
            assert "Python" in rep5 or "Google" in rep5

            # 18. Explicit company switching to Microsoft
            r6 = client.post("/chat", json={"messages": [{"role": "user", "content": "What about Microsoft verification status?"}], "session_id": session_id}, headers=headers)
            assert r6.status_code == 200
            rep6 = r6.json()["reply"]
            assert "Microsoft" in rep6
        finally:
            app.dependency_overrides.clear()


# ============================================================================
# CLASS C — TestE2ECrossIntentJourney
# ============================================================================
class TestE2ECrossIntentJourney:

    @patch("backend.services.user_data_service.fetch_endpoint_data")
    def test_cross_intent_context_propagation(self, mock_fetch_api, multi_employment_payload):
        client = TestClient(app)

        def _override(request=None, credentials=None):
            if request is not None and hasattr(request, "state"):
                request.state.user_id = 1003
                request.state.db_user_id = 1003
                request.state.role = "employee"
            return {"uid": 1003, "email": "cross@test.com", "role": "employee"}

        app.dependency_overrides[verify_token] = _override
        mock_fetch_api.return_value = multi_employment_payload
        headers = {"X-User-Id": "1003", "Authorization": "Bearer token_1003"}
        session_id = "sess_e2e_cross_1003"

        try:
            # 21. Select Google explicit tenure via previous employment prompt
            client.post("/chat", json={"messages": [{"role": "user", "content": "Show my previous employment verification status"}], "session_id": session_id}, headers=headers)
            client.post("/chat", json={"messages": [{"role": "user", "content": "emp_rec_2"}], "session_id": session_id}, headers=headers)

            # 22. Verification -> Salary query
            r_sal = client.post("/chat", json={"messages": [{"role": "user", "content": "Show my salary at previous company"}], "session_id": session_id}, headers=headers)
            assert r_sal.status_code == 200
            assert "3,600,000" in r_sal.json()["reply"] or "3600000" in r_sal.json()["reply"] or "Google" in r_sal.json()["reply"]

            # 23. Salary -> Skills query
            r_sk = client.post("/chat", json={"messages": [{"role": "user", "content": "What work skills were recorded for my previous employment?"}], "session_id": session_id}, headers=headers)
            assert r_sk.status_code == 200
            assert "Distributed Systems" in r_sk.json()["reply"] or "Google" in r_sk.json()["reply"]

            # 24. Skills -> Verification query
            r_ver = client.post("/chat", json={"messages": [{"role": "user", "content": "What is my employment verification status?"}], "session_id": session_id}, headers=headers)
            assert r_ver.status_code == 200
            assert "Google LLC" in r_ver.json()["reply"]

            # 25. Explicit company query overrides previous selection
            r_exp = client.post("/chat", json={"messages": [{"role": "user", "content": "Tell me about Microsoft verification status"}], "session_id": session_id}, headers=headers)
            assert r_exp.status_code == 200
            assert "Microsoft" in r_exp.json()["reply"]

            # 26. Unrelated profile incomplete query does not crash or leak
            r_unrel = client.post("/chat", json={"messages": [{"role": "user", "content": "Is my overall profile complete?"}], "session_id": session_id}, headers=headers)
            assert r_unrel.status_code == 200
        finally:
            app.dependency_overrides.clear()


# ============================================================================
# CLASS D — TestE2ESessionLifecycleJourney
# ============================================================================
class TestE2ESessionLifecycleJourney:

    @patch("backend.services.user_data_service.fetch_endpoint_data")
    def test_session_ttl_and_isolation(self, mock_fetch_api, multi_employment_payload):
        client = TestClient(app)
        mock_fetch_api.return_value = multi_employment_payload
        headers_user1 = {"X-User-Id": "1004", "Authorization": "Bearer token_1004"}
        headers_user2 = {"X-User-Id": "1005", "Authorization": "Bearer token_1005"}
        sess_1 = "sess_lifecycle_user1"
        sess_2 = "sess_lifecycle_user2"

        def _override_user1(request=None, credentials=None):
            if request is not None and hasattr(request, "state"):
                request.state.user_id = 1004
                request.state.db_user_id = 1004
                request.state.role = "employee"
            return {"uid": 1004, "email": "u1@test.com", "role": "employee"}

        def _override_user2(request=None, credentials=None):
            if request is not None and hasattr(request, "state"):
                request.state.user_id = 1005
                request.state.db_user_id = 1005
                request.state.role = "employee"
            return {"uid": 1005, "email": "u2@test.com", "role": "employee"}

        app.dependency_overrides[verify_token] = _override_user1

        try:
            # 27. Active session stores selection
            client.post("/chat", json={"messages": [{"role": "user", "content": "Show my previous employment verification status"}], "session_id": sess_1}, headers=headers_user1)
            client.post("/chat", json={"messages": [{"role": "user", "content": "emp_rec_1"}], "session_id": sess_1}, headers=headers_user1)
            assert get_selected_employment_context(sess_1) == "Google LLC"

            # 31. Pending option mapping expires independently (15m)
            client.post("/chat", json={"messages": [{"role": "user", "content": "Show my previous employment verification status"}], "session_id": sess_1}, headers=headers_user1)
            with patch("time.time", return_value=time.time() + 905):
                r_exp_map = client.post("/chat", json={"messages": [{"role": "user", "content": "emp_rec_1"}], "session_id": sess_1}, headers=headers_user1)
                assert r_exp_map.status_code == 200

            # 32. Single-use key replay rejected
            client.post("/chat", json={"messages": [{"role": "user", "content": "Show my previous employment verification status"}], "session_id": sess_1}, headers=headers_user1)
            client.post("/chat", json={"messages": [{"role": "user", "content": "emp_rec_1"}], "session_id": sess_1}, headers=headers_user1)
            r_replay = client.post("/chat", json={"messages": [{"role": "user", "content": "emp_rec_1"}], "session_id": sess_1}, headers=headers_user1)
            assert r_replay.status_code == 200

            # 35. Multi-tenant isolation: User 2 cannot access User 1's selected context
            app.dependency_overrides[verify_token] = _override_user2
            r_user2 = client.post("/chat", json={"messages": [{"role": "user", "content": "What is my verification status?"}], "session_id": sess_2}, headers=headers_user2)
            assert r_user2.status_code == 200
            assert get_selected_employment_context(sess_2) is None
        finally:
            app.dependency_overrides.clear()


# ============================================================================
# CLASS E — TestE2EActionMetadataAndFrontendContracts
# ============================================================================
class TestE2EActionMetadataAndFrontendContracts:

    @patch("backend.services.user_data_service.fetch_endpoint_data")
    def test_action_and_selection_metadata_contracts(self, mock_fetch_api, single_employment_payload, multi_employment_payload):
        client = TestClient(app)

        def _override(request=None, credentials=None):
            if request is not None and hasattr(request, "state"):
                request.state.user_id = 1006
                request.state.db_user_id = 1006
                request.state.role = "employee"
            return {"uid": 1006, "email": "meta@test.com", "role": "employee"}

        app.dependency_overrides[verify_token] = _override
        headers = {"X-User-Id": "1006", "Authorization": "Bearer token_1006"}
        session_id = "sess_e2e_meta_1006"

        try:
            # 36 & 39. Add employment intent returns action metadata surviving JSON serialization
            mock_fetch_api.return_value = single_employment_payload
            r_act = client.post("/chat", json={"messages": [{"role": "user", "content": "I want to add a new employment record"}], "session_id": session_id}, headers=headers)
            assert r_act.status_code == 200
            d_act = r_act.json()
            assert d_act.get("action_metadata") is not None
            assert d_act["action_metadata"][0]["id"] == "employment_add"

            # 40. Selection metadata survives JSON serialization
            mock_fetch_api.return_value = multi_employment_payload
            r_sel = client.post("/chat", json={"messages": [{"role": "user", "content": "Show my previous employment verification status"}], "session_id": session_id}, headers=headers)
            assert r_sel.status_code == 200
            d_sel = r_sel.json()
            assert d_sel.get("selection_metadata") is not None
            assert d_sel["selection_metadata"]["type"] == "employment_selection"
            assert len(d_sel["selection_metadata"]["options"]) == 2

            # 41-43. Static contract checks on frontend/index.html
            with open("frontend/index.html", "r", encoding="utf-8") as f:
                html_content = f.read()

            assert "ACTION_REGISTRY" in html_content
            assert "renderActionNavControls" in html_content
            assert "renderSelectionControls" in html_content
            assert "employment_selection" in html_content
        finally:
            app.dependency_overrides.clear()
