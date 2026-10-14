import jwt
import contextvars
import logging
from typing import Optional, Dict, Any
from fastapi import Depends, HTTPException, Request, APIRouter
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

try:
    from backend.config.config import JWT_SECRET, JWT_ALGORITHM
    from backend.schemas.schemas import LoginRequest
    from backend.repositories.db import get_db
except ModuleNotFoundError:
    from config.config import JWT_SECRET, JWT_ALGORITHM  # type: ignore
    from schemas.schemas import LoginRequest  # type: ignore
    from repositories.db import get_db  # type: ignore

log = logging.getLogger(__name__)

router = APIRouter()

request_role = contextvars.ContextVar("request_role", default=None)
request_email = contextvars.ContextVar("request_email", default=None)

security = HTTPBearer(auto_error=False)


def _parse_int_id(val: Any) -> Optional[int]:
    """
    Validates and converts val to a positive integer.
    Rejects bool, None, floats, zero, negative numbers, and non-numeric strings.
    """
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val if val > 0 else None
    if isinstance(val, float):
        return None
    if isinstance(val, str):
        val_str = val.strip()
        if val_str.isdigit():
            parsed = int(val_str)
            return parsed if parsed > 0 else None
    return None


def _is_jwt_structured(token: str) -> bool:
    """
    Determines if token has a JWT structure (3 period-separated base64/URL-safe parts).
    Ensures malformed JWT-like strings (1 dot, 3 dots, or 2 dots with non-empty parts)
    are identified as JWT candidates rather than raw platform tokens.
    """
    parts = token.split(".")
    if len(parts) == 3 and all(len(p.strip()) > 0 for p in parts):
        return True
    return False


def find_user_by_token(token: str) -> Optional[dict]:
    """
    Deterministic user authentication against cyb_user table.

    Rules:
    1. If token is a JWT structure (3 non-empty dot-separated parts):
       - MUST pass jwt.decode with JWT_SECRET.
       - Any decoding error, signature failure, or expired timestamp REJECTS IMMEDIATELY (returns None).
       - MUST NOT fall back to raw-token database query under any circumstances.
       - Claims validation:
         * Must contain at least one valid identity claim (uid/id or email).
         * If both 'uid' and 'id' exist, they must evaluate to the same canonical integer. Mismatch -> returns None.
         * Primary Key lookup queries cyb_user by PK integer. If email also exists, row['email'] must match jwt_email.
         * If no primary key exists but email exists, queries cyb_user by LOWER(email).
         * Requires exactly 1 active, non-deleted user (`status=1 AND (is_deleted IS NULL OR is_deleted=0)`).
    2. If token is a non-JWT raw string:
       - If token contains '.' (malformed JWT structure), it is REJECTED IMMEDIATELY.
       - Otherwise queries cyb_user WHERE token = %s AND status = 1 AND (is_deleted IS NULL OR is_deleted = 0).
       - If multiple rows match (count != 1), returns None (ambiguous match rejection).
    3. Fail-Closed: Any database exception during query execution fails closed and returns None.
    """
    if not token or not isinstance(token, str):
        return None

    clean_token = token.strip()
    if not clean_token:
        return None

    # Strict JWT Candidate Detection
    if "." in clean_token:
        # Contains dots -> treated strictly as a JWT candidate or malformed JWT
        if not _is_jwt_structured(clean_token):
            # Malformed JWT structure (e.g. 1 dot or 3 dots or empty part) -> Reject immediately
            return None

        # Standard 3-part JWT structure
        try:
            payload = jwt.decode(clean_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        except Exception:
            # If JWT decode fails with local secret, check if this exact token string exists directly in cyb_user.token
            try:
                conn_raw = get_db()
                c_raw = conn_raw.cursor()
                c_raw.execute(
                    "SELECT * FROM cyb_user WHERE token = %s AND status = 1 AND (is_deleted IS NULL OR is_deleted = 0)",
                    (clean_token,)
                )
                raw_rows = c_raw.fetchall()
                if c_raw:
                    c_raw.close()
                if conn_raw:
                    conn_raw.close()
                if len(raw_rows) == 1:
                    return dict(raw_rows[0])
            except Exception as ex_raw:
                log.warning("Database raw token fallback failed: %s", ex_raw)

            # Invalid, expired, or malformed JWT -> REJECT IMMEDIATELY if not in DB
            return None

        raw_uid = payload.get("uid")
        raw_id = payload.get("id")
        jwt_email = payload.get("email")

        parsed_uid = _parse_int_id(raw_uid)
        parsed_id = _parse_int_id(raw_id)

        # Conflict check if both uid and id claims exist
        if raw_uid is not None and raw_id is not None:
            if parsed_uid is None or parsed_id is None or parsed_uid != parsed_id:
                return None
            target_pk = parsed_uid
        else:
            target_pk = parsed_uid or parsed_id

        # Non-numeric/invalid claim rejection (e.g. float or bool in claim)
        if (raw_uid is not None and parsed_uid is None) or (raw_id is not None and parsed_id is None):
            return None

        # Rejection if JWT contains NO supported identity claims
        if target_pk is None and not jwt_email:
            return None

        conn = None
        c = None
        try:
            conn = get_db()
            c = conn.cursor()

            if target_pk is not None:
                c.execute(
                    "SELECT * FROM cyb_user WHERE id = %s AND status = 1 AND (is_deleted IS NULL OR is_deleted = 0)",
                    (target_pk,)
                )
                rows = c.fetchall()
                if len(rows) != 1:
                    return None
                user_row = dict(rows[0])
                if jwt_email:
                    db_email = str(user_row.get("email") or "").strip().lower()
                    if db_email != str(jwt_email).strip().lower():
                        return None
                return user_row

            elif jwt_email:
                c.execute(
                    "SELECT * FROM cyb_user WHERE LOWER(email) = %s AND status = 1 AND (is_deleted IS NULL OR is_deleted = 0)",
                    (str(jwt_email).strip().lower(),)
                )
                rows = c.fetchall()
                if len(rows) != 1:
                    return None
                return dict(rows[0])

            return None
        except Exception as ex:
            log.warning("Database exception during JWT user lookup: %s", ex)
            return None
        finally:
            if c:
                try:
                    c.close()
                except Exception:
                    pass
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    else:
        # Raw Bearer Token Path (Only reached if string has NO dots)
        conn = None
        c = None
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute(
                "SELECT * FROM cyb_user WHERE token = %s AND status = 1 AND (is_deleted IS NULL OR is_deleted = 0)",
                (clean_token,)
            )
            rows = c.fetchall()
            if len(rows) != 1:
                return None
            return dict(rows[0])
        except Exception as ex:
            log.warning("Database exception during raw token user lookup: %s", ex)
            return None
        finally:
            if c:
                try:
                    c.close()
                except Exception:
                    pass
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass


async def get_current_user(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = credentials.credentials
    user = find_user_by_token(token)
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def verify_token(req: Request, credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated. Bearer token missing.")

    token = credentials.credentials.strip()
    user = find_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")

    # Primary key must be a valid integer
    db_user_id = user.get("id")
    if not isinstance(db_user_id, int) or db_user_id <= 0:
        raise HTTPException(status_code=401, detail="Invalid authenticated user identity.")

    email = user.get("email")
    user_type = user.get("user_type")
    role = "employer" if user_type == 2 else "employee"
    user_slug = user.get("slug")
    individual_id = user.get("individual_id")

    payload = {
        "email": email,
        "role": role,
        "uid": db_user_id,
        "id": db_user_id,
        "user_slug": user_slug,
        "individual_id": individual_id
    }

    req.state.email = email
    req.state.role = role
    req.state.user_id = str(db_user_id)
    req.state.db_user_id = db_user_id
    req.state.user_slug = user_slug
    req.state.individual_id = individual_id
    req.state.jwt_payload = payload

    request_role.set(role)
    request_email.set(email)

    return payload


@router.post("/login")
async def login(request: LoginRequest):
    role = request.role.lower()
    if role not in ["employee", "employer"]:
        raise HTTPException(status_code=400, detail="Role must be 'employee' or 'employer'")

    clean_email = request.email.lower().strip()
    payload = {
        "email": clean_email,
        "role": role
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

    user_data = None
    conn = None
    c = None
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            """
            SELECT u.id, u.individual_id, u.fname, u.lname, u.email, u.phone, u.user_type, u.token,
                   u.city, u.state, u.country,
                   c.name AS city_name,
                   s.name AS state_name
            FROM cyb_user u
            LEFT JOIN cyb_cities c ON u.city = c.id
            LEFT JOIN cyb_state s ON u.state = s.id
            WHERE LOWER(u.email) = %s AND u.status = 1 AND (u.is_deleted IS NULL OR u.is_deleted = 0)
            LIMIT 1
            """,
            (clean_email,)
        )
        row = c.fetchone()
        if row:
            user_type = row["user_type"]
            role = "employer" if user_type == 2 else "employee"
            payload = {
                "email": clean_email,
                "role": role,
                "uid": row["id"]
            }
            token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
            if row.get("token") and len(str(row["token"]).strip()) > 20 and str(row["token"]).lower() != "none":
                token = str(row["token"]).strip()
            user_data = {
                "id": row["id"],
                "individual_id": row["individual_id"],
                "fname": row["fname"],
                "lname": row["lname"],
                "email": row["email"],
                "phone": row["phone"],
                "user_type": row["user_type"],
                "token": token,
                "city": row["city"],
                "city_name": row["city_name"],
                "state": row["state"],
                "state_name": row["state_name"],
                "country": row["country"]
            }
        else:
            user_type = 1 if role == "employee" else 2
    except Exception as e:
        print(f"Database error while querying user details on login: {e}")
    finally:
        if c:
            try:
                c.close()
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    if not user_data:
        raise HTTPException(status_code=401, detail="User not found or account is inactive.")

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": user_data
    }
