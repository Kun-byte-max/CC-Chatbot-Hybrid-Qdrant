# CollarCheck Phase 2D.3.6.3 — CI Coverage Alignment Report

**Document:** `docs/phase2d_3_6_3_ci_coverage_alignment_report.md`  
**Date:** November 10, 2026  
**Status:** COMPLETE (WORKING TREE ALIGNED — NO FILES STAGED, COMMITTED, OR PUSHED)  
**Target Branch:** `phase2d-redis7-verification`  
**Base Commit:** `07633c1` (`origin/main`)  
**Implementation Purpose:** Update `.github/workflows/redis7_integration.yml` to sequentially execute all five additional test modules required for the Option A Integrated Pipeline PR while strictly preserving the existing four Redis suites and the ten-test live Redis zero-skip gate.

---

## 1. Executive Summary

In Phase 2D.3.6.2, sequential regression testing across nine suites established **208 passed, 10 expected local Redis skips, and zero failures** in isolated processes.

To align GitHub Actions CI coverage with the integrated PR:
1. **`.github/workflows/redis7_integration.yml` was updated** with five sequential execution steps covering the full integrated pipeline, Server-Sent Events (SSE) keepalive streaming, conversation context facade, and query rewriter.
2. **Every suite executes sequentially in its own distinct workflow step** (`python -m pytest <suite> -v --junitxml=reports/<name>.xml`) to prevent state leakage or cross-suite fixture contamination.
3. **The ten-test live Redis 7 deployment gate was preserved with 100% fidelity**:
   - `EXPECTED_CROSS_WORKER_TESTS` (4 tests) and `EXPECTED_CONVERSATION_HISTORY_TESTS` (6 tests) remain mandatory.
   - The zero-skip enforcement and Redis live command counter check (`delta_cmds >= 20`) remain intact.
4. **All five additional test modules were reviewed and verified compatible with CI**:
   - Confirmed 100% passing (`109 passed in 30.94s`) when the local untracked `conftest.py` is bypassed (`-p no:conftest`).
   - Confirmed zero reliance on production credentials, external network APIs, or unbounded wait loops.
5. **Strict Operating Constraints Maintained:**
   - No files were staged or unstaged.
   - No application source files or existing test assertions were modified.
   - No commits or pushes were executed.

---

## 2. Exact Workflow Changes (`.github/workflows/redis7_integration.yml`)

The workflow was updated to insert five sequential test steps immediately following the existing Phase 2C.2 pipeline integration suite and before the live Redis zero-skip gate.

### 2.1 Git Diff Inspection

```diff
diff --git a/.github/workflows/redis7_integration.yml b/.github/workflows/redis7_integration.yml
index 4c71732..76f8253 100644
--- a/.github/workflows/redis7_integration.yml
+++ b/.github/workflows/redis7_integration.yml
@@ -159,6 +159,26 @@ jobs:
         run: |
           python -m pytest backend/tests/test_phase2c2_pipeline_integration.py -v --junitxml=reports/phase2c2_pipeline.xml
 
+      - name: Run SSE Streaming Regression Suite
+        run: |
+          python -m pytest backend/tests/test_sse_streaming.py -v --junitxml=reports/sse_streaming.xml
+
+      - name: Run SSE Keepalive Streaming Suite
+        run: |
+          python -m pytest backend/tests/test_phase5_sse_keepalive.py -v --junitxml=reports/sse_keepalive.xml
+
+      - name: Run SSE Hardening Regression Suite
+        run: |
+          python -m pytest backend/tests/test_phase4_1_sse_hardening.py -v --junitxml=reports/sse_hardening.xml
+
+      - name: Run Conversation Context Facade Suite
+        run: |
+          python -m pytest backend/tests/test_conversation_context.py -v --junitxml=reports/conversation_context.xml
+
+      - name: Run Query Rewriter Regression Suite
+        run: |
+          python -m pytest backend/tests/test_query_rewriter.py -v --junitxml=reports/query_rewriter.xml
+
       - name: Enforce Real-Test Execution, Zero Skips & Non-Substitution Gate
         run: |
           python - << 'EOF'
```

### 2.2 Preservation of Existing Behavior:
- **No changes to Redis service container:** `redis:7.2-alpine`, port 6379, `--health-cmd "redis-cli ping"`.
- **No changes to runtime Redis configuration assertions:** `save = ""` (RAM-only), `appendonly = no`, `maxmemory = 268435456` (256MB), `maxmemory-policy = noeviction`.
- **No changes to environment variables:** `REDIS_ENABLED=true`, `REDIS_HOST=127.0.0.1`, `REDIS_PORT=6379`, `REDIS_DB=1`, `LANGFUSE_ENABLED=false`.
- **No changes to the ten-test live Redis gate:** Lines 171–274 remain strictly identical, asserting that all 10 live Redis integration tests pass with zero skips and that genuine network commands were processed on the live socket.
- **Sequential Execution Guaranteed:** Every test suite runs in its own GitHub Actions step. GitHub Actions executes steps sequentially within a job, launching each in an isolated subshell.

---

## 3. Compatibility Review of the Five Additional Test Modules

Each of the five additional test modules was inspected and validated against the GitHub Actions execution environment:

| Module | Tests | Conftest Independence | Redis Interaction | Credential / API Isolation | Timeout Safety |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `backend/tests/test_sse_streaming.py` | 32 | Verified (no imports or fixture use) | Zero Redis calls (mocks HTTP and LLM stream) | Synthetic JWT tokens signed with test secrets; mocked LLM | Bounded timeout (`timeout=30.0` in mock) |
| `backend/tests/test_phase5_sse_keepalive.py` | 25 | Verified (no imports or fixture use) | Zero Redis calls (mocks pipeline results) | Synthetic tokens; mocked LLM completion streams | Bounded interval floats (`0.01s` test intervals) |
| `backend/tests/test_phase4_1_sse_hardening.py` | 9 | Verified (no imports or fixture use) | Zero Redis calls (mocks endpoints & handlers) | Synthetic tokens; local client disconnect mocks | Bounded async generators |
| `backend/tests/test_conversation_context.py` | 25 | Verified (defines self-contained fixture) | Uses internal scoped FakeRedis fixture; teardown restores `None` | Local test data; no external credentials | Clean synchronous and asynchronous mock loops |
| `backend/tests/test_query_rewriter.py` | 18 | Verified (no imports or fixture use) | Zero Redis calls (unit logic over Pydantic models) | Synthetic prompt strings; no credentials | Pure synchronous parsing algorithms |

### 3.1 Conftest-Bypass Verification Evidence

To verify that the five modules execute without relying on the local untracked `backend/tests/conftest.py`, pytest was executed with `-p no:conftest`:

```powershell
.\venv\Scripts\python -m pytest backend/tests/test_sse_streaming.py backend/tests/test_phase5_sse_keepalive.py backend/tests/test_phase4_1_sse_hardening.py backend/tests/test_conversation_context.py backend/tests/test_query_rewriter.py -p no:conftest
```

**Result:**
```
============================ 109 passed in 30.94s =============================
```

All 109 tests passed with zero skips, zero errors, and zero failures. This confirms complete independence from untracked local fixtures.

---

## 4. Scope Wording & Offloading Clarifications

In accordance with instruction 3, `docs/phase2d_3_6_2_final_integrated_pr_readiness_audit.md` was updated in-place to clarify three critical architectural points:

1. **Explicit Inclusion of `backend/services/query_rewriter.py`:**  
   Section 5.1 and Section 5.2 now explicitly state that `query_rewriter.py` is an active component of Option A (required by `unittest.mock.patch` in `test_phase2c2_pipeline_integration.py` and dynamically invoked in `backend/main.py`), and is distinct from the excluded search/parser services (`employer_search_executor.py`, `hybrid_job_search.py`, `search_router.py`, etc.).
2. **Clarification on `test_phase5_sse_keepalive.py`:**  
   Section 5.1 clarifies that despite its legacy filename prefix `phase5_`, this test module covers the foundational SSE keepalive comment utilities (`format_sse_comment`, `await_with_keepalive`, `stream_with_keepalive`) in `backend/utils/sse.py`, which are part of the integrated SSE implementation. It contains zero Phase 5 dialogue or intent collision tests.
3. **Bounded Claim on Threadpool Offloading Coverage:**  
   Section 2.3 was refined to acknowledge that while `test_phase2c2_pipeline_integration.py` verifies `run_in_threadpool` invocation for user platform token retrieval, full thread-level concurrency isolation for all six offloaded operations was originally tested in `test_phase4_3_blocking_io_offloading.py`. Because that module is excluded due to hanging obsolete MySQL session tests, **we explicitly do not claim complete execution-context test coverage for all offloaded paths in this PR**. That coverage will be formally restored in a focused follow-up task.

---

## 5. Validation Results

### 5.1 YAML Parsing & Structural Inspection

A Python script utilizing PyYAML parsed `.github/workflows/redis7_integration.yml` and verified all job steps:

```powershell
.\venv\Scripts\python -c "import yaml; data = yaml.safe_load(open('.github/workflows/redis7_integration.yml', encoding='utf-8')); steps = [s.get('name') for s in data['jobs']['redis7-integration-gate']['steps']]; print(f'Total steps: {len(steps)}'); [print(f'  - {s}') for s in steps if 'Suite' in s or 'Gate' in s]"
```

**Output:**
```
Total steps: 18
  - Run Phase 2D Cross-Worker Suite
  - Run Phase 2C Redis Conversation History Suite
  - Run Targeted Redis Session Regression Suite
  - Run Targeted Phase 2C.2 Pipeline Integration Regression Suite
  - Run SSE Streaming Regression Suite
  - Run SSE Keepalive Streaming Suite
  - Run SSE Hardening Regression Suite
  - Run Conversation Context Facade Suite
  - Run Query Rewriter Regression Suite
  - Enforce Real-Test Execution, Zero Skips & Non-Substitution Gate
```

### 5.2 Formatting & Diff Check

`git diff --check` was executed across the modified workflow and audit report:

```powershell
git diff --check .github/workflows/redis7_integration.yml docs/phase2d_3_6_2_final_integrated_pr_readiness_audit.md
```

**Output:** 0 whitespace or formatting errors. Clean exit code 0.

### 5.3 Disclaimer on CI Execution

> [!IMPORTANT]
> The workflow file `.github/workflows/redis7_integration.yml` has been updated locally in the working tree. **No commit or push has occurred.** This report validates local syntax, step ordering, and module compatibility; it does not claim a successful remote GitHub Actions run, which can only occur after publishing the branch and triggering the workflow.

---

## 6. Strict Boundaries Confirmation

- **Git Staging State:** Zero files staged or unstaged during this phase. (The 23 previously staged files remain staged in the index).
- **Application Code:** Zero modifications to `backend/main.py` or other application source files.
- **Test Code:** Zero modifications to any existing tests.
- **Ten-Test Gate:** 100% preserved; no assertions weakened or modified.
- **Git Push/Commit:** **None.**

---

## 7. Next Steps

With CI coverage aligned and verified:
1. Review this report.
2. When ready, authorize selective staging of the 15 additional files (including the updated workflow file).
3. Commit the integrated PR on `phase2d-redis7-verification`.
4. Push to remote and observe the 9 sequential suites and the live Redis 7 zero-skip gate run in GitHub Actions.

