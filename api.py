"""
api.py — FastAPI search API backed by Meilisearch (jobs / users / companies).
v3.0.1 — Bug fixes.

Changes from v3.0.0:
- Fix UnboundLocalError: tier2_total initialized before conditional block (bug 2)
- Fix stale `res` variable after on_exploring loop causing wrong has_more (bug 1)
- Fix should_show() treating missing DB rows same as empty exploring_option (bug 3)
- Fix tiered keyword search ignoring offset param — pagination now works (bug 4)
- Fix est_total flooring to prevent hiding results when pass_rate is 0 (bug 6)
- Fix on_immidate typo alias deduplication to prevent duplicate filter values (bug 7)
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Union

import asyncio
import aiomysql
import requests
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from indexer import INDEXES, run_index

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

MEILI_URL = os.getenv("MEILI_URL", "http://localhost:7701")
MEILI_KEY = os.getenv("MEILI_MASTER_KEY", "masterKey123")

SEARCH_API_KEY = os.getenv("SEARCH_API_KEY", "")
HEADERS = {"Authorization": f"Bearer {MEILI_KEY}"}

PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}

_ws = re.compile(r"\s+")
_punct_only = re.compile(r"[^a-z0-9 ]+")
_non_alnum = re.compile(r"[^a-z0-9]+")

STOPWORDS = frozenset(["a", "an", "the", "in", "at", "for", "of", "and", "or", "with", "to", "is", "are"])

SPLIT_PREFIXES = [
    "frontend", "backend", "fullstack", "full", "senior", "junior", "lead",
    "staff", "principal", "chief", "head", "software", "hardware", "data",
    "machine", "deep", "cloud", "mobile", "android", "ios", "web", "devops",
    "product", "project", "business", "graphic", "visual", "content",
    "digital", "marketing", "sales", "human", "quality",
]

DB_DSN = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", "3306")),
    "user":     os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "db":       os.getenv("DB_NAME", "collarcheck"),
    "autocommit": True,
}

# How many extra results to fetch per page when on_exploring is active,
# to compensate for post-filter losses.
EXPLORING_OVERFETCH_MULTIPLIER = 5
EXPLORING_MAX_FETCH = 200  # hard cap to prevent runaway queries


# ---------------------------------------------------------------------------
# Array param helpers — accept both ?p=1&p=2 and ?p[0]=1&p[1]=2
# ---------------------------------------------------------------------------

def parse_int_array(name: str):
    async def _parse(request: Request) -> Optional[List[int]]:
        values: List[int] = []
        for key, value in request.query_params.multi_items():
            if key == name or key.startswith(f"{name}["):
                try:
                    values.append(int(value))
                except ValueError:
                    log.warning("Invalid non-integer value for param '%s': %r — skipped", name, value)
        return values if values else None
    return _parse


def parse_str_array(name: str):
    async def _parse(request: Request) -> Optional[List[str]]:
        values: List[str] = []
        for key, value in request.query_params.multi_items():
            if key == name or key.startswith(f"{name}["):
                v = value.strip()
                if v:
                    values.append(v)
        return values if values else None
    return _parse


# ---------------------------------------------------------------------------
# DB pool — shared across requests, initialized at startup
# ---------------------------------------------------------------------------

async def get_db_pool(request: Request) -> aiomysql.Pool:
    return request.app.state.db_pool


# ---------------------------------------------------------------------------
# on_exploring — batched DB post-filter
# Mirrors PHP show_exploring() exactly, max 4-5 queries total per call.
# ---------------------------------------------------------------------------

async def filter_exploring_users(
    users: List[Dict],
    login_user_id: int,
    pool: aiomysql.Pool,
) -> List[Dict]:
    """
    Post-filter a list of user dicts based on their on_exploring settings.
    Uses the shared DB pool. Returns only users visible to login_user_id.
    """
    if not users or not login_user_id:
        return users

    user_ids = [u["id"] for u in users]
    if not user_ids:
        return users

    try:
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:

                # ── Query 1: fetch exploring settings for all users at once ──
                ph = ",".join(["%s"] * len(user_ids))
                await cur.execute(
                    f"""SELECT user_id, exploring_option, exploring_details
                        FROM cyb_user_details
                        WHERE user_id IN ({ph})""",
                    user_ids,
                )
                exploring_rows: Dict[int, Dict] = {
                    r["user_id"]: r for r in await cur.fetchall()
                }

                # Bucket users by which options they have set.
                # PHP: exploring_option is a list of ints e.g. [1,3] or [2,4].
                #      exploring_details is ONE flat list of user/company ids shared
                #      across all opt 3/4 checks for that user ($optionDetailList).
                users_opt1: List[int] = []            # hide from current company
                users_opt2: List[int] = []            # hide from current company employees
                users_opt3: List[int] = []            # hide from specific companies (opt 3)
                users_opt4: List[int] = []            # hide from specific people (opt 4)
                # user_id -> flat list of detail ids (shared across opt 3 and opt 4)
                user_detail_ids: Dict[int, List[int]] = {}

                for uid in user_ids:
                    row = exploring_rows.get(uid)
                    if not row or not row.get("exploring_option"):
                        continue
                    try:
                        options = json.loads(row["exploring_option"])
                    except Exception:
                        options = []

                    if not options:
                        continue

                    # Parse exploring_details once per user — PHP: $optionDetailList
                    try:
                        details = row.get("exploring_details") or []
                        if isinstance(details, str):
                            details = json.loads(details)
                        if not isinstance(details, list):
                            details = []
                    except Exception:
                        details = []

                    for opt in options:
                        try:
                            opt = int(opt)
                        except (TypeError, ValueError):
                            continue

                        if opt == 1:
                            users_opt1.append(uid)
                        elif opt == 2:
                            users_opt2.append(uid)
                        elif opt == 3:
                            users_opt3.append(uid)
                            user_detail_ids[uid] = [
                                int(d) for d in details
                                if isinstance(d, (int, str)) and str(d).isdigit()
                            ]
                        elif opt == 4:
                            users_opt4.append(uid)
                            user_detail_ids[uid] = [
                                int(d) for d in details
                                if isinstance(d, (int, str)) and str(d).isdigit()
                            ]

                # ── Query 2: opt=1 — get current companies of these users ──
                # PHP: hide_current_company() — still_working=1
                hidden_from_company: Dict[int, set] = {}
                if users_opt1:
                    ph = ",".join(["%s"] * len(users_opt1))
                    await cur.execute(
                        f"""SELECT DISTINCT user, company
                            FROM cyb_user_experience
                            WHERE user IN ({ph})
                              AND still_working = 1
                              AND is_deleted = 0
                              AND status = 1""",
                        users_opt1,
                    )
                    for r in await cur.fetchall():
                        hidden_from_company.setdefault(r["user"], set()).add(r["company"])

                # ── Query 3 + 4: opt=2 — get current companies, then their employees ──
                # PHP: hide_current_company_employees()
                hidden_from_employees: Dict[int, set] = {}
                if users_opt2:
                    ph = ",".join(["%s"] * len(users_opt2))
                    await cur.execute(
                        f"""SELECT DISTINCT user, company
                            FROM cyb_user_experience
                            WHERE user IN ({ph})
                              AND still_working = 1
                              AND is_deleted = 0
                              AND status = 1""",
                        users_opt2,
                    )
                    opt2_companies: Dict[int, List[int]] = {}
                    for r in await cur.fetchall():
                        opt2_companies.setdefault(r["user"], []).append(r["company"])

                    all_company_ids = list({c for clist in opt2_companies.values() for c in clist})
                    if all_company_ids:
                        ph2 = ",".join(["%s"] * len(all_company_ids))
                        await cur.execute(
                            f"""SELECT DISTINCT user, company
                                FROM cyb_user_experience
                                WHERE company IN ({ph2})
                                  AND is_deleted = 0
                                  AND status = 1""",
                            all_company_ids,
                        )
                        company_to_employees: Dict[int, List[int]] = {}
                        for r in await cur.fetchall():
                            company_to_employees.setdefault(r["company"], []).append(r["user"])

                        for uid, cids in opt2_companies.items():
                            employees: set = set()
                            for cid in cids:
                                employees.update(company_to_employees.get(cid, []))
                            employees.discard(uid)  # PHP: whereNotIn user_id (exclude self)
                            hidden_from_employees[uid] = employees

                # ── Query 5: opt=3/4 — resolve user_type of ids in exploring_details ──
                # PHP: get_exploring_user_list($optionDetailList)
                # One query across all users' detail id lists combined.
                # user_type=1 → person (used by opt 4), user_type=2 → company (used by opt 3)
                all_detail_ids: List[int] = list({
                    did
                    for ids in user_detail_ids.values()
                    for did in ids
                })
                detail_user_types: Dict[int, int] = {}
                if all_detail_ids:
                    ph = ",".join(["%s"] * len(all_detail_ids))
                    await cur.execute(
                        f"SELECT id, user_type FROM cyb_user WHERE id IN ({ph})",
                        all_detail_ids,
                    )
                    for r in await cur.fetchall():
                        detail_user_types[r["id"]] = r["user_type"]

    except Exception as e:
        log.error("on_exploring DB error: %s", e)
        # Fail open — return unfiltered rather than crashing
        return users

    # ── Apply filter logic in memory — mirrors PHP show_exploring() exactly ──
    def should_show(uid: int) -> bool:
        row = exploring_rows.get(uid)

        # PHP: get_exporing_option returns [] when no row exists, then
        # show_exploring does `if (empty($exploring)) return false`.
        # Both missing row AND empty exploring_option mean: not visible.
        if not row or not row.get("exploring_option"):
            return False

        try:
            options = json.loads(row["exploring_option"])
        except Exception:
            options = []

        if not options:
            return False

        explore = True  # PHP default

        for opt in options:
            try:
                opt = int(opt)
            except (TypeError, ValueError):
                continue

            if opt == 1:
                # Hide from viewers who are the user's current company
                company_ids = hidden_from_company.get(uid, set())
                if company_ids and login_user_id in company_ids:
                    explore = False

            elif opt == 2:
                # Hide from viewers who are employees of user's current company
                employee_ids = hidden_from_employees.get(uid, set())
                if employee_ids and login_user_id in employee_ids:
                    explore = False

            elif opt in (3, 4):
                # PHP: resolve company/person ids from the shared optionDetailList,
                # then check login_user_id membership per opt.
                detail_ids = user_detail_ids.get(uid, [])
                company_ids = [d for d in detail_ids if detail_user_types.get(d) == 2]
                person_ids  = [d for d in detail_ids if detail_user_types.get(d) == 1]

                if opt == 3 and company_ids and login_user_id in company_ids:
                    explore = False
                if opt == 4 and person_ids and login_user_id in person_ids:
                    explore = False

        return explore

    return [u for u in users if should_show(u["id"])]


# ---------------------------------------------------------------------------
# Background sync scheduler
# Runs inside the same process — no cron or external scheduler needed.
#
# Schedule (configurable via env vars):
#   SYNC_PARTIAL_INTERVAL_SECONDS  — how often to run partial sync (default: 900 = 15 min)
#   SYNC_PARTIAL_WINDOW_MINUTES    — how far back to look for changes (default: 20 min)
#   SYNC_FULL_INTERVAL_SECONDS     — how often to run full rebuild (default: 86400 = 24 hrs)
# ---------------------------------------------------------------------------

SYNC_PARTIAL_INTERVAL = int(os.getenv("SYNC_PARTIAL_INTERVAL_SECONDS", 900))   # 15 min
SYNC_PARTIAL_WINDOW   = int(os.getenv("SYNC_PARTIAL_WINDOW_MINUTES", 20))       # 20 min lookback
SYNC_FULL_INTERVAL    = int(os.getenv("SYNC_FULL_INTERVAL_SECONDS", 86400))     # 24 hrs

# Lock to ensure only one sync runs at a time (prevents overlapping partial + full syncs)
_sync_lock = asyncio.Lock()


async def _partial_sync_loop() -> None:
    """
    Runs every SYNC_PARTIAL_INTERVAL seconds.
    Syncs all records modified in the last SYNC_PARTIAL_WINDOW minutes.
    Lightweight — only pulls recently changed records from MySQL.
    """
    # Wait one full interval before first run so startup indexing completes first
    await asyncio.sleep(SYNC_PARTIAL_INTERVAL)

    while True:
        try:
            async with _sync_lock:
                since_dt = datetime.utcnow() - timedelta(minutes=SYNC_PARTIAL_WINDOW)
                since_str = since_dt.strftime("%Y-%m-%d %H:%M:%S")
                log.info("[sync] Starting partial sync (since %s)...", since_str)
                totals = await run_index(
                    indexes_to_run=list(INDEXES.keys()),
                    batch_size=500,
                    since=since_str,
                )
                log.info("[sync] Partial sync complete: %s", totals)
        except Exception as e:
            log.error("[sync] Partial sync failed: %s", e)

        await asyncio.sleep(SYNC_PARTIAL_INTERVAL)


async def _full_sync_loop() -> None:
    """
    Runs every SYNC_FULL_INTERVAL seconds (default 24hrs).
    Full wipe + rebuild of all indexes.
    Catches any inconsistencies that accumulated during the day.
    Runs at startup after a short delay, then every interval.
    """
    # Wait for the full interval before running the first full rebuild
    # so we don't hammer the database on every restart
    await asyncio.sleep(SYNC_FULL_INTERVAL)

    while True:
        try:
            async with _sync_lock:
                log.info("[sync] Starting full rebuild of all indexes...")
                totals = await run_index(
                    indexes_to_run=list(INDEXES.keys()),
                    batch_size=1000,
                    since=None,  # None = full rebuild
                )
                log.info("[sync] Full rebuild complete: %s", totals)
        except Exception as e:
            log.error("[sync] Full rebuild failed: %s", e)

        await asyncio.sleep(SYNC_FULL_INTERVAL)


# ---------------------------------------------------------------------------
# Startup — initialize DB pool + start background sync tasks
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not SEARCH_API_KEY:
        log.warning("SEARCH_API_KEY is not set — all non-public requests will be rejected!")
    else:
        log.info("API started with key authentication enabled.")

    # Initialize shared DB connection pool
    app.state.db_pool = await aiomysql.create_pool(
        minsize=2,
        maxsize=10,
        **DB_DSN,
    )
    log.info("DB connection pool initialized.")

    # Start background sync tasks
    partial_task = asyncio.create_task(_partial_sync_loop())
    full_task    = asyncio.create_task(_full_sync_loop())
    log.info(
        "[sync] Scheduler started — partial every %ds, full every %ds.",
        SYNC_PARTIAL_INTERVAL,
        SYNC_FULL_INTERVAL,
    )

    yield

    # Cleanup — cancel sync tasks gracefully
    partial_task.cancel()
    full_task.cancel()
    try:
        await asyncio.gather(partial_task, full_task, return_exceptions=True)
    except Exception:
        pass

    app.state.db_pool.close()
    await app.state.db_pool.wait_closed()
    log.info("DB connection pool closed.")


app = FastAPI(
    title="Search API",
    version="3.0.1",
    description="Meilisearch-backed search for jobs, users (employees), and companies.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "*",
        "null",
        "http://localhost",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
        "http://localhost:8000",
        "http://localhost:8001",
        "http://127.0.0.1:8000",
        "http://127.0.0.1:8001",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

REBUILD_JOBS: Dict[str, Dict[str, Any]] = {}


async def _run_rebuild_job(job_id: str, targets: List[str], batch_size: int, since: Optional[str]) -> None:
    REBUILD_JOBS[job_id] = {"status": "running", "targets": targets, "batch_size": batch_size, "since": since}
    try:
        async with _sync_lock:
            totals = await run_index(targets, batch_size=batch_size, since=since)
        REBUILD_JOBS[job_id] = {"status": "succeeded", "targets": targets, "since": since, "totals": totals}
    except Exception as exc:
        REBUILD_JOBS[job_id] = {"status": "failed", "targets": targets, "since": since, "error": str(exc)}
        log.exception("Rebuild job failed [job_id=%s]", job_id)


# ---------------------------------------------------------------------------
# Middleware — API key auth
# ---------------------------------------------------------------------------

@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    request.state.request_id = str(uuid.uuid4())[:8]

    if request.url.path in PUBLIC_PATHS:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    api_key = (
        request.headers.get("X-API-Key")
        or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        or request.query_params.get("api_key")
    )

    if SEARCH_API_KEY and (not api_key or api_key != SEARCH_API_KEY):
        log.warning("Unauthorized request to %s [request_id=%s]", request.url.path, request.state.request_id)
        return JSONResponse(
            status_code=401,
            content={
                "error": "Invalid or missing API key.",
                "status_code": 401,
                "path": str(request.url.path),
                "request_id": request.state.request_id,
            },
        )

    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail,
            "status_code": exc.status_code,
            "path": str(request.url.path),
            "request_id": getattr(request.state, "request_id", None),
        },
    )


# ---------------------------------------------------------------------------
# Health / rebuild
# ---------------------------------------------------------------------------

@app.get("/health", tags=["meta"])
def health():
    try:
        r = requests.get(f"{MEILI_URL}/health", headers=HEADERS, timeout=5)
        meili_ok = r.status_code == 200
    except Exception:
        meili_ok = False
    return {"api": "ok", "meilisearch": "ok" if meili_ok else "unreachable"}


@app.post("/rebuild-index", tags=["meta"])
async def rebuild_index(payload: Dict[str, Any], background_tasks: BackgroundTasks):
    requested_index = payload.get("index")
    since = payload.get("since")
    batch_size = int(payload.get("batch_size", 1000))
    run_async = bool(payload.get("async", True))

    if batch_size < 1 or batch_size > 5000:
        raise HTTPException(status_code=400, detail="batch_size must be between 1 and 5000")

    if requested_index is None:
        targets = list(INDEXES.keys())
    else:
        if requested_index not in INDEXES:
            raise HTTPException(status_code=400, detail=f"index must be one of: {', '.join(INDEXES.keys())}")
        targets = [requested_index]

    if run_async:
        job_id = str(uuid.uuid4())
        REBUILD_JOBS[job_id] = {"status": "queued", "targets": targets, "since": since, "batch_size": batch_size}
        background_tasks.add_task(_run_rebuild_job, job_id, targets, batch_size, since)
        return {"status": "accepted", "job_id": job_id, "targets": targets, "since": since}

    async with _sync_lock:
        totals = await run_index(targets, batch_size=batch_size, since=since)
    return {"status": "completed", "targets": targets, "since": since, "totals": totals}


@app.get("/rebuild-index/{job_id}", tags=["meta"])
def rebuild_index_status(job_id: str):
    job = REBUILD_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_id not found")
    return job


# ---------------------------------------------------------------------------
# Query normalisation & variant expansion
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    return _ws.sub(" ", s.strip())


def _remove_stopwords(tokens: List[str]) -> List[str]:
    return [t for t in tokens if t not in STOPWORDS]


def _try_split_glued(glued: str) -> Optional[str]:
    lower = glued.lower()
    for prefix in SPLIT_PREFIXES:
        if lower.startswith(prefix) and len(lower) > len(prefix) + 2:
            remainder = lower[len(prefix):]
            return f"{prefix} {remainder}"
    return None


def build_query_variants(keyword: str) -> List[str]:
    primary = _norm(keyword)
    variants: List[str] = [primary]
    lower = primary.lower()
    tokens = lower.split()

    no_punct = _punct_only.sub("", lower).strip()
    no_punct = _ws.sub(" ", no_punct)
    if no_punct and no_punct != lower:
        variants.append(no_punct)

    filtered_tokens = _remove_stopwords(tokens)
    filtered = " ".join(filtered_tokens)
    if filtered and filtered != lower and filtered not in variants:
        variants.append(filtered)

    compact = _non_alnum.sub("", lower)
    if compact and compact != lower and compact not in variants:
        variants.append(compact)

    if len(tokens) == 1:
        split = _try_split_glued(tokens[0])
        if split and split not in variants:
            variants.append(split)

    return list(dict.fromkeys(variants))


# ---------------------------------------------------------------------------
# Meilisearch helpers
# ---------------------------------------------------------------------------

def _meili_search(
    index: str,
    q: str,
    limit: int,
    offset: int,
    filters: Optional[List[str]] = None,
    sort: Optional[List[str]] = None,
    facets: Optional[List[str]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "q": q,
        "limit": limit,
        "offset": offset,
        "showRankingScore": True,
        "matchingStrategy": os.getenv("MEILI_MATCHING_STRATEGY", "frequency"),
    }
    if filters:
        payload["filter"] = " AND ".join(f"({f})" for f in filters)
    if sort:
        payload["sort"] = sort
    if facets:
        payload["facets"] = facets

    try:
        r = requests.post(
            f"{MEILI_URL}/indexes/{index}/search",
            headers=HEADERS,
            json=payload,
            timeout=10,
        )
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="Meilisearch request timed out.")
    except requests.exceptions.ConnectionError:
        raise HTTPException(status_code=502, detail="Cannot reach Meilisearch.")

    if r.status_code != 200:
        log.error("Meilisearch error: %s %s", r.status_code, r.text[:300])
        raise HTTPException(status_code=502, detail=f"Meilisearch error: {r.status_code}")

    return r.json()


def _merge_hits(primary: List[Dict], secondary: List[Dict], limit: int) -> List[Dict]:
    seen: set = set()
    out: List[Dict] = []
    for h in primary + secondary:
        hid = h.get("id")
        if hid in seen:
            continue
        seen.add(hid)
        out.append(h)
        if len(out) >= limit:
            break
    return out


def _format(
    hits: List[Dict],
    total: int,
    offset: int,
    limit: int,
    debug: Optional[Dict] = None,
    facet_distribution: Optional[Dict] = None,
) -> Dict:
    next_offset = offset + limit
    result: Dict[str, Any] = {
        "total": total,
        "has_more": next_offset < total,
        "next_offset": next_offset if next_offset < total else None,
        "data": hits,
    }
    if debug:
        result["debug"] = debug
    if facet_distribution:
        result["facet_distribution"] = facet_distribution
    return result


def search_with_fallback(
    index: str,
    keyword: str,
    limit: int,
    offset: int,
    filters: Optional[List[str]] = None,
    sort: Optional[List[str]] = None,
    facets: Optional[List[str]] = None,
) -> Dict[str, Any]:
    variants = build_query_variants(keyword)
    primary_q = variants[0]

    res1 = _meili_search(index, primary_q, limit, offset, filters, sort, facets)
    hits1: List[Dict] = res1.get("hits") or []
    total1: int = res1.get("estimatedTotalHits") or 0
    facet_distribution: Optional[Dict] = res1.get("facetDistribution")

    fallback_threshold = min(3, max(1, limit // 5))
    if len(hits1) >= fallback_threshold or len(variants) == 1:
        return _format(hits1, total1, offset, limit, facet_distribution=facet_distribution)

    all_hits = list(hits1)
    best_total = total1
    debug_variants: List[Dict] = []

    for fallback_q in variants[1:]:
        if fallback_q == primary_q:
            continue
        res2 = _meili_search(index, fallback_q, limit, 0, filters, sort, facets)
        hits2: List[Dict] = res2.get("hits") or []
        total2: int = res2.get("estimatedTotalHits") or 0
        debug_variants.append({"q": fallback_q, "hits": len(hits2), "total": total2})
        all_hits = _merge_hits(all_hits, hits2, limit)
        best_total = max(best_total, total2)
        if len(all_hits) >= fallback_threshold:
            break

    debug = {
        "fallback_used": True,
        "q_primary": primary_q,
        "hits_primary": len(hits1),
        "variants": debug_variants,
    }
    return _format(all_hits, best_total, offset, limit, debug, facet_distribution=facet_distribution)


# ---------------------------------------------------------------------------
# Filter helpers
# ---------------------------------------------------------------------------

def _in(field: str, values: Optional[List[int]]) -> Optional[str]:
    if not values:
        return None
    vals = [int(v) for v in values]
    return f"{field} IN [{', '.join(map(str, vals))}]"


def _not_in(field: str, values: Optional[List[int]]) -> Optional[str]:
    if not values:
        return None
    vals = [int(v) for v in values]
    return f"{field} NOT IN [{', '.join(map(str, vals))}]"


def _skills_filter(skill_ids: Optional[List[int]]) -> Optional[str]:
    return _in("skill_ids", skill_ids)


def _create_window_to_cutoff_ts(values: Optional[List[str]]) -> Optional[int]:
    """
    Returns lower-bound cutoff UNIX timestamp.
    Matches PHP: max(days) going back from now, or 1hr.
    """
    if not values:
        return None
    v = [str(x).strip() for x in values if str(x).strip()]
    if not v:
        return None

    now = datetime.now(timezone.utc)

    if any(x.lower() in ("1hr", "1h", "hour") for x in v):
        return int((now - timedelta(hours=1)).timestamp())

    days: List[int] = []
    for x in v:
        try:
            days.append(int(x))
        except ValueError:
            continue
    if not days:
        return None

    return int((now - timedelta(days=max(days))).timestamp())


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


# ---------------------------------------------------------------------------
# Endpoints — Jobs
# ---------------------------------------------------------------------------

@app.get("/search/jobs", tags=["search"])
def search_jobs(
    request: Request,
    keyword:     str = Query(""),
    limit:       int = Query(20, ge=1, le=100),
    offset:      int = Query(0, ge=0),
    vacancy:     Optional[List[int]] = Depends(parse_int_array("vacancy")),
    urgent:      Optional[int] = Query(None),
    facets:      Optional[List[str]] = Query(None),
    company:     Optional[List[int]] = Depends(parse_int_array("company")),
    industry:    Optional[List[int]] = Depends(parse_int_array("industry")),
    department:  Optional[List[int]] = Depends(parse_int_array("department")),
    designation: Optional[List[int]] = Depends(parse_int_array("designation")),
    salary:      Optional[List[int]] = Depends(parse_int_array("salary")),
    experience:  Optional[List[int]] = Depends(parse_int_array("experience")),
    role_type:   Optional[List[int]] = Depends(parse_int_array("role_type")),
    skill:       Optional[List[int]] = Depends(parse_int_array("skill")),
    country:     Optional[List[int]] = Depends(parse_int_array("country")),
    state:       Optional[List[int]] = Depends(parse_int_array("state")),
    city:        Optional[List[int]] = Depends(parse_int_array("city")),
    benefits:    Optional[List[int]] = Depends(parse_int_array("benefits")),
    job_mode:    Optional[List[int]] = Depends(parse_int_array("job_mode")),
    applicant:   Optional[List[int]] = Depends(parse_int_array("applicant")),
    create_date: Optional[List[str]] = Depends(parse_str_array("create_date")),
    exclude_jobs: Optional[List[int]] = Depends(parse_int_array("exclude_jobs")),
):
    filters: List[str] = []

    for expr in [
        _in("company_id",  company),
        _in("industry_id", industry),
        _in("department",  department),
        _in("designation", designation),
        _in("salary",      salary),
        _in("experience",  experience),
        _in("role_type",   role_type),
        _skills_filter(skill),
        _in("country",     country),
        _in("state",       state),
        _in("city",        city),
        _in("benefit_ids", benefits),
        _in("job_mode",    job_mode),
        _not_in("id",      exclude_jobs),
    ]:
        if expr:
            filters.append(expr)

    # vacancy filter: 1 = more than 1 vacancy, 0 = exactly 1 vacancy
    # Behavior: if both 0 and 1 are requested, do not apply a vacancy filter
    if vacancy:
        try:
            vals = {int(v) for v in vacancy}
        except Exception:
            vals = set()

        if vals == {0, 1}:
            # both options selected -> no vacancy filter
            pass
        elif 1 in vals:
            filters.append("vacancy > 1")
        elif 0 in vals:
            filters.append("vacancy = 1")
    if urgent is not None:
        filters.append(f"urgent = {str(bool(urgent)).lower()}")

    # applicant range — applicant[0]=10&applicant[1]=20 = between 10 and 20
    # single value — applicant[0]=50 = up to 50 (matches PHP max behavior)
    if applicant:
        vals = sorted(int(v) for v in applicant)
        if len(vals) >= 2:
            filters.append(f"applicant_count >= {vals[0]}")
            filters.append(f"applicant_count <= {vals[-1]}")
        else:
            filters.append(f"applicant_count <= {vals[0]}")

    # create_date: between cutoff and now — matches PHP fromDate/toDate
    cutoff = _create_window_to_cutoff_ts(create_date)
    if cutoff is not None:
        filters.append(f"create_ts >= {cutoff}")
        filters.append(f"create_ts <= {_now_ts()}")

    sort_rules = ["create_ts:desc"] if not keyword else None

    return search_with_fallback("jobs", keyword or "", limit, offset, filters, sort=sort_rules, facets=facets)


# ---------------------------------------------------------------------------
# Endpoints — Companies
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Endpoints — Users
# ---------------------------------------------------------------------------

@app.get("/search/users", tags=["search"])
async def search_users(
    request: Request,
    keyword:             str = Query(""),
    limit:               int = Query(20, ge=1, le=100),
    offset:              int = Query(0, ge=0),
    verified_identity:   Optional[int] = Query(None),
    login_user_id:       Optional[int] = Query(None),
    current_company:     Optional[int] = Query(None),
    facets:              Optional[List[str]] = Query(None),
    country:             Optional[List[int]] = Depends(parse_int_array("country")),
    state:               Optional[List[int]] = Depends(parse_int_array("state")),
    city:                Optional[List[int]] = Depends(parse_int_array("city")),
    department:          Optional[List[int]] = Depends(parse_int_array("department")),
    designation:         Optional[List[int]] = Depends(parse_int_array("designation")),
    skill:               Optional[List[int]] = Depends(parse_int_array("skill")),
    salary:              Optional[List[int]] = Depends(parse_int_array("salary")),
    company:             Optional[List[int]] = Depends(parse_int_array("company")),
    industry:            Optional[List[int]] = Depends(parse_int_array("industry")),
    work_type:           Optional[List[int]] = Depends(parse_int_array("work_type")),
    employment_type:     Optional[List[int]] = Depends(parse_int_array("employment_type")),
    verified_employement: Optional[List[int]] = Depends(parse_int_array("verified_employement")),
    verified_salary:     Optional[List[int]] = Depends(parse_int_array("verified_salary")),
    on_exploring:        Optional[List[int]] = Depends(parse_int_array("on_exploring")),
    on_notice:           Optional[List[int]] = Depends(parse_int_array("on_notice")),
    on_immediate:        Optional[List[int]] = Depends(parse_int_array("on_immediate")),
    on_immidate:         Optional[List[int]] = Depends(parse_int_array("on_immidate")),  # PHP typo alias
    starRating:          Optional[List[int]] = Depends(parse_int_array("starRating")),
    yearExperience:      Optional[List[int]] = Depends(parse_int_array("yearExperience")),
    course:              Optional[List[int]] = Depends(parse_int_array("course")),
    course_type:         Optional[List[int]] = Depends(parse_int_array("course_type")),
):
    filters: List[str] = []

    for expr in [
        _in("country",            country),
        _in("state",              state),
        _in("city",               city),
        _in("current_company",    company),
        _in("current_possition",  designation),
        _in("work_status",        work_type),
        _in("exp_department",     department),
        _in("exp_employment_type", employment_type),
        _in("exp_salary",         salary),
        _skills_filter(skill),
        # industry_id = current company's industry (fixed in indexer to use cc.industry)
        _in("industry_id",        industry),
    ]:
        if expr:
            filters.append(expr)

    # current_company: single value direct filter — matches PHP $filter['current_company']
    if current_company is not None:
        filters.append(f"current_company = {int(current_company)}")

    # on_notice
    if on_notice:
        expr = _in("on_notice", on_notice)
        if expr:
            filters.append(expr)

    # FIX (bug 7): deduplicate merged list to prevent duplicate filter values
    # when client sends both on_immediate and on_immidate with the same values.
    merged_immediate = list({v for v in (on_immediate or []) + (on_immidate or [])})
    if merged_immediate:
        expr = _in("on_immediate", merged_immediate)
        if expr:
            filters.append(expr)

    # verified_employement — approved=1 (verified), approved=0 (not verified)
    if verified_employement:
        vals = {int(v) for v in verified_employement}
        if vals == {0, 1}:
            pass
        elif 1 in vals:
            filters.append("verified_employement = true")
        elif 0 in vals:
            filters.append("verified_employement = false")

    # verified_salary — salary IS NOT NULL (1) or IS NULL (0)
    if verified_salary:
        vals = {int(v) for v in verified_salary}
        if vals == {0, 1}:
            pass
        elif 1 in vals:
            filters.append("verified_salary = true")
        elif 0 in vals:
            filters.append("verified_salary = false")

    # verified_identity — EXISTS on cyb_verify_document with verify=1
    if verified_identity is not None:
        filters.append(f"verified_identity = {str(int(verified_identity) == 1).lower()}")

    # starRating — min value, matches PHP getstarRatingEmployies($minStar)
    if starRating:
        filters.append(f"star_rating_avg >= {min(int(v) for v in starRating)}")

    # yearExperience — min value, matches PHP getExperienceEmployee($minYear)
    if yearExperience:
        filters.append(f"total_experience_years >= {min(int(v) for v in yearExperience)}")

    # course / course_type — indexed from cyb_user_education
    if course:
        expr = _in("education_course_ids", course)
        if expr:
            filters.append(expr)
    if course_type:
        expr = _in("education_course_type_ids", course_type)
        if expr:
            filters.append(expr)

    sort_rules = ["rank_score:desc"] if not keyword else None

    # on_exploring path — unchanged, just pass through to existing logic
    needs_exploring = bool(on_exploring and 1 in [int(x) for x in on_exploring])

    if needs_exploring and not login_user_id:
        res = search_with_fallback("users", keyword or "", limit, offset, filters, sort=sort_rules, facets=facets)
        res["warning"] = "on_exploring requires login_user_id — filter not applied."
        return res

    if needs_exploring:
        fetch_limit = min(limit * EXPLORING_OVERFETCH_MULTIPLIER, EXPLORING_MAX_FETCH)
        pool: aiomysql.Pool = request.app.state.db_pool

        collected: List[Dict] = []
        current_offset = offset
        estimated_total = None
        iterations = 0
        max_iterations = 5
        # FIX (bug 1): track loop exhaustion explicitly so has_more is correct
        # even when we exit due to max_iterations rather than running out of data.
        exhausted = False

        while len(collected) < limit and iterations < max_iterations:
            res = search_with_fallback(
                "users", keyword or "", fetch_limit, current_offset,
                filters, sort=sort_rules, facets=facets,
            )
            batch = res.get("data", [])
            if estimated_total is None:
                estimated_total = res.get("total", 0)

            if not batch:
                exhausted = True
                break

            filtered_batch = await filter_exploring_users(batch, login_user_id, pool)
            collected.extend(filtered_batch)
            current_offset += fetch_limit
            iterations += 1

            if not res.get("has_more"):
                exhausted = True
                break

        page_data = collected[:limit]
        pass_rate = len(collected) / max(fetch_limit * iterations, 1)
        # FIX (bug 6): floor to len(page_data) so we never report total=0
        # when the DB post-filter dropped everything in the sampled batches.
        est_total = max(int((estimated_total or 0) * pass_rate), len(page_data))
        has_more = len(collected) > limit or not exhausted

        return {
            "total": est_total,
            "has_more": has_more,
            "next_offset": (offset + limit) if has_more else None,
            "data": page_data,
        }

    # ── Tiered search — verified users always above unverified ──────────────
    if keyword:
        # FIX (bug 2): initialize tier2_total before the conditional block to
        # prevent UnboundLocalError when tier1 fills the page (remaining == 0).
        tier2_total = 0

        # Tier 1: any verification
        # FIX (bug 4): pass caller's offset so pagination advances correctly.
        verified_filters = filters + ["(verified_identity = true OR verified_employement = true)"]
        tier1 = search_with_fallback(
            "users", keyword, limit, offset, verified_filters, sort_rules, facets
        )
        tier1_hits = tier1.get("data", [])

        # Sort tier1 by rank_score desc within verified group
        tier1_hits.sort(key=lambda x: x.get("rank_score", 0), reverse=True)

        # Tier 2: fill remaining slots with unverified
        remaining = limit - len(tier1_hits)
        tier2_hits = []
        if remaining > 0:
            tier1_ids = {h["id"] for h in tier1_hits}
            unverified_filters = filters + [
                "verified_identity = false",
                "verified_employement = false",
            ]
            tier2 = search_with_fallback(
                "users", keyword, remaining * 2, offset, unverified_filters, sort_rules, facets
            )
            tier2_total = tier2.get("total", 0)

            tier2_hits = [
                h for h in tier2.get("data", [])
                if h["id"] not in tier1_ids
            ]
            tier2_hits.sort(key=lambda x: x.get("rank_score", 0), reverse=True)

        merged = tier1_hits + tier2_hits[:remaining]
        total = tier1.get("total", 0) + tier2_total
        next_offset = offset + limit

        return {
            "total": total,
            "has_more": next_offset < total,
            "next_offset": next_offset if next_offset < total else None,
            "data": merged,
        }

    # No keyword — pure rank_score sort
    return search_with_fallback(
        "users", keyword or "", limit, offset, filters,
        sort=["rank_score:desc"],
        facets=facets,
    )
