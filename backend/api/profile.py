from fastapi import APIRouter, Depends, HTTPException, Request
from typing import Optional
import sqlite3

try:
    from backend.repositories.db import get_db
    from backend.api.auth import verify_token
    from backend.schemas.schemas import ProfileUpdateRequest, ProfileMissingFieldsResponse, AddressUpdateRequest, SkillAddRequest, EducationAddRequest, EmploymentAddRequest
except ModuleNotFoundError:
    from repositories.db import get_db  # type: ignore
    from api.auth import verify_token  # type: ignore
    from schemas.schemas import ProfileUpdateRequest, ProfileMissingFieldsResponse, AddressUpdateRequest, SkillAddRequest, EducationAddRequest, EmploymentAddRequest  # type: ignore

router = APIRouter(prefix="/profile", tags=["profile"])

@router.get("/missing-fields", response_model=ProfileMissingFieldsResponse)
async def get_missing_fields(req: Request, _token_payload: dict = Depends(verify_token)):
    email = req.state.email
    role = req.state.role
    user_type = 1 if role == "employee" else 2

    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                """
                SELECT id, email, fname, lname, phone, gender, profile_description, city, state, country
                FROM cyb_user
                WHERE LOWER(email) = %s AND user_type = %s AND status = 1 AND (is_deleted IS NULL OR is_deleted = 0)
                LIMIT 1
                """,
                (email.lower().strip(), user_type)
            )
            row = c.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="User profile not found in database.")

            missing_fields = []
            fields_to_check = ["fname", "lname", "phone", "gender", "dob", "profile_description", "city", "state", "country", "expected_salary"]

            for field in fields_to_check:
                val = row[field]
                if val is None:
                    missing_fields.append(field)
                elif isinstance(val, str) and not val.strip():
                    missing_fields.append(field)
                elif isinstance(val, int) and val == 0:
                    missing_fields.append(field)

            if not row.get("profile"):
                missing_fields.append("profile_image")

            if not row.get("resume"):
                missing_fields.append("resume")

            # Also check if skills, education, employment/experience, or certificates are missing
            c.execute("SELECT COUNT(*) FROM cyb_user_skill WHERE user = %s AND status = 1 AND (is_deleted IS NULL OR is_deleted = 0)", (row["id"],))
            skills_cnt = c.fetchone()["COUNT(*)"]
            if skills_cnt == 0:
                missing_fields.append("skills")

            c.execute("SELECT COUNT(*) FROM cyb_user_education WHERE user = %s AND status = 1 AND (is_deleted IS NULL OR is_deleted = 0)", (row["id"],))
            edu_cnt = c.fetchone()["COUNT(*)"]
            if edu_cnt == 0:
                missing_fields.append("education")

            c.execute("SELECT COUNT(*) FROM cyb_user_experience WHERE user = %s AND status = 1 AND (is_deleted IS NULL OR is_deleted = 0)", (row["id"],))
            exp_cnt = c.fetchone()["COUNT(*)"]
            if exp_cnt == 0:
                missing_fields.append("employment/experience")

            c.execute("SELECT COUNT(*) FROM cyb_user_certificate WHERE user = %s AND status = 1 AND (is_deleted IS NULL OR is_deleted = 0)", (row["id"],))
            cert_cnt = c.fetchone()["COUNT(*)"]
            if cert_cnt == 0:
                missing_fields.append("certifications")

            return ProfileMissingFieldsResponse(
                user_id=row["id"],
                email=row["email"],
                missing_fields=missing_fields,
                profile_complete=len(missing_fields) == 0
            )
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        log.exception("Database error in get_missing_fields: %s", e)
        raise HTTPException(status_code=500, detail="Database error occurred while fetching profile fields.")

@router.put("/update")
async def update_profile(
    request: ProfileUpdateRequest,
    req: Request,
    _token_payload: dict = Depends(verify_token)
):
    email = req.state.email
    role = req.state.role
    user_type = 1 if role == "employee" else 2

    # Get only the fields that were actually provided in the request
    update_data = request.model_dump(exclude_unset=True)
    if "phone" in update_data and update_data["phone"]:
        try:
            from backend.services.profile_parser import normalize_phone_number
        except ModuleNotFoundError:
            from services.profile_parser import normalize_phone_number  # type: ignore
        norm_phone = normalize_phone_number(update_data["phone"])
        if norm_phone:
            update_data["phone"] = norm_phone

    if "gender" in update_data and update_data["gender"] is not None:
        try:
            from backend.services.profile_parser import resolve_gender_id
        except ModuleNotFoundError:
            from services.profile_parser import resolve_gender_id  # type: ignore
        gender_id = resolve_gender_id(update_data["gender"])
        if gender_id is not None:
            update_data["gender"] = gender_id

    if not update_data:
        return {"success": True, "message": "No profile fields to update."}


    try:
        with get_db() as conn:
            c = conn.cursor()
            # Check if user exists
            c.execute(
                "SELECT id FROM cyb_user WHERE LOWER(email) = %s AND user_type = %s AND status = 1 AND (is_deleted IS NULL OR is_deleted = 0) LIMIT 1",
                (email.lower().strip(), user_type)
            )
            user = c.fetchone()
            if not user:
                raise HTTPException(status_code=404, detail="User profile not found in database.")

            # Build dynamic query
            set_clauses = []
            params = []
            for key, val in update_data.items():
                set_clauses.append(f"{key} = %s")
                params.append(val)

            # Add email and user_type for WHERE clause
            params.extend([email.lower().strip(), user_type])

            query = f"""
                UPDATE cyb_user
                SET {', '.join(set_clauses)}, modify_date = NOW()
                WHERE LOWER(email) = %s AND user_type = %s AND status = 1 AND (is_deleted IS NULL OR is_deleted = 0)
            """

            c.execute(query, params)
            conn.commit()

            return {"success": True, "message": "Profile updated successfully.", "updated_fields": list(update_data.keys())}
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        log.exception("Database error in update_profile: %s", e)
        raise HTTPException(status_code=500, detail="Database error occurred while updating profile.")


@router.put("/address")
async def update_address(
    request: AddressUpdateRequest,
    req: Request,
    _token_payload: dict = Depends(verify_token)
):
    email = req.state.email
    role = req.state.role
    user_type = 1 if role == "employee" else 2

    raw_data = request.model_dump(exclude_unset=True)
    if not raw_data:
        raise HTTPException(status_code=400, detail="No address fields provided.")

    try:
        from backend.services.profile_parser import resolve_state_id, resolve_city_id, resolve_country_id, resolve_city_details
    except ModuleNotFoundError:
        from services.profile_parser import resolve_state_id, resolve_city_id, resolve_country_id, resolve_city_details  # type: ignore

    db_updates = {}
    profile_link = "https://www.collarcheck.com/dashboard/user/profile?tab=address"

    # Present address vs Permanent address logic
    pres_addr = raw_data.get("present_address")
    perm_addr = raw_data.get("permanent_address")
    gen_addr = raw_data.get("address") or raw_data.get("street_address")
    addr_type = raw_data.get("address_type")
    same_addr = raw_data.get("same_address")

    if pres_addr:
        db_updates["present_address"] = pres_addr
    if perm_addr:
        db_updates["permanent_address"] = perm_addr

    # If general address provided:
    if gen_addr and not pres_addr and not perm_addr:
        if addr_type == "permanent":
            db_updates["permanent_address"] = gen_addr
        elif addr_type == "both" or same_addr == 1:
            db_updates["present_address"] = gen_addr
            db_updates["permanent_address"] = gen_addr
            db_updates["same_address"] = 1
        else: # default to present address
            db_updates["present_address"] = gen_addr

    # Check same_address flag
    if same_addr is not None:
        db_updates["same_address"] = 1 if same_addr else 0
        if same_addr == 1:
            # If same_address is 1, copy present_address to permanent_address if present exists
            if db_updates.get("present_address"):
                db_updates["permanent_address"] = db_updates["present_address"]
            elif db_updates.get("permanent_address"):
                db_updates["present_address"] = db_updates["permanent_address"]

    # Resolve location IDs using smart semantic matching
    state_val = raw_data.get("state")
    state_id = None
    if isinstance(state_val, int):
        state_id = state_val
    elif isinstance(state_val, str) and state_val.strip():
        state_id = resolve_state_id(state_val)
    if state_id:
        db_updates["state"] = state_id
        if isinstance(state_val, str):
            city_details = resolve_city_details(state_val)
            if city_details and city_details.get("city_id"):
                db_updates["city"] = city_details["city_id"]

    city_val = raw_data.get("city")
    if isinstance(city_val, int):
        db_updates["city"] = city_val
    elif isinstance(city_val, str) and city_val.strip():
        city_details = resolve_city_details(city_val)
        if city_details:
            db_updates["city"] = city_details["city_id"]
            if city_details.get("state_id") and not db_updates.get("state"):
                db_updates["state"] = city_details["state_id"]
        else:
            city_id = resolve_city_id(city_val, state_id)
            if city_id:
                db_updates["city"] = city_id

    country_val = raw_data.get("country")
    if isinstance(country_val, int):
        db_updates["country"] = country_val
    elif isinstance(country_val, str) and country_val.strip():
        country_id = resolve_country_id(country_val)
        if country_id:
            db_updates["country"] = country_id

    if not db_updates:
        return {
            "success": True,
            "message": "No valid address fields to update.",
            "updated_fields": [],
            "profile_link": profile_link
        }

    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT id, present_address, permanent_address FROM cyb_user WHERE LOWER(email) = %s AND user_type = %s AND status = 1 AND (is_deleted IS NULL OR is_deleted = 0) LIMIT 1",
                (email.lower().strip(), user_type)
            )
            user = c.fetchone()
            if not user:
                raise HTTPException(status_code=404, detail="User profile not found in database.")

            # If same_address flag set to 1 without explicit text, copy from existing DB record
            if db_updates.get("same_address") == 1:
                curr_pres = db_updates.get("present_address") or user["present_address"]
                if curr_pres:
                    db_updates["present_address"] = curr_pres
                    db_updates["permanent_address"] = curr_pres

            set_clauses = [f"{k} = %s" for k in db_updates.keys()]
            params = list(db_updates.values())
            params.extend([email.lower().strip(), user_type])

            query = f"""
                UPDATE cyb_user
                SET {', '.join(set_clauses)}, modify_date = NOW()
                WHERE LOWER(email) = %s AND user_type = %s AND status = 1 AND (is_deleted IS NULL OR is_deleted = 0)
            """

            c.execute(query, params)
            conn.commit()

            return {
                "success": True,
                "message": f"Address updated successfully. You can view your updated address profile here: {profile_link}",
                "updated_fields": list(db_updates.keys()),
                "profile_link": profile_link
            }
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        log.exception("Database error in update_address: %s", e)
        raise HTTPException(status_code=500, detail="Database error occurred while updating address.")


@router.get("/skills")
async def get_user_skills(
    req: Request,
    _token_payload: dict = Depends(verify_token)
):
    email = req.state.email
    role = req.state.role
    user_type = 1 if role == "employee" else 2

    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT id FROM cyb_user WHERE LOWER(email) = %s AND user_type = %s AND status = 1 AND (is_deleted IS NULL OR is_deleted = 0) LIMIT 1",
                (email.lower().strip(), user_type)
            )
            user = c.fetchone()
            if not user:
                raise HTTPException(status_code=404, detail="User profile not found.")

            user_id = user["id"]
            c.execute(
                """
                SELECT us.id as user_skill_id, us.skill as skill_id, s.name as skill_name, us.rating
                FROM cyb_user_skill us
                JOIN cyb_skill s ON us.skill = s.id
                WHERE us.user = %s AND us.status = 1 AND (us.is_deleted IS NULL OR us.is_deleted = 0)
                """,
                (user_id,)
            )
            rows = c.fetchall()
            skills_list = []
            for r in rows:
                skills_list.append({
                    "user_skill_id": r["user_skill_id"],
                    "skill_id": r["skill_id"],
                    "skill_name": r["skill_name"],
                    "rating": r["rating"]
                })

            return {"success": True, "skills": skills_list}
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        log.exception("Database error in get_user_skills: %s", e)
        raise HTTPException(status_code=500, detail="Database error occurred while fetching skills.")


@router.post("/skills")
async def add_user_skills(
    request: SkillAddRequest,
    req: Request,
    _token_payload: dict = Depends(verify_token)
):
    email = req.state.email
    role = req.state.role
    user_type = 1 if role == "employee" else 2

    if not request.skills:
        raise HTTPException(status_code=400, detail="No skills provided to add.")

    try:
        from backend.services.profile_parser import resolve_or_create_skill_id
    except ModuleNotFoundError:
        from services.profile_parser import resolve_or_create_skill_id  # type: ignore

    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT id FROM cyb_user WHERE LOWER(email) = %s AND user_type = %s AND status = 1 AND (is_deleted IS NULL OR is_deleted = 0) LIMIT 1",
                (email.lower().strip(), user_type)
            )
            user = c.fetchone()
            if not user:
                raise HTTPException(status_code=404, detail="User profile not found.")

            user_id = user["id"]
            added_skills = []
            rating = request.rating or 5

            for item in request.skills:
                resolved = resolve_or_create_skill_id(item)
                if resolved:
                    sk_id = resolved["skill_id"]
                    sk_name = resolved["skill_name"]

                    # Check if user already has this skill
                    c.execute(
                        "SELECT id FROM cyb_user_skill WHERE user = %s AND skill = %s AND (is_deleted IS NULL OR is_deleted = 0) LIMIT 1",
                        (user_id, sk_id)
                    )
                    existing = c.fetchone()
                    if not existing:
                        c.execute(
                            "INSERT INTO cyb_user_skill (user, skill, rating, status, is_deleted, create_date) VALUES (%s, %s, %s, 1, 0, NOW())",
                            (user_id, sk_id, rating)
                        )
                    else:
                        # Update status to active if soft-deleted
                        c.execute(
                            "UPDATE cyb_user_skill SET status = 1, is_deleted = 0, rating = %s, modify_date = NOW() WHERE id = %s",
                            (rating, existing["id"])
                        )

                    added_skills.append({"skill_id": sk_id, "skill_name": sk_name, "rating": rating})

            conn.commit()
            profile_link = "https://www.collarcheck.com/dashboard/user/profile?tab=skills"

            return {
                "success": True,
                "message": f"Successfully added/updated {len(added_skills)} skill(s). View your profile skills here: {profile_link}",
                "added_skills": added_skills,
                "profile_link": profile_link
            }
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        log.exception("Database error in add_user_skills: %s", e)
        raise HTTPException(status_code=500, detail="Database error occurred while adding skills.")


@router.get("/education")
async def get_user_education(
    req: Request,
    _token_payload: dict = Depends(verify_token)
):
    email = req.state.email
    role = req.state.role
    user_type = 1 if role == "employee" else 2

    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT id FROM cyb_user WHERE LOWER(email) = %s AND user_type = %s AND status = 1 AND (is_deleted IS NULL OR is_deleted = 0) LIMIT 1",
                (email.lower().strip(), user_type)
            )
            user = c.fetchone()
            if not user:
                raise HTTPException(status_code=404, detail="User profile not found.")

            user_id = user["id"]
            c.execute(
                """
                SELECT e.id, inst.name as university_name, crs.name as course_name, e.course_type, e.ishighest, e.starting_date, e.ending_date, e.ongoing, ct.name as city_name, st.name as state_name, cnt.name as country_name
                FROM cyb_user_education e
                LEFT JOIN cyb_institutions inst ON e.university = inst.id
                LEFT JOIN cyb_courses crs ON e.course = crs.id
                LEFT JOIN cyb_cities ct ON e.city = ct.id
                LEFT JOIN cyb_state st ON e.state = st.id
                LEFT JOIN cyb_country cnt ON e.country = cnt.id
                WHERE e.user = %s AND e.status = 1 AND (e.is_deleted IS NULL OR e.is_deleted = 0)
                """,
                (user_id,)
            )
            rows = c.fetchall()
            edu_list = []
            for r in rows:
                edu_list.append({
                    "id": r["id"],
                    "university": r["university_name"],
                    "course": r["course_name"],
                    "course_type": "Full Time" if r["course_type"] == 1 else "Online",
                    "starting_date": r["starting_date"],
                    "ending_date": r["ending_date"],
                    "ongoing": r["ongoing"],
                    "ishighest": r["ishighest"],
                    "city": r["city_name"],
                    "state": r["state_name"],
                    "country": r["country_name"]
                })

            return {"success": True, "education": edu_list}
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        log.exception("Database error in get_user_education: %s", e)
        raise HTTPException(status_code=500, detail="Database error occurred while fetching education.")


@router.put("/education")
async def add_user_education(
    request: EducationAddRequest,
    req: Request,
    _token_payload: dict = Depends(verify_token)
):
    email = req.state.email
    role = req.state.role
    user_type = 1 if role == "employee" else 2

    try:
        from backend.services.profile_parser import (
            resolve_or_create_institution_id,
            resolve_or_create_course_id,
            resolve_country_id,
            resolve_state_id,
            resolve_city_id,
            resolve_city_details,
        )
    except ModuleNotFoundError:
        from services.profile_parser import (  # type: ignore
            resolve_or_create_institution_id,
            resolve_or_create_course_id,
            resolve_country_id,
            resolve_state_id,
            resolve_city_id,
            resolve_city_details,
        )

    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT id FROM cyb_user WHERE LOWER(email) = %s AND user_type = %s AND status = 1 AND (is_deleted IS NULL OR is_deleted = 0) LIMIT 1",
                (email.lower().strip(), user_type)
            )
            user = c.fetchone()
            if not user:
                raise HTTPException(status_code=404, detail="User profile not found.")

            u_id = user["id"]

            univ_id = resolve_or_create_institution_id(request.university, user_id=u_id)["institution_id"] if request.university else None
            course_id = resolve_or_create_course_id(request.course, user_id=u_id)["course_id"] if request.course else None

            c_type = request.course_type
            course_type_val = 2 if c_type in (2, 0, "2", "0", "Online") else 1

            country_id = resolve_country_id(request.country) if isinstance(request.country, str) else request.country
            state_id = resolve_state_id(request.state) if isinstance(request.state, str) else request.state
            city_id = None
            if isinstance(request.city, str) and request.city.strip():
                c_details = resolve_city_details(request.city)
                if c_details:
                    city_id = c_details["city_id"]
                    if c_details.get("state_id") and not state_id:
                        state_id = c_details["state_id"]
                else:
                    city_id = resolve_city_id(request.city, state_id)
            elif isinstance(request.city, int):
                city_id = request.city

            ishighest_val = 1 if request.ishighest in (1, True, "1", "yes") else 0

            if ishighest_val == 1:
                c.execute("UPDATE cyb_user_education SET ishighest = 0 WHERE user = %s", (u_id,))

            c.execute(
                """
                INSERT INTO cyb_user_education
                (user, university, course, course_type, country, state, city, ishighest, starting_date, ending_date, ongoing, status, is_deleted, create_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, 0, NOW())
                """,
                (u_id, univ_id, course_id, course_type_val, country_id, state_id, city_id, ishighest_val, request.starting_date, request.ending_date, request.ongoing)
            )
            conn.commit()

            profile_link = "https://www.collarcheck.com/dashboard/user/profile?tab=education"
            return {"success": True, "message": f"Education record added/updated successfully. View profile: {profile_link}", "profile_link": profile_link}
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        log.exception("Database error in add_user_education: %s", e)
        raise HTTPException(status_code=500, detail="Database error occurred while adding education.")


@router.get("/employment")
async def get_user_employment(
    req: Request,
    _token_payload: dict = Depends(verify_token)
):
    email = req.state.email
    role = req.state.role
    user_type = 1 if role == "employee" else 2

    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT id FROM cyb_user WHERE LOWER(email) = %s AND user_type = %s AND status = 1 AND (is_deleted IS NULL OR is_deleted = 0) LIMIT 1",
                (email.lower().strip(), user_type)
            )
            user = c.fetchone()
            if not user:
                raise HTTPException(status_code=404, detail="User profile not found.")

            user_id = user["id"]
            c.execute(
                """
                SELECT e.id, e.company, d.name as designation_name, dept.name as department_name, et.name as emp_type_name, e.joining_date, e.worked_till_date, e.still_working, e.salary, e.salary_inhand, e.salary_mode, e.description, e.hired
                FROM cyb_user_experience e
                LEFT JOIN cyb_designation d ON e.designation = d.id
                LEFT JOIN cyb_department dept ON e.department = dept.id
                LEFT JOIN cyb_employement_type et ON e.employment_type = et.id
                WHERE e.user = %s AND e.status = 1 AND (e.is_deleted IS NULL OR e.is_deleted = 0)
                """,
                (user_id,)
            )
            rows = c.fetchall()
            emp_list = []
            for r in rows:
                c_val = r["company"] or ""
                if str(c_val).isdigit():
                    c.execute("SELECT fname, lname FROM cyb_user WHERE id = %s LIMIT 1", (int(c_val),))
                    comp_u = c.fetchone()
                    if comp_u:
                        c_val = f"{comp_u['fname'] or ''} {comp_u['lname'] or ''}".strip()

                emp_list.append({
                    "id": r["id"],
                    "company": c_val,
                    "designation": r["designation_name"],
                    "department": r["department_name"],
                    "employment_type": r["emp_type_name"],
                    "joining_date": r["joining_date"],
                    "worked_till_date": r["worked_till_date"],
                    "still_working": r["still_working"],
                    "salary": r["salary"],
                    "salary_inhand": r["salary_inhand"],
                    "salary_mode": r["salary_mode"],
                    "description": r["description"],
                    "hired": r["hired"]
                })

            return {"success": True, "employment": emp_list}
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        log.exception("Database error in get_user_employment: %s", e)
        raise HTTPException(status_code=500, detail="Database error occurred while fetching employment history.")


@router.put("/employment")
async def add_user_employment(
    request: EmploymentAddRequest,
    req: Request,
    _token_payload: dict = Depends(verify_token)
):
    email = req.state.email
    role = req.state.role
    user_type = 1 if role == "employee" else 2

    try:
        from backend.services.profile_parser import (
            resolve_or_create_designation_id,
            resolve_or_create_department_id,
            resolve_employment_type_id,
            resolve_company_id,
            resolve_or_create_skill_id,
        )
    except ModuleNotFoundError:
        from services.profile_parser import (  # type: ignore
            resolve_or_create_designation_id,
            resolve_or_create_department_id,
            resolve_employment_type_id,
            resolve_company_id,
            resolve_or_create_skill_id,
        )

    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT id FROM cyb_user WHERE LOWER(email) = %s AND user_type = %s AND status = 1 AND (is_deleted IS NULL OR is_deleted = 0) LIMIT 1",
                (email.lower().strip(), user_type)
            )
            user = c.fetchone()
            if not user:
                raise HTTPException(status_code=404, detail="User profile not found.")

            u_id = user["id"]

            comp_val = resolve_company_id(request.company) or request.company or ""
            desig_id = resolve_or_create_designation_id(request.designation, user_id=u_id)["designation_id"] if request.designation else None
            dept_id = resolve_or_create_department_id(request.department, user_id=u_id)["department_id"] if request.department else None
            emp_type_id = resolve_employment_type_id(request.employment_type)

            skill_ids_list = []
            if request.skill:
                for sk in request.skill:
                    res_sk = resolve_or_create_skill_id(sk)
                    if res_sk:
                        skill_ids_list.append(str(res_sk["skill_id"]))
            skill_json_str = json.dumps(skill_ids_list) if skill_ids_list else None

            c.execute(
                """
                INSERT INTO cyb_user_experience
                (user, company, designation, department, employment_type, joining_date, worked_till_date, still_working, hired, description, salary, salary_inhand, salary_mode, skill, status, is_deleted, create_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, 0, NOW())
                """,
                (u_id, str(comp_val), desig_id, dept_id, emp_type_id, request.joining_date, request.worked_till_date, request.still_working or 0, request.hired or 0, request.description or "", str(request.salary) if request.salary else None, request.salary_inhand or "CTC", request.salary_mode or "Annually", skill_json_str)
            )
            conn.commit()

            profile_link = "https://www.collarcheck.com/dashboard/user/profile?tab=experience"
            return {"success": True, "message": f"Employment experience record added/updated successfully. View profile: {profile_link}", "profile_link": profile_link}
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        log.exception("Database error in add_user_employment: %s", e)
        raise HTTPException(status_code=500, detail="Database error occurred while adding employment experience.")



