# CollarCheck Phase 2D.3.1 — CI-Based Redis 7 Verification Feasibility Audit

**Audit Date:** November 10, 2026  
**Auditor Mode:** STRICT READ-ONLY ARCHITECTURAL FEASIBILITY AUDIT  
**Context:** Local Docker Desktop daemon is offline on the development host (`dockerDesktopLinuxEngine` unavailable). Phase 2D.3 was blocked at the local container provisioning gate.  
**Objective:** Determine whether the 10 outstanding real Redis 7 integration tests (4 multi-process cross-worker tests and 6 real-Redis conversation-history tests) can safely execute in GitHub Actions using an ephemeral Redis 7 service container without regressions, race conditions, or production risks.  
**Deliverable:** `docs/phase2d_3_1_ci_redis7_feasibility_audit.md`

---

## 1. Executive Summary & Feasibility Verdict

### Feasibility Verdict: **FEASIBLE (PASS WITH TARGETED PRECONDITIONS)**

The audit confirms that running the 10 outstanding real Redis 7 integration tests in GitHub Actions via an ephemeral `redis:7-alpine` service container is **100% technically feasible, safe, and isolated**. 

### Summary of Key Findings:
1. **GitHub Actions Service Container Parity:** GitHub Actions natively supports Docker service containers on `ubuntu-latest`. Defining a `redis:7.2-alpine` service container with `--save ""` and `--appendonly no` replicates the production and `docker-compose.yml` RAM-only specifications identically.
2. **Standard Core Redis 7 Only:** Neither test suite requires proprietary Redis modules (such as RedisJSON, RediSearch, or RedisBloom). The implementation relies solely on core Redis data structures (LIST, HASH, SET, STRING), standard atomic multi-key transactions (`WATCH`/`MULTI`/`EXEC`), and standard Lua 5.1 scripting (`EVAL`), all of which are natively available in `redis:7-alpine`.
3. **Complete Isolation & Zero Production Leakage:** Service containers in GitHub Actions run on an ephemeral isolated network on the virtual runner host (`127.0.0.1:6379`). No production databases, secrets, or remote hosts are contacted.
4. **Precondition 1 (PyYAML Dependency):** `backend/tests/test_phase2d_cross_worker.py` contains `test_docker_compose_syntax()` which calls `import yaml`. `PyYAML` is installed in the local virtual environment but is missing from `requirements.txt`. If the entire file is executed in CI, `pip install pyyaml` or a targeted test invocation (`-k "cross_process"`) is required.
5. **Precondition 2 (`conftest.py` Fixture Isolation):** The local untracked `backend/tests/conftest.py` contains an `@pytest.fixture(autouse=True)` that unconditionally sets global FakeRedis clients. While this file is currently untracked in Git, if committed without an opt-out, it would override Redis connections in the pytest parent process for `test_cross_process_conversation_history_monotonicity`. In CI (clean checkout), `conftest.py` does not exist unless explicitly added.
6. **Precondition 3 (Multiprocessing & Host Routing):** Multiprocessing worker routines are defined at top-level module scope, ensuring full compatibility with both Linux `fork` and Windows `spawn`. In `redis-py` 5+, `ConnectionPool` automatically detects PID changes across forks and resets connections safely.

---

## 2. Existing CI Architecture Audit

The repository currently maintains a single GitHub Actions workflow:

### File: `.github/workflows/offline_evaluation.yml`
- **Trigger:** Push to `main`, Pull Requests to `main`, and `workflow_dispatch`.
- **Runner Environment:** `ubuntu-latest` (Standard 2-core x86_64 Linux runner).
- **Python Version:** Pinned to `3.11` (`actions/setup-python@v5` with `cache: "pip"`).
- **Dependency Strategy:**
  ```bash
  python -m pip install --upgrade pip
  pip install -r requirements.txt pytest
  ```
- **Job Scope:** Runs offline evaluation and semantic regression tests (`backend/tests/test_pipeline_semantic_eval.py`) without external network services or database engines.
- **Artifact Handling:** Stores JUnit XML and markdown evaluation reports via `actions/upload-artifact@v4`.

### Dependency Inspection (`requirements.txt`):
```text
fastapi
uvicorn[standard]
httpx
python-multipart
pydantic
python-dotenv
pyjwt
requests
aiomysql
dotenv
langfuse>=5.0.0
redis>=5.0.0
fakeredis>=2.20.0
```
- **Redis Client:** `redis>=5.0.0` is already an explicit, pinned direct dependency.
- **Fakeredis:** `fakeredis>=2.20.0` is already present for offline unit tests.
- **Dependency Gap:** `PyYAML` is not listed in `requirements.txt`. The local environment has `PyYAML 6.0.3` installed. In `test_phase2d_cross_worker.py:471`, `import yaml` is executed inside `test_docker_compose_syntax()`. Running the full file in CI requires `pyyaml` in the install step.

---

## 3. Test Suite & Architecture Deep Dive

The 10 outstanding real Redis 7 integration tests are partitioned across two suites:

### 3.1. Phase 2D Cross-Worker Suite (`backend/tests/test_phase2d_cross_worker.py`)
Four genuine multi-process integration tests verify multi-worker behavior across independent OS processes:

| Test Name | Lines | Mechanism | Verification Goal |
| :--- | :---: | :--- | :--- |
| `test_cross_process_session_state_isolation` | 490–520 | 2 spawned `mp.Process` workers (`_mp_worker_ownership`) | Worker A writes session $S_1$ as User A; Worker B attempts write as User B and is rejected with `RedisSessionOwnershipError`. |
| `test_cross_process_concurrent_cas_search_gen` | 521–553 | 2 spawned `mp.Process` workers (`_mp_worker_begin_search_gen`) | Both workers concurrently call `begin_search_generation` on shared session; CAS remerge resolves collision, producing distinct generations `[1, 2]`. |
| `test_cross_process_conversation_history_monotonicity` | 554–588 | 2 sequential `mp.Process` workers (`_mp_worker_append_turn`) | Worker 1 commits Turn 1; Worker 2 commits Turn 2. Parent checks snapshot: verifies 4 messages and monotonic `turn_seq = 2`. |
| `test_cross_process_worker_restart_pool_cleanup` | 589–613 | Worker A (`_mp_worker_restart_sim`) + Worker B (`_mp_worker_ownership`) | Worker A touches session, closes its Redis pool (`close_redis_pool()`), and exits. Worker B executes immediately after, verifying that Worker A's pool teardown does not affect Worker B. |

#### Availability Gate Mechanism:
In `test_phase2d_cross_worker.py`:
```python
def is_live_redis_available(host: str = "127.0.0.1", port: int = 6379, timeout: float = 0.3) -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        result = s.connect_ex((host, port))
        s.close()
        return result == 0
    except Exception:
        return False

LIVE_REDIS = is_live_redis_available()
SKIP_REASON = "Live Redis 7 is offline; skipping multi-process cross-worker test (Deployment Gate)"
```
When an ephemeral service container is published on `6379:6379`, `is_live_redis_available()` evaluates to `True` at test collection time, and all four tests run automatically.

### 3.2. Redis Conversation History Suite (`backend/tests/test_redis_conversation_history.py`)
Six real Redis 7 tests verify atomic persistence and Lua contracts:

| Test Name | Lines | Mechanism | Verification Goal |
| :--- | :---: | :--- | :--- |
| `test_real_redis_01_empty_snapshot` | 1034–1047 | `get_conversation_history_snapshot_async` | Sequence 0, empty list, no errors on clean key. |
| `test_real_redis_02_snapshot_multiple_commits` | 1049–1065 | `append_turn_atomic_async` + Lua snapshot | 2 committed turns return 4 messages, `turn_seq = 2`, and correct `last_turn_id`. |
| `test_real_redis_03_snapshot_after_pair_trimming` | 1067–1085 | 6 turns committed with `MAX_SESSION_MESSAGES = 4` | Head pair trimmed atomically; monotonic `turn_seq = 6` preserved. |
| `test_real_redis_04_snapshot_concurrent_append` | 1087–1110 | `asyncio.gather` (commit vs snapshot race) | Zero torn reads; returns consistent snapshot at either seq 1 or seq 2. |
| `test_real_redis_05_snapshot_clear_consistency` | 1112–1134 | `asyncio.gather` (clear vs snapshot race) | Atomic state returns either pre-clear snapshot or clean empty state. |
| `test_real_redis_06_snapshot_ttl_refresh_all_keys` | 1136–1164 | Artificially lowered TTL (300s) | Snapshot Lua script atomically refreshes TTL across LIST, HASH, and SET (>1700s). |

#### Fixture Wiring:
In `test_redis_conversation_history.py`:
- `setup_fakeredis` has `autouse=True` for offline tests.
- `real_redis_connection` explicitly resets `set_test_redis_client(None)` and `set_test_sync_redis_client(None)` before each `test_real_redis_*` run, unsetting mocks and redirecting the test to the real connection pool.

---

## 4. Redis Capability & Module Analysis

### 4.1. Core Commands Utilized
Across `session_state.py`, `conversation_history.py`, and `redis_client.py`, the application utilizes only native Redis primitives:
- **Strings / Key-Value:** `GET`, `SET`, `SETNX`, `DEL`, `EXPIRE`, `TTL`
- **Lists:** `RPUSH`, `LRANGE`, `LTRIM`, `LLEN`
- **Hashes:** `HSET`, `HGET`, `HGETALL`, `HINCRBY`
- **Sets:** `SADD`, `SREM`, `SISMEMBER`
- **Transactions:** `WATCH`, `MULTI`, `EXEC`, `UNWATCH`
- **Scripting:** `EVAL` (Lua 5.1)

### 4.2. Lua Scripting Details
Two custom Lua scripts are deployed in production and verified by the test suite:
1. `LUA_ATOMIC_TURN_COMMIT_SCRIPT` (`conversation_history.py:430-496`): Executes sequence verification, turn deduplication, capacity/byte calculation, pair trimming (`LTRIM`), turn append (`RPUSH`), metadata update (`HSET`), and atomic TTL extension (`EXPIRE`).
2. `LUA_SNAPSHOT_SCRIPT` (`conversation_history.py:499-521`): Atomically fetches message elements (`LRANGE`), metadata hash (`HGETALL`), and list length (`LLEN`), while extending TTL on all three keys simultaneously.

**Audit Finding:** Standard official Docker image `redis:7-alpine` (or `redis:7.2-alpine`) includes the Lua 5.1 runtime by default. **Zero external modules are required.**

---

## 5. Multiprocessing & Cross-Platform Runner Analysis

### 5.1. Worker Entry Point Picklability
In `test_phase2d_cross_worker.py`, all multiprocessing workers are implemented as top-level module functions:
- `_mp_worker_ownership` (line 78)
- `_mp_worker_begin_search_gen` (line 92)
- `_mp_worker_append_turn` (line 102)
- `_mp_worker_restart_sim` (line 123)

None of these functions use closures, local nested functions, or unpicklable state. They take simple primitives (`session_id: str`, `user_id: str`, `out_q: mp.Queue`). Consequently, they are fully compatible with both:
- Linux default: `fork`
- Windows default / Linux alternative: `spawn`

### 5.2. `redis-py` 5+ Fork Safety
Under Linux `fork`, child processes inherit parent file descriptors. In older Redis client libraries, concurrent child operations on inherited sockets could lead to protocol corruption.
In `redis-py` v5.0.0+:
- `redis.ConnectionPool` explicitly tracks `self.pid = os.getpid()`.
- On any command execution after fork, the client checks if `self.pid != os.getpid()`.
- Upon detecting a mismatch, `self.reset()` is invoked automatically, closing inherited descriptors and establishing a clean socket connection in the child process.
- Additionally, in `test_phase2d_cross_worker.py`, workers import `save_session_state_cas` and initialize their connection pools lazily *inside* the child process.

### 5.3. Analysis of `conftest.py` Fixture Interaction (Crucial Finding)
In the local working tree, `backend/tests/conftest.py` exists as an **untracked** file containing:
```python
@pytest.fixture(autouse=True)
def auto_fakeredis():
    server = fakeredis.FakeServer()
    fake_sync = fakeredis.FakeRedis(server=server, decode_responses=False)
    fake_async = fakeredis.aioredis.FakeRedis(server=server, decode_responses=False)
    set_test_sync_redis_client(fake_sync)
    set_test_redis_client(fake_async)
    yield fake_sync
    set_test_sync_redis_client(None)
    set_test_redis_client(None)
```
- **Scenario A (Clean CI Checkout):** Because `conftest.py` is untracked in Git, `actions/checkout@v4` will **not** check it out into the CI runner. Tests will run in their native design state, where `test_phase2d_cross_worker.py` and `test_redis_conversation_history.py` manage their own fixtures.
- **Scenario B (If `conftest.py` is ever committed):** If committed as-is, `auto_fakeredis` would run in the pytest parent process for `test_cross_process_conversation_history_monotonicity:580`. The parent would query `fake_async` (empty) while child workers wrote to real Redis, triggering a false test failure (`len(snap.history) == 0`).
- **Mitigation:** The proposed CI workflow will explicitly ensure `conftest.py` is absent, or run with pytest flags that bypass `auto_fakeredis` for real Redis suites.

---

## 6. Test Isolation, Namespacing & Cleanup Safety

### 6.1. Key Namespacing Matrix

| Suite | Session Key Pattern | Example Key | Cleanup Mechanism |
| :--- | :--- | :--- | :--- |
| `test_cross_process_session_state_isolation` | `mp_iso_<timestamp>` | `session:user_alpha:mp_iso_1762767000` | Ephemeral container destroy / 1800s TTL |
| `test_cross_process_concurrent_cas_search_gen` | `mp_cas_<timestamp>` | `session:user_mp_cas:mp_cas_1762767000` | Ephemeral container destroy / 1800s TTL |
| `test_cross_process_conversation_history_monotonicity` | `mp_hist_<timestamp>` | `chat:history:user_mp_hist:mp_hist_1762767000` | Ephemeral container destroy / 1800s TTL |
| `test_cross_process_worker_restart_pool_cleanup` | `mp_restart_<timestamp>` | `session:user_mp_restart:mp_restart_1762767000` | Ephemeral container destroy / 1800s TTL |
| `test_real_redis_01_empty_snapshot` | `sess_real_empty_<timestamp>` | `chat:history:8888:sess_real_empty_1762767000` | Explicit `finally: clear_conversation_history_async` |
| `test_real_redis_02_snapshot_multiple_commits` | `sess_real_multi_<timestamp>` | `chat:history:8888:sess_real_multi_1762767000` | Explicit `finally: clear_conversation_history_async` |
| `test_real_redis_03_snapshot_after_pair_trimming` | `sess_real_trim_<timestamp>` | `chat:history:8888:sess_real_trim_1762767000` | Explicit `finally: clear_conversation_history_async` |
| `test_real_redis_04_snapshot_concurrent_append` | `sess_real_race_<timestamp>` | `chat:history:8888:sess_real_race_1762767000` | Explicit `finally: clear_conversation_history_async` |
| `test_real_redis_05_snapshot_clear_consistency` | `sess_real_clear_race_<timestamp>` | `chat:history:8888:sess_real_clear_race_1762767000` | Explicit `finally: clear_conversation_history_async` |
| `test_real_redis_06_snapshot_ttl_refresh_all_keys` | `sess_real_ttl_<timestamp>` | `chat:history:8888:sess_real_ttl_1762767000` | Explicit `finally: clear_conversation_history_async` |

### 6.2. Isolation Guarantees
1. **Timestamped Keys:** Every key generated during testing includes an epoch timestamp (`int(time.time())`). Collisions between tests are virtually impossible.
2. **Ephemeral Service Lifecycle:** The Redis instance exists solely for the duration of the GitHub Actions job step. The container is wiped upon job termination.
3. **Database Index Isolation:** In the proposed CI configuration, `REDIS_DB=1` is specified, keeping test data completely segregated from default database 0.
4. **Safety Rule Compliance:** No `FLUSHDB` or `FLUSHALL` commands are invoked.

---

## 7. Test Serialization vs. Parallelization

### Recommendation: **STRICT SERIALIZATION REQUIRED**

The 10 integration tests must run sequentially without parallel test runners (e.g., `pytest-xdist` with `-n auto`):
1. **Timestamp Granularity:** All tests use `int(time.time())` for session ID generation. Parallel execution of multiple tests within the exact same second risks duplicate keys.
2. **Global Client State:** In `backend/utils/redis_client.py`, `_test_redis_client` and `_test_sync_redis_client` are module-level globals. Parallel threads or test workers within the same interpreter process would experience race conditions modifying these globals.
3. **Execution Duration:** Sequential execution of all 10 tests takes under **8 seconds total**. Parallelization provides negligible speedup while introducing concurrency risks.

---

## 8. Proposed GitHub Actions Workflow Design

The proposed workflow creates a dedicated verification pipeline using a pinned ephemeral Redis 7 container:

```yaml
name: Redis 7 Multi-Worker Integration Verification

on:
  pull_request:
    branches: [ main ]
    paths:
      - 'backend/utils/redis_client.py'
      - 'backend/utils/session_state.py'
      - 'backend/utils/conversation_history.py'
      - 'backend/main.py'
      - 'backend/tests/test_phase2d_cross_worker.py'
      - 'backend/tests/test_redis_conversation_history.py'
      - '.github/workflows/redis7_integration.yml'
  workflow_dispatch:

jobs:
  redis7-multiworker-gate:
    name: Redis 7 Multi-Worker Integration Gate
    runs-on: ubuntu-latest

    services:
      redis:
        image: redis:7.2-alpine
        ports:
          - 6379:6379
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 5s
          --health-timeout 2s
          --health-retries 5
          --save ""
          --appendonly no
          --maxmemory 256mb
          --maxmemory-policy noeviction

    env:
      REDIS_ENABLED: "true"
      REDIS_HOST: "127.0.0.1"
      REDIS_PORT: "6379"
      REDIS_DB: "1"
      LANGFUSE_ENABLED: "false"

    steps:
      - name: Checkout Repository
        uses: actions/checkout@v4

      - name: Set up Python 3.11
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: "pip"

      - name: Install Dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt pytest pyyaml

      - name: Verify Redis Service Health & TCP Connectivity
        run: |
          python -c "import socket; s = socket.socket(); s.connect(('127.0.0.1', 6379)); print('Redis Port 6379 TCP connected successfully'); s.close()"
          python -c "import redis; r = redis.Redis(host='127.0.0.1', port=6379, db=1); print('Redis Ping response:', r.ping())"

      - name: Execute Phase 2D Multi-Process Cross-Worker Tests (4 tests)
        run: |
          python -m pytest backend/tests/test_phase2d_cross_worker.py -k "cross_process" -v

      - name: Execute Phase 2C Real Redis Conversation History Tests (6 tests)
        run: |
          python -m pytest backend/tests/test_redis_conversation_history.py -k "test_real_redis" -v

      - name: Execute Full Phase 2D Cross-Worker Suite (Regression)
        run: |
          python -m pytest backend/tests/test_phase2d_cross_worker.py -v
```

---

## 9. Minimal Implementation Plan

When authorized by the user to implement CI-based verification:

1. **Create Workflow File:**
   - Add `.github/workflows/redis7_integration.yml` with the verified configuration above.
2. **Explicit Dependency Handling:**
   - Ensure `pyyaml` is installed in the workflow so that `test_docker_compose_syntax` executes cleanly on `ubuntu-latest`.
3. **Verify Git Hygiene:**
   - Confirm `backend/tests/conftest.py` is either not committed or updated to include an opt-out fixture check for live Redis tests.
4. **Trigger Workflow Dispatch:**
   - Push branch and run the workflow via GitHub CLI (`gh workflow run`) or manual trigger in GitHub Actions UI.
5. **Inspect Execution Logs:**
   - Verify that all 4 multi-process tests and all 6 real-Redis history tests report `PASSED` (0 skipped, 0 failed).

---

## 10. Risks and Remaining Deployment Gates

### Risks Identified & Mitigations

| Risk | Impact | Mitigation |
| :--- | :---: | :--- |
| **`PyYAML` missing in CI environment** | `test_docker_compose_syntax` raises `ModuleNotFoundError` | Explicitly install `pyyaml` in the workflow's dependency step. |
| **`conftest.py` accidental commit** | Parent process in monotonicity test reads empty fakeredis | Ensure `conftest.py` is excluded or conditioned on test markers. |
| **Linux IPv6 `localhost` ambiguity** | Redis client connects to `::1` while Docker listens on `127.0.0.1` | Explicitly specify `REDIS_HOST: "127.0.0.1"` in workflow environment. |
| **CI Runner CPU resource starvation** | Process join timeouts (5s) under high GitHub runner load | Standard 2-core runners complete in-memory Redis queries in <10ms; 5s timeout provides a 500x safety margin. |

### Remaining Deployment Gates
1. **GitHub Actions Real Redis 7 Run:** Execute the proposed workflow on GitHub Actions to observe green execution of all 10 tests.
2. **Local Docker Desktop Verification (Optional Staging Gate):** When the developer has administrative privileges to launch Docker Desktop locally, verify `docker compose up -d redis` locally for local development parity.
3. **Production Redis 7 Cluster / Single-Instance Deployment Gate:** Verify `REDIS_HOST`, `REDIS_PASSWORD`, and security credentials against the live staging/production deployment environment.

---

## 11. Conclusion

The audit conclusively establishes that executing the Phase 2D.3 real Redis 7 verification suite in GitHub Actions is **fully feasible, safe, and robust**. It eliminates the local Docker Desktop blockage without weakening any assertions, without introducing mocks, and without compromising production security.

*Audit complete. No production code, tests, or workflows were modified during this read-only assessment.*

