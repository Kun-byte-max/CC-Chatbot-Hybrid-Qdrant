from typing import List, Dict, Any, Optional
import logging
import re

try:
    from backend.repositories.db import get_db
except ModuleNotFoundError:
    from repositories.db import get_db  # type: ignore

log = logging.getLogger(__name__)


def normalize_company_name(name: Optional[str]) -> str:
    """
    Normalizes company names across queries and DB records to a canonical token:
    E.g. 'Collar Check Pvt. Ltd.' -> 'collarcheck'
         'Collar-Check Inc.'     -> 'collarcheck'
         'VVDN Technology'       -> 'vvdntechnology'
    """
    if not name:
        return ""
    text = name.lower().strip()
    text = re.sub(r"\b(pvt|private|ltd|limited|inc|incorporated|llc|corp|corporation)\b", "", text)
    text = re.sub(r"[^a-z0-9]", "", text)
    return text


class PeopleRepository:
    """
    Dedicated Repository for Phase 3 Employee -> People Search.
    Queries active individual candidate/employee profiles (cyb_user where user_type = 1 and status = 1).
    Encapsulates strict current-title, current-company, skills, location, and current-user exclusion filters.
    """

    @staticmethod
    def search_people(
        skills: Optional[List[str]] = None,
        title: Optional[str] = None,
        city_id: Optional[int] = None,
        state_id: Optional[int] = None,
        company: Optional[str] = None,
        exclude_user_id: Optional[int] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        Executes database SQL search for matching people profiles using AND conditions.
        - Title matching: Current designation only (cyb_designation via current experience e.still_working = 1 or latest e.id).
        - Company matching: Current company only (via current experience e.still_working = 1 or latest e.id).
        - Current user exclusion: Excludes exclude_user_id from results.
        """
        conn = get_db()
        cursor = conn.cursor()
        try:
            # Base SQL query joining active user, location names, and current experience
            query = """
                SELECT DISTINCT 
                    u.id, 
                    u.fname, 
                    u.lname, 
                    u.city, 
                    u.state, 
                    0.0 as total_experience_years,
                    ct.name as city_name, 
                    st.name as state_name, 
                    cnt.name as country_name
                FROM cyb_user u
                LEFT JOIN cyb_cities ct ON u.city = ct.id
                LEFT JOIN cyb_state st ON u.state = st.id AND st.country = 101
                LEFT JOIN cyb_country cnt ON u.country = cnt.id
            """
            params: List[Any] = []

            # Join current experience if title or company filter is requested
            if (title and title.strip()) or (company and company.strip()):
                query += """
                    JOIN (
                        SELECT e1.user, e1.designation, e1.company
                        FROM cyb_user_experience e1
                        JOIN (
                            SELECT user, MAX(CONCAT(LPAD(still_working, 2, '0'), '_', LPAD(id, 10, '0'))) as max_key
                            FROM cyb_user_experience
                            WHERE status = 1 AND (is_deleted IS NULL OR is_deleted = 0)
                            GROUP BY user
                        ) latest ON e1.user = latest.user AND CONCAT(LPAD(e1.still_working, 2, '0'), '_', LPAD(e1.id, 10, '0')) = latest.max_key
                    ) e ON e.user = u.id
                    LEFT JOIN cyb_designation d ON e.designation = d.id
                    LEFT JOIN cyb_user comp_u ON (str_to_date(NULL, '') IS NULL AND e.company REGEXP '^[0-9]+$' AND CAST(e.company AS UNSIGNED) = comp_u.id AND comp_u.user_type = 2)
                """

            query += """
                WHERE u.user_type = 1 
                  AND u.status = 1 
                  AND (u.is_deleted IS NULL OR u.is_deleted = 0)
            """

            # Exclude current authenticated user
            if exclude_user_id is not None:
                query += " AND u.id != %s"
                params.append(exclude_user_id)

            # Location Filters (AND condition)
            if city_id is not None:
                query += " AND u.city = %s"
                params.append(city_id)
            elif state_id is not None:
                query += " AND u.state = %s"
                params.append(state_id)

            # Title & Skills Matching Logic:
            # Broaden database filtering to allow candidate evaluation pool (skills or title matching)
            if title and title.strip() and skills:
                clean_title = title.strip().lower()
                title_words = [w for w in clean_title.split() if len(w) > 2]
                title_clause = " OR ".join(["LOWER(d.name) LIKE %s" for _ in title_words]) if title_words else "LOWER(d.name) LIKE %s"
                if title_words:
                    params.extend([f"%{w}%" for w in title_words])
                else:
                    params.append(f"%{clean_title}%")

                skill_clauses = []
                for s in skills:
                    if s and s.strip():
                        skill_clauses.append("(LOWER(sk.name) LIKE LOWER(%s) OR LOWER(us.skill) LIKE LOWER(%s))")
                        params.extend([f"%{s.strip()}%", f"%{s.strip()}%"])

                skill_or_sql = " OR ".join(skill_clauses)
                query += f"""
                    AND (
                        ({title_clause})
                        OR u.id IN (
                            SELECT us.user 
                            FROM cyb_user_skill us
                            LEFT JOIN cyb_skill sk ON us.skill = sk.id
                            WHERE us.status = 1 AND (us.is_deleted IS NULL OR us.is_deleted = 0)
                              AND ({skill_or_sql})
                        )
                    )
                """

            elif title and title.strip():
                clean_title = title.strip().lower()
                title_words = [w for w in clean_title.split() if len(w) > 2]
                if title_words:
                    title_clause = " OR ".join(["LOWER(d.name) LIKE %s" for _ in title_words])
                    query += f" AND ({title_clause})"
                    params.extend([f"%{w}%" for w in title_words])
                else:
                    query += " AND LOWER(d.name) LIKE %s"
                    params.append(f"%{clean_title}%")

            elif skills:
                skill_clauses = []
                for s in skills:
                    if s and s.strip():
                        skill_clauses.append("(LOWER(sk.name) LIKE LOWER(%s) OR LOWER(us.skill) LIKE LOWER(%s))")
                        params.extend([f"%{s.strip()}%", f"%{s.strip()}%"])
                if skill_clauses:
                    skill_or_sql = " OR ".join(skill_clauses)
                    query += f"""
                        AND u.id IN (
                            SELECT us.user 
                            FROM cyb_user_skill us
                            LEFT JOIN cyb_skill sk ON us.skill = sk.id
                            WHERE us.status = 1 AND (us.is_deleted IS NULL OR us.is_deleted = 0)
                              AND ({skill_or_sql})
                        )
                    """


            # Company Filter (Current Company ONLY, space/punctuation normalized matching)
            if company and company.strip():
                clean_comp = normalize_company_name(company)
                if clean_comp:
                    query += """
                        AND (
                            LOWER(REGEXP_REPLACE(e.company, '[^a-zA-Z0-9]', '')) LIKE %s
                            OR LOWER(REGEXP_REPLACE(CONCAT(COALESCE(comp_u.fname, ''), ' ', COALESCE(comp_u.lname, '')), '[^a-zA-Z0-9]', '')) LIKE %s
                        )
                    """
                    params.extend([f"%{clean_comp}%", f"%{clean_comp}%"])
                else:
                    raw_comp = company.strip().lower()
                    query += " AND (LOWER(e.company) LIKE %s OR LOWER(CONCAT(COALESCE(comp_u.fname, ''), ' ', COALESCE(comp_u.lname, ''))) LIKE %s)"
                    params.extend([f"%{raw_comp}%", f"%{raw_comp}%"])

            query += " ORDER BY u.id DESC LIMIT %s"

            params.append(limit)

            cursor.execute(query, params)
            rows = cursor.fetchall()

            people = []
            seen_ids = set()

            for r in rows:
                if isinstance(r, dict):
                    p_id = r["id"]
                    fname = r.get("fname") or ""
                    lname = r.get("lname") or ""
                    c_name = r.get("city_name") or str(r.get("city") or "")
                    s_name = r.get("state_name") or str(r.get("state") or "")
                    country_name = r.get("country_name") or ""
                else:
                    p_id = r[0]
                    fname = r[1] or ""
                    lname = r[2] or ""
                    c_name = r[5] or str(r[3] or "")
                    s_name = r[6] or str(r[4] or "")
                    country_name = r[7] or ""

                if p_id in seen_ids:
                    continue

                seen_ids.add(p_id)

                # Fetch CURRENT / Latest Experience Record for display
                curr_exp = PeopleRepository._get_current_experience(cursor, p_id)
                desig_name = curr_exp.get("designation_name")
                comp_name = curr_exp.get("company_name") or curr_exp.get("latest_company_name")

                loc_parts = [p for p in (c_name, s_name) if p and p.lower() not in ("none", "n/a", "null")]
                loc_str = ", ".join(loc_parts) if loc_parts else "Location not specified"

                user_skills = PeopleRepository._get_user_skills(cursor, p_id)

                tot_exp = r.get("total_experience_years") if isinstance(r, dict) else (r[5] if len(r) > 8 else 0.0)
                try:
                    tot_exp = float(tot_exp) if tot_exp is not None else 0.0
                except (ValueError, TypeError):
                    tot_exp = 0.0

                people.append({
                    "id": p_id,
                    "name": f"{fname} {lname}".strip() or "Anonymous User",
                    "fname": fname,
                    "lname": lname,
                    "current_title": desig_name or "Professional",
                    "company": comp_name or "Not specified",
                    "location": loc_str,
                    "city": c_name,
                    "state": s_name,
                    "country": country_name,
                    "skills": user_skills,
                    "total_experience_years": tot_exp,
                })

                if len(people) >= limit:
                    break

            return people
        except Exception:
            log.exception("Error executing search_people query in PeopleRepository")
            return []
        finally:
            conn.close()

    @staticmethod
    def _get_current_experience(cursor: Any, user_id: int) -> Dict[str, Any]:
        """
        Fetches the current experience record for a user following project convention:
        ORDER BY still_working DESC, id DESC LIMIT 1
        If still_working is 0 on all experiences, returns is_current=False so historical
        employers are not falsely labeled as active CURRENT COMPANY.
        """
        try:
            cursor.execute(
                """
                SELECT e.id, e.company, d.name as designation_name, e.still_working
                FROM cyb_user_experience e
                LEFT JOIN cyb_designation d ON e.designation = d.id
                WHERE e.user = %s AND e.status = 1 AND (e.is_deleted IS NULL OR e.is_deleted = 0)
                ORDER BY e.still_working DESC, e.id DESC
                LIMIT 1
                """,
                (user_id,)
            )
            row = cursor.fetchone()
            if not row:
                return {}

            if isinstance(row, dict):
                c_val = row.get("company") or ""
                desig = row.get("designation_name")
                is_current = bool(row.get("still_working"))
            else:
                c_val = row[1] or ""
                desig = row[2]
                is_current = bool(row[3]) if len(row) > 3 else False

            comp_name = str(c_val)
            if str(c_val).isdigit():
                cursor.execute("SELECT fname, lname FROM cyb_user WHERE id = %s AND user_type = 2 LIMIT 1", (int(c_val),))
                comp_u = cursor.fetchone()
                if comp_u:
                    if isinstance(comp_u, dict):
                        comp_name = f"{comp_u.get('fname') or ''} {comp_u.get('lname') or ''}".strip()
                    else:
                        comp_name = f"{comp_u[0] or ''} {comp_u[1] or ''}".strip()

            return {
                "company_name": comp_name if is_current else "",
                "latest_company_name": comp_name,
                "designation_name": desig,
                "is_current": is_current,
            }
        except Exception:
            return {}

    @staticmethod
    def _get_user_skills(cursor: Any, user_id: int) -> List[str]:
        """Helper method to fetch skills for a specific user."""
        try:
            cursor.execute(
                """
                SELECT COALESCE(s.name, us.skill) as skill_name
                FROM cyb_user_skill us
                LEFT JOIN cyb_skill s ON us.skill = s.id
                WHERE us.user = %s AND us.status = 1 AND (us.is_deleted IS NULL OR us.is_deleted = 0)
                """,
                (user_id,)
            )
            rows = cursor.fetchall()
            skills = []
            for r in rows:
                val = r["skill_name"] if isinstance(r, dict) else r[0]
                if val:
                    skills.append(str(val).strip())
            return skills
        except Exception:
            return []
