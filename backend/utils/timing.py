import contextvars
import logging
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from typing import Dict, Optional

log = logging.getLogger(__name__)

_request_timing_var: contextvars.ContextVar[Optional["TimingContext"]] = contextvars.ContextVar("request_timing", default=None)
_request_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("request_id", default=None)

class TimingContext:
    def __init__(self, request_id: str):
        self.request_id: str = request_id
        self.start_time: float = time.perf_counter()
        self.stages: Dict[str, int] = {}
        self.compose_prompt_tokens: int = -1
        self.compose_completion_tokens: int = -1
        self.profile_parse_ran: bool = False
        self.search_parse_ran: bool = False
        self.profile_update_intent_found: bool = False
        self.profile_retrieved: bool = False
        self.search_query_found: bool = False
        self.search_executed: bool = False
        self.history_messages_sent: int = 0
        self.history_messages_dropped: int = 0
        self.reply_validation_violated: bool = False

    def mark_stage(self, stage_name: str, ms: int) -> None:
        try:
            self.stages[stage_name] = max(0, int(ms))
        except Exception:
            pass

    def record_elapsed(self, stage_name: str, start_perf: float) -> None:
        try:
            ms = int(round((time.perf_counter() - start_perf) * 1000))
            self.mark_stage(stage_name, ms)
        except Exception:
            pass

    def total_elapsed_ms(self) -> int:
        try:
            return int(round((time.perf_counter() - self.start_time) * 1000))
        except Exception:
            return 0

def start_request_timing(custom_request_id: Optional[str] = None) -> TimingContext:
    try:
        req_id = custom_request_id or uuid.uuid4().hex[:12]
        ctx = TimingContext(req_id)
        _request_timing_var.set(ctx)
        _request_id_var.set(req_id)
        return ctx
    except Exception:
        ctx = TimingContext(custom_request_id or "000000000000")
        return ctx

def get_request_timing() -> Optional[TimingContext]:
    try:
        return _request_timing_var.get()
    except Exception:
        return None

def get_request_id() -> Optional[str]:
    try:
        return _request_id_var.get()
    except Exception:
        return None

def mark_stage(stage_name: str, ms: int) -> None:
    try:
        ctx = get_request_timing()
        if ctx is not None:
            ctx.mark_stage(stage_name, ms)
    except Exception:
        pass

@contextmanager
def stage_timer(stage_name: str):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        try:
            ctx = get_request_timing()
            if ctx is not None:
                ctx.record_elapsed(stage_name, t0)
        except Exception:
            pass

@asynccontextmanager
async def stage_timer_async(stage_name: str):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        try:
            ctx = get_request_timing()
            if ctx is not None:
                ctx.record_elapsed(stage_name, t0)
        except Exception:
            pass

def set_compose_tokens(prompt_tokens: int, completion_tokens: int) -> None:
    try:
        ctx = get_request_timing()
        if ctx is not None:
            ctx.compose_prompt_tokens = prompt_tokens
            ctx.compose_completion_tokens = completion_tokens
    except Exception:
        pass

def set_gate_flags(profile_parse_ran: Optional[bool] = None, search_parse_ran: Optional[bool] = None) -> None:
    try:
        ctx = get_request_timing()
        if ctx is not None:
            if profile_parse_ran is not None:
                ctx.profile_parse_ran = profile_parse_ran
            if search_parse_ran is not None:
                ctx.search_parse_ran = search_parse_ran
    except Exception:
        pass

def set_decision_flags(
    profile_update_intent_found: Optional[bool] = None,
    profile_retrieved: Optional[bool] = None,
    search_query_found: Optional[bool] = None,
    search_executed: Optional[bool] = None,
    profile_found: Optional[bool] = None  # Backwards compatibility alias for profile_update_intent_found
) -> None:
    try:
        ctx = get_request_timing()
        if ctx is not None:
            if profile_update_intent_found is not None:
                ctx.profile_update_intent_found = profile_update_intent_found
            elif profile_found is not None:
                ctx.profile_update_intent_found = profile_found
            if profile_retrieved is not None:
                ctx.profile_retrieved = profile_retrieved
            if search_query_found is not None:
                ctx.search_query_found = search_query_found
            if search_executed is not None:
                ctx.search_executed = search_executed
    except Exception:
        pass

def set_history_metrics(sent: int, dropped: int) -> None:
    try:
        ctx = get_request_timing()
        if ctx is not None:
            ctx.history_messages_sent = sent
            ctx.history_messages_dropped = dropped
    except Exception:
        pass

def set_validation_flag(violated: bool) -> None:
    try:
        ctx = get_request_timing()
        if ctx is not None:
            ctx.reply_validation_violated = violated
    except Exception:
        pass
