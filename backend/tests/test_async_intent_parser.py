"""
test_async_intent_parser.py — Targeted test suite verifying native async user-data intent parsing.
Ensures parse_user_data_intents_async executes inside an active asyncio event loop
without nest_asyncio and without raising RuntimeError: This event loop is already running.
"""

import pytest
from unittest.mock import patch, AsyncMock
from backend.services.user_data_intent_parser import (
    UserIntent,
    parse_user_data_intents_async,
    parse_user_data_intents_llm,
    parse_user_data_intents,
)


@pytest.mark.anyio
async def test_async_intent_parser_with_running_loop():
    """Verify parse_user_data_intents_llm runs inside active event loop cleanly."""
    with patch("backend.services.llm_service.LLMService.get_chat_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = '["PROFILE_SUMMARY"]'
        
        intents = await parse_user_data_intents_llm("Please provide a comprehensive summary overview of my account profile")
        
        assert UserIntent.PROFILE_SUMMARY in intents
        mock_llm.assert_awaited_once()


@pytest.mark.anyio
async def test_async_intent_parser_llm_failure_fallback():
    """Verify deterministic fallback works when LLM throws an exception inside active event loop."""
    with patch("backend.services.llm_service.LLMService.get_chat_completion", side_effect=Exception("LLM Timeout")):
        intents = await parse_user_data_intents_async("Show my employment history")
        
        # Deterministic parser should catch this query instantly
        assert UserIntent.EMPLOYMENT_HISTORY in intents


def test_sync_intent_parser_deterministic_fast_path():
    """Verify synchronous interface continues working for deterministic rules."""
    intents = parse_user_data_intents("What is my salary?")
    assert UserIntent.EMPLOYMENT_SALARY in intents
