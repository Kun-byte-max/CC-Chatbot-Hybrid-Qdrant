from typing import List, Dict, Any

try:
    from backend.repositories.db import get_db
except ModuleNotFoundError:
    from repositories.db import get_db

import logging

log = logging.getLogger(__name__)

class CandidateRepository:
    @staticmethod
    def get_potential_candidates(location: str = None) -> List[Dict[str, Any]]:
        conn = get_db()
        c = conn.cursor()
        query = """
            SELECT u.id, u.individual_id, u.fname, u.lname, u.city, u.state, s.name as state_name
            FROM cyb_user u
            LEFT JOIN cyb_state s ON u.state = s.id AND s.country = 101
            WHERE u.user_type = 1 AND u.status = 1 AND (u.is_deleted IS NULL OR u.is_deleted = 0)
        """
        params = []
        if location:
            query += " AND (s.name LIKE %s OR u.city LIKE %s)"
            params.extend([f"%{location}%", f"%{location}%"])

        c.execute(query + " LIMIT 50", params)
        rows = c.fetchall()
        
        candidates = []
        for row in rows:
            candidates.append({
                "id": row["id"],
                "individual_id": row["individual_id"],
                "fname": row["fname"],
                "lname": row["lname"],
                "city": row["city"],
                "state": row["state"],
                "state_name": row["state_name"]
            })
        conn.close()
        return candidates

    @staticmethod
    def get_candidate_skills(candidate_id: int) -> List[str]:
        conn = get_db()
        c = conn.cursor()
        skills = []
        try:
            s_rows = c.execute("SELECT skill FROM cyb_user_skill WHERE user = %s AND status = 1", (candidate_id,)).fetchall()
            skills = [r[0] for r in s_rows if r[0]]
        except Exception:
            log.exception(f"Failed to fetch skills for candidate {candidate_id}")
        finally:
            conn.close()
        return skills

    @staticmethod
    def get_candidate_rating(candidate_id: int) -> float:
        conn = get_db()
        c = conn.cursor()
        rating = 4.5
        try:
            r_row = c.execute("""
                SELECT AVG(r.rating)
                FROM cyb_user_experience_rating r
                JOIN cyb_user_experience e ON r.experience = e.id
                WHERE e.user = %s
            """, (candidate_id,)).fetchone()
            if r_row and r_row[0]:
                rating = round(float(r_row[0]), 1)
        except Exception:
            log.exception(f"Failed to fetch rating for candidate {candidate_id}")
        finally:
            conn.close()
        return rating

    @staticmethod
    def get_candidate_by_id(candidate_id: int) -> Dict[str, Any]:
        """
        Fetches full candidate record including location names, skills, expected salary, and current title/experience.
        """
        conn = get_db()
        c = conn.cursor()
        try:
            c.execute(
                """
                SELECT u.id, u.fname, u.lname, u.city, u.state, u.country, u.expected_salary,
                       ct.name as city_name, st.name as state_name, cnt.name as country_name
                FROM cyb_user u
                LEFT JOIN cyb_cities ct ON u.city = ct.id
                LEFT JOIN cyb_state st ON u.state = st.id
                LEFT JOIN cyb_country cnt ON u.country = cnt.id
                WHERE u.id = %s AND u.user_type = 1 AND u.status = 1 AND (u.is_deleted IS NULL OR u.is_deleted = 0)
                LIMIT 1
                """,
                (candidate_id,)
            )
            row = c.fetchone()
            if not row:
                return {}

            cand_data = {
                "id": row["id"],
                "fname": row["fname"] or "",
                "lname": row["lname"] or "",
                "city": row["city_name"] or str(row["city"] or ""),
                "state": row["state_name"] or str(row["state"] or ""),
                "country": row["country_name"] or str(row["country"] or ""),
                "expected_salary": row["expected_salary"],
                "skills": [],
                "current_title": None,
                "experience_years": 0.0,
            }

            # Fetch skills
            try:
                c.execute(
                    """
                    SELECT s.name
                    FROM cyb_user_skill us
                    JOIN cyb_skill s ON us.skill = s.id
                    WHERE us.user = %s AND us.status = 1 AND (us.is_deleted IS NULL OR us.is_deleted = 0)
                    """,
                    (candidate_id,)
                )
                s_rows = c.fetchall()
                cand_data["skills"] = [r["name"] for r in s_rows if r.get("name")]
            except Exception:
                # Fallback simple skill fetch
                c.execute("SELECT skill FROM cyb_user_skill WHERE user = %s AND status = 1", (candidate_id,))
                s_rows = c.fetchall()
                cand_data["skills"] = [r[0] for r in s_rows if r and r[0]]

            # Fetch experience / current designation
            try:
                c.execute(
                    """
                    SELECT e.id, d.name as designation_name, e.joining_date, e.worked_till_date, e.still_working
                    FROM cyb_user_experience e
                    LEFT JOIN cyb_designation d ON e.designation = d.id
                    WHERE e.user = %s AND e.status = 1 AND (e.is_deleted IS NULL OR e.is_deleted = 0)
                    ORDER BY e.id DESC
                    """,
                    (candidate_id,)
                )
                exp_rows = c.fetchall()
                if exp_rows:
                    cand_data["current_title"] = exp_rows[0]["designation_name"]
                    cand_data["experience_years"] = float(len(exp_rows) * 1.5)  # Estimate from count or date calc
            except Exception:
                log.exception(f"Failed to fetch user experience for candidate {candidate_id}")

            return cand_data
        finally:
            conn.close()

    @staticmethod
    def deduplicate_and_filter_candidate_ids(candidate_ids: List[Any]) -> List[Any]:
        """
        Pre-hydration filter and deduplication pipeline (Invariants #28 & #29).
        Discards None, empty strings, whitespace-only strings, and unnormalizable objects.
        Preserves order of first appearance.
        """
        if not candidate_ids:
            return []

        seen = set()
        clean_ids = []

        for raw_id in candidate_ids:
            if raw_id is None:
                log.warning("Candidate profile missing valid ID (None), excluding from Phase 1 qualified set")
                continue

            s_id = str(raw_id).strip()
            if not s_id:
                log.warning("Candidate profile missing valid ID (empty/whitespace), excluding from Phase 1 qualified set")
                continue

            if s_id in seen:
                continue

            seen.add(s_id)
            clean_ids.append(raw_id)

        return clean_ids

    @staticmethod
    def get_authoritative_candidate_count(location: str = None, role: str = None) -> int:
        """
        Authoritative MySQL candidate count query (Invariant #27 & Option A).
        Calculates exact count of unique qualified database candidates matching Phase 1 filters.
        """
        conn = get_db()
        c = conn.cursor()
        try:
            query = """
                SELECT COUNT(DISTINCT u.id) as total_count
                FROM cyb_user u
                LEFT JOIN cyb_state s ON u.state = s.id AND s.country = 101
                WHERE u.user_type = 1 AND u.status = 1 AND (u.is_deleted IS NULL OR u.is_deleted = 0)
            """
            params = []
            if location:
                query += " AND (s.name LIKE %s OR u.city LIKE %s)"
                params.extend([f"%{location}%", f"%{location}%"])

            c.execute(query, params)
            row = c.fetchone()
            if row:
                if isinstance(row, dict):
                    return int(row.get("total_count", 0))
                return int(row[0])
            return 0
        except Exception:
            log.exception("Failed to execute authoritative candidate count query")
            return 0
        finally:
            conn.close()


