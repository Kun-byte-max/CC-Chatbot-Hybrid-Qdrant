import logging
from typing import List, Dict, Any, Optional
from backend.schemas.search_query import SearchQuery, LocationMode, MAX_SNAPSHOT_CANDIDATES
try:
    from backend.services.search_client import search_jobs
    from backend.repositories.job_repository import JobRepository
    from backend.utils.location_resolver import LocationResolver
except ModuleNotFoundError:
    from services.search_client import search_jobs  # type: ignore
    from repositories.job_repository import JobRepository  # type: ignore
    from utils.location_resolver import LocationResolver  # type: ignore

log = logging.getLogger(__name__)


class SearchExecutor:
    """
    SearchExecutor determines HOW to execute the search:
    - Pushes hard constraints (title, canonical location, work mode, company, experience bounds)
      directly down to Meilisearch filter expressions or MySQL WHERE clauses.
    - Caps candidate pool retrieval at MAX_SNAPSHOT_CANDIDATES = 100.
    - Invokes LocationResolver to compute geospatial distances for candidate jobs.
    """

    @classmethod
    def execute_search(cls, query: SearchQuery) -> List[Dict[str, Any]]:
        """
        Executes candidate job retrieval pushing Hard Filters to database/search engine.
        Returns a list of up to MAX_SNAPSHOT_CANDIDATES candidate job dictionaries.
        """
        candidate_jobs: List[Dict[str, Any]] = []

        # 1. TRY MEILISEARCH / SEARCH API RETRIEVAL WITH FILTER PUSHDOWN
        try:
            filter_kwargs = {}
            if query.title:
                filter_kwargs["keyword"] = query.title
            if query.canonical_location or query.location:
                filter_kwargs["location"] = query.canonical_location or query.location
            if query.work_mode:
                filter_kwargs["job_mode"] = query.work_mode
            if query.company:
                filter_kwargs["company"] = query.company

            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # If in running loop, run in background thread or fallback to db
                    meili_res = {}
                else:
                    meili_res = loop.run_until_complete(search_jobs(**filter_kwargs))
            except Exception:
                meili_res = asyncio.run(search_jobs(**filter_kwargs))

            if isinstance(meili_res, dict) and meili_res.get("success") and meili_res.get("data"):
                candidate_jobs = meili_res["data"]
                log.info("SearchExecutor retrieved %d candidates from Search API", len(candidate_jobs))
        except Exception as e:
            log.warning(f"Search API execution fallback: {e}")


        # 2. FALLBACK TO MYSQL JOB REPOSITORY WITH SQL WHERE PUSHDOWN
        if not candidate_jobs:
            try:
                candidate_jobs = cls._execute_mysql_fallback(query)
                log.info("SearchExecutor retrieved %d candidates from MySQL JobRepository", len(candidate_jobs))
            except Exception as e:
                log.error(f"MySQL execution error: {e}")
                candidate_jobs = []

        # 3. APPLY GEOSPATIAL DISTANCE CALCULATION & RADIUS FILTERING
        final_candidates: List[Dict[str, Any]] = []
        user_lat = query.latitude
        user_lon = query.longitude
        radius_km = query.radius_km

        for job in candidate_jobs:
            job_loc = job.get("job_location") or job.get("city_name") or job.get("location")
            job_lat = job.get("latitude")
            job_lon = job.get("longitude")

            dist_km = LocationResolver.compute_job_distance(
                user_lat=user_lat,
                user_lon=user_lon,
                job_location_str=job_loc,
                job_lat=job_lat,
                job_lon=job_lon,
            )

            # Apply hard Radius Filter if specified
            if radius_km and radius_km > 0:
                if dist_km is not None and dist_km > radius_km:
                    continue  # Filter out jobs beyond radius_km

            # Attach distance metadata to candidate job
            job["distance_km"] = dist_km
            job["distance_label"] = LocationResolver.format_distance_label(dist_km)
            final_candidates.append(job)

        # Return capped candidate pool (<= 100)
        return final_candidates[:MAX_SNAPSHOT_CANDIDATES]

    @classmethod
    def _execute_mysql_fallback(cls, query: SearchQuery) -> List[Dict[str, Any]]:
        """
        Executes MySQL query with SQL WHERE clause pushdown.
        """
        repo = JobRepository()

        # Build SQL filters
        title_keyword = query.title
        skills = query.skills or []

        # Location terms
        loc_terms = query.metro_cluster or ([query.canonical_location] if query.canonical_location else [])

        # Experience bounds
        exp_max = 99
        if query.experience_level and "fresher" in query.experience_level.lower():
            exp_max = 1

        # Use JobRepository.search_jobs or get_jobs_for_skills
        if title_keyword:
            jobs = repo.search_jobs(
                role_keyword=title_keyword,
                location="",
                limit=MAX_SNAPSHOT_CANDIDATES,
            )
        elif skills:
            jobs = repo.get_jobs_for_skills(
                skills=skills,
                limit=MAX_SNAPSHOT_CANDIDATES,
            )
        else:
            jobs = repo.get_active_jobs(limit=MAX_SNAPSHOT_CANDIDATES)

        # Apply in-memory Hard Filter pushdown fallback if SQL filter not fully pushed
        filtered = []
        for job in jobs:
            # Check location hard constraint
            if loc_terms:
                job_city = (job.get("city_name") or job.get("job_location") or "").lower()
                if not any(term.lower() in job_city for term in loc_terms):
                    continue

            # Check work mode hard constraint
            if query.work_mode:
                job_mode = (job.get("job_type") or job.get("work_mode") or "").lower()
                if query.work_mode.lower() not in job_mode:
                    continue

            # Check company hard constraint
            if query.company:
                job_comp = (job.get("company") or "").lower()
                if query.company.lower() not in job_comp:
                    continue

            filtered.append(job)

        return filtered
