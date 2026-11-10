# CollarCheck Phase 2D.3.6.1 — Integrated Pipeline Scope & Dependency Audit

**Document:** `docs/phase2d_3_6_1_integrated_pipeline_scope_audit.md`  
**Date:** November 10, 2026  
**Status:** COMPLETE (READ-ONLY AUDIT — NO FILES STAGED, COMMITTED, OR PUSHED)  
**Target Branch:** `phase2d-redis7-verification`  
**Base Commit:** `07633c1` (`origin/main`)  
**Audit Purpose:** Comprehensive dependency, scope, and test coverage audit for **Option A — Integrated Pipeline PR**, establishing the exact boundary of changes required to integrate Phase 2C.2 conversation history, Phase 2D multi-worker reliability, and Phase 4 SSE keepalive / threadpool offloading without dragging in unrelated Phase 5 or API changes.

---

## 1. Executive Summary

Following the accepted Phase 2D.3.5 Pre-PR Staging Safety Audit and Phase 2D.3.6 Dedicated Branch Creation, the repository is on branch `phase2d-redis7-verification`.

To resolve the architectural entanglement between **Phase 2C.2 conversation-history persistence** and **Phase 4 Server-Sent Events (SSE) keepalive streaming / threadpool offloading**, Option A incorporates the pipeline runtime in `backend/main.py`.

Before modifying the git staging index, this audit independently inspected:
1. Complete diffs of `backend/main.py`, `backend/schemas/schemas.py`, and `backend/utils/sse.py`.
2. Direct upstream dependencies: `backend/utils/gates.py`, `backend/utils/timing.py`, `backend/schemas/conversation_context.py`, and `backend/services/query_rewriter.py`.
3. Test suite coverage across all included behaviors (218 collected tests).
4. Diagnosis of why `test_phase4_3_blocking_io_offloading.py` must NOT be staged (obsolete MySQL session tests causing asyncio event-loop infinite hangs).

**Audit Verdict:** **READY FOR SELECTIVE STAGING.**  
Option A can be staged cleanly with zero test regressions, complete architectural cohesion, and zero inclusion of unrelated employer/employee APIs or Phase 5 multiturn files.

---

## 2. Complete Diff Inspections

### 2.1 `backend/schemas/schemas.py`

**Status in Git:** Modified tracked file (`M backend/schemas/schemas.py`).  
**Diff Size:** 5 hunks, +28 lines, -3 lines.

#### Detailed Inspection:
1. **Pydantic Model Validator Import (Lines 1–4):**
   ```python
   from pydantic import BaseModel, model_validator
   ```
   Required for runtime payload validation.

2. **`ChatRequest` Schema Modernization (Lines 10–33):**
   ```python
   class ChatRequest(BaseModel):
       messages: Optional[List[ChatMessage]] = None
       message: Optional[str] = None
       resume_context: Optional[str] = None
       user_type: Optional[str] = "employee"
       session_id: Optional[str] = None
       ...
       @model_validator(mode="before")
       @classmethod
       def check_at_least_one_message_or_action(cls, data: Any) -> Any:
           if isinstance(data, dict):
               if "message" not in data and "messages" not in data and "action" not in data:
                   raise ValueError("Either 'message', 'messages', or 'action' must be provided.")
           return data
   ```
   *Justification:*
   - Phase 2B/2C.2 replaces client-supplied message arrays with authoritative server-side history. Clients provide `message: str` for new turns.
   - `messages: Optional[List[ChatMessage]]` maintains backwards compatibility while allowing single messages.
   - `session_id: Optional[str] = None` (replaces hardcoded `"default_session"`), allowing the server to generate a secure UUID session ID when none is provided.
   - `test_phase2c2_pipeline_integration.py` instantiates `ChatRequest(message="Hello", session_id="test-session")`. Without this schema update, validation fails immediately with `ValidationError`.

3. **`ChatResponse` Session Return (Line 114):**
   ```python
   session_id: Optional[str] = None
   ```
   Allows the server to return the resolved or generated `session_id` back to the client.

*Scope Verdict:* **100% Phase 2C.2 / 2D.** Contains zero Phase 5 or API changes. Safe for whole-file staging.

---

### 2.2 `backend/utils/sse.py`

**Status in Git:** Untracked file (`?? backend/utils/sse.py`).  
**File Size:** 260 lines.

#### Detailed Inspection:
Provides clean, self-contained SSE serialization, keepalive streaming, and public error sanitization:
- `validate_keepalive_interval(val: Any, default: float = 15.0) -> float`: Robust float parser guarding against NaN, infinity, negative values, and non-numeric inputs.
- `SAFE_PUBLIC_ERROR_DETAILS`: Bounded allowlist of client-safe error messages.
- `map_safe_sse_error_detail(detail: Any, status_code: Optional[int]) -> str`: Sanitizes exceptions, redacting file paths, SQL queries, and internal database details.
- `format_sse_event(event: str, data: Any) -> str`: Standard SSE wire serialization (`event: <name>\ndata: <json>\n\n`).
- `parse_sse_event(chunk: str) -> Dict[str, Any]`: Parser for unit tests and SSE consumers.
- `format_sse_comment(comment: str) -> str`: Emits comment frames (`: keepalive\n\n`) to keep proxy connections alive without triggering EventSource listeners.
- `await_with_keepalive(coro, interval, req, comment)`: Async generator wrapping an awaitable coroutine with periodic keepalive comments during slow retrieval operations.
- `stream_with_keepalive(stream_gen, interval, req, comment)`: Async generator wrapping token streams with keepalive comments when waiting for upstream LLM token chunks.

*Scope Verdict:* **100% Phase 4.1 SSE Infrastructure.** Fully self-contained. Soft optional dependency on `backend.utils.telemetry.sanitize_string_value` is safely wrapped in try/except fallback. Safe for whole-file staging.

---

### 2.3 `backend/main.py`

**Status in Git:** Modified tracked file (`M backend/main.py`).  
**Diff Size:** 31 hunks (+692 lines, -604 lines).

#### Comprehensive Architectural Decomposition:

| Hunk Range | Target Area | Implementation Details | Phase Alignment |
| :--- | :--- | :--- | :--- |
| **Hunks 1–4** (L1–L102) | Imports & Dependencies | Imports `StreamingResponse`, `run_in_threadpool`, `ClientDisconnect`, Redis exceptions (`RedisConnectionError`, `RedisTimeoutError`, `RedisUnavailableError`, `RedisSessionOOMError`, `RedisSessionOwnershipError`), conversation history helpers (`append_turn_atomic_async`, `get_conversation_history_snapshot_async`, `clear_conversation_history_async`), SSE utilities, `is_widget_relevant_query`, and `set_ttft`. | Phase 2C.2, 2D, Phase 4 |
| **Hunks 5–9** (L155–L260) | Lifespan & Global Handlers | Adds graceful shutdown of background sync loops, search HTTP clients, PyMySQL pools, and `close_redis_pool()` in `finally:`. Registers FastAPI exception handlers for `RedisUnavailableError` (503) and `RedisSessionOOMError` (507). | Phase 2D (Multi-worker lifecycle) |
| **Hunks 10–12** (L559–L575) | Endpoint Threadpool Offloading | Wraps `reverse_geocode` in `run_in_threadpool` and uses a context manager for the `/health` DB connection. | Phase 4.3 (Blocking I/O offload) |
| **Hunk 13** (L588–L650) | Reply Sanitization | Adds Missing Salary Guard (Phase 1 Fix 3) in `sanitize_job_results_reply` to prevent fabricated salary figures when database context is "Not specified". | Phase 1 / Regression protection |
| **Hunk 14** (L652–L700) | `ChatPipelineResult` Dataclass | Decouples pipeline state from HTTP responses, supporting both JSON (`/chat`) and SSE (`/chat/stream`) generation. | Phase 2C.2 & Phase 4.1 |
| **Hunks 15–16** (L701–L930) | Profile I/O Extraction | Extracts synchronous profile queries and profile updates into `_fetch_user_profile_context()` and `_execute_profile_updates()` to allow clean threadpool offloading. | Phase 4.3 (Threadpool offload) |
| **Hunks 17–21** (L931–L1735) | `execute_chat_pipeline()` | 1. **Rule F-01:** Rejects multi-message client payloads (status 400).<br>2. **Payload Validation:** Enforces message content size via `validate_message_content`.<br>3. **Token Resolution:** Offloads `get_user_platform_token` via `run_in_threadpool`.<br>4. **Session ID:** Generates UUID session ID if none provided; validates format with `is_valid_session_id`.<br>5. **Session Touch:** Asynchronously touches session TTL via `touch_session_state_async`.<br>6. **Conversation Context:** Loads snapshot via `get_conversation_context_async`.<br>7. **Redis History Load:** Loads authoritative history snapshot via `get_conversation_history_snapshot_async`.<br>8. **Offloaded Updates/Fetch:** Runs profile updates and fetches via `run_in_threadpool`. | Phase 2C.2 & Phase 2D (Authoritative Redis history & state) + Phase 4.3 (Offloading) |
| **Hunks 22–26** (L2101–L2304) | POST `/chat` Endpoint | Executes `execute_chat_pipeline`. Atomically persists turn to Redis via `append_turn_atomic_async` before returning `ChatResponse`. Maps `TurnConcurrencyConflictError` (409) and Redis exceptions. | Phase 2C.2 (Atomic turn append) |
| **Hunks 27–29** (L2306–L2740) | POST `/chat/stream` Endpoint | Server-Sent Events endpoint with keepalive streaming (`await_with_keepalive`, `stream_with_keepalive`). Formats events: `start`, `results`, `status`, `delta`, `done`, `error`. Commits turns atomically to Redis **before** emitting `done`. | Phase 4.1 (SSE streaming & keepalive) + Phase 2C.2 (History commit) |
| **Hunks 30–31** (L2742–L2875) | History Endpoints | GET `/chat/history`: Returns validated history snapshot with `turn_seq` and message list.<br>DELETE `/chat/history`: Clears history and idempotency keys while preserving search filters. | Phase 2C.2 (History API) |

*Scope Verdict:* **Unified Core Pipeline.** The Redis conversation history and SSE streaming are inextricably integrated into `execute_chat_pipeline`. Staging `backend/main.py` in its entirety is required for Option A.

---

## 3. Direct Upstream Dependencies of `backend/main.py`

Inspection of `backend/main.py` diff identified four upstream dependencies that must be present in the working tree and index:

### 3.1 `backend/utils/gates.py`
- **Import in `main.py` (Line 77):** `from backend.utils.gates import is_widget_relevant_query`
- **Diff:** Adds `is_widget_relevant_query(msg)` and `is_profile_job_search(msg)`.
- **Impact if missing:** `ImportError` on startup.
- **Action:** Stage `backend/utils/gates.py`.

### 3.2 `backend/utils/timing.py`
- **Import in `main.py` (Line 95):** `from backend.utils.timing import set_ttft`
- **Diff:** Adds `ttft_ms` to `TimingContext` and defines `set_ttft(ttft_ms: float)` and `set_retrieval_metadata(...)`.
- **Impact if missing:** `ImportError` on startup.
- **Action:** Stage `backend/utils/timing.py`.

### 3.3 `backend/schemas/conversation_context.py`
- **Imports in `backend/utils/session_state.py` (Lines 2189, 2323):**  
  `from backend.schemas.conversation_context import ConversationContext`
- **Diff:** Untracked 50-line Pydantic model defining the canonical `ConversationContext` facade over Redis session data.
- **Impact if missing:** `ModuleNotFoundError` whenever `get_conversation_context()` or `get_conversation_context_async()` is invoked.
- **Action:** Stage `backend/schemas/conversation_context.py`.

### 3.4 `backend/services/query_rewriter.py`
- **Imports in `test_phase2c2_pipeline_integration.py` (Lines 200, 273, 346, 379):**  
  `patch("backend.services.query_rewriter.QueryRewriterService.rewrite_query", ...)`
- **Impact if missing:** `unittest.mock.patch` attempts to import `backend.services.query_rewriter`, failing with `ModuleNotFoundError` during CI execution.
- **Action:** Stage `backend/services/query_rewriter.py`.

---

## 4. Test Suite Mapping & Validation Results

To ensure test coverage integrity (Rule 5: "Preserve test coverage for every behavior included in the PR"), all relevant test suites were executed together.

### 4.1 Included Test Suites

| Test Suite | Tests | Purpose | Status |
| :--- | :---: | :--- | :---: |
| `backend/tests/test_phase2d_cross_worker.py` | 13 | Cross-worker CAS updates, generation collisions, Redis multi-process isolation | **9 Passed, 4 Skipped** (real Redis CI gate) |
| `backend/tests/test_redis_session.py` | 23 | Redis session state, CAS remerge, TTL expiration, composite key hashing | **23 Passed** |
| `backend/tests/test_redis_conversation_history.py` | 44 | Atomic turn append, sequence checks, turn idempotency, snapshot retrieval | **38 Passed, 6 Skipped** (real Redis CI gate) |
| `backend/tests/test_phase2c2_pipeline_integration.py` | 29 | Pipeline execution with Redis history, session ID resolution, multi-message rejection | **29 Passed** |
| `backend/tests/test_sse_streaming.py` | 32 | SSE wire framing, `/chat/stream` auth, error sanitization, card decoupling | **32 Passed** |
| `backend/tests/test_phase5_sse_keepalive.py` | 25 | Keepalive comments, interval validation, stream generator cancellation | **25 Passed** |
| `backend/tests/test_phase4_1_sse_hardening.py` | 9 | Client disconnect during streaming, generator cleanup, provider error mapping | **9 Passed** |
| `backend/tests/test_conversation_context.py` | 25 | Canonical `ConversationContext` facade methods over Redis session | **25 Passed** |
| `backend/tests/test_query_rewriter.py` | 18 | Contextual query rewriting using conversation context | **18 Passed** |
| **TOTAL** | **218** | **Full integrated test suite** | **208 Passed, 10 Skipped, 0 Failed** |

### 4.2 Diagnosis of `test_phase4_3_blocking_io_offloading.py` (DO NOT STAGE)

During test execution, `backend/tests/test_phase4_3_blocking_io_offloading.py` was inspected and run. It was found to **hang indefinitely** during `TestAsyncSessionReads`:
```python
def slow_db_load(key):
    read_started.set()
    can_proceed_read.wait(timeout=5.0)

with patch("backend.utils.session_state._db_load_session", side_effect=slow_db_load):
    session_task = asyncio.create_task(get_session_state_async("resp_test_session"))
    while not read_started.is_set():
        await asyncio.sleep(0.01)
```

**Root Cause:**
- `test_phase4_3_blocking_io_offloading.py` was written for an obsolete implementation of `backend/utils/session_state.py` that used MySQL-backed `_db_load_session` and in-memory `_SESSION_STATES`.
- In **Phase 2D.1**, all MySQL session fallback and `_db_load_session` calls were eliminated in favor of pure Redis CAS (`atomic_cas_session_update`).
- Because `_db_load_session` is never called by `get_session_state_async`, `read_started.set()` is never triggered, causing an **infinite event-loop hang**.
- **Conclusion:** `test_phase4_3_blocking_io_offloading.py` is obsolete and incompatible with Phase 2D. It must remain **strictly unstaged**.

---

## 5. Isolation from Phase 5 and API Changes

The following files present in the working tree are **strictly excluded** from Option A to maintain PR scope discipline:

1. **Employer / Employee API Changes (Excluded):**
   - `backend/api/employee.py`
   - `backend/api/employer.py`
   - `backend/api/profile.py`
   - `backend/api/search.py`
2. **Search / Recommendation Engine Services (Excluded):**
   - `backend/services/employer_search_executor.py`
   - `backend/services/hybrid_job_search.py`
   - `backend/services/job_match_client.py`
   - `backend/services/llm_service.py`
   - `backend/services/people_search_service.py`
   - `backend/services/profile_parser.py`
   - `backend/services/search_client.py`
   - `backend/services/search_parser.py`
   - `backend/services/search_router.py`
   - `backend/services/user_data_formatter.py`
   - `backend/services/user_data_intent_parser.py`
   - `backend/services/user_data_service.py`
3. **Phase 5 Dialogue & Multiturn Tests (Excluded):**
   - `backend/tests/test_phase5_multiturn_dialogue.py`
   - `backend/tests/test_phase5_7_intent_collision.py`
4. **Other Test & Fixture Files (Excluded):**
   - `backend/tests/test_phase2c_employment_e2e.py`
   - `backend/tests/test_claimed_company_status.py`
   - `backend/tests/test_phase4_conversational_ux_hardening.py`
   - `backend/tests/test_phase4_2_2_exact_record_context.py`
   - `backend/tests/test_phase4_response_formatting.py`
   - `backend/tests/test_async_intent_parser.py`
   - `backend/tests/conftest.py` (**CRITICAL:** Must remain untracked / unstaged to prevent `fakeredis` overrides in CI).
5. **Miscellaneous Workspace Files (Excluded):**
   - `frontend/index.html`
   - `README.md`
   - `requirements.txt`
   - `.env.example`

---

## 6. Staging Plan for Option A

### 6.1 Already Staged in Git Index (23 Files)
- `.github/workflows/redis7_integration.yml`
- `backend/config/config.py` (selectively staged `REDIS_*` block)
- `backend/services/ranking_service.py`
- `backend/utils/session_state.py`
- `backend/utils/conversation_history.py`
- `backend/utils/redis_client.py`
- `docker-compose.yml`
- 4 test suites:
  - `backend/tests/test_phase2d_cross_worker.py`
  - `backend/tests/test_redis_session.py`
  - `backend/tests/test_redis_conversation_history.py`
  - `backend/tests/test_phase2c2_pipeline_integration.py`
- 12 documentation reports (`docs/phase2d_0_...` to `docs/phase2d_3_5_...`)

### 6.2 Files to Stage for Option A (10 Additional Files)
1. `backend/schemas/schemas.py` (Modernized `ChatRequest` and `ChatResponse`)
2. `backend/utils/sse.py` (SSE serialization, keepalive, and error mapping)
3. `backend/utils/gates.py` (`is_widget_relevant_query` helper)
4. `backend/utils/timing.py` (`set_ttft` and telemetry metadata helpers)
5. `backend/schemas/conversation_context.py` (`ConversationContext` facade schema)
6. `backend/services/query_rewriter.py` (`QueryRewriterService` for test mock compatibility)
7. `backend/main.py` (Integrated pipeline and `/chat`, `/chat/stream`, `/chat/history` routes)
8. `backend/tests/test_sse_streaming.py` (32 tests for SSE foundation)
9. `backend/tests/test_phase5_sse_keepalive.py` (25 tests for SSE keepalive streaming)
10. `backend/tests/test_phase4_1_sse_hardening.py` (9 tests for SSE disconnect handling)
11. `backend/tests/test_conversation_context.py` (25 tests for conversation context)
12. `backend/tests/test_query_rewriter.py` (18 tests for query rewriter)
13. `docs/phase2d_3_6_branch_staging_report.md` (Branch staging report)
14. `docs/phase2d_3_6_1_integrated_pipeline_scope_audit.md` (This document)

---

## 7. Next Steps & Operating Rules

1. **NO COMMIT OR PUSH HAS OCCURRED.**
2. The working tree and index remain completely uncommitted.
3. This audit report is presented to the user for formal review.
4. Upon user confirmation, the specified 14 files will be staged into the index on `phase2d-redis7-verification`, followed by a final pre-commit regression test run before any commit is authored.

