import re
from typing import List, Dict, Any, Optional


try:
    from backend.repositories.db import get_db
    from backend.config.config import JOB_MODE_MAP, JOB_PROFILE_SKILLS_MAP
    from backend.recommendation.skill_matcher import canonicalize_skill
except ModuleNotFoundError:
    from repositories.db import get_db  # type: ignore
    from config.config import JOB_MODE_MAP, JOB_PROFILE_SKILLS_MAP  # type: ignore
    from recommendation.skill_matcher import canonicalize_skill  # type: ignore


def get_dynamic_job_profile_skills(job_profile: str, limit: int = 5) -> List[str]:
    """
    Dynamically fetches required skills for a job title/profile directly from the MySQL database
    (cyb_company_job_skill JOIN cyb_skill JOIN cyb_company_job).
    """
    if not job_profile or not isinstance(job_profile, str):
        return []
    words = [w.strip() for w in job_profile.split() if len(w.strip()) > 2]
    if not words:
        return []

    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        conds = ' OR '.join(['j.job_title LIKE %s OR d.name LIKE %s' for _ in words])
        params = []
        for w in words:
            params.extend([f'%{w}%', f'%{w}%'])

        sql = f'''
            SELECT s.name, COUNT(*) as cnt
            FROM cyb_company_job_skill js
            JOIN cyb_skill s ON js.skill = s.id
            JOIN cyb_company_job j ON js.company_job = j.id
            LEFT JOIN cyb_department d ON j.department = d.id
            WHERE j.status = 1 AND (js.is_deleted IS NULL OR js.is_deleted = 0) AND s.status = 1 AND ({conds})
            GROUP BY s.id, s.name
            ORDER BY cnt DESC
            LIMIT %s
        '''
        c.execute(sql, params + [limit])
        rows = c.fetchall()
        if rows:
            return [r['name'] for r in rows if r.get('name')]
    except Exception:
        pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return []


def infer_job_skills(title: str, dept: str, desc: str) -> List[str]:
    """
    Infers complete job skills from title, department, and description text.
    Preserves all matching skills found in the job text and canonicalizes them.
    Dynamically queries database for job profile skill mappings before fallback.
    """
    title_str = title or ""
    dept_str = dept or ""
    desc_str = desc or ""
    text = f"{title_str} {dept_str} {desc_str}".lower()

    skills = []

    # 1. Dynamic Database Mapping for job profile skills
    dyn_skills = get_dynamic_job_profile_skills(f"{title_str} {dept_str}")
    if dyn_skills:
        skills.extend(dyn_skills)
    else:
        # Fallback to predefined static map
        for k, s_list in JOB_PROFILE_SKILLS_MAP.items():
            if k.lower() in title_str.lower() or k.lower() in dept_str.lower():
                skills.extend(s_list)

    # 2. Comprehensive tech & role term scanning
    tech_candidates = [
        ("php", "PHP"),
        ("frontend developer", "Frontend Developer"),
        ("frontend", "Frontend Developer"),
        ("front end", "Frontend Developer"),
        ("front-end", "Frontend Developer"),
        ("next.js", "Next.js"),
        ("next js", "Next.js"),
        ("nextjs", "Next.js"),
        ("jquery", "jQuery"),
        ("data analysis", "Data Analysis"),
        ("data analyst", "Data Analysis"),
        ("data analytics", "Data Analysis"),
        ("python", "Python"),
        ("java", "Java"),
        ("react", "React"),
        ("react.js", "React"),
        ("reactjs", "React"),
        ("node.js", "Node.js"),
        ("nodejs", "Node.js"),
        ("node js", "Node.js"),
        ("sql", "SQL"),
        ("mysql", "MySQL"),
        ("fastapi", "FastAPI"),
        ("django", "Django"),
        ("aws", "AWS"),
        ("docker", "Docker"),
        ("javascript", "JavaScript"),
        ("typescript", "TypeScript"),
        ("c++", "C++"),
        ("flutter", "Flutter"),
    ]

    for term, canon in tech_candidates:
        pattern = r"\b" + re.escape(term) + r"\b"
        if re.search(pattern, text):
            skills.append(canon)

    # Clean & deduplicate while preserving canonical representation
    seen = set()
    canonical_skills = []
    for s in skills:
        c_skill = canonicalize_skill(s)
        key = c_skill.lower().strip()
        if key and key not in seen:
            seen.add(key)
            canonical_skills.append(c_skill)

    return canonical_skills


class JobRepository:
    PER_SKILL_RETRIEVAL_LIMIT: int = 20

    @staticmethod
    def search_jobs(role_keyword: str, location: str, limit: int = 15) -> List[Dict[str, Any]]:
        conn = get_db()
        c = conn.cursor()

        base = """
            SELECT j.id, j.job_title, j.job_description, j.experience,
                   j.vacancy, j.job_mode, j.urgent,
                   d.name AS dept_name,
                   s.name AS state_name
            FROM cyb_company_job j
            LEFT JOIN cyb_department d ON j.department = d.id
            LEFT JOIN cyb_state s ON j.state = s.id AND s.country = 101
            WHERE j.status = 1 AND j.is_deleted = 0
        """

        rows = []
        role_conds, role_params = [], []
        if role_keyword:
            for word in [w for w in role_keyword.split() if len(w) > 2]:
                role_conds.append(
                    "(j.job_title LIKE %s OR j.job_description LIKE %s OR d.name LIKE %s)"
                )
                role_params.extend([f"%{word}%", f"%{word}%", f"%{word}%"])

        loc_cond, loc_params = "", []
        if location:
            loc_cond = " AND (s.name LIKE %s OR j.location LIKE %s)"
            loc_params = [f"%{location}%", f"%{location}%"]

        order = f" ORDER BY j.urgent DESC, j.create_date DESC LIMIT {limit}"

        if role_conds:
            q = base + " AND " + " AND ".join(role_conds) + loc_cond + order
            c.execute(q, role_params + loc_params)
            rows = c.fetchall()
            if not rows and len(role_conds) > 1:
                q = base + " AND (" + " OR ".join(role_conds) + ")" + loc_cond + order
                c.execute(q, role_params + loc_params)
                rows = c.fetchall()
            if not rows and loc_cond:
                q = base + " AND " + " AND ".join(role_conds) + order
                c.execute(q, role_params)
                rows = c.fetchall()
        else:
            q = base + loc_cond + order
            c.execute(q, loc_params)
            rows = c.fetchall()

        conn.close()
        results = []
        seen = set()
        for row in rows:
            title = (row["job_title"] or "").strip().lower()
            dept = (row["dept_name"] or "").strip().lower()
            state = (row["state_name"] or "").strip().lower()
            exp = str(row["experience"] or "").strip().lower()
            
            unique_key = (title, dept, state, exp)
            if unique_key in seen:
                continue
            seen.add(unique_key)

            desc = re.sub(r"<[^>]+>", " ", str(row["job_description"] or ""))
            desc = re.sub(r"\s+", " ", desc).strip()[:300]
            results.append({
                "id": row["id"],
                "job_title": row["job_title"],
                "title":      row["job_title"],
                "department": (row["dept_name"] or "").strip(),
                "state":      row["state_name"] or "",
                "experience": row["experience"] or "Not specified",
                "vacancy":    row["vacancy"],
                "mode":       JOB_MODE_MAP.get(row["job_mode"], "Office"),
                "preview":    desc,
            })

        if role_keyword:
            clean_kw = role_keyword.lower().strip()
            words = [w for w in clean_kw.split() if len(w) > 2]

            def _calc_relevance(r: dict) -> float:
                score = 0.0
                t = (r.get("job_title") or "").lower()
                d = (r.get("department") or "").lower()
                desc = (r.get("preview") or "").lower()

                if clean_kw in t:
                    score += 500.0
                elif all(w in t for w in words if len(w) > 2):
                    score += 200.0
                elif clean_kw in d:
                    score += 100.0

                matched_words = 0
                for w in words:
                    if w in t:
                        score += 30.0
                        matched_words += 1
                    elif w in d:
                        score += 15.0
                    elif w in desc:
                        score += 1.0

                if len(words) > 1 and matched_words < len(words) and clean_kw not in t and clean_kw not in d:
                    score *= 0.2

                return score

            results.sort(key=_calc_relevance, reverse=True)
            # Filter out low-relevance noise (score < 30) if high-relevance matches exist
            if any(_calc_relevance(r) >= 50.0 for r in results):
                results = [r for r in results if _calc_relevance(r) >= 30.0]

        return results

    @staticmethod
    def search_faqs(keyword: str = "") -> List[Dict[str, str]]:
        conn = get_db()
        if keyword:
            rows = conn.execute("""
                SELECT question, answer FROM cyb_faqs
                WHERE status = 1 AND (question LIKE %s OR answer LIKE %s)
                LIMIT 5
            """, [f"%{keyword}%", f"%{keyword}%"]).fetchall()
        else:
            rows = conn.execute(
                "SELECT question, answer FROM cyb_faqs WHERE status = 1 LIMIT 10"
            ).fetchall()
        conn.close()
        return [{"question": r["question"], "answer": r["answer"]} for r in rows]

    @staticmethod
    def _map_job_row(row: dict) -> Dict[str, Any]:
        """Maps a raw DB job row dictionary to JobProfile dictionary payload."""
        title = (row.get("job_title") or "Job Title").strip()
        company = (row.get("company_name") or "Company").strip()
        dept = (row.get("dept_name") or "").strip()
        desc = row.get("job_description") or ""

        skills = infer_job_skills(title, dept, desc)

        exp_val = 0.0
        if row.get("experience"):
            exp_str = str(row["experience"])
            num_match = re.search(r"(\d+)", exp_str)
            if num_match:
                exp_val = float(num_match.group(1))

        sal_min = None
        sal_max = None
        if row.get("salary"):
            try:
                sal_min = float(row["salary"])
                sal_max = sal_min
            except Exception:
                pass

        return {
            "id": row["id"],
            "title": title,
            "company_name": company,
            "required_skills": skills,
            "required_experience_years": exp_val,
            "location": row.get("location") or row.get("city_name") or row.get("state_name"),
            "city": row.get("city_name"),
            "state": row.get("state_name"),
            "country": row.get("country_name"),
            "job_mode": row.get("job_mode") if row.get("job_mode") in (1, 2, 3) else 1,
            "offered_salary_min": sal_min,
            "offered_salary_max": sal_max,
            "raw_data": dict(row) if isinstance(row, dict) else {}
        }

    @staticmethod
    def get_active_jobs(limit: int = 50) -> List[Dict[str, Any]]:
        """
        Retrieves active jobs with profile metadata mapped for RecommendationEngine.
        """
        conn = get_db()
        c = conn.cursor()
        query = """
            SELECT j.id, j.job_title, j.job_description, j.experience,
                   j.vacancy, j.job_mode, j.urgent, j.location,
                   j.salary,
                   d.name AS dept_name,
                   s.name AS state_name,
                   ct.name AS city_name,
                   cnt.name AS country_name,
                   COALESCE(comp.full_name, comp.fname) AS company_name
            FROM cyb_company_job j
            LEFT JOIN cyb_department d ON j.department = d.id
            LEFT JOIN cyb_state s ON j.state = s.id
            LEFT JOIN cyb_cities ct ON j.city = ct.id
            LEFT JOIN cyb_country cnt ON j.country = cnt.id
            LEFT JOIN cyb_user comp ON j.company = comp.id
            WHERE j.status = 1 AND (j.is_deleted IS NULL OR j.is_deleted = 0)
            ORDER BY j.id DESC LIMIT %s
        """
        try:
            c.execute(query, (limit,))
            rows = c.fetchall()
            return [JobRepository._map_job_row(r) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def get_jobs_for_skills(
        skills: List[str], limit: int = 50, per_skill_limit: int = 20, job_mode: Optional[int] = None, city_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Retrieves active jobs matching ANY candidate skill via balanced per-skill candidate retrieval.
        Optionally filters by work mode (e.g. job_mode=2 for Remote) and city_id.
        """
        if not skills:
            return []

        clean_skills = [canonicalize_skill(s) for s in skills if s and str(s).strip()]
        if not clean_skills:
            return []

        conn = get_db()
        c = conn.cursor()

        mode_clause = " AND j.job_mode = %s" if job_mode is not None else ""
        city_clause = " AND j.city = %s" if city_id is not None else ""
        query_template = f"""
            SELECT j.id, j.job_title, j.job_description, j.experience,
                   j.vacancy, j.job_mode, j.urgent, j.location,
                   j.salary,
                   d.name AS dept_name,
                   s.name AS state_name,
                   ct.name AS city_name,
                   cnt.name AS country_name,
                   COALESCE(comp.full_name, comp.fname) AS company_name
            FROM cyb_company_job j
            LEFT JOIN cyb_department d ON j.department = d.id
            LEFT JOIN cyb_state s ON j.state = s.id
            LEFT JOIN cyb_cities ct ON j.city = ct.id
            LEFT JOIN cyb_country cnt ON j.country = cnt.id
            LEFT JOIN cyb_user comp ON j.company = comp.id
            WHERE j.status = 1 AND (j.is_deleted IS NULL OR j.is_deleted = 0)
              {mode_clause}
              {city_clause}
              AND ({{where_clause}})
            ORDER BY j.id DESC LIMIT %s
        """

        merged_jobs = []
        seen_ids = set()

        try:
            for sk in clean_skills:
                terms = [sk.lower()]
                if sk == "Next.js":
                    terms.extend(["next js", "nextjs", "next.js"])
                elif sk == "jQuery":
                    terms.extend(["jquery"])
                elif sk == "Data Analysis":
                    terms.extend(["data analysis", "data analyst", "data analytics"])
                elif sk == "Frontend Developer":
                    terms.extend(["frontend", "front end", "front-end"])
                elif sk == "PHP":
                    terms.extend(["php"])

                conds = []
                params = []
                if job_mode is not None:
                    params.append(job_mode)
                if city_id is not None:
                    params.append(city_id)

                for t in terms:
                    conds.append("(LOWER(j.job_title) LIKE %s OR LOWER(j.job_description) LIKE %s OR LOWER(d.name) LIKE %s)")
                    pattern = f"%{t}%"
                    params.extend([pattern, pattern, pattern])

                where_clause = " OR ".join(conds)
                query = query_template.format(where_clause=where_clause)

                c.execute(query, params + [per_skill_limit])
                rows = c.fetchall()

                seen_title_company = set()
                for row in rows:
                    j_id = row["id"]
                    if j_id not in seen_ids:
                        t_norm = str(row.get("job_title") or "").strip().lower()
                        c_norm = str(row.get("company_name") or "").strip().lower()
                        words = re.findall(r"[a-zA-Z]{3,}", t_norm)
                        stop_w = {"job", "job1", "job2", "job3", "and", "the", "for", "with", "needed", "urgently"}
                        token_tuple = tuple(sorted([w for w in words if w not in stop_w]))
                        dedup_key = (c_norm, token_tuple) if token_tuple else (c_norm, t_norm)

                        if dedup_key in seen_title_company:
                            continue

                        seen_ids.add(j_id)
                        seen_title_company.add(dedup_key)
                        job_dict = JobRepository._map_job_row(row)
                        merged_jobs.append(job_dict)
                        if len(merged_jobs) >= limit:
                            break
                if len(merged_jobs) >= limit:
                    break

            return merged_jobs
        finally:
            conn.close()


