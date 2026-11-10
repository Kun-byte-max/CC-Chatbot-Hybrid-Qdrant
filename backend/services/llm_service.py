import asyncio
import json
import logging
import time
from typing import Optional, AsyncGenerator
import httpx
from fastapi import HTTPException

try:
    from backend.config.config import OPENAI_API_KEY, OPENAI_API_URL, MODEL
    from backend.utils.timing import set_compose_tokens, set_ttft
    from backend.utils.telemetry import trace_generation
except ModuleNotFoundError:
    from config.config import OPENAI_API_KEY, OPENAI_API_URL, MODEL  # type: ignore
    from utils.timing import set_compose_tokens, set_ttft  # type: ignore
    from utils.telemetry import trace_generation  # type: ignore

log = logging.getLogger(__name__)


# Application-managed LLM HTTP client state (loop-aware)
_app_llm_client: Optional[httpx.AsyncClient] = None
_app_llm_loop: Optional[asyncio.AbstractEventLoop] = None
_test_injected_client: bool = False


def init_llm_client(
    timeout: float = 30.0,
    max_connections: int = 50,
    max_keepalive_connections: int = 20,
    keepalive_expiry: float = 30.0,
) -> httpx.AsyncClient:
    """
    Initializes a shared, connection-pooled httpx.AsyncClient bound to the current running event loop.
    Intended to be called during application lifespan startup.
    """
    global _app_llm_client, _app_llm_loop, _test_injected_client
    loop = asyncio.get_running_loop()
    limits = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive_connections,
        keepalive_expiry=keepalive_expiry,
    )
    _app_llm_client = httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
    )
    _app_llm_loop = loop
    _test_injected_client = False
    return _app_llm_client


async def close_llm_client() -> None:
    """
    Closes the application-managed LLM client and drains connections.
    Intended to be called during application lifespan shutdown.
    Idempotent and safe to call repeatedly.
    """
    global _app_llm_client, _app_llm_loop, _test_injected_client
    client = _app_llm_client
    is_injected = _test_injected_client
    _app_llm_client = None
    _app_llm_loop = None
    _test_injected_client = False
    if client is not None and not client.is_closed and not is_injected:
        await client.aclose()


def set_llm_client(client: Optional[httpx.AsyncClient], is_injected: bool = True) -> None:
    """
    Sets or resets the application client (useful for testing and dependency injection).
    """
    global _app_llm_client, _app_llm_loop, _test_injected_client
    _app_llm_client = client
    _test_injected_client = bool(client is not None and is_injected)
    if client is not None:
        try:
            _app_llm_loop = asyncio.get_running_loop()
        except RuntimeError:
            _app_llm_loop = None
    else:
        _app_llm_loop = None


def get_llm_client() -> Optional[httpx.AsyncClient]:
    """Returns the current application-managed LLM client, or None."""
    return _app_llm_client


def get_llm_client_loop() -> Optional[asyncio.AbstractEventLoop]:
    """Returns the event loop that owns the current application-managed LLM client."""
    return _app_llm_loop


def _get_active_llm_client() -> tuple[Optional[httpx.AsyncClient], bool]:
    """
    Returns (client, is_pooled).
    If a valid pooled client exists for the current running event loop and is open,
    returns (pooled_client, True). Otherwise returns (None, False).
    """
    current_loop = None
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if (
        _app_llm_client is not None
        and not _app_llm_client.is_closed
        and _app_llm_loop is not None
        and current_loop is not None
        and _app_llm_loop == current_loop
        and not current_loop.is_closed()
    ):
        return _app_llm_client, True
    return None, False


class LLMService:
    @staticmethod
    async def get_chat_completion(
        system_prompt: str,
        messages: list,
        timeout: float = 30.0,
        record_tokens_as: Optional[str] = None,
        max_tokens: int = 1024,
        name: str = "llm_completion"
    ) -> str:
        payload_messages = [{"role": "system", "content": system_prompt}] + messages
        gen_meta = {
            "max_tokens": max_tokens,
            "temperature": 0.65,
            "stream": False,
            "operation": name,
        }

        with trace_generation(name=name, model=MODEL, metadata=gen_meta) as gen:
            client, is_pooled = _get_active_llm_client()
            transient_client = None
            if not is_pooled:
                transient_client = httpx.AsyncClient(timeout=timeout)
                client = transient_client

            try:
                resp = await client.post(
                    OPENAI_API_URL,
                    headers={
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": MODEL,
                        "messages": payload_messages,
                        "max_tokens": max_tokens,
                        "temperature": 0.65,
                        "stream": False,
                    },
                    timeout=timeout
                )

                if resp.status_code != 200:
                    log.error("Upstream LLM error in get_chat_completion: status=%d error=%s", resp.status_code, resp.text[:500])
                    gen.set_error(f"HTTP_{resp.status_code}")
                    raise HTTPException(
                        status_code=502,
                        detail="The AI service is temporarily unavailable. Please try again shortly."
                    )

                data = resp.json()

                usage = data.get("usage", {})
                p_tok = usage.get("prompt_tokens", -1) if isinstance(usage, dict) else -1
                c_tok = usage.get("completion_tokens", -1) if isinstance(usage, dict) else -1
                t_tok = usage.get("total_tokens", -1) if isinstance(usage, dict) else -1

                if p_tok >= 0 and c_tok >= 0:
                    gen.set_usage(prompt_tokens=p_tok, completion_tokens=c_tok, total_tokens=t_tok if t_tok >= 0 else None)

                choices = data.get("choices", [])
                if choices and isinstance(choices, list) and len(choices) > 0:
                    finish_reason = choices[0].get("finish_reason")
                    if finish_reason:
                        gen.set_finish_reason(str(finish_reason))

                if record_tokens_as == "compose":
                    set_compose_tokens(p_tok, c_tok)

                return data["choices"][0]["message"]["content"]

            except httpx.TimeoutException:
                gen.set_error("TimeoutException")
                raise HTTPException(status_code=504, detail="Request timed out.")
            except HTTPException as he:
                gen.set_error(f"HTTPException_{he.status_code}")
                raise
            except Exception as e:
                gen.set_error(type(e).__name__)
                log.exception("Unexpected error in get_chat_completion: %s", e)
                raise HTTPException(status_code=500, detail="Failed to communicate with the AI service.")
            finally:
                if transient_client is not None and not transient_client.is_closed:
                    await transient_client.aclose()

    @staticmethod
    async def stream_chat_completion(
        system_prompt: str,
        messages: list,
        timeout: float = 30.0,
        record_tokens_as: Optional[str] = None,
        max_tokens: int = 1024,
        name: str = "llm_stream_completion"
    ) -> AsyncGenerator[str, None]:
        """
        Streams chat completions from OpenAI-compatible Chat Completions API as an async generator.
        Yields content text tokens incrementally as they arrive from upstream.
        Instruments TTFT and usage observations non-invasively.
        """
        payload_messages = [{"role": "system", "content": system_prompt}] + messages
        gen_meta = {
            "max_tokens": max_tokens,
            "temperature": 0.65,
            "stream": True,
            "operation": name,
        }

        with trace_generation(name=name, model=MODEL, metadata=gen_meta) as gen:
            client, is_pooled = _get_active_llm_client()
            transient_client = None
            if not is_pooled:
                transient_client = httpx.AsyncClient(timeout=timeout)
                client = transient_client

            start_perf = time.perf_counter()
            first_token_received = False
            try:
                async with client.stream(
                    "POST",
                    OPENAI_API_URL,
                    headers={
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": MODEL,
                        "messages": payload_messages,
                        "max_tokens": max_tokens,
                        "temperature": 0.65,
                        "stream": True,
                        "stream_options": {"include_usage": True},
                    },
                    timeout=timeout,
                ) as resp:
                    if resp.status_code != 200:
                        error_bytes = await resp.aread()
                        error_text = error_bytes.decode("utf-8", errors="replace")
                        log.error("Upstream LLM stream error: status=%d error=%s", resp.status_code, error_text[:500])
                        gen.set_error(f"HTTP_{resp.status_code}")
                        raise HTTPException(
                            status_code=502,
                            detail="The AI service is temporarily unavailable. Please try again shortly."
                        )

                    async for line in resp.aiter_lines():
                        clean_line = line.strip()
                        if not clean_line or not clean_line.startswith("data:"):
                            continue

                        data_str = clean_line[len("data:"):].strip()
                        if not data_str:
                            continue
                        if data_str == "[DONE]":
                            break

                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            log.warning("Skipping malformed JSON chunk in LLM stream: %s", data_str[:80])
                            continue

                        # Record usage if present in usage-only final chunk or standard chunk
                        if isinstance(chunk.get("usage"), dict):
                            usage = chunk["usage"]
                            p_tok = usage.get("prompt_tokens", -1)
                            c_tok = usage.get("completion_tokens", -1)
                            t_tok = usage.get("total_tokens", -1)
                            if p_tok >= 0 and c_tok >= 0:
                                gen.set_usage(prompt_tokens=p_tok, completion_tokens=c_tok, total_tokens=t_tok if t_tok >= 0 else None)
                            if record_tokens_as == "compose":
                                set_compose_tokens(p_tok, c_tok)

                        choices = chunk.get("choices")
                        if choices and isinstance(choices, list) and len(choices) > 0:
                            finish_reason = choices[0].get("finish_reason")
                            if finish_reason:
                                gen.set_finish_reason(str(finish_reason))
                            delta = choices[0].get("delta", {})
                            if isinstance(delta, dict):
                                content = delta.get("content")
                                if content:
                                    if not first_token_received:
                                        first_token_received = True
                                        ttft_ms = (time.perf_counter() - start_perf) * 1000.0
                                        gen.set_ttft(ttft_ms)
                                        try:
                                            set_ttft(ttft_ms)
                                        except Exception:
                                            pass
                                    yield content

            except httpx.TimeoutException:
                gen.set_error("TimeoutException")
                raise HTTPException(status_code=504, detail="Request timed out.")
            except HTTPException as he:
                gen.set_error(f"HTTPException_{he.status_code}")
                raise
            except asyncio.CancelledError:
                gen.set_error("CancelledError")
                raise
            except Exception as e:
                gen.set_error(type(e).__name__)
                log.exception("Unexpected error in stream_chat_completion: %s", e)
                raise HTTPException(status_code=500, detail="Failed to communicate with the AI service.")
            finally:
                if transient_client is not None and not transient_client.is_closed:
                    await transient_client.aclose()
