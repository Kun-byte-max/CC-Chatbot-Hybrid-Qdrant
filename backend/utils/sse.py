"""
backend/utils/sse.py — Server-Sent Events (SSE) serialization utilities.
"""

import asyncio
import json
import math
from typing import Any, AsyncGenerator, AsyncIterator, Dict, Optional, Tuple


def validate_keepalive_interval(val: Any, default: float = 15.0) -> float:
    """
    Validates a keepalive interval setting:
    - Returns a positive, finite float.
    - Falls back to `default` (15.0) if val is None, invalid string, zero,
      negative, NaN, positive infinity, or negative infinity.
    """
    try:
        if val is None:
            return float(default)
        num = float(val)
        if math.isfinite(num) and num > 0:
            return num
        return float(default)
    except (ValueError, TypeError):
        return float(default)


# Allowlist of safe, intentionally client-facing error details.
SAFE_PUBLIC_ERROR_DETAILS = frozenset({
    "Invalid request content",
    "Request timed out.",
    "Upstream request timed out.",
    "The AI service is temporarily unavailable. Please try again shortly.",
    "Authentication token required.",
    "User slug parameter is required.",
    "Invalid or expired token.",
    "Invalid or expired token",
    "Not authenticated. Bearer token missing.",
    "Invalid authenticated user identity.",
    "User not found or account is inactive.",
    "Access denied. Only employers can rank/view candidates.",
    "Failed to communicate with the AI service.",
    "Request failed",
})

DEFAULT_SAFE_CLIENT_ERROR_MESSAGE = "Request could not be processed. Please try again."


def map_safe_sse_error_detail(detail: Any, status_code: Optional[int] = None) -> str:
    """
    Maps an HTTPException detail to an approved, allowlisted public client-visible message.
    
    Guarantees that arbitrary internal details (file paths, SQL syntax/schemas,
    column names, candidate/salary data, unredacted tokens, or unknown exception
    strings) are replaced with a bounded safe message.
    """
    if detail is None:
        return DEFAULT_SAFE_CLIENT_ERROR_MESSAGE

    clean_str = str(detail).strip()

    if clean_str in SAFE_PUBLIC_ERROR_DETAILS:
        try:
            from backend.utils.telemetry import sanitize_string_value
            return sanitize_string_value(clean_str)
        except Exception:
            return clean_str

    if status_code == 504 or "timed out" in clean_str.lower():
        return "Request timed out."
    elif status_code == 502:
        return "The AI service is temporarily unavailable. Please try again shortly."
    elif status_code == 401:
        return "Invalid or expired token."
    elif status_code == 403:
        return "Access denied."

    return DEFAULT_SAFE_CLIENT_ERROR_MESSAGE


def format_sse_event(event: str, data: Any) -> str:
    """
    Serializes an event name and data payload into the standard SSE wire format:
    
        event: <event_name>\\n
        data: <json_string>\\n\\n

    JSON serialization ensures complex objects, lists, primitives, and multiline
    text are safely escaped without injecting raw line breaks into the SSE stream.
    """
    if isinstance(data, (dict, list, int, float, bool)):
        data_str = json.dumps(data, ensure_ascii=False)
    elif isinstance(data, str):
        data_str = json.dumps(data, ensure_ascii=False)
    elif data is None:
        data_str = "null"
    else:
        data_str = json.dumps(str(data), ensure_ascii=False)

    return f"event: {event}\ndata: {data_str}\n\n"


def parse_sse_event(chunk: str) -> Dict[str, Any]:
    """
    Utility parser for tests and consumer validation.
    Extracts event name and JSON data from an SSE message block.
    Comment frames (starting with ':') return event='comment' and data=comment_text.
    """
    stripped = chunk.strip()
    if stripped.startswith(":"):
        comment_text = stripped[1:].strip()
        return {"event": "comment", "data": comment_text}

    event_name = "message"
    data_lines = []

    for line in stripped.split("\n"):
        line = line.strip()
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())

    raw_data = "\n".join(data_lines)
    try:
        parsed_data = json.loads(raw_data) if raw_data else None
    except json.JSONDecodeError:
        parsed_data = raw_data

    return {"event": event_name, "data": parsed_data}


def format_sse_comment(comment: str = "keepalive") -> str:
    """
    Serializes an SSE comment frame:
        : <comment>\n\n

    Comments maintain connection activity for intermediary proxies (preventing idle timeouts)
    without triggering event handlers in client EventSource / SSE parsers.
    """
    clean_comment = str(comment).replace("\r", "").replace("\n", " ").strip()
    return f": {clean_comment}\n\n"


def is_sse_comment(chunk: str) -> bool:
    """
    Checks if an SSE chunk is a comment frame (starts with ':').
    """
    stripped = chunk.strip()
    return stripped.startswith(":")


async def _is_disconnected(req: Optional[Any]) -> bool:
    """
    Safely inspects request disconnect status across Starlette Requests and mock objects.
    """
    if req is None:
        return False
    try:
        is_disc = getattr(req, "is_disconnected", None)
        if callable(is_disc):
            res = is_disc()
            if asyncio.iscoroutine(res):
                return await res
            return bool(res)
    except Exception:
        pass
    return False


async def await_with_keepalive(
    coro,
    interval: float,
    req: Optional[Any] = None,
    comment: str = "keepalive",
) -> AsyncGenerator[Tuple[str, Any], None]:
    """
    Executes an awaitable coroutine while periodically yielding SSE keepalive comments
    if the awaitable takes longer than `interval` seconds.

    Yields:
        ("comment", ": keepalive\\n\\n") when waiting exceeds interval.
        ("result", result_value) when the coroutine completes.
    """
    effective_interval = validate_keepalive_interval(interval, default=15.0)
    task = asyncio.create_task(coro)
    try:
        while True:
            done, _ = await asyncio.wait([task], timeout=effective_interval)
            if done:
                res = task.result()
                yield ("result", res)
                return

            if await _is_disconnected(req):
                return

            yield ("comment", format_sse_comment(comment))
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


async def stream_with_keepalive(
    stream_gen: AsyncIterator[str],
    interval: float,
    req: Optional[Any] = None,
    comment: str = "keepalive",
) -> AsyncGenerator[Tuple[str, str], None]:
    """
    Consumes an async generator while periodically yielding SSE keepalive comments
    if waiting for the next item takes longer than `interval` seconds.

    Yields:
        ("comment", ": keepalive\\n\\n") when waiting for next item exceeds interval.
        ("token", token_text) when the next token arrives.
    """
    effective_interval = validate_keepalive_interval(interval, default=15.0)
    anext_task: Optional[asyncio.Task] = None
    aiterator = stream_gen.__aiter__()
    try:
        while True:
            if anext_task is None:
                anext_task = asyncio.create_task(aiterator.__anext__())

            done, _ = await asyncio.wait([anext_task], timeout=effective_interval)
            if not done:
                if await _is_disconnected(req):
                    return
                yield ("comment", format_sse_comment(comment))
                continue

            try:
                item = anext_task.result()
            except StopAsyncIteration:
                anext_task = None
                return
            except Exception:
                anext_task = None
                raise
            else:
                anext_task = None
                yield ("token", item)
    finally:
        if anext_task is not None and not anext_task.done():
            anext_task.cancel()
            try:
                await anext_task
            except (asyncio.CancelledError, Exception):
                pass
        if stream_gen is not None and hasattr(stream_gen, "aclose"):
            try:
                await stream_gen.aclose()
            except Exception:
                pass
