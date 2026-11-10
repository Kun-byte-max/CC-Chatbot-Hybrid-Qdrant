# CollarCheck Phase 2D.3.5 — Pre-PR Change Scope & Staging Safety Audit

**Audit Date:** November 10, 2026  
**Auditor Mode:** STRICT READ-ONLY PRE-PR STAGING AUDIT  
**Repository:** `C:\UNG\chatbot_Quadrant`  
**Current Branch:** `main` (Commit: `07633c1`)  
**Target Remote:** `https://github.com/kun-byte-max/CC-Chatbot-Hybrid-Qdrant.git`  
**Deliverable:** `docs/phase2d_3_5_pre_pr_staging_safety_audit.md`  
**Verdict:** **READY WITH SELECTIVE STAGING**

---

## 1. Executive Summary & Verdict

Phase 2D.3.5 conducted a strictly read-only audit of the entire repository working tree to prepare the Phase 2D multi-worker Redis 7 integration for a dedicated, safe GitHub Pull Request.

### Verdict: **READY WITH SELECTIVE STAGING**
- **Working Tree Analysis:** The local working tree is shared across multiple concurrent enhancement efforts. In addition to Phase 2D Redis multi-worker changes, the tree contains modifications from Phase 4 (conversational UX, SSE keepalive, threadpool offloading) and Phase 5 (intent collision, multi-turn dialogue), as well as employer/employee API updates.
- **Selective Staging Mandate:** A blanket `git add .` or wildcard `git add docs/phase2d_*` would inadvertently sweep in Phase 4/5 logic, test harness artifacts, or untracked test fixtures (specifically `backend/tests/conftest.py`).
- **Safety Boundaries Established:**
  1. **16 Files/Paths Safe for Whole-File Staging:** 4 test suites, 2 core Redis utils, 1 workflow, 2 configuration/service files, and 12 exact Phase 2D documentation reports.
  2. **3 Files Requiring Selective Patch Staging (`git add -p`):** `backend/main.py`, `backend/config/config.py`, and `backend/utils/session_state.py`.
  3. **35+ Files That Must Remain Unstaged:** Including `backend/tests/conftest.py`, API endpoints, and unrelated Phase 4/5 tests.
- **Runbook Provided:** An exact, ordered, copy-pasteable PowerShell runbook is provided in Section 7 for the user to execute manually. Zero branches were created, zero files were staged, and zero commits were made during this audit.

---

## 2. Repository State Inventory

### 2.1. Git Environment Baseline
```powershell
git branch --show-current   # main
git log -1 --oneline        # 07633c1 docs: update architecture, hybrid search RRF, CAS state retention, and verification roadmap
git remote -v               # origin https://github.com/kun-byte-max/CC-Chatbot-Hybrid-Qdrant.git (fetch & push)
```

### 2.2. Working Tree Snapshot
- **Existing Staged Changes:** `0` files (Clean index; `git diff --cached` is empty).
- **Modified Tracked Files:** `37` files.
- **Untracked Directories/Files:** `38` items.
- **Deleted / Renamed Files:** `0`.

---

## 3. Per-File Change Scope Assessment

Every file proposed for the Phase 2D Redis 7 integration Pull Request was audited hunk-by-hunk:

### 3.1. `.github/workflows/redis7_integration.yml`
- **Status:** Untracked (`??`).
- **Modifications:** New workflow file specifying the pinned `redis:7.2-alpine` ephemeral service container, runtime configuration verification, and 10-test JUnit zero-skip gate.
- **Scope Assessment:** 100% Phase 2D.3.2.
- **Staging Recommendation:** **SAFE FOR WHOLE-FILE STAGING** (`git add .github/workflows/redis7_integration.yml`).

### 3.2. `backend/main.py`
- **Status:** Tracked, Modified (`M`).
- **Modifications:**
  - *Phase 2D.1:* Clean lifespan pool shutdown in `finally` block (`await close_redis_pool()`); removal of dead in-memory `EMPLOYER_SESSIONS` imports.
  - *Phase 2C.2:* Conversation history endpoints (`GET /chat/history`, `DELETE /chat/history`) and history loading in `execute_chat_pipeline`.
  - *Phase 4 (Unrelated):* SSE streaming keepalive (`await_with_keepalive`, `stream_with_keepalive`) and threadpool offloading (`run_in_threadpool`).
  - *Phase 5 (Unrelated):* Intent collision gate checks.
- **Scope Assessment:** Mixed. Contains necessary Phase 2D lifespan cleanup, but is heavily intertwined with Phase 4/5 streaming and threadpool offloading.
- **Staging Recommendation:** **REQUIRES SELECTIVE STAGING (`git add -p backend/main.py`)**.

### 3.3. `backend/config/config.py`
- **Status:** Tracked, Modified (`M`).
- **Modifications:**
  - *Phase 2B/2C/2D:* Added `REDIS_ENABLED`, `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`, `REDIS_DB`, `REDIS_MAX_CONNECTIONS`, `REDIS_CONNECT_TIMEOUT_SECONDS`, `REDIS_SOCKET_TIMEOUT_SECONDS`, `SESSION_TTL_SECONDS`, `MAX_SESSION_MESSAGES`, `MAX_SESSION_BYTES`, `MAX_MESSAGE_CHARS` (lines 59–72).
  - *Phase 4 (Unrelated):* `SSE_KEEPALIVE_INTERVAL_SECONDS` (lines 44–46).
  - *Unrelated Resilience:* `validate_sample_rate` (lines 10–31) and `validate_positive_timeout` / `DB_*_TIMEOUT_SECONDS` (lines 101–136).
- **Scope Assessment:** Mixed.
- **Staging Recommendation:** **REQUIRES SELECTIVE STAGING (`git add -p backend/config/config.py`)** to stage the Redis configuration block while leaving unrelated timeout/telemetry logic in the working tree.

### 3.4. `backend/utils/session_state.py`
- **Status:** Tracked, Modified (`M`).
- **Modifications:**
  - *Phase 2D.1:* `begin_search_generation` CAS remerge via `_remerge_gen` and `remerge_fn`.
  - *Phase 2D.2.1:* `claim_next_pagination_page` remerge via `_remerge_claim` and boolean return check; `set_candidate_search_criteria` remerge via `remerge_crit`.
  - *Phase 2B/2C Foundation:* Core Redis session storage (`get_session_state`, `save_session_state_cas`, TTL, user isolation).
  - *Phase 4 (Context Facade):* `get_conversation_context_async` (lines 1040–1180).
- **Scope Assessment:** Core session state engine.
- **Staging Recommendation:** **REQUIRES SELECTIVE STAGING (`git add -p backend/utils/session_state.py`)** if isolating Phase 2D from Phase 4 context facade; or **SAFE FOR WHOLE-FILE STAGING** if the Pydantic context model (`ConversationContext`) is bundled.

### 3.5. `backend/utils/redis_client.py`
- **Status:** Untracked (`??`).
- **Modifications:** New file. High-performance connection-pooled Async and Sync Redis client manager; implements `init_redis_pool`, `close_redis_pool`, and domain exceptions (`RedisUnavailableError`, `RedisSessionOwnershipError`, etc.).
- **Scope Assessment:** 100% Phase 2 Redis infrastructure.
- **Staging Recommendation:** **SAFE FOR WHOLE-FILE STAGING** (`git add backend/utils/redis_client.py`).

### 3.6. `backend/utils/conversation_history.py`
- **Status:** Untracked (`??`).
- **Modifications:** New file. Authoritative conversation history engine implementing atomic turn commits, pair trimming, bounded idempotency, and Lua scripts (`LUA_ATOMIC_TURN_COMMIT_SCRIPT`, `LUA_SNAPSHOT_SCRIPT`).
- **Scope Assessment:** 100% Phase 2 Redis conversation history infrastructure.
- **Staging Recommendation:** **SAFE FOR WHOLE-FILE STAGING** (`git add backend/utils/conversation_history.py`).

### 3.7. `backend/services/ranking_service.py`
- **Status:** Tracked, Modified (`M`).
- **Modifications:** Added deprecation docstrings/annotations to `EMPLOYER_SESSIONS`, `get_employer_session`, and `reset_employer_session`, replacing hardcoded static session IDs with `generate_secure_session_id()`.
- **Scope Assessment:** 100% Phase 2D.1 dead legacy state cleanup.
- **Staging Recommendation:** **SAFE FOR WHOLE-FILE STAGING** (`git add backend/services/ranking_service.py`).

### 3.8. `docker-compose.yml`
- **Status:** Tracked, Modified (`M`).
- **Modifications:** Added the isolated `redis:7-alpine` container definition with RAM-only flags (`--save "" --appendonly no --maxmemory 256mb --maxmemory-policy noeviction`). Fixed line 1 typo.
- **Scope Assessment:** 100% Phase 2D Redis infrastructure.
- **Staging Recommendation:** **SAFE FOR WHOLE-FILE STAGING** (`git add docker-compose.yml`).

### 3.9. Target Test Suites (All 4 Untracked)
- `backend/tests/test_phase2d_cross_worker.py`: 100% Phase 2D multi-worker tests. **SAFE FOR WHOLE-FILE STAGING**.
- `backend/tests/test_redis_conversation_history.py`: 100% Phase 2C/2D history tests. **SAFE FOR WHOLE-FILE STAGING**.
- `backend/tests/test_redis_session.py`: 100% Phase 2B/2D session tests. **SAFE FOR WHOLE-FILE STAGING**.
- `backend/tests/test_phase2c2_pipeline_integration.py`: 100% Phase 2C.2 pipeline integration tests. **SAFE FOR WHOLE-FILE STAGING**.

---

## 4. Exact Documentation Inventory

The audit verified that all 12 Phase 2D audit and implementation reports physically exist on disk at their exact paths (no wildcards):

| # | Exact Documentation File Path | Status on Disk | Phase Association |
| :-: | :--- | :---: | :--- |
| 1 | `docs/phase2d_0_cross_worker_reliability_diagnosis.md` | Present (19,165 B) | Phase 2D.0 Architecture Diagnosis |
| 2 | `docs/phase2d_1_multi_worker_implementation_report.md` | Present (15,318 B) | Phase 2D.1 Implementation Report |
| 3 | `docs/phase2d_1_1_independent_acceptance_audit.md` | Present (17,411 B) | Phase 2D.1.1 Independent Audit |
| 4 | `docs/phase2d_2_pagination_cas_diagnosis.md` | Present (19,447 B) | Phase 2D.2 Pagination CAS Diagnosis |
| 5 | `docs/phase2d_2_1_pagination_cas_implementation_report.md` | Present (12,471 B) | Phase 2D.2.1 Pagination CAS Report |
| 6 | `docs/phase2d_2_2_independent_acceptance_audit.md` | Present (17,360 B) | Phase 2D.2.2 Independent Audit |
| 7 | `docs/phase2d_3_real_redis7_deployment_verification.md` | Present (13,079 B) | Phase 2D.3 Local Verification Report |
| 8 | `docs/phase2d_3_1_ci_redis7_feasibility_audit.md` | Present (21,357 B) | Phase 2D.3.1 CI Feasibility Audit |
| 9 | `docs/phase2d_3_2_ci_redis7_implementation_report.md` | Present (10,377 B) | Phase 2D.3.2 CI Workflow Report |
| 10 | `docs/phase2d_3_3_ci_redis7_independent_acceptance_audit.md` | Present (14,488 B) | Phase 2D.3.3 Independent Audit |
| 11 | `docs/phase2d_3_4_github_actions_redis7_acceptance_report.md` | Present (10,992 B) | Phase 2D.3.4 Execution Assessment |
| 12 | `docs/phase2d_3_5_pre_pr_staging_safety_audit.md` | Current Deliverable | Phase 2D.3.5 Staging Safety Audit |

---

## 5. Files That Must Remain Unstaged

To avoid polluting the Phase 2D PR with unrelated development work or breaking CI, the following files must **NOT** be staged:

| Path | Reason for Exclusion |
| :--- | :--- |
| `backend/tests/conftest.py` | **CRITICAL:** Contains `auto_fakeredis` fixture that would override live Redis connections in CI, causing multi-process test failures. |
| `.github/workflows/offline_evaluation.yml` | Belongs to offline semantic evaluation; unrelated to Redis integration. |
| `backend/tests/test_phase4_*` | Phase 4 conversational UX, SSE keepalive, and blocking I/O tests. |
| `backend/tests/test_phase5_*` | Phase 5 multiturn dialogue and intent collision tests. |
| `backend/api/*.py` (employee, employer, profile, search) | Unrelated API layer modifications. |
| `backend/services/*.py` (job_match, llm, search, people_search, profile_parser) | Unrelated search client and parser modifications. |
| `frontend/index.html` | Frontend UI modifications. |
| `README.md`, `.env.example`, `requirements.txt` | Unrelated documentation and environment changes. |

---

## 6. CI-Specific Safety Review

1. **Workflow Location:** `.github/workflows/redis7_integration.yml` is correctly positioned within `.github/workflows/`.
2. **Absence on Remote:** Remote `main` does not yet contain `.github/workflows/redis7_integration.yml`. Pushing to a dedicated feature branch is safe and will not affect production `main`.
3. **No Mocks in Real Tests:** The 10 mandatory real-Redis tests in `test_phase2d_cross_worker.py` and `test_redis_conversation_history.py` remain strictly unchanged and explicitly bypass FakeRedis.
4. **No Secrets / Credentials:** All credentials are `None` or use isolated local loopback (`127.0.0.1:6379`).
5. **No Temporary Artifacts:** Neither `reports/` nor `.pytest_cache/` will be staged.

---

## 7. Ordered PowerShell Staging Runbook

Execute the following commands in PowerShell from the repository root (`C:\UNG\chatbot_Quadrant`):

### Step 1: Create and Switch to Dedicated Verification Branch
```powershell
git checkout -b phase2d-redis7-verification
```

### Step 2: Stage Confirmed Whole-File Components
```powershell
# 1. CI Workflow Specification
git add .github/workflows/redis7_integration.yml

# 2. Redis Core Engine & Legacy Deprecation
git add backend/utils/redis_client.py
git add backend/utils/conversation_history.py
git add backend/services/ranking_service.py
git add docker-compose.yml

# 3. Targeted Test Suites (4 Modules)
git add backend/tests/test_phase2d_cross_worker.py
git add backend/tests/test_redis_conversation_history.py
git add backend/tests/test_redis_session.py
git add backend/tests/test_phase2c2_pipeline_integration.py

# 4. Exact Documentation Reports (12 Files)
git add docs/phase2d_0_cross_worker_reliability_diagnosis.md
git add docs/phase2d_1_multi_worker_implementation_report.md
git add docs/phase2d_1_1_independent_acceptance_audit.md
git add docs/phase2d_2_pagination_cas_diagnosis.md
git add docs/phase2d_2_1_pagination_cas_implementation_report.md
git add docs/phase2d_2_2_independent_acceptance_audit.md
git add docs/phase2d_3_real_redis7_deployment_verification.md
git add docs/phase2d_3_1_ci_redis7_feasibility_audit.md
git add docs/phase2d_3_2_ci_redis7_implementation_report.md
git add docs/phase2d_3_3_ci_redis7_independent_acceptance_audit.md
git add docs/phase2d_3_4_github_actions_redis7_acceptance_report.md
git add docs/phase2d_3_5_pre_pr_staging_safety_audit.md
```

### Step 3: Selectively Stage Mixed Files via Interactive Patch
For files containing both Phase 2D and Phase 4/5 changes, stage only the Redis hunks:

```powershell
# A. Selectively stage Redis configuration in config.py
# Accept (y) the hunk defining REDIS_ENABLED, REDIS_HOST, REDIS_PORT, SESSION_TTL_SECONDS, etc.
# Reject (n) hunks defining SSE_KEEPALIVE_INTERVAL_SECONDS or DB_*_TIMEOUT_SECONDS.
git add -p backend/config/config.py

# B. Selectively stage Redis lifespan & history in main.py
# Accept (y) hunks adding close_redis_pool() in lifespan finally, removing EMPLOYER_SESSIONS imports, and /chat/history endpoints.
# Reject (n) hunks introducing Phase 4 SSE keepalive streaming or threadpool offloading.
git add -p backend/main.py

# C. Selectively stage CAS remerge logic in session_state.py
# Accept (y) hunks for begin_search_generation, claim_next_pagination_page, and set_candidate_search_criteria.
git add -p backend/utils/session_state.py
```
*(Alternative: If the PR scope is accepted as an integrated Phase 2/4 backend branch, `backend/config/config.py`, `backend/main.py`, and `backend/utils/session_state.py` can be added via `git add` provided `backend/schemas/conversation_context.py` is also staged).*

### Step 4: Verify the Staged Changes
Inspect the staged index before committing to ensure zero contamination:
```powershell
# 1. Verify exact staged file count and names
git diff --cached --stat

# 2. Verify absence of trailing whitespace or formatting defects
git diff --cached --check

# 3. Confirm backend/tests/conftest.py is NOT staged
git status --short
```

### Step 5: Commit Phase 2D Changes
```powershell
git commit -m "feat(redis): multi-worker Redis 7 integration, CAS concurrency, and CI verification gate"
```

### Step 6: Push Feature Branch to Remote Origin
```powershell
git push -u origin phase2d-redis7-verification
```

### Step 7: Open Pull Request
Open a Pull Request into `main` at:  
👉 `https://github.com/kun-byte-max/CC-Chatbot-Hybrid-Qdrant/compare`

---

## 8. Pre-Commit Safety Checklist

- [ ] Current branch is `phase2d-redis7-verification` (never commit directly to `main`).
- [ ] `backend/tests/conftest.py` is untracked and **NOT** in the staged index (`git diff --cached --name-only`).
- [ ] No temporary files, test reports (`reports/`), or `.pytest_cache` are staged.
- [ ] No production credentials or secrets are present.
- [ ] Unstaged files from Phase 4/5 remain untouched in the working tree.
- [ ] All 10 mandatory real-Redis tests are included and unmodified.

---

## 9. Audit Confirmation

*This staging safety audit was conducted in strict read-only mode. Zero files were staged, committed, or pushed. Zero working-tree modifications were disturbed.*

