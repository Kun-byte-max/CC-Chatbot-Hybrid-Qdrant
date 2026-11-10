# CollarCheck Phase 2D.2 — Pagination CAS and Collision-Test Diagnosis

**Date:** November 10, 2026  
**Auditor Mode:** STRICTLY READ-ONLY DIAGNOSIS  
**Status:** COMPLETE  
**Deliverable:** `docs/phase2d_2_pagination_cas_diagnosis.md`

---

## 1. Executive Verdict

### Verdict: **PASS WITH FOLLOW-UPS**

Neither finding blocks system stability or causes database/memory corruption, but both represent identifiable correctness and verification gaps under concurrent multi-worker operations:

1. **F-01 (Collision Test Rigor):** Low production risk, but medium verification gap. `begin_search_generation()` is correctly implemented with `remerge_fn=_remerge_gen`, but its unit test in `backend/tests/test_phase2d_cross_worker.py:175-206` executes purely sequential calls. As a result, the `_remerge_gen` callback is never executed during that test.
2. **F-02 (`claim_next_pagination_page` Missing Remerge):** Medium production risk. It does **not** corrupt Redis or overwrite unrelated fields because `save_session_state_cas()` strictly enforces version sequencing (`current_ver > exp_ver`) and aborts stale writes. However, it causes **silent write loss**: when a conflict occurs, `save_session_state_cas` returns `False`, but `claim_next_pagination_page` ignores the boolean return value and unconditionally returns `"success": True`. The user is served the incremented page in-memory for that turn, but Redis remains on the old page; on the subsequent turn, the user receives the duplicate page.
3. **Additional Finding F-04 (`set_candidate_search_criteria` Missing Remerge):** Low/Medium risk. `set_candidate_search_criteria` in `session_state.py:1608-1614` also calls `save_session_state_cas` without `remerge_fn`, causing candidate criteria updates to be dropped under CAS contention.

---

## 2. File and Line Reference Inventory

| Component | File Path | Line Range | Role & Relevant Behavior |
| :--- | :--- | :--- | :--- |
| `claim_next_pagination_page` | `backend/utils/session_state.py` | 1861–1933 | Computes next page and calls `save_session_state_cas` without `remerge_fn` (line 1924); ignores return value and hardcodes `"success": True` (line 1927). |
| `handle_pagination_command` | `backend/utils/session_state.py` | 1805–1859 | Reference implementation for pagination CAS; defines `apply_pagination` and correctly passes `remerge_fn=apply_pagination` (line 1851). |
| `save_session_state_cas` | `backend/utils/session_state.py` | 1216–1335 | Synchronous CAS optimistic locking implementation using Redis `WATCH / MULTI / EXEC`. Enforces `current_ver > exp_ver` check (lines 1262–1275) and handles `WatchError` (lines 1304–1312). |
| `save_session_state_cas_async` | `backend/utils/session_state.py` | 1337–1440 | Asynchronous counterpart to sync CAS; identical remerge and version check logic (lines 1383–1396 and 1424–1432). |
| `set_candidate_search_criteria` | `backend/utils/session_state.py` | 1608–1614 | Call site lacking `remerge_fn` when saving candidate search criteria. |
| `begin_search_generation` | `backend/utils/session_state.py` | 87–126 | Upgraded in Phase 2D.1 with `_remerge_gen` and closure tracking. |
| `test_begin_search_generation_remerge_on_cas_conflict` | `backend/tests/test_phase2d_cross_worker.py` | 175–206 | Unit test intended to verify CAS conflict, but runs sequential increments without injecting a conflict. |

---

## 3. Audit 1 — Actual CAS Retry Semantics

### Step-by-Step Execution Trace of `save_session_state_cas`

When a function calls `save_session_state_cas(session_id, payload, role=..., remerge_fn=...)`:

```
1. Attempt loop starts (range(max_retries = 3)):
   a. pipe = client.pipeline()
   b. pipe.watch(rkey)
   c. raw = pipe.get(rkey) -> existing = json.loads(raw)
   d. current_ver = existing["version_seq"] (e.g. 5)
   e. exp_ver = payload["version_seq"] (e.g. 4)
   
2. Version comparison check:
   if existing and current_ver > exp_ver:
       pipe.unwatch()
       if remerge_fn and callable(remerge_fn):
           current_payload = remerge_fn(existing)
           continue  --> [RETRY ATTEMPT with fresh state]
       else:
           log.warning("CAS rejected stale write...")
           return False  --> [ABORT WITHOUT WRITE]

3. If version matches (current_ver == exp_ver):
   a. write_payload["version_seq"] = current_ver + 1
   b. pipe.multi()
   c. pipe.set(rkey, serialized, ex=SESSION_TTL)
   d. pipe.execute()
      ├── SUCCESS: returns True
      └── WatchError (another client committed during transaction):
          if remerge_fn and callable(remerge_fn):
              fresh_raw = client.get(rkey)
              if fresh_raw:
                  current_payload = remerge_fn(json.loads(fresh_raw))
          continue  --> [RETRY ATTEMPT with updated payload]
```

### Answers to Core Diagnosis Questions

1. **What state is read before `advance_search_pagination()` (i.e. `claim_next_pagination_page`) calls CAS?**  
   At line 1885, `state = get_session_state(session_id, role=role, user_id=user_id)` reads the complete Redis session record as a dictionary at that instant, including `version_seq`, `search_gen`, `pagination`, filters, and metadata.
2. **What happens when Redis raises `WatchError`?**  
   `save_session_state_cas` catches `WatchError` at line 1304. Because `remerge_fn` is `None`, lines 1305–1311 are bypassed. It executes `continue` to attempt 2. In attempt 2, `pipe.get(rkey)` loads the newly committed record where `current_ver` is higher than `payload["version_seq"]`. The check at line 1265 triggers: since `remerge_fn` is `None`, it unwatches and immediately returns `False`.
3. **Does the retry reload the latest Redis state?**  
   Yes. The retry executes `pipe.watch(rkey)` and `raw = pipe.get(rkey)`, successfully loading the latest state from Redis into `existing`.
4. **Without `remerge_fn`, can the retry overwrite concurrent changes?**  
   **No.** Lines 1265–1275 explicitly reject any write where `current_ver > exp_ver` when `remerge_fn` is missing. Stale state is never written to Redis.
5. **Can pagination advancement lose another worker's pagination update?**  
   No, it will not overwrite another worker's update. However, **its own pagination update is silently dropped**: `save_session_state_cas` returns `False`, but `claim_next_pagination_page` ignores this return value and reports `"success": True`.
6. **Can it overwrite unrelated fields (filters, employer context, generation)?**  
   **No.** Because the stale write is rejected by CAS, neither pagination nor unrelated fields can overwrite concurrent commits.
7. **Does behavior differ between sync and async CAS?**  
   **No.** Lines 1386–1396 and 1424–1432 of `save_session_state_cas_async` implement identical version-comparison, rejection, and `WatchError` logic.
8. **Does retry exhaustion fail safely?**  
   Inside `save_session_state_cas`, retry exhaustion returns `False` and logs a warning. However, inside `claim_next_pagination_page`, line 1924 ignores the return value:
   ```python
   res_state = dict(state)
   res_state["pagination"] = pag
   save_session_state_cas(session_id, res_state, role=role, user_id=user_id)  # <-- Return value ignored!
   return {
       "success": True,  # <-- Hardcoded True!
       "claimed_page": claimed_page,
       ...
   }
   ```
   This causes a **silent failure** at the API layer.

---

## 4. Audit 2 — Survey of Existing Remerge Implementations

A codebase-wide scan of all 22 calls to `save_session_state_cas` in `backend/utils/session_state.py` revealed:

| Line | Function | Has `remerge_fn`? | Behavior on CAS Conflict |
| :--- | :--- | :---: | :--- |
| 113 | `begin_search_generation` | **YES** (`_remerge_gen`) | Reads `fresh_state["search_gen"] + 1`, updates closure tracker, preserves all fields. |
| 722 | `set_search_context` | **YES** (`remerge_ctx`) | Re-applies `search_context` onto fresh state. |
| 744 | `set_employer_search_context` | **YES** (`remerge_emp_ctx`) | Re-applies employer requirements onto fresh state. |
| 776 | `set_search_snapshot` | **YES** (`remerge_snap`) | Re-applies `search_snapshot` onto fresh state. |
| 901 | `set_pagination_state` | **YES** (`remerge_pag`) | Validates `search_gen`, checks `query_fingerprint`, guards monotonic cursor. |
| 928 | `set_language_preference` | **YES** (`remerge_lang`) | Re-applies `language_preference` onto fresh state. |
| 965 | `update_employer_session_cas` | **YES** (`remerge_emp`) | Re-merges employer state machine onto fresh state. |
| 981 | `clear_employer_requirements` | **YES** (`remerge_clear`) | Sets `employer_requirements = None` on fresh state. |
| 996 | `set_pending_requested_level` | **YES** (`remerge_level`) | Re-applies pending level onto fresh state. |
| 1024 | `clear_pending_requested_level` | **YES** (`remerge_clear_lvl`) | Clears pending level on fresh state. |
| 1071 | `set_pending_option_mapping` | **YES** (`remerge_opt`) | Re-applies option mapping onto fresh state. |
| 1105 | `clear_pending_option_mapping` | **YES** (`remerge_clear_opt`) | Clears option mapping on fresh state. |
| 1178 | `set_recommendation_session` | **YES** (`remerge_rec`) | Re-applies recommendation context onto fresh state. |
| 1195 | `set_last_referenced_job_id` | **YES** (`remerge_ref`) | Re-applies last job ID onto fresh state. |
| 1213 | `set_session_state` | **YES** (`remerge_full`) | Full dictionary merge onto fresh state. |
| 1503 | `set_selected_employment_context` | **YES** (`remerge_fn`) | Re-applies selected record onto fresh state. |
| 1561 | `clear_selected_employment_context` | **YES** (`remerge_fn`) | Clears selected record on fresh state. |
| 1581 | `clear_pagination_state` | **YES** (`remerge_clear_pag`) | Sets `pagination = None` on fresh state. |
| 1613 | `set_candidate_search_criteria` | **NO** | **Missing `remerge_fn`**; criteria update dropped on conflict. |
| 1793 | `reset_search_context` | **YES** (`apply_reset`) | Re-applies reset onto fresh state. |
| 1847 | `handle_pagination_command` | **YES** (`apply_pagination`) | Calculates new page against fresh state's `total_pages`. |
| 1924 | `claim_next_pagination_page` | **NO** | **Missing `remerge_fn`**; pagination advance dropped on conflict. |

**Key Finding:** 20 out of 22 call sites correctly supply `remerge_fn`. Only `claim_next_pagination_page` (line 1924) and `set_candidate_search_criteria` (line 1613) lack remerge protection.

---

## 5. Audit 3 — F-01 Deterministic Collision-Test Design

### Why the Existing Test Fails to Test Contention
`backend/tests/test_phase2d_cross_worker.py:175-206`:
1. Seeds Redis with `search_gen: 2` (version 1).
2. Calls `begin_search_generation()` sequentially.
3. `begin_search_generation()` reads version 1, attempts CAS with expected version 1.
4. Redis key was not modified, so version 1 matches version 1. CAS succeeds on attempt 0.
5. `_remerge_gen` is never invoked, and `WatchError` is never raised.

### Recommended Deterministic Test Architecture

To test the remerge logic deterministically without flaky asynchronous timing, use pipeline execution hooking:

```python
def test_begin_search_generation_deterministic_cas_conflict(fakeredis_setup):
    """
    Deterministically simulates a concurrent commit during begin_search_generation,
    verifying that WatchError triggers _remerge_gen against the freshest Redis state.
    """
    server, fake_sync, _ = fakeredis_setup
    sid = "test_cas_deterministic_session"
    uid = "user_deterministic_100"

    # Step 1: Seed session with initial generation 2 and an unrelated filter
    from backend.utils.session_state import save_session_state_cas, get_session_state, begin_search_generation
    save_session_state_cas(
        session_id=sid,
        state_dict={"search_gen": 2, "filters": {"department": "ICU"}},
        user_id=uid,
    )

    # Step 2: Hook the Redis pipeline's execute to simulate a competing write on attempt 0
    original_pipeline = fake_sync.pipeline
    attempt_count = [0]

    def _competing_pipeline(*args, **kwargs):
        pipe = original_pipeline(*args, **kwargs)
        real_execute = pipe.execute

        def _execute_hook():
            attempt_count[0] += 1
            if attempt_count[0] == 1:
                # Competing worker updates Redis directly before attempt 0 commits
                competing_state = {
                    "search_gen": 5,
                    "filters": {"department": "ICU", "shift": "night"},
                    "version_seq": 2,
                    "user_id": uid,
                    "session_id": sid,
                }
                from backend.utils.session_state import to_redis_key, make_composite_key
                rkey = to_redis_key(make_composite_key(sid, user_id=uid), user_id=uid, session_id=sid)
                import json
                fake_sync.set(rkey, json.dumps(competing_state))
                # Trigger real Redis WatchError on attempt 0
                from redis.exceptions import WatchError
                raise WatchError("Simulated concurrent commit by competing worker")
            return real_execute()

        pipe.execute = _execute_hook
        return pipe

    fake_sync.pipeline = _competing_pipeline

    # Step 3: Call begin_search_generation
    # Expectation:
    # 1. Attempt 0 fails with WatchError
    # 2. _remerge_gen receives competing_state where search_gen == 5
    # 3. Next generation is computed as 5 + 1 = 6
    # 4. Attempt 1 succeeds and commits search_gen == 6
    gen = begin_search_generation(session_id=sid, user_id=uid)

    assert attempt_count[0] >= 2, "Test did not trigger retry"
    assert gen == 6, f"Expected generation 6 after remerge, got {gen}"

    # Step 4: Verify persisted state in Redis
    final_state = get_session_state(session_id=sid, user_id=uid)
    assert final_state.get("search_gen") == 6
    # Verify unrelated concurrent updates were preserved
    assert final_state.get("filters") == {"department": "ICU", "shift": "night"}
```

**Verification Properties Guaranteed:**
- Injects a real `redis.exceptions.WatchError`.
- Proves `fresh_state` from the competing worker is supplied to `_remerge_gen`.
- Proves generation increments monotonically (`5 -> 6`).
- Proves unrelated fields committed by the competing worker (`shift: night`) are preserved.

---

## 6. Audit 4 — F-02 Concrete Concurrency Failure Scenario

### Concrete Concurrency Example:

```
Timeline:
  T0: User performs search for "software engineer" -> Page 1 displayed (total 3 pages).
      Redis session holds: current_page = 1, total_pages = 3, version_seq = 4.

  T1: User clicks "Next Page". Worker A receives request.
      Worker A reads state at T1: current_page = 1, version_seq = 4.
      Worker A computes claimed_page = 2.

  T2: Simultaneously, a background task or SSE turn commits session state (e.g. saves telemetry or search criteria).
      Worker B commits version_seq = 5 to Redis.

  T3: Worker A executes save_session_state_cas(..., {pagination: {current_page: 2}, version_seq: 4}, ...).
      - Redis checks: current_ver (5) > exp_ver (4).
      - remerge_fn is None!
      - save_session_state_cas rejects write and returns False.

  T4: Worker A's claim_next_pagination_page ignores False:
      return {"success": True, "claimed_page": 2, "saved_p_state": {current_page: 2}}

  T5: Worker A returns Page 2 items to the user. User sees Page 2.
      CRITICAL DEFECT: Redis STILL contains current_page = 1 (from version 5)!

  T6: User reads Page 2 and clicks "Next Page" again. Worker C receives request.
      Worker C reads Redis: current_page = 1!
      Worker C computes 1 + 1 = 2!
      Worker C returns Page 2 AGAIN instead of Page 3!
```

### Proposed Minimal Remediation for `claim_next_pagination_page`:

```python
def claim_next_pagination_page(
    session_id: str,
    command: str,
    role: Optional[str] = None,
    user_id: Optional[Union[str, int]] = None,
) -> Dict[str, Any]:
    key = make_composite_key(session_id, role, user_id=user_id)
    if not key:
        return {"success": False, "error": "Invalid session identifier"}

    with get_session_lock(key):
        state = get_session_state(session_id, role=role, user_id=user_id)
        pag = dict(state.get("pagination") or {})
        if not pag:
            return {"success": False, "error": "No active pagination state found"}

        claimed_page_box = [1]
        is_oob_box = [False]
        saved_pag_box = [{}]

        def _remerge_claim(fresh_state: Dict[str, Any]) -> Dict[str, Any]:
            merged = dict(fresh_state)
            fresh_pag = dict(merged.get("pagination") or {})
            if not fresh_pag:
                return merged
            curr_p = int(fresh_pag.get("current_page", 1))
            tot_p = int(fresh_pag.get("total_pages", 1))
            cmd_norm = str(command).strip().lower()
            if "next" in cmd_norm:
                c_p = curr_p + 1
            elif "prev" in cmd_norm:
                c_p = max(curr_p - 1, 1)
            elif "page" in cmd_norm:
                m = re.search(r"page\s*(\d+)", cmd_norm)
                c_p = max(int(m.group(1)), 1) if m else curr_p
            else:
                c_p = curr_p
            oob = c_p > tot_p
            if oob:
                c_p = max(tot_p + 1, 1)
            fresh_pag["current_page"] = c_p
            fresh_pag["has_next"] = c_p < tot_p
            fresh_pag["has_prev"] = c_p > 1
            fresh_pag["search_gen"] = int(fresh_pag.get("search_gen") or merged.get("search_gen", 1))
            merged["pagination"] = fresh_pag
            claimed_page_box[0] = c_p
            is_oob_box[0] = oob
            saved_pag_box[0] = fresh_pag
            return merged

        res_state = _remerge_claim(state)
        ok = save_session_state_cas(
            session_id,
            res_state,
            role=role,
            remerge_fn=_remerge_claim,
            user_id=user_id,
        )
        if not ok:
            log.warning("Failed to persist pagination claim via CAS for session %s", key)
            return {"success": False, "error": "Failed to persist pagination claim via CAS"}

        return {
            "success": True,
            "claimed_page": claimed_page_box[0],
            "is_oob": is_oob_box[0],
            "saved_p_state": saved_pag_box[0],
            "error": None
        }
```

---

## 7. Audit 5 — Targeted Test Suite Inventory

To verify the eventual changes without running the full test suite, the following targeted test runs are recommended:

1. **Multi-Worker & Concurrency Suite:**
   ```bash
   .\venv\Scripts\python.exe -m pytest backend/tests/test_phase2d_cross_worker.py -v
   ```
2. **Session State & CAS Suite:**
   ```bash
   .\venv\Scripts\python.exe -m pytest backend/tests/test_redis_session.py -k "pagination or cas" -v
   ```
3. **Pipeline Streaming & Turn Persistence Suite:**
   ```bash
   .\venv\Scripts\python.exe -m pytest backend/tests/test_phase2c2_pipeline_integration.py -v
   ```

---

## 8. Confirmation of Read-Only Compliance

- **Production / Source Files Modified:** ZERO (0) files modified.
- **Test Files Modified:** ZERO (0) files modified.
- **Services Started / Stopped:** NONE (No Docker or Redis daemon commands issued).
- **Git State:** Current working tree preserved with zero new commits or branch changes.
- **Implementation State:** NO code changes were applied for Phase 2D.2; this report concludes the diagnosis phase.

