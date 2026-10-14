"""
token_resolver.py — Dynamic DB Token Resolution Service.

Provides get_user_platform_token(cyb_user_id) to retrieve cyb_user.token strictly
by canonical integer primary key (id). Ensures strict input validation, isolated
connection/cursor cleanup, and sanitized error logging.
"""

import logging
from enum import Enum
from typing import Optional
from dataclasses import dataclass

try:
    from backend.repositories.db import get_db
except ModuleNotFoundError:
    from repositories.db import get_db  # type: ignore

log = logging.getLogger(__name__)


class TokenResolutionStatus(Enum):
    SUCCESS = "success"
    USER_NOT_FOUND = "user_not_found"
    TOKEN_MISSING = "token_missing"
    DATABASE_ERROR = "database_error"


@dataclass
class TokenResolutionResult:
    status: TokenResolutionStatus
    token: Optional[str] = None


def get_user_platform_token(cyb_user_id: int) -> TokenResolutionResult:
    """
    Retrieves cyb_user.token strictly by canonical primary key integer (id).
    Accepts ONLY a positive integer id. Rejects bool, None, str, <=0 IDs.
    Returns explicit TokenResolutionResult enum status.
    Never logs query parameters, SQL text, or tokens.
    """
    if cyb_user_id is None or isinstance(cyb_user_id, bool) or not isinstance(cyb_user_id, int) or cyb_user_id <= 0:
        return TokenResolutionResult(status=TokenResolutionStatus.USER_NOT_FOUND)

    sql = """
        SELECT token FROM cyb_user 
        WHERE id = %s 
          AND status = 1 
          AND (is_deleted IS NULL OR is_deleted = 0)
        LIMIT 1
    """
    conn = None
    c = None
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute(sql, (cyb_user_id,))
        row = c.fetchone()

        if not row:
            return TokenResolutionResult(status=TokenResolutionStatus.USER_NOT_FOUND)

        tok = str(row.get("token") or "").strip()
        if tok and tok.lower() != "none":
            return TokenResolutionResult(status=TokenResolutionStatus.SUCCESS, token=tok)

        return TokenResolutionResult(status=TokenResolutionStatus.TOKEN_MISSING)

    except Exception:
        log.warning("Token resolution encountered a database infrastructure error.")
        return TokenResolutionResult(status=TokenResolutionStatus.DATABASE_ERROR)
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
