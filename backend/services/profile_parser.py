import asyncio
import json
import logging
import httpx
from typing import Dict, Any, Optional

try:
    from backend.services.llm_service import LLMService
    from backend.repositories.db import get_db
except ModuleNotFoundError:
    from services.llm_service import LLMService  # type: ignore
    from repositories.db import get_db  # type: ignore

log = logging.getLogger(__name__)

async def parse_profile_update(message: str, messages: list = None) -> Dict[str, Any]:
    prompt = """
    You are an AI assistant designed to extract profile, address, and skill updates from user messages for a career portal.
    
    Extract the following details from the user's message if they want to update their profile, address, add skills, or add education:
    - fname: The first name (e.g., "John")
    - lname: The last name (e.g., "Doe")
    - phone: The phone number (e.g., "+919999999999")
    - gender: The gender, preferably capitalized (e.g., "Male", "Female", "Other")
    - dob: Date of birth in YYYY-MM-DD format (e.g., "1997-09-24")
    - profile_description: The profile description or "about me" summary (e.g., "Experienced Full Stack Developer with 5 years in React and Python")
    - city: The city name (e.g., "Delhi NCR", "Mumbai")
    - state: The state name (e.g., "Delhi", "Maharashtra")
    - country: The country name (e.g., "India")
    - present_address: The present/current street address (e.g., "123 Main St, Sector 62")
    - permanent_address: The permanent home address (e.g., "456 Village Road")
    - same_address: Set to 1 if user indicates present and permanent addresses are the same (or if they ask to make them same), otherwise 0 or null
    - address_type: "present" if explicitly updating present address, "permanent" if explicitly updating permanent address, "both" if updating both or saying address is same
    - skills: List of skills the user wants to add to their profile (e.g. ["Python", "3D Graphics", "Project Management"])
    - education: Object or dictionary containing education details if the user mentions education/degree/university/college/course:
        - university: Name of the university/college/institution (e.g., "Delhi University", "IIT Bombay")
        - course: Name of the course or degree (e.g., "B.Tech Computer Science", "MBA", "B.Sc")
        - course_type: 1 if full time/regular, 2 if online/distance/part-time (or 0/2 based on text)
        - country: Country of the university (e.g., "India")
        - state: State of the university (e.g., "Delhi")
        - city: City of the university (e.g., "New Delhi")
        - ishighest: 1 if this is indicated as their highest qualification/degree, otherwise 0
        - starting_date: Start date of course in YYYY-MM-DD format (or YYYY or YYYY-MM if exact day missing, e.g. "2020-08-01")
        - ending_date: End/completion date of course in YYYY-MM-DD format (or YYYY or YYYY-MM, e.g. "2024-05-31")
        - ongoing: 1 if user is currently studying/pursuing this course, otherwise 0
    - is_profile_update_intent: Set to true ONLY if the user is explicitly telling you to update, set, change, or enter their profile/address/skills/education information (e.g. "update my gender to Male", "update my dob to 1997-09-24", "add B.Tech from Delhi University", "my address is 123 Main St"). If the message is a general query, search query, greeting, or faq, set it to false.
    
    Response MUST be a single raw JSON object matching this schema:
    {
      "fname": "string or null",
      "lname": "string or null",
      "phone": "string or null",
      "gender": "string or null",
      "dob": "string or null",
      "profile_description": "string or null",
      "city": "string or null",
      "state": "string or null",
      "country": "string or null",
      "present_address": "string or null",
      "permanent_address": "string or null",
      "same_address": integer or null,
      "address_type": "string or null",
      "skills": ["array of strings"] or null,
      "education": {
        "university": "string or null",
        "course": "string or null",
        "course_type": integer or null,
        "country": "string or null",
        "state": "string or null",
        "city": "string or null",
        "ishighest": integer or null,
        "starting_date": "string or null",
        "ending_date": "string or null",
        "ongoing": integer or null
      } or null,
      "employment": {
        "company": "string or null",
        "designation": "string or null",
        "department": "string or null",
        "joining_date": "string or null",
        "worked_till_date": "string or null",
        "still_working": integer or null,
        "employment_type": "string or integer or null",
        "hired": integer or null,
        "description": "string or null",
        "salary": "string or null",
        "salary_inhand": "string or null",
        "salary_mode": "string or null",
        "skill": ["array of strings"] or null
      } or null,
      "is_profile_update_intent": boolean
    }
    """
    
    history_str = ""
    if messages:
        recent = messages[-5:]
        formatted_msgs = []
        for m in recent:
            if isinstance(m, dict):
                formatted_msgs.append(f"{m.get('role', 'user').capitalize()}: {m.get('content', '')}")
            else:
                formatted_msgs.append(f"{m.role.capitalize()}: {m.content}")
        history_str = "\n    Conversation History:\n    " + "\n    ".join(formatted_msgs) + "\n"

    prompt += f"""{history_str}
    User message: "{message}"
    
    JSON:
    """
    try:
        completion = await LLMService.get_chat_completion(prompt, [], timeout=12.0, name="profile_parser")
        cleaned_completion = completion.strip()
        if "```json" in cleaned_completion:
            cleaned_completion = cleaned_completion.split("```json")[1].split("```")[0].strip()
        elif "```" in cleaned_completion:
            cleaned_completion = cleaned_completion.split("```")[1].split("```")[0].strip()
        
        parsed = json.loads(cleaned_completion)
        if isinstance(parsed, dict) and parsed.get("phone"):
            parsed["phone"] = normalize_phone_number(parsed["phone"])
        return parsed
    except (httpx.TimeoutException, asyncio.TimeoutError):
        log.warning("extraction_timeout parser=profile_parse")
        return {
            "fname": None,
            "lname": None,
            "phone": None,
            "gender": None,
            "profile_description": None,
            "city": None,
            "state": None,
            "country": None,
            "is_profile_update_intent": False
        }
    except Exception as e:
        log.exception("Error parsing profile update with LLM")
        return {
            "fname": None,
            "lname": None,
            "phone": None,
            "gender": None,
            "profile_description": None,
            "city": None,
            "state": None,
            "country": None,
            "is_profile_update_intent": False
        }

def resolve_city_details(city_name: str) -> Optional[Dict[str, int]]:
    """
    Given a city name, search cyb_cities and return a dict with {"city_id": int, "state_id": int}
    if found in DB.
    """
    if not city_name:
        return None
    city_name_lower = str(city_name).lower().strip()
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT id, state FROM cyb_cities WHERE LOWER(name) = %s AND status = 1 LIMIT 1",
            (city_name_lower,)
        )
        row = cursor.fetchone()
        if row:
            return {"city_id": int(row[0]), "state_id": int(row[1]) if row[1] else None}

        cursor.execute(
            "SELECT id, state FROM cyb_cities WHERE LOWER(name) LIKE %s AND status = 1 LIMIT 1",
            (f"%{city_name_lower}%",)
        )
        row = cursor.fetchone()
        if row:
            return {"city_id": int(row[0]), "state_id": int(row[1]) if row[1] else None}
    except Exception:
        log.exception("Failed to query city details from DB")
    finally:
        conn.close()
    return None

def resolve_state_id(state_name: str) -> Optional[int]:
    if not state_name:
        return None
    state_name_lower = str(state_name).lower().strip()
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT id FROM cyb_state WHERE LOWER(name) = %s AND status = 1 LIMIT 1",
            (state_name_lower,)
        )
        row = cursor.fetchone()
        if row:
            return int(row[0])
        
        cursor.execute(
            "SELECT id FROM cyb_state WHERE LOWER(name) LIKE %s AND status = 1 LIMIT 1",
            (f"%{state_name_lower}%",)
        )
        row = cursor.fetchone()
        if row:
            return int(row[0])

        # Semantic Fallback: Check if the user passed a city name under state
        cursor.execute(
            "SELECT state FROM cyb_cities WHERE LOWER(name) = %s AND status = 1 LIMIT 1",
            (state_name_lower,)
        )
        row = cursor.fetchone()
        if row and row[0]:
            return int(row[0])
    except Exception:
        log.exception("Failed to query state ID from DB")
    finally:
        conn.close()
    return None

def resolve_city_id(city_name: str, state_id: int = None) -> Optional[int]:
    if not city_name:
        return None
    city_name_lower = str(city_name).lower().strip()
    conn = get_db()
    cursor = conn.cursor()
    try:
        if state_id:
            cursor.execute(
                "SELECT id FROM cyb_cities WHERE LOWER(name) = %s AND state = %s AND status = 1 LIMIT 1",
                (city_name_lower, state_id)
            )
            row = cursor.fetchone()
            if row:
                return int(row[0])
        
        cursor.execute(
            "SELECT id FROM cyb_cities WHERE LOWER(name) = %s AND status = 1 LIMIT 1",
            (city_name_lower,)
        )
        row = cursor.fetchone()
        if row:
            return int(row[0])

        cursor.execute(
            "SELECT id FROM cyb_cities WHERE LOWER(name) LIKE %s AND status = 1 LIMIT 1",
            (f"%{city_name_lower}%",)
        )
        row = cursor.fetchone()
        if row:
            return int(row[0])
    except Exception:
        log.exception("Failed to query city ID from DB")
    finally:
        conn.close()
    return None

def resolve_country_id(country_name: str) -> Optional[int]:
    if not country_name:
        return None
    country_name_lower = country_name.lower().strip()
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT id FROM cyb_country WHERE LOWER(name) = %s AND status = 1 LIMIT 1",
            (country_name_lower,)
        )
        row = cursor.fetchone()
        if row:
            return int(row[0])
        
        cursor.execute(
            "SELECT id FROM cyb_country WHERE LOWER(name) LIKE %s AND status = 1 LIMIT 1",
            (f"%{country_name_lower}%",)
        )
        row = cursor.fetchone()
        if row:
            return int(row[0])
    except Exception:
        log.exception("Failed to query country ID from SQLite")
    finally:
        conn.close()
    return None

def resolve_gender_id(gender_name: Any) -> Optional[int]:
    if gender_name is None:
        return None
    gender_name_str = str(gender_name).strip().lower()
    if gender_name_str in ("1", "1.0", "male"):
        return 1
    elif gender_name_str in ("2", "2.0", "female"):
        return 2
    elif gender_name_str in ("3", "3.0", "others", "other"):
        return 3
    return None


def normalize_dob(dob_input: Any) -> Optional[str]:
    """
    Format and validate date of birth into YYYY-MM-DD format.
    Accepts formats like 1997-09-24, 24/09/1997, 24-09-1997, September 24 1997, etc.
    """
    if not dob_input:
        return None
    s = str(dob_input).strip()
    try:
        from datetime import datetime
        # Try YYYY-MM-DD
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            dt = datetime.strptime(s, "%Y-%m-%d")
            return dt.strftime("%Y-%m-%d")
        
        # Try DD-MM-YYYY or DD/MM/YYYY
        for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y", "%B %d, %Y"):
            try:
                dt = datetime.strptime(s, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
    except Exception:
        log.exception("Failed to parse date of birth")
    return s if len(s) == 10 and s[4] == "-" and s[7] == "-" else None


def normalize_phone_number(phone: Any) -> Optional[str]:
    if not phone:
        return None
    s = str(phone).strip()
    cleaned = "".join(c for c in s if c.isdigit() or c == "+")
    if not cleaned:
        return None
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    if cleaned.startswith("+"):
        digits_only = "".join(c for c in cleaned if c.isdigit())
        if 7 <= len(digits_only) <= 15:
            return cleaned
        return None
    digits = "".join(c for c in cleaned if c.isdigit())
    if len(digits) == 10:
        return f"+91{digits}"
    elif len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"
    elif len(digits) == 11 and digits.startswith("0"):
        return f"+91{digits[1:]}"
    return None


def resolve_or_create_skill_id(skill_input: Any) -> Optional[Dict[str, Any]]:
    """
    Given a skill name (str) or skill ID (int), search cyb_skill table.
    If skill name does not exist, insert new user-defined skill into cyb_skill and return its new ID.
    Returns dict: {"skill_id": int, "skill_name": str}
    """
    if not skill_input:
        return None

    conn = get_db()
    cursor = conn.cursor()
    try:
        # 1. If passed an integer ID or digit string
        if isinstance(skill_input, int) or (isinstance(skill_input, str) and skill_input.isdigit()):
            skill_id = int(skill_input)
            cursor.execute("SELECT id, name FROM cyb_skill WHERE id = %s LIMIT 1", (skill_id,))
            row = cursor.fetchone()
            if row:
                return {"skill_id": int(row[0]), "skill_name": str(row[1])}

        # 2. Match skill by name (exact or case-insensitive)
        skill_name = str(skill_input).strip()
        skill_name_lower = skill_name.lower()

        cursor.execute("SELECT id, name FROM cyb_skill WHERE LOWER(name) = %s LIMIT 1", (skill_name_lower,))
        row = cursor.fetchone()
        if row:
            return {"skill_id": int(row[0]), "skill_name": str(row[1])}

        cursor.execute("SELECT id, name FROM cyb_skill WHERE LOWER(name) LIKE %s LIMIT 1", (f"%{skill_name_lower}%",))
        row = cursor.fetchone()
        if row:
            return {"skill_id": int(row[0]), "skill_name": str(row[1])}

        # 3. Insert new custom skill into cyb_skill table
        cursor.execute(
            "INSERT INTO cyb_skill (name, status, user_defined, create_date) VALUES (%s, 1, 1, NOW())",
            (skill_name,)
        )
        new_skill_id = cursor.lastrowid
        conn.commit()
        return {"skill_id": int(new_skill_id), "skill_name": skill_name}
    except Exception:
        log.exception("Failed to resolve or create skill in DB")
        conn.rollback()
    finally:
        conn.close()
    return None


def resolve_or_create_institution_id(univ_input: Any, user_id: int = None) -> Optional[Dict[str, Any]]:
    """
    Search cyb_institutions by ID or name. If not found, insert user-defined institution.
    """
    if not univ_input:
        return None

    conn = get_db()
    cursor = conn.cursor()
    try:
        if isinstance(univ_input, int) or (isinstance(univ_input, str) and univ_input.isdigit()):
            u_id = int(univ_input)
            cursor.execute("SELECT id, name FROM cyb_institutions WHERE id = %s LIMIT 1", (u_id,))
            row = cursor.fetchone()
            if row:
                return {"institution_id": int(row[0]), "institution_name": str(row[1])}

        u_name = str(univ_input).strip()
        u_name_lower = u_name.lower()

        cursor.execute("SELECT id, name FROM cyb_institutions WHERE LOWER(name) = %s LIMIT 1", (u_name_lower,))
        row = cursor.fetchone()
        if row:
            return {"institution_id": int(row[0]), "institution_name": str(row[1])}

        cursor.execute("SELECT id, name FROM cyb_institutions WHERE LOWER(name) LIKE %s LIMIT 1", (f"%{u_name_lower}%",))
        row = cursor.fetchone()
        if row:
            return {"institution_id": int(row[0]), "institution_name": str(row[1])}

        cursor.execute(
            "INSERT INTO cyb_institutions (name, user_defined, status, user_id, create_date) VALUES (%s, 1, 1, %s, NOW())",
            (u_name, user_id)
        )
        new_id = cursor.lastrowid
        conn.commit()
        return {"institution_id": int(new_id), "institution_name": u_name}
    except Exception:
        log.exception("Failed to resolve or create institution in DB")
        conn.rollback()
    finally:
        conn.close()
    return None


def resolve_or_create_course_id(course_input: Any, user_id: int = None) -> Optional[Dict[str, Any]]:
    """
    Search cyb_courses by ID or name. If not found, insert user-defined course.
    """
    if not course_input:
        return None

    conn = get_db()
    cursor = conn.cursor()
    try:
        if isinstance(course_input, int) or (isinstance(course_input, str) and course_input.isdigit()):
            c_id = int(course_input)
            cursor.execute("SELECT id, name FROM cyb_courses WHERE id = %s LIMIT 1", (c_id,))
            row = cursor.fetchone()
            if row:
                return {"course_id": int(row[0]), "course_name": str(row[1])}

        c_name = str(course_input).strip()
        c_name_lower = c_name.lower()

        cursor.execute("SELECT id, name FROM cyb_courses WHERE LOWER(name) = %s LIMIT 1", (c_name_lower,))
        row = cursor.fetchone()
        if row:
            return {"course_id": int(row[0]), "course_name": str(row[1])}

        cursor.execute("SELECT id, name FROM cyb_courses WHERE LOWER(name) LIKE %s LIMIT 1", (f"%{c_name_lower}%",))
        row = cursor.fetchone()
        if row:
            return {"course_id": int(row[0]), "course_name": str(row[1])}

        cursor.execute(
            "INSERT INTO cyb_courses (name, user_defined, status, user_id, create_date) VALUES (%s, 1, 1, %s, NOW())",
            (c_name, user_id)
        )
        new_id = cursor.lastrowid
        conn.commit()
        return {"course_id": int(new_id), "course_name": c_name}
    except Exception:
        log.exception("Failed to resolve or create course in DB")
        conn.rollback()
    finally:
        conn.close()
    return None


def resolve_or_create_designation_id(desig_input: Any, user_id: int = None) -> Optional[Dict[str, Any]]:
    """
    Search cyb_designation by ID or name. If not found, insert user-defined designation.
    """
    if not desig_input:
        return None

    conn = get_db()
    cursor = conn.cursor()
    try:
        if isinstance(desig_input, int) or (isinstance(desig_input, str) and desig_input.isdigit()):
            d_id = int(desig_input)
            cursor.execute("SELECT id, name FROM cyb_designation WHERE id = %s LIMIT 1", (d_id,))
            row = cursor.fetchone()
            if row:
                return {"designation_id": int(row[0]), "designation_name": str(row[1])}

        d_name = str(desig_input).strip()
        d_name_lower = d_name.lower()

        cursor.execute("SELECT id, name FROM cyb_designation WHERE LOWER(name) = %s LIMIT 1", (d_name_lower,))
        row = cursor.fetchone()
        if row:
            return {"designation_id": int(row[0]), "designation_name": str(row[1])}

        cursor.execute("SELECT id, name FROM cyb_designation WHERE LOWER(name) LIKE %s LIMIT 1", (f"%{d_name_lower}%",))
        row = cursor.fetchone()
        if row:
            return {"designation_id": int(row[0]), "designation_name": str(row[1])}

        cursor.execute(
            "INSERT INTO cyb_designation (name, user_defined, status, user_id, create_date) VALUES (%s, 1, 1, %s, NOW())",
            (d_name, user_id)
        )
        new_id = cursor.lastrowid
        conn.commit()
        return {"designation_id": int(new_id), "designation_name": d_name}
    except Exception:
        log.exception("Failed to resolve or create designation in DB")
        conn.rollback()
    finally:
        conn.close()
    return None


def resolve_or_create_department_id(dept_input: Any, user_id: int = None) -> Optional[Dict[str, Any]]:
    """
    Search cyb_department by ID or name. If not found, insert user-defined department.
    """
    if not dept_input:
        return None

    conn = get_db()
    cursor = conn.cursor()
    try:
        if isinstance(dept_input, int) or (isinstance(dept_input, str) and dept_input.isdigit()):
            dp_id = int(dept_input)
            cursor.execute("SELECT id, name FROM cyb_department WHERE id = %s LIMIT 1", (dp_id,))
            row = cursor.fetchone()
            if row:
                return {"department_id": int(row[0]), "department_name": str(row[1])}

        dp_name = str(dept_input).strip()
        dp_name_lower = dp_name.lower()

        cursor.execute("SELECT id, name FROM cyb_department WHERE LOWER(name) = %s LIMIT 1", (dp_name_lower,))
        row = cursor.fetchone()
        if row:
            return {"department_id": int(row[0]), "department_name": str(row[1])}

        cursor.execute("SELECT id, name FROM cyb_department WHERE LOWER(name) LIKE %s LIMIT 1", (f"%{dp_name_lower}%",))
        row = cursor.fetchone()
        if row:
            return {"department_id": int(row[0]), "department_name": str(row[1])}

        cursor.execute(
            "INSERT INTO cyb_department (name, user_defined, status, user_id, create_date) VALUES (%s, 1, 1, %s, NOW())",
            (dp_name, user_id)
        )
        new_id = cursor.lastrowid
        conn.commit()
        return {"department_id": int(new_id), "department_name": dp_name}
    except Exception:
        log.exception("Failed to resolve or create department in DB")
        conn.rollback()
    finally:
        conn.close()
    return None


def resolve_employment_type_id(emp_type_input: Any) -> Optional[int]:
    """
    Map employment type input string/int to cyb_employement_type ID (1: Full-time, 2: Part-time, 3: Self-employed, 4: Freelance, 5: Internship, 6: Trainee).
    """
    if not emp_type_input:
        return 1
    if isinstance(emp_type_input, int) and 1 <= emp_type_input <= 6:
        return emp_type_input
    s = str(emp_type_input).strip().lower()
    if "full" in s:
        return 1
    elif "part" in s:
        return 2
    elif "self" in s:
        return 3
    elif "free" in s:
        return 4
    elif "intern" in s:
        return 5
    elif "train" in s:
        return 6
    return 1


def resolve_company_id(company_input: Any) -> Optional[int]:
    """
    Look up company in cyb_user (user_type = 2) by name or id.
    """
    if not company_input:
        return None

    conn = get_db()
    cursor = conn.cursor()
    try:
        if isinstance(company_input, int) or (isinstance(company_input, str) and company_input.isdigit()):
            c_id = int(company_input)
            cursor.execute("SELECT id FROM cyb_user WHERE id = %s AND user_type = 2 LIMIT 1", (c_id,))
            row = cursor.fetchone()
            if row:
                return int(row[0])

        comp_name = str(company_input).strip().lower()
        cursor.execute("SELECT id FROM cyb_user WHERE user_type = 2 AND (LOWER(fname) = %s OR LOWER(lname) = %s OR LOWER(email) LIKE %s) LIMIT 1", (comp_name, comp_name, f"%{comp_name}%"))
        row = cursor.fetchone()
        if row:
            return int(row[0])

        cursor.execute("SELECT id FROM cyb_user WHERE user_type = 2 AND (LOWER(fname) LIKE %s OR LOWER(lname) LIKE %s) LIMIT 1", (f"%{comp_name}%", f"%{comp_name}%"))
        row = cursor.fetchone()
        if row:
            return int(row[0])
    except Exception:
        log.exception("Failed to query company ID from DB")
    finally:
        conn.close()
    return None





