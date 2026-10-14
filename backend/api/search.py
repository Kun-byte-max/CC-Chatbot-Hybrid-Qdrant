"""
backend/api/search.py — FastAPI search router backed by Meilisearch (jobs / users / companies).
Integrated search module for unified chatbot API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiomysql
import requests
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from indexer import INDEXES, run_index

log = logging.getLogger(__name__)

router = APIRouter(tags=["search"])

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

EXPLORING_OVERFETCH_MULTIPLIER = 5
EXPLORING_MAX_FETCH = 200

# Background sync config
SYNC_PARTIAL_INTERVAL = int(os.getenv("SYNC_PARTIAL_INTERVAL_SECONDS", 900))
SYNC_PARTIAL_WINDOW   = int(os.getenv("SYNC_PARTIAL_WINDOW_MINUTES", 20))
SYNC_FULL_INTERVAL    = int(os.getenv("SYNC_FULL_INTERVAL_SECONDS", 86400))

_sync_lock = asyncio.Lock()
REBUILD_JOBS: Dict[str, Dict[str, Any]] = {}


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
# DB pool getter
# ---------------------------------------------------------------------------

async def get_db_pool(request: Request) -> aiomysql.Pool:
    return getattr(request.app.state, "db_pool", None)


# ---------------------------------------------------------------------------
# on_exploring — batched DB post-filter
# ---------------------------------------------------------------------------

async def filter_exploring_users(
    users: List[Dict],
    login_user_id: int,
    pool: aiomysql.Pool,
) -> List[Dict]:
    if not users or not login_user_id or not pool:
        return users

    user_ids = [u["id"] for u in users]
    if not user_ids:
        return users

    try:
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
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

                users_opt1: List[int] = []
                users_opt2: List[int] = []
                users_opt3: List[int] = []
                users_opt4: List[int] = []
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

                hidden_from_company: Dict[int, set] = {}
                hidden_from_employees: Dict[int, set] = {}

                # opt=1
                if users_opt1:
                    ph = ",".join(["%s"] * len(users_opt1))
                    await cur.execute(
                        f"""SELECT user_id, company_id FROM cyb_user_employment_history
                            WHERE user_id IN ({ph}) AND still_working = '1' AND approved = 1 AND status = 1""",
                        users_opt1,
                    )
                    for r in await cur.fetchall():
                        try:
                            cid = int(r["company_id"])
                            hidden_from_company.setdefault(r["user_id"], set()).add(cid)
                        except (ValueError, TypeError):
                            pass

                # opt=2
                if users_opt2:
                    ph = ",".join(["%s"] * len(users_opt2))
                    await cur.execute(
                        f"""SELECT user_id, company_id FROM cyb_user_employment_history
                            WHERE user_id IN ({ph}) AND still_working = '1' AND approved = 1 AND status = 1""",
                        users_opt2,
                    )
                    opt2_companies: Dict[int, List[int]] = {}
                    for r in await cur.fetchall():
                        try:
                            cid = int(r["company_id"])
                            opt2_companies.setdefault(r["user_id"], []).append(cid)
                        except (ValueError, TypeError):
                            pass

                    all_company_ids = list({cid for cids in opt2_companies.values() for cid in cids})
                    if all_company_ids:
                        ph_c = ",".join(["%s"] * len(all_company_ids))
                        await cur.execute(
                            f"""SELECT user_id as user, company_id as company
                                FROM cyb_user_employment_history
                                WHERE company_id IN ({ph_c}) AND still_working = '1' AND approved = 1 AND status = 1""",
                            all_company_ids,
                        )
                        company_to_employees: Dict[int, List[int]] = {}
                        for r in await cur.fetchall():
                            company_to_employees.setdefault(r["company"], []).append(r["user"])

                        for uid, cids in opt2_companies.items():
                            employees: set = set()
                            for cid in cids:
                                employees.update(company_to_employees.get(cid, []))
                            employees.discard(uid)
                            hidden_from_employees[uid] = employees

                # opt=3/4
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
        return users

    def should_show(uid: int) -> bool:
        row = exploring_rows.get(uid)
        if not row or not row.get("exploring_option"):
            return False

        try:
            options = json.loads(row["exploring_option"])
        except Exception:
            options = []

        if not options:
            return False

        explore = True
        for opt in options:
            try:
                opt = int(opt)
            except (TypeError, ValueError):
                continue

            if opt == 1:
                company_ids = hidden_from_company.get(uid, set())
                if company_ids and login_user_id in company_ids:
                    explore = False
            elif opt == 2:
                employee_ids = hidden_from_employees.get(uid, set())
                if employee_ids and login_user_id in employee_ids:
                    explore = False
            elif opt in (3, 4):
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
# Background Sync Loops
# ---------------------------------------------------------------------------

async def _partial_sync_loop() -> None:
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
    await asyncio.sleep(SYNC_FULL_INTERVAL)
    while True:
        try:
            async with _sync_lock:
                log.info("[sync] Starting full rebuild of all indexes...")
                totals = await run_index(
                    indexes_to_run=list(INDEXES.keys()),
                    batch_size=1000,
                    since=None,
                )
                log.info("[sync] Full rebuild complete: %s", totals)
        except Exception as e:
            log.error("[sync] Full rebuild failed: %s", e)
        await asyncio.sleep(SYNC_FULL_INTERVAL)


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
# Query normalization & variant expansion
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
# Endpoints — Health & Index Rebuild
# ---------------------------------------------------------------------------

@router.get("/health")
def health():
    try:
        r = requests.get(f"{MEILI_URL}/health", headers=HEADERS, timeout=5)
        meili_ok = r.status_code == 200
    except Exception:
        meili_ok = False
    return {"api": "ok", "meilisearch": "ok" if meili_ok else "unreachable"}


@router.post("/rebuild-index")
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


@router.get("/rebuild-index/{job_id}")
def rebuild_index_status(job_id: str):
    job = REBUILD_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_id not found")
    return job


# ---------------------------------------------------------------------------
# Endpoints — Jobs Search
# ---------------------------------------------------------------------------

@router.get("/search/jobs")
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

    if vacancy:
        try:
            vals = {int(v) for v in vacancy}
        except Exception:
            vals = set()

        if vals == {0, 1}:
            pass
        elif 1 in vals:
            filters.append("vacancy > 1")
        elif 0 in vals:
            filters.append("vacancy = 1")

    if urgent is not None:
        filters.append(f"urgent = {str(bool(urgent)).lower()}")

    if applicant:
        vals = sorted(int(v) for v in applicant)
        if len(vals) >= 2:
            filters.append(f"applicant_count >= {vals[0]}")
            filters.append(f"applicant_count <= {vals[-1]}")
        else:
            filters.append(f"applicant_count <= {vals[0]}")

    cutoff = _create_window_to_cutoff_ts(create_date)
    if cutoff is not None:
        filters.append(f"create_ts >= {cutoff}")
        filters.append(f"create_ts <= {_now_ts()}")

    sort_rules = ["create_ts:desc"] if not keyword else None
    return search_with_fallback("jobs", keyword or "", limit, offset, filters, sort=sort_rules, facets=facets)


# ---------------------------------------------------------------------------
# Endpoints — Users Search
# ---------------------------------------------------------------------------

@router.get("/search/users")
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
    on_immidate:         Optional[List[int]] = Depends(parse_int_array("on_immidate")),
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
        _in("industry_id",        industry),
    ]:
        if expr:
            filters.append(expr)

    if current_company is not None:
        filters.append(f"current_company = {int(current_company)}")

    if on_notice:
        expr = _in("on_notice", on_notice)
        if expr:
            filters.append(expr)

    merged_immediate = list({v for v in (on_immediate or []) + (on_immidate or [])})
    if merged_immediate:
        expr = _in("on_immediate", merged_immediate)
        if expr:
            filters.append(expr)

    if verified_employement:
        vals = {int(v) for v in verified_employement}
        if vals == {0, 1}:
            pass
        elif 1 in vals:
            filters.append("verified_employement = true")
        elif 0 in vals:
            filters.append("verified_employement = false")

    if verified_salary:
        vals = {int(v) for v in verified_salary}
        if vals == {0, 1}:
            pass
        elif 1 in vals:
            filters.append("verified_salary = true")
        elif 0 in vals:
            filters.append("verified_salary = false")

    if verified_identity is not None:
        filters.append(f"verified_identity = {str(int(verified_identity) == 1).lower()}")

    if starRating:
        filters.append(f"star_rating_avg >= {min(int(v) for v in starRating)}")

    if yearExperience:
        filters.append(f"total_experience_years >= {min(int(v) for v in yearExperience)}")

    if course:
        expr = _in("education_course_ids", course)
        if expr:
            filters.append(expr)
    if course_type:
        expr = _in("education_course_type_ids", course_type)
        if expr:
            filters.append(expr)

    sort_rules = ["rank_score:desc"] if not keyword else None
    needs_exploring = bool(on_exploring and 1 in [int(x) for x in on_exploring])

    if needs_exploring and not login_user_id:
        res = search_with_fallback("users", keyword or "", limit, offset, filters, sort=sort_rules, facets=facets)
        res["warning"] = "on_exploring requires login_user_id — filter not applied."
        return res

    if needs_exploring:
        fetch_limit = min(limit * EXPLORING_OVERFETCH_MULTIPLIER, EXPLORING_MAX_FETCH)
        pool = getattr(request.app.state, "db_pool", None)

        collected: List[Dict] = []
        current_offset = offset
        estimated_total = None
        iterations = 0
        max_iterations = 5
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

            filtered_batch = await filter_exploring_users(batch, login_user_id, pool) if pool else batch
            collected.extend(filtered_batch)
            current_offset += fetch_limit
            iterations += 1

            if not res.get("has_more"):
                exhausted = True
                break

        page_data = collected[:limit]
        pass_rate = len(collected) / max(fetch_limit * iterations, 1)
        est_total = max(int((estimated_total or 0) * pass_rate), len(page_data))
        has_more = len(collected) > limit or not exhausted

        return {
            "total": est_total,
            "has_more": has_more,
            "next_offset": (offset + limit) if has_more else None,
            "data": page_data,
        }

    if keyword:
        tier2_total = 0
        verified_filters = filters + ["(verified_identity = true OR verified_employement = true)"]
        tier1 = search_with_fallback(
            "users", keyword, limit, offset, verified_filters, sort_rules, facets
        )
        tier1_hits = tier1.get("data", [])
        tier1_hits.sort(key=lambda x: x.get("rank_score", 0), reverse=True)

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

    return search_with_fallback(
        "users", keyword or "", limit, offset, filters,
        sort=["rank_score:desc"],
        facets=facets,
    )
