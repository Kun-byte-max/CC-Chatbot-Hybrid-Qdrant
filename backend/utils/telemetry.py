"""
backend/utils/telemetry.py — Non-invasive, opt-in Langfuse observability module.

Provides thread-safe and async-safe context managers for tracing requests and
LLM generations. Fully feature-flagged via LANGFUSE_ENABLED=false by default.
When disabled, creates zero clients, zero background threads, and zero network calls.
"""

import hashlib
import hmac
import logging
import os
import re
import time
from contextlib import contextmanager
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

# Keys strictly forbidden in telemetry metadata for privacy and security
SENSITIVE_METADATA_KEYS = {
    "token", "jwt", "password", "access_token", "auth_token", "refresh_token",
    "cookie", "secret", "authorization", "api_key", "db_password", "apikey",
    "phone", "email", "salary", "salary_inhand", "dob", "ssn", "aadhaar", "pan",
    "credential", "credentials", "private_key",
    "prompt", "system_prompt", "raw_prompt", "user_message", "last_user_msg",
    "query", "resolved_query", "raw_query", "resume", "cv", "profile_text",
}

# Regex patterns for redacting sensitive values within arbitrary string metadata
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[a-zA-Z0-9_\-\.=]+", re.IGNORECASE)
_JWT_RE = re.compile(r"\beyJ[a-zA-Z0-9_\-]{8,}\.[a-zA-Z0-9_\-]{8,}\.[a-zA-Z0-9_\-]+")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b|\+?\d{10,13}\b")
_API_KEY_RE = re.compile(r"\b(?:sk|pk|api[_-]?key)[-_][a-zA-Z0-9_\-]{14,}\b", re.IGNORECASE)


def sanitize_string_value(val: str, max_len: int = 500) -> str:
    """
    Redacts bearer tokens, JWTs, emails, phone numbers, and API keys from string values.
    Truncates pathological strings to prevent payload bloat.
    Never logs original string values.
    """
    if not val:
        return val
    try:
        s = val[:max_len]
        s = _BEARER_RE.sub("[REDACTED_BEARER]", s)
        s = _JWT_RE.sub("[REDACTED_JWT]", s)
        s = _EMAIL_RE.sub("[REDACTED_EMAIL]", s)
        s = _PHONE_RE.sub("[REDACTED_PHONE]", s)
        s = _API_KEY_RE.sub("[REDACTED_API_KEY]", s)
        return s
    except Exception:
        return "[REDACTED]"


def sanitize_telemetry_metadata(data: Any, depth: int = 0) -> Any:
    """
    Recursively scrubs sensitive keys, credentials, and PII from telemetry metadata.
    Enforces depth and size limits to avoid circular references and payload bloat.
    Fails closed: drops unsafe structures if sanitization encounters an unexpected error.
    Never logs raw metadata content.
    """
    if depth > 4:
        return "[TRUNCATED_DEPTH]"
    try:
        if isinstance(data, dict):
            clean = {}
            for i, (k, v) in enumerate(data.items()):
                if i >= 50:
                    clean["[TRUNCATED_KEYS]"] = True
                    break
                k_str = str(k).strip()
                k_lower = k_str.lower()
                if any(s in k_lower for s in SENSITIVE_METADATA_KEYS):
                    continue
                clean[k_str] = sanitize_telemetry_metadata(v, depth + 1)
            return clean
        elif isinstance(data, (list, tuple, set)):
            clean_list = []
            for i, item in enumerate(data):
                if i >= 50:
                    clean_list.append("[TRUNCATED_ITEMS]")
                    break
                clean_list.append(sanitize_telemetry_metadata(item, depth + 1))
            return clean_list
        elif isinstance(data, str):
            return sanitize_string_value(data)
        elif isinstance(data, (int, float, bool)) or data is None:
            return data
        else:
            return sanitize_string_value(str(data)[:200])
    except Exception as ex:
        log.warning("Telemetry metadata sanitization failed closed: %s", type(ex).__name__)
        return {} if isinstance(data, dict) else []


def compute_session_hmac(session_id: Any) -> Optional[str]:
    """
    Computes a keyed HMAC-SHA256 reference for cross-request session correlation.
    Only computes if LANGFUSE_SESSION_HMAC_SECRET is explicitly configured.
    Never uses or falls back to plain SHA-256, Langfuse keys, or auth secrets.
    Returns None if no dedicated secret is configured or session_id is empty.
    Never logs the original session identifier.
    """
    if not session_id:
        return None
    try:
        from backend.config.config import LANGFUSE_SESSION_HMAC_SECRET
    except Exception:
        LANGFUSE_SESSION_HMAC_SECRET = os.getenv("LANGFUSE_SESSION_HMAC_SECRET", "")

    if not LANGFUSE_SESSION_HMAC_SECRET or not LANGFUSE_SESSION_HMAC_SECRET.strip():
        return None

    try:
        key = LANGFUSE_SESSION_HMAC_SECRET.strip().encode("utf-8")
        msg = str(session_id).encode("utf-8")
        return hmac.new(key, msg, hashlib.sha256).hexdigest()[:16]
    except Exception as ex:
        log.debug("Session HMAC computation failed: %s", type(ex).__name__)
        return None


# Allowed categorical values for retrieval metadata
ALLOWED_SEARCH_MODES = {
    "PROFILE_RECOMMENDATION", "EXPLICIT_KEYWORD", "EXPLICIT_LOCATION",
    "NEAR_ME", "PEOPLE_SEARCH", "CANDIDATE_SEARCH",
    "profile_recommendation", "explicit_keyword", "explicit_location",
    "near_me", "people_search", "candidate_search", "none",
}

ALLOWED_SEARCH_PROVIDERS = {
    "hybrid", "meilisearch", "jobmatch", "jobmatch_qdrant", "mysql_db", "people_repo", "none",
}

ALLOWED_PROVIDER_STATUSES = {
    "success", "partial_failure", "unavailable", "timeout", "disabled", "skipped", "empty",
}

ALLOWED_JOB_MODES = {
    "remote", "hybrid", "office", "field",
}

ALLOWED_JOBMATCH_OUTCOMES = {
    "disabled",
    "missing_candidate_id",
    "success_with_results",
    "empty_results",
    "candidate_not_indexed",
    "authentication_failure",
    "timeout",
    "service_unavailable",
    "contract_error",
    "unexpected_error",
    "not_attempted_other",
}

ALLOWED_JOBMATCH_FALLBACK_REASONS = {
    "disabled",
    "missing_candidate_id",
    "empty_results",
    "candidate_not_indexed",
    "authentication_failure",
    "timeout",
    "service_unavailable",
    "contract_error",
    "unexpected_error",
    "not_attempted_other",
    # Backward-compatible tokens
    "none", "disabled_feature_flag", "candidate_not_indexed_409",
    "auth_failure_401_403", "contract_validation_400_422", "service_unavailable_5xx",
    "malformed_response",
}

ALLOWED_PEOPLE_SEARCH_OUTCOMES = {
    "success_with_results",
    "empty_results",
    "location_unavailable",
    "provider_failure",
    "not_attempted",
}

ALLOWED_CANDIDATE_SEARCH_OUTCOMES = {
    "success_with_results",
    "empty_results",
    "safe_filter_failure",
    "provider_failure",
    "not_attempted",
}

ALLOWED_FILTER_CATEGORIES = {
    "city", "state", "country", "job_mode", "role_type", "urgent",
    "salary", "salary_min", "salary_max",
    "experience", "experience_min", "experience_max",
    "fresher", "department", "company", "keyword",
    "employment_type", "on_immediate", "on_notice",
}

ALLOWED_COUNT_FIELDS = {
    "meilisearch_count", "jobmatch_count", "fused_count",
    "eligible_count", "final_count", "retrieval_result_count",
    "job_mode_rejected_count", "salary_rejected_count",
    "experience_rejected_count", "emp_type_rejected_count",
    "rrf_input_count", "rrf_output_count",
}

ALLOWED_LATENCY_FIELDS = {
    "meili_ms", "jobmatch_ms", "total_ms", "retrieval_total_ms",
}

ALLOWED_BOOL_FIELDS = {
    "candidate_present", "meilisearch_attempted", "jobmatch_attempted", "hybrid_executed",
    "retrieval_attempted",
}

ALLOWED_PROVIDER_NAMES = {"meilisearch", "jobmatch", "qdrant", "mysql", "people_repo"}
ALLOWED_DROP_CATEGORIES = {"job_mode", "salary", "experience", "employment_type", "urgent", "domain"}


def validate_retrieval_metadata(data: Any) -> Dict[str, Any]:
    """
    Validates and bounds retrieval metadata against a strict allowlist schema.
    Fails closed: returns an empty dict on error, never raises.
    Rejects raw queries, PII, candidate/user IDs, document payloads, and arbitrary strings.
    Never fabricates metrics that were not provided.
    """
    if not isinstance(data, dict):
        return {}

    clean: Dict[str, Any] = {}

    try:
        # 1. Categorical String Enums (Closed Sets)
        if "search_mode" in data and isinstance(data["search_mode"], str):
            sm = str(data["search_mode"]).strip()
            if sm in ALLOWED_SEARCH_MODES:
                clean["search_mode"] = sm

        if "search_provider" in data and isinstance(data["search_provider"], str):
            sp = str(data["search_provider"]).strip().lower()
            if sp in ALLOWED_SEARCH_PROVIDERS:
                clean["search_provider"] = sp

        if "provider_status" in data and isinstance(data["provider_status"], str):
            ps = str(data["provider_status"]).strip().lower()
            if ps in ALLOWED_PROVIDER_STATUSES:
                clean["provider_status"] = ps

        if "job_mode_requested" in data and isinstance(data["job_mode_requested"], str):
            jm = str(data["job_mode_requested"]).strip().lower()
            if jm in ALLOWED_JOB_MODES:
                clean["job_mode_requested"] = jm

        if "jobmatch_outcome" in data and isinstance(data["jobmatch_outcome"], str):
            jo = str(data["jobmatch_outcome"]).strip().lower()
            if jo in ALLOWED_JOBMATCH_OUTCOMES:
                clean["jobmatch_outcome"] = jo

        if "jobmatch_fallback_reason" in data and isinstance(data["jobmatch_fallback_reason"], str):
            fr = str(data["jobmatch_fallback_reason"]).strip().lower()
            if fr in ALLOWED_JOBMATCH_FALLBACK_REASONS:
                clean["jobmatch_fallback_reason"] = fr

        if "people_search_outcome" in data and isinstance(data["people_search_outcome"], str):
            pso = str(data["people_search_outcome"]).strip().lower()
            if pso in ALLOWED_PEOPLE_SEARCH_OUTCOMES:
                clean["people_search_outcome"] = pso

        if "candidate_search_outcome" in data and isinstance(data["candidate_search_outcome"], str):
            cso = str(data["candidate_search_outcome"]).strip().lower()
            if cso in ALLOWED_CANDIDATE_SEARCH_OUTCOMES:
                clean["candidate_search_outcome"] = cso

        if "retrieval_error_type" in data and isinstance(data["retrieval_error_type"], str):
            et = str(data["retrieval_error_type"]).strip()
            if 0 < len(et) <= 64 and re.match(r"^[A-Za-z0-9_.]+$", et):
                clean["retrieval_error_type"] = et

        # 2. Non-negative Bounded Integers (Counts)
        for k in ALLOWED_COUNT_FIELDS:
            if k in data:
                v = data[k]
                if not isinstance(v, bool) and isinstance(v, int) and 0 <= v <= 100_000:
                    clean[k] = v

        # 3. Non-negative Bounded Floats (Latencies in ms)
        for k in ALLOWED_LATENCY_FIELDS:
            if k in data:
                v = data[k]
                if not isinstance(v, bool) and isinstance(v, (int, float)) and 0.0 <= float(v) <= 300_000.0:
                    clean[k] = round(float(v), 1)

        # 4. Booleans
        for k in ALLOWED_BOOL_FIELDS:
            if k in data and isinstance(data[k], bool):
                clean[k] = data[k]

        # 5. Allowlisted Categorical Lists (filters_applied)
        if "filters_applied" in data and isinstance(data["filters_applied"], (list, tuple, set)):
            clean_filters = []
            for item in data["filters_applied"]:
                if len(clean_filters) >= 16:
                    break
                if isinstance(item, str):
                    f_item = item.strip().lower()
                    if f_item in ALLOWED_FILTER_CATEGORIES and f_item not in clean_filters:
                        clean_filters.append(f_item)
            clean["filters_applied"] = clean_filters

        # 6. Nested Bounded Mappings (Max depth 1, allowlisted keys)
        if "provider_latency_ms" in data and isinstance(data["provider_latency_ms"], dict):
            clean_lat = {}
            for pk, pv in data["provider_latency_ms"].items():
                if len(clean_lat) >= 5:
                    break
                pk_clean = str(pk).strip().lower()
                if pk_clean in ALLOWED_PROVIDER_NAMES and not isinstance(pv, bool) and isinstance(pv, (int, float)) and 0.0 <= float(pv) <= 300_000.0:
                    clean_lat[pk_clean] = round(float(pv), 1)
            if clean_lat:
                clean["provider_latency_ms"] = clean_lat

        if "provider_hit_count" in data and isinstance(data["provider_hit_count"], dict):
            clean_hits = {}
            for pk, pv in data["provider_hit_count"].items():
                if len(clean_hits) >= 5:
                    break
                pk_clean = str(pk).strip().lower()
                if pk_clean in ALLOWED_PROVIDER_NAMES and not isinstance(pv, bool) and isinstance(pv, int) and 0 <= pv <= 100_000:
                    clean_hits[pk_clean] = pv
            if clean_hits:
                clean["provider_hit_count"] = clean_hits

        if "filter_drop_counts" in data and isinstance(data["filter_drop_counts"], dict):
            clean_drops = {}
            for dk, dv in data["filter_drop_counts"].items():
                if len(clean_drops) >= 10:
                    break
                dk_clean = str(dk).strip().lower()
                if dk_clean in ALLOWED_DROP_CATEGORIES and not isinstance(dv, bool) and isinstance(dv, int) and 0 <= dv <= 100_000:
                    clean_drops[dk_clean] = dv
            if clean_drops:
                clean["filter_drop_counts"] = clean_drops

        return clean
    except Exception as ex:
        log.debug("validate_retrieval_metadata non-fatal error: %s", ex)
        return {}


class NoOpObservation:
    """Zero-overhead no-op placeholder when telemetry is disabled or failed."""
    id: str = "noop"
    trace_id: str = "noop"

    def set_outcome(self, outcome: str) -> None:
        pass

    def set_role(self, role: str) -> None:
        pass

    def set_intent_hint(self, intent_hint: str) -> None:
        pass

    def set_total_ms(self, total_ms: int) -> None:
        pass

    def set_error(self, err_type: str) -> None:
        pass

    def set_retrieval_metadata(self, retrieval_meta: Any) -> None:
        pass

    def update(self, **kwargs: Any) -> None:
        pass


class NoOpGeneration:
    """Zero-overhead no-op placeholder for LLM generation tracing."""
    id: str = "noop"
    trace_id: str = "noop"

    def set_usage(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: Optional[int] = None,
    ) -> None:
        pass

    def set_ttft(self, ttft_ms: float) -> None:
        pass

    def set_finish_reason(self, finish_reason: str) -> None:
        pass

    def set_error(self, err_type: str) -> None:
        pass

    def update(self, **kwargs: Any) -> None:
        pass


class RequestObservationWrapper:
    """Safe wrapper around Langfuse observation for /chat or /chat/stream."""

    def __init__(self, raw_obs: Any):
        self.raw_obs = raw_obs
        self.metadata: Dict[str, Any] = {}
        self.outcome: str = "ok"
        self.error_type: Optional[str] = None
        self.total_ms: Optional[int] = None

    def set_outcome(self, outcome: str) -> None:
        self.outcome = outcome
        self.metadata["outcome"] = outcome

    def set_role(self, role: str) -> None:
        self.metadata["role"] = str(role or "unknown")

    def set_intent_hint(self, intent_hint: str) -> None:
        self.metadata["intent_hint"] = str(intent_hint or "unknown")

    def set_total_ms(self, total_ms: int) -> None:
        self.total_ms = int(total_ms)
        self.metadata["total_ms"] = int(total_ms)

    def set_error(self, err_type: str) -> None:
        self.error_type = str(err_type)
        self.metadata["error_type"] = str(err_type)

    def set_retrieval_metadata(self, retrieval_meta: Any) -> None:
        try:
            if not isinstance(retrieval_meta, dict):
                return
            clean_retrieval = validate_retrieval_metadata(retrieval_meta)
            if clean_retrieval:
                self.metadata["retrieval"] = clean_retrieval
        except Exception as ex:
            log.debug("Telemetry set_retrieval_metadata non-fatal warning: %s", ex)

    def finalize(self) -> None:
        try:
            if hasattr(self.raw_obs, "update"):
                retrieval_meta = self.metadata.pop("retrieval", None)
                clean_meta = sanitize_telemetry_metadata(self.metadata)
                if retrieval_meta is not None:
                    try:
                        clean_retrieval = validate_retrieval_metadata(retrieval_meta)
                        if clean_retrieval:
                            clean_meta["retrieval"] = clean_retrieval
                            self.metadata["retrieval"] = clean_retrieval
                    except Exception as val_ex:
                        log.debug("Telemetry finalize retrieval revalidation non-fatal warning: %s", val_ex)
                lvl = "ERROR" if self.error_type else "DEFAULT"
                self.raw_obs.update(
                    metadata=clean_meta,
                    level=lvl,
                    status_message=self.error_type or self.outcome,
                )
        except Exception as ex:
            log.debug("Telemetry request finalize non-fatal warning: %s", ex)


class GenerationObservationWrapper:
    """Safe wrapper around Langfuse generation observation for LLMService."""

    def __init__(self, raw_obs: Any):
        self.raw_obs = raw_obs
        self.metadata: Dict[str, Any] = {}
        self.usage_details: Optional[Dict[str, int]] = None
        self.finish_reason: Optional[str] = None
        self.error_type: Optional[str] = None
        self.ttft_ms: Optional[float] = None

    def set_usage(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: Optional[int] = None,
    ) -> None:
        # Strictly avoid fabricating zero when usage is unavailable (-1 or None)
        if prompt_tokens is not None and prompt_tokens >= 0 and completion_tokens is not None and completion_tokens >= 0:
            tot = total_tokens if (total_tokens is not None and total_tokens >= 0) else (prompt_tokens + completion_tokens)
            self.usage_details = {
                "input": int(prompt_tokens),
                "output": int(completion_tokens),
                "total": int(tot),
            }

    def set_ttft(self, ttft_ms: float) -> None:
        if ttft_ms is not None and ttft_ms >= 0:
            self.ttft_ms = round(float(ttft_ms), 2)
            self.metadata["ttft_ms"] = self.ttft_ms

    def set_finish_reason(self, finish_reason: str) -> None:
        if finish_reason:
            self.finish_reason = str(finish_reason)
            self.metadata["finish_reason"] = self.finish_reason

    def set_error(self, err_type: str) -> None:
        self.error_type = str(err_type)
        self.metadata["error_type"] = str(err_type)

    def finalize(self) -> None:
        try:
            if hasattr(self.raw_obs, "update"):
                clean_meta = sanitize_telemetry_metadata(self.metadata)
                lvl = "ERROR" if self.error_type else "DEFAULT"
                update_kwargs: Dict[str, Any] = {
                    "metadata": clean_meta,
                    "level": lvl,
                    "status_message": self.error_type or self.finish_reason,
                }
                if self.usage_details:
                    update_kwargs["usage_details"] = self.usage_details
                self.raw_obs.update(**update_kwargs)
        except Exception as ex:
            log.debug("Telemetry generation finalize non-fatal warning: %s", ex)


# Global singleton client cache
_langfuse_client: Optional[Any] = None
_client_initialized: bool = False


def is_telemetry_enabled() -> bool:
    """Checks whether Langfuse tracing is enabled via config or env."""
    try:
        from backend.config.config import LANGFUSE_ENABLED
        return bool(LANGFUSE_ENABLED)
    except Exception:
        return os.getenv("LANGFUSE_ENABLED", "false").lower() in ("true", "1", "yes")


def get_langfuse_client() -> Optional[Any]:
    """
    Returns the singleton Langfuse client if enabled and credentials are valid.
    Fails closed and returns None if credentials are missing or Langfuse fails to init.
    """
    global _langfuse_client, _client_initialized

    if not is_telemetry_enabled():
        return None

    if _client_initialized:
        return _langfuse_client

    try:
        from backend.config.config import (
            LANGFUSE_PUBLIC_KEY,
            LANGFUSE_SECRET_KEY,
            LANGFUSE_HOST,
            LANGFUSE_RELEASE,
            LANGFUSE_SAMPLE_RATE,
        )
    except Exception:
        try:
            from backend.config.config import validate_sample_rate
        except Exception:
            def validate_sample_rate(val: Any, default: float = 1.0) -> float:
                try:
                    p = float(val)
                    return p if (0.0 <= p <= 1.0) else default
                except Exception:
                    return default

        LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
        LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
        LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
        LANGFUSE_RELEASE = os.getenv("LANGFUSE_RELEASE", "1.0.0")
        LANGFUSE_SAMPLE_RATE = validate_sample_rate(os.getenv("LANGFUSE_SAMPLE_RATE", "1.0"))

    if not LANGFUSE_PUBLIC_KEY or not LANGFUSE_SECRET_KEY:
        log.warning(
            "LANGFUSE_ENABLED is true but LANGFUSE_PUBLIC_KEY or LANGFUSE_SECRET_KEY is missing. "
            "Telemetry will remain disabled."
        )
        _client_initialized = True
        _langfuse_client = None
        return None

    try:
        from langfuse import Langfuse
        _langfuse_client = Langfuse(
            public_key=LANGFUSE_PUBLIC_KEY,
            secret_key=LANGFUSE_SECRET_KEY,
            host=LANGFUSE_HOST,
            release=LANGFUSE_RELEASE,
            sample_rate=LANGFUSE_SAMPLE_RATE,
        )
        log.info("Langfuse telemetry client initialized successfully (release=%s).", LANGFUSE_RELEASE)
    except Exception as ex:
        log.warning("Failed to initialize Langfuse telemetry client: %s. Continuing without telemetry.", ex)
        _langfuse_client = None

    _client_initialized = True
    return _langfuse_client


def reset_telemetry_client_for_testing() -> None:
    """Testing helper to reset cached client state."""
    global _langfuse_client, _client_initialized
    _langfuse_client = None
    _client_initialized = False


def shutdown_telemetry() -> None:
    """Safely flushes and shuts down the Langfuse client during application teardown."""
    global _langfuse_client
    if _langfuse_client is not None:
        try:
            if hasattr(_langfuse_client, "shutdown"):
                _langfuse_client.shutdown()
            elif hasattr(_langfuse_client, "flush"):
                _langfuse_client.flush()
            log.info("Langfuse telemetry client shut down cleanly.")
        except Exception as ex:
            log.warning("Error during Langfuse shutdown cleanup: %s", type(ex).__name__)
        finally:
            _langfuse_client = None


@contextmanager
def trace_request(
    endpoint: str,
    request_id: str,
    role: str = "unknown",
    session_id: Optional[str] = None,
):
    """
    Context manager that traces a /chat or /chat/stream request.
    Yields a RequestObservationWrapper (or NoOpObservation if telemetry is disabled).
    Never raises an exception from telemetry itself.
    """
    client = get_langfuse_client()
    if client is None:
        yield NoOpObservation()
        return

    obs_context = None
    raw_obs = None
    wrapper = None
    span_name = "chat.request" if endpoint == "/chat" else "chat.stream"
    
    initial_meta: Dict[str, Any] = {
        "endpoint": endpoint,
        "request_id": request_id,
        "role": str(role or "unknown"),
        "streamed": endpoint == "/chat/stream",
    }
    session_ref = compute_session_hmac(session_id)
    if session_ref is not None:
        initial_meta["session_ref"] = session_ref

    safe_initial_meta = sanitize_telemetry_metadata(initial_meta)

    try:
        obs_context = client.start_as_current_observation(
            name=span_name,
            as_type="chain",
            metadata=safe_initial_meta,
        )
        raw_obs = obs_context.__enter__()
        wrapper = RequestObservationWrapper(raw_obs)
    except Exception as ex:
        log.warning("Failed starting Langfuse request observation: %s", ex)
        wrapper = NoOpObservation()

    try:
        yield wrapper
    except BaseException as exc:
        if isinstance(wrapper, RequestObservationWrapper):
            wrapper.set_outcome("error")
            wrapper.set_error(type(exc).__name__)
        raise
    finally:
        if isinstance(wrapper, RequestObservationWrapper):
            wrapper.finalize()
        if obs_context is not None:
            try:
                obs_context.__exit__(None, None, None)
            except Exception as close_ex:
                log.debug("Error closing request observation context: %s", close_ex)


@contextmanager
def trace_generation(
    name: str = "llm_completion",
    model: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """
    Context manager that traces an LLM completion or stream generation in LLMService.
    Distinguishes parser generations (profile_parser, search_parser) from final answers.
    Yields a GenerationObservationWrapper (or NoOpGeneration if disabled).
    """
    client = get_langfuse_client()
    if client is None:
        yield NoOpGeneration()
        return

    obs_context = None
    raw_obs = None
    wrapper = None

    try:
        from backend.config.config import MODEL
        resolved_model = model or MODEL
    except Exception:
        resolved_model = model or "gpt-4o-mini"

    safe_initial_meta = sanitize_telemetry_metadata(metadata or {})

    try:
        obs_context = client.start_as_current_observation(
            name=name,
            as_type="generation",
            model=resolved_model,
            metadata=safe_initial_meta,
        )
        raw_obs = obs_context.__enter__()
        wrapper = GenerationObservationWrapper(raw_obs)
    except Exception as ex:
        log.warning("Failed starting Langfuse generation observation: %s", ex)
        wrapper = NoOpGeneration()

    try:
        yield wrapper
    except BaseException as exc:
        if isinstance(wrapper, GenerationObservationWrapper):
            wrapper.set_error(type(exc).__name__)
        raise
    finally:
        if isinstance(wrapper, GenerationObservationWrapper):
            wrapper.finalize()
        if obs_context is not None:
            try:
                obs_context.__exit__(None, None, None)
            except Exception as close_ex:
                log.debug("Error closing generation observation context: %s", close_ex)
