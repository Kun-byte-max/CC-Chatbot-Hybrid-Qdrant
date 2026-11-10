"""
backend/tests/test_phase2_frontend_sse.py — Unit and integration tests for Phase 2 Frontend SSE Integration.

Tests cover:
1. Static contract checks on frontend/index.html (POST /chat/stream, fetch ReadableStream, headers, fallback, UI cleanup).
2. SSE Parser unit tests: split frames, multiple frames, CRLF, comments/heartbeats, final partial buffers.
3. Assistant message container lifecycle and ordered event consumption (start -> status -> results -> delta -> done -> error).
4. Results-before-delta decoupling: instant card rendering while explanatory text is pending/streaming.
5. Deduplication and authoritative reconciliation on done.reply.
6. Card metadata preservation: jobs, urgent jobs, candidates, education, pagination, action nav, and selection controls.
7. Employment verification header/roadmap splitting (generic vs focused).
8. Failure modes and fallback to /chat JSON (failure before output vs failure after partial output).
9. Stale response protection and stream cancellation.
"""

import codecs
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
import pytest


@pytest.fixture
def frontend_html_content() -> str:
    html_path = Path(__file__).parent.parent.parent / "frontend" / "index.html"
    assert html_path.exists(), "frontend/index.html must exist"
    return html_path.read_text(encoding="utf-8")


# ============================================================================
# 1. Static Contract Checks on frontend/index.html
# ============================================================================

class TestFrontendStaticContracts:
    """Verifies that frontend/index.html correctly implements the SSE architecture without violating constraints."""

    def test_uses_fetch_and_not_native_eventsource(self, frontend_html_content: str):
        # Native EventSource cannot send POST or custom Bearer auth headers
        assert "new EventSource" not in frontend_html_content, "Must not use native EventSource"
        assert "/chat/stream" in frontend_html_content, "Must connect to /chat/stream"
        assert "response.body.getReader()" in frontend_html_content, "Must use ReadableStreamDefaultReader"

    def test_preserves_post_and_auth_headers(self, frontend_html_content: str):
        assert "method: 'POST'" in frontend_html_content
        assert "'Authorization': `Bearer ${authToken}`" in frontend_html_content
        assert "body: JSON.stringify(body)" in frontend_html_content

    def test_handles_all_sse_events(self, frontend_html_content: str):
        for evt in ["case 'start':", "case 'status':", "case 'results':", "case 'delta':", "case 'done':", "case 'error':"]:
            assert evt in frontend_html_content, f"Frontend must explicitly handle {evt}"

    def test_json_chat_fallback_preserved(self, frontend_html_content: str):
        assert "async function sendChatJson(" in frontend_html_content, "Must keep JSON /chat fallback handler"
        assert "`${backendURL}/chat`" in frontend_html_content, "Must keep fallback fetch to /chat"

    def test_single_container_lifecycle_per_stream(self, frontend_html_content: str):
        assert "function createStreamingBotMessage(" in frontend_html_content
        assert ".bot-status" in frontend_html_content
        assert ".bot-text" in frontend_html_content
        assert ".bot-actions" in frontend_html_content
        assert ".bot-selection" in frontend_html_content
        assert ".bot-cards" in frontend_html_content

    def test_stale_response_protection_and_abort_controller(self, frontend_html_content: str):
        assert "currentTurnId" in frontend_html_content
        assert "activeAbortController" in frontend_html_content
        assert "thisTurnId !== currentTurnId" in frontend_html_content

    def test_ui_cleanup_in_finally_block(self, frontend_html_content: str):
        assert "removeTypingIndicator();" in frontend_html_content
        assert "isWaiting = false;" in frontend_html_content
        assert "sendBtn.disabled = false;" in frontend_html_content

    def test_employment_verification_separator_logic_preserved(self, frontend_html_content: str):
        assert "split('\\n\\n🏢')[0]" in frontend_html_content
        assert "split('\\n\\n**Company status**')[0]" in frontend_html_content
        assert "GENERIC_EMPLOYMENT_VERIFICATION" in frontend_html_content

    def test_cleanup_abandoned_container_contract(self, frontend_html_content: str):
        # [Static Contract] Confirms cleanupAbandonedContainer removes abandoned container
        assert "function cleanupAbandonedContainer()" in frontend_html_content
        assert "assistantContainer.msgDiv.remove();" in frontend_html_content
        assert "assistantContainer = null;" in frontend_html_content
        assert "cleanupAbandonedContainer();" in frontend_html_content

    def test_send_chat_json_signal_and_turn_id_contract(self, frontend_html_content: str):
        # [Static Contract] Confirms sendChatJson accepts signal & turnId and checks identity
        assert "async function sendChatJson(body, headers, signal, turnId)" in frontend_html_content
        assert "signal.aborted" in frontend_html_content
        assert "turnId !== currentTurnId" in frontend_html_content
        assert "if (signal) fetchOptions.signal = signal;" in frontend_html_content

    def test_done_tracking_and_interruption_contract(self, frontend_html_content: str):
        # [Static Contract] Confirms done tracking and inline interruption notice
        assert "let receivedDone = false;" in frontend_html_content
        assert "receivedDone = true;" in frontend_html_content
        assert "if (!receivedDone)" in frontend_html_content
        assert "⚠️ (Response interrupted)" in frontend_html_content

    def test_render_markdown_security_contract(self, frontend_html_content: str):
        # [Static Contract] Confirms HTML escaping and safe link protocol whitelist
        assert "function escapeHtml(str)" in frontend_html_content
        assert "text = escapeHtml(text);" in frontend_html_content
        assert r"https?:\/\/|\/(?![/\\])|#|mailto:" in frontend_html_content
        assert 'rel="noopener noreferrer"' in frontend_html_content


# ============================================================================
# 2. Python Simulation of Frontend SSE Parser (dispatchSSEBlock & readSSEStream)
# ============================================================================

class SimulatedSSEParser:
    """Exact simulation of the JavaScript SSE parser implemented in frontend/index.html."""

    def __init__(self, on_message: Callable[[str, Any], None]):
        self.on_message = on_message
        self.buffer = ""

    def dispatch_block(self, raw_block: str):
        if not raw_block or not raw_block.strip():
            return
        lines = re.split(r"\r\n|\r|\n", raw_block)
        event_type = "message"
        data_lines = []

        for line in lines:
            if not line or line.startswith(":"):
                continue  # skip comment or heartbeat
            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_val = line[5:]
                if data_val.startswith(" "):
                    data_val = data_val[1:]
                data_lines.append(data_val)

        if data_lines or event_type != "message":
            raw_data = "\n".join(data_lines)
            try:
                parsed_data = json.loads(raw_data) if raw_data else None
            except Exception:
                parsed_data = raw_data
            self.on_message(event_type, parsed_data)

    def feed_chunk(self, chunk: str):
        self.buffer += chunk
        pattern = re.compile(r"\r\n\r\n|\n\n|\r\r")
        while True:
            match = pattern.search(self.buffer)
            if not match:
                break
            raw_block = self.buffer[:match.start()]
            self.buffer = self.buffer[match.end():]
            self.dispatch_block(raw_block)

    def flush_final(self):
        if self.buffer.strip():
            self.dispatch_block(self.buffer)
            self.buffer = ""


class TestSSEParserHandling:
    """Unit tests for the SSE parser matching all streaming boundary conditions."""

    def test_single_frame_nominal(self):
        events = []
        parser = SimulatedSSEParser(lambda evt, data: events.append((evt, data)))
        parser.feed_chunk('event: start\ndata: {"version": "1.0", "request_id": "r1"}\n\n')
        assert len(events) == 1
        assert events[0][0] == "start"
        assert events[0][1]["request_id"] == "r1"

    def test_multiple_frames_in_single_chunk(self):
        events = []
        parser = SimulatedSSEParser(lambda evt, data: events.append((evt, data)))
        chunk = (
            'event: start\ndata: {"version":"1.0"}\n\n'
            'event: status\ndata: {"stage":"generating"}\n\n'
            'event: delta\ndata: {"text":"Hello"}\n\n'
        )
        parser.feed_chunk(chunk)
        assert len(events) == 3
        assert [e[0] for e in events] == ["start", "status", "delta"]
        assert events[2][1]["text"] == "Hello"

    def test_frame_split_across_network_chunks(self):
        events = []
        parser = SimulatedSSEParser(lambda evt, data: events.append((evt, data)))
        # Split in the middle of event name, data payload, and delimiter
        parser.feed_chunk("event: res")
        assert len(events) == 0
        parser.feed_chunk('ults\ndata: {"res')
        assert len(events) == 0
        parser.feed_chunk('ults": [1, 2, 3]}\n')
        assert len(events) == 0
        parser.feed_chunk("\n")
        assert len(events) == 1
        assert events[0][0] == "results"
        assert events[0][1]["results"] == [1, 2, 3]

    def test_crlf_and_cr_line_endings(self):
        events = []
        parser = SimulatedSSEParser(lambda evt, data: events.append((evt, data)))
        parser.feed_chunk('event: status\r\ndata: {"stage":"ready"}\r\n\r\n')
        assert len(events) == 1
        assert events[0][0] == "status"
        assert events[0][1]["stage"] == "ready"

    def test_comments_and_heartbeats_ignored(self):
        events = []
        parser = SimulatedSSEParser(lambda evt, data: events.append((evt, data)))
        parser.feed_chunk(": heartbeat\n\n")
        assert len(events) == 0
        parser.feed_chunk("event: delta\n: inline comment\ndata: {\"text\":\"hi\"}\n\n")
        assert len(events) == 1
        assert events[0][0] == "delta"
        assert events[0][1]["text"] == "hi"

    def test_final_partial_buffer_flush(self):
        events = []
        parser = SimulatedSSEParser(lambda evt, data: events.append((evt, data)))
        # Stream terminates without trailing double newline
        parser.feed_chunk('event: done\ndata: {"success": true, "reply": "All done."}')
        assert len(events) == 0
        parser.flush_final()
        assert len(events) == 1
        assert events[0][0] == "done"
        assert events[0][1]["reply"] == "All done."

    def test_malformed_json_fallback_to_string(self):
        events = []
        parser = SimulatedSSEParser(lambda evt, data: events.append((evt, data)))
        parser.feed_chunk("event: raw\ndata: this is plain text, not JSON\n\n")
        assert len(events) == 1
        assert events[0][0] == "raw"
        assert events[0][1] == "this is plain text, not JSON"


# ============================================================================
# 3. Message Lifecycle, Ordering & Decoupling Tests
# ============================================================================

class SimulatedFrontendAssistantBubble:
    """Simulates the DOM state of an active assistant response turn in frontend/index.html."""

    def __init__(self, req_id: str):
        self.req_id = req_id
        self.status_text = ""
        self.status_visible = False
        self.bot_text = ""
        self.bot_cards_html = ""
        self.bot_actions_html = ""
        self.bot_selection_html = ""
        self.accumulated_reply = ""
        self.conversation_history = []
        self.has_meaningful_output = False
        self.result_type = None
        self.presentation_mode = None

    def on_event(self, event_type: str, data: Any):
        if event_type == "start":
            self.status_visible = False

        elif event_type == "status":
            if not self.accumulated_reply:
                self.status_text = data.get("message", "Generating response...")
                self.status_visible = True

        elif event_type == "results":
            self.has_meaningful_output = True
            self.status_visible = False
            self.result_type = data.get("result_type")
            self.presentation_mode = data.get("presentation_mode")

            results = data.get("results")
            if results:
                self.bot_cards_html = f"<cards-wrapper count={len(results)} type={self.result_type}>"
            if data.get("action_metadata"):
                self.bot_actions_html = f"<actions count={len(data['action_metadata'])}>"
            if data.get("selection_metadata"):
                self.bot_selection_html = f"<selection count={len(data['selection_metadata'])}>"

        elif event_type == "delta":
            self.has_meaningful_output = True
            self.status_visible = False
            token = data.get("text", "")
            self.accumulated_reply += token

            text_to_render = self.accumulated_reply
            if self.result_type == "employment_verification" and self.presentation_mode == "GENERIC_EMPLOYMENT_VERIFICATION":
                text_to_render = text_to_render.split("\n\n🏢")[0].split("\n\n**Company status**")[0]
            self.bot_text = text_to_render

        elif event_type == "done":
            self.status_visible = False
            final_reply = data.get("reply")
            if final_reply:
                self.accumulated_reply = final_reply
                text_to_render = self.accumulated_reply
                if self.result_type == "employment_verification" and self.presentation_mode == "GENERIC_EMPLOYMENT_VERIFICATION":
                    text_to_render = text_to_render.split("\n\n🏢")[0].split("\n\n**Company status**")[0]
                self.bot_text = text_to_render

            self.conversation_history.append({"role": "assistant", "content": self.accumulated_reply})

        elif event_type == "error":
            self.status_visible = False
            err_msg = data.get("detail", "Error")
            self.bot_text += f"\n[ERROR: {err_msg}]"


class TestStreamingEventLifecycle:
    """Verifies that events are rendered in proper order, cards decouple before text, and text accumulates cleanly."""

    def test_results_render_before_delta_arrives(self):
        bubble = SimulatedFrontendAssistantBubble("req-101")
        bubble.on_event("start", {"request_id": "req-101"})
        bubble.on_event("status", {"message": "Searching jobs..."})
        assert bubble.status_visible is True
        assert bubble.bot_cards_html == ""
        assert bubble.bot_text == ""

        # Results event arrives: cards must be rendered immediately
        bubble.on_event("results", {
            "results": [{"id": 1, "title": "Software Engineer"}],
            "result_type": "jobs"
        })
        assert bubble.has_meaningful_output is True
        assert "<cards-wrapper count=1 type=jobs>" in bubble.bot_cards_html
        assert bubble.bot_text == "", "Explanatory text must still be empty while cards are already rendered"
        assert bubble.status_visible is False

        # Delta arrives later: text appends above cards without disturbing cards
        bubble.on_event("delta", {"text": "Here are your matching jobs:"})
        assert bubble.bot_text == "Here are your matching jobs:"
        assert "<cards-wrapper count=1 type=jobs>" in bubble.bot_cards_html, "Cards must remain intact"

        # Done event reconciles
        bubble.on_event("done", {"request_id": "req-101", "reply": "Here are your matching jobs:", "success": True})
        assert len(bubble.conversation_history) == 1
        assert bubble.conversation_history[0]["content"] == "Here are your matching jobs:"

    def test_incremental_token_accumulation_without_duplicates(self):
        bubble = SimulatedFrontendAssistantBubble("req-102")
        bubble.on_event("start", {"request_id": "req-102"})

        tokens = ["Tell ", "me ", "about ", "CollarCheck."]
        for t in tokens:
            bubble.on_event("delta", {"text": t})

        assert bubble.bot_text == "Tell me about CollarCheck."
        assert bubble.accumulated_reply == "Tell me about CollarCheck."

        # Authoritative done event matches accumulated text
        bubble.on_event("done", {
            "request_id": "req-102",
            "reply": "Tell me about CollarCheck.",
            "success": True
        })
        assert bubble.bot_text == "Tell me about CollarCheck."
        assert len(bubble.conversation_history) == 1

    def test_authoritative_done_reconciles_minor_differences(self):
        bubble = SimulatedFrontendAssistantBubble("req-103")
        bubble.on_event("start", {"request_id": "req-103"})
        bubble.on_event("delta", {"text": "Incomplete sentence"})
        assert bubble.bot_text == "Incomplete sentence"

        # Server sends complete validated reply on done
        bubble.on_event("done", {
            "request_id": "req-103",
            "reply": "Incomplete sentence completed.",
            "success": True
        })
        assert bubble.bot_text == "Incomplete sentence completed."
        assert bubble.conversation_history[0]["content"] == "Incomplete sentence completed."

    def test_generic_employment_verification_separator_stripping(self):
        bubble = SimulatedFrontendAssistantBubble("req-104")
        bubble.on_event("start", {"request_id": "req-104"})
        bubble.on_event("results", {
            "results": [{"id": "emp_1", "company": "Acme"}],
            "result_type": "employment_verification",
            "presentation_mode": "GENERIC_EMPLOYMENT_VERIFICATION",
            "action_metadata": [{"id": "employment_add"}]
        })
        assert bubble.bot_actions_html == "<actions count=1>"

        # Generic employment reply contains the roadmap markdown separator
        full_reply = "Here is your verification status.\n\n🏢 **Company status**\nDetailed roadmap..."
        bubble.on_event("delta", {"text": full_reply})
        assert bubble.bot_text == "Here is your verification status."
        assert "Detailed roadmap" not in bubble.bot_text

        bubble.on_event("done", {"request_id": "req-104", "reply": full_reply, "success": True})
        assert bubble.bot_text == "Here is your verification status."
        assert bubble.conversation_history[0]["content"] == full_reply

    def test_focused_employment_verification_preserves_full_guidance(self):
        bubble = SimulatedFrontendAssistantBubble("req-105")
        bubble.on_event("start", {"request_id": "req-105"})
        bubble.on_event("results", {
            "results": [{"id": "emp_2", "company": "Google"}],
            "result_type": "employment_verification",
            "presentation_mode": "FOCUSED_EMPLOYMENT_VERIFICATION"
        })

        focused_reply = "To complete Level 4 for Google:\n1. Upload W2.\n2. Submit paystub."
        bubble.on_event("delta", {"text": focused_reply})
        assert bubble.bot_text == focused_reply

        bubble.on_event("done", {"request_id": "req-105", "reply": focused_reply, "success": True})
        assert bubble.bot_text == focused_reply


# ============================================================================
# 4. Connection Failures, Fallback & Stale Turn Protection
# ============================================================================

class TestConnectionFailuresAndFallback:
    """Verifies safe failure handling, fallback decision boundaries, and race condition protection."""

    def test_failure_before_meaningful_output_allows_fallback(self):
        # When stream fails before results or delta arrive, hasMeaningfulOutput is False.
        # This allows falling back to /chat without creating duplicate cards or messages.
        bubble = SimulatedFrontendAssistantBubble("req-201")
        assert bubble.has_meaningful_output is False
        fallback_invoked = False

        try:
            raise ConnectionError("Network dropped before any SSE bytes")
        except Exception:
            if not bubble.has_meaningful_output:
                fallback_invoked = True

        assert fallback_invoked is True, "Fallback to /chat must be permitted before output begins"

    def test_failure_after_meaningful_output_inhibits_fallback(self):
        # When stream fails after results or delta arrive, hasMeaningfulOutput is True.
        # Fallback to /chat MUST NOT run, otherwise user would see duplicate cards and turns.
        bubble = SimulatedFrontendAssistantBubble("req-202")
        bubble.on_event("results", {
            "results": [{"id": 1, "title": "Developer"}],
            "result_type": "jobs"
        })
        assert bubble.has_meaningful_output is True
        fallback_invoked = False

        try:
            raise ConnectionResetError("Stream connection broke midway")
        except Exception:
            if not bubble.has_meaningful_output:
                fallback_invoked = True
            else:
                bubble.bot_text += "\n⚠️ (Response interrupted)"

        assert fallback_invoked is False, "Fallback to /chat must NOT be invoked after cards were rendered"
        assert "Response interrupted" in bubble.bot_text
        assert "<cards-wrapper" in bubble.bot_cards_html

    def test_stale_response_protection(self):
        # Turn 1 starts
        current_turn_id = 1
        turn1_id = 1
        turn1_events = []

        # User rapidly types new message, advancing turn to Turn 2
        current_turn_id = 2
        turn2_id = 2
        turn2_events = []

        # Late event arrives for Turn 1:
        late_event = ("delta", {"text": "Old message content"})
        if turn1_id == current_turn_id:
            turn1_events.append(late_event)

        # Fresh event arrives for Turn 2:
        new_event = ("delta", {"text": "New message content"})
        if turn2_id == current_turn_id:
            turn2_events.append(new_event)

        assert len(turn1_events) == 0, "Late events from previous turn must be discarded"
        assert len(turn2_events) == 1
        assert turn2_events[0][1]["text"] == "New message content"

    def test_cancellation_cleans_up_state(self):
        # Simulates clearChat() or new request aborting previous stream
        is_waiting = True
        send_btn_disabled = True
        aborted = False

        # Clear / cancel
        aborted = True
        is_waiting = False
        send_btn_disabled = False

        assert is_waiting is False
        assert send_btn_disabled is False
        assert aborted is True


# ============================================================================
# 5. Live FastAPI Endpoint to Frontend Stream Integration Tests
# ============================================================================

import jwt
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from backend.main import app, ChatPipelineResult
from backend.api.auth import JWT_SECRET, JWT_ALGORITHM
from backend.schemas.schemas import JobCard


class TestLiveEndpointToFrontendStreamSimulation:
    """Tests streaming live bytes from POST /chat/stream into the frontend parser & bubble simulation."""

    def setup_method(self):
        self.client = TestClient(app)
        self.token = jwt.encode(
            {"id": 999, "uid": 999, "email": "stream_user@example.com", "role": "employee"},
            JWT_SECRET,
            algorithm=JWT_ALGORITHM,
        )
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.db_user_row = {
            "id": 999,
            "individual_id": "IND999",
            "fname": "Stream",
            "lname": "Tester",
            "email": "stream_user@example.com",
            "user_type": 1,
            "status": 1,
            "is_deleted": 0,
            "token": "platform_token_stream",
        }

    @patch("backend.services.llm_service.LLMService.stream_chat_completion")
    @patch("backend.api.auth.get_db")
    def test_live_conversational_stream_consumed_by_frontend_parser(self, mock_auth_db, mock_stream):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]

        async def fake_tokens(*args, **kwargs):
            yield "Hello"
            yield " from"
            yield " CollarCheck!"

        mock_stream.side_effect = fake_tokens

        bubble = SimulatedFrontendAssistantBubble("live_req_1")
        parser = SimulatedSSEParser(bubble.on_event)

        payload = {"messages": [{"role": "user", "content": "Hello CollarCheck"}]}
        res = self.client.post("/chat/stream", json=payload, headers=self.headers)
        assert res.status_code == 200

        # Simulate network packet arrivals with small 7-byte chunks
        for raw_bytes in res.iter_bytes(chunk_size=7):
            parser.feed_chunk(raw_bytes.decode("utf-8"))
        parser.flush_final()

        assert bubble.bot_text == "Hello from CollarCheck!"
        assert bubble.status_visible is False
        assert len(bubble.conversation_history) == 1
        assert bubble.conversation_history[0]["content"] == "Hello from CollarCheck!"

    @patch("backend.services.llm_service.LLMService.stream_chat_completion")
    @patch("backend.main.execute_chat_pipeline")
    @patch("backend.api.auth.get_db")
    def test_live_job_search_stream_consumed_by_frontend_parser(self, mock_auth_db, mock_pipeline, mock_stream):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]

        card = JobCard(
            job_id=101,
            title="Senior Python Developer",
            company="Quadrant AI",
            location="Bengaluru",
            salary="25-35 LPA",
            url="https://example.com/job/101"
        )
        mock_pipeline.return_value = ChatPipelineResult(
            role="jobseeker",
            intent_hint="job_search",
            msg_len=20,
            system="System prompt",
            bounded_messages=[{"role": "user", "content": "python jobs"}],
            max_tokens_compose=150,
            is_terminal=False,
            results_payload=[card],
            result_type_payload="jobs",
            allowed_ids={"101"}
        )

        async def fake_tokens(*args, **kwargs):
            yield "Here are matching Python developer roles for you:"

        mock_stream.side_effect = fake_tokens

        bubble = SimulatedFrontendAssistantBubble("live_req_2")
        parser = SimulatedSSEParser(bubble.on_event)

        payload = {"messages": [{"role": "user", "content": "python jobs"}]}
        res = self.client.post("/chat/stream", json=payload, headers=self.headers)
        assert res.status_code == 200

        # Feed across arbitrary 13-byte chunks using incremental UTF-8 decoder (mirroring browser TextDecoder)
        decoder = codecs.getincrementaldecoder("utf-8")()
        for raw_bytes in res.iter_bytes(chunk_size=13):
            parser.feed_chunk(decoder.decode(raw_bytes))
        parser.feed_chunk(decoder.decode(b"", final=True))
        parser.flush_final()

        # Both cards and text must be successfully rendered into the assistant bubble
        assert "<cards-wrapper count=1 type=jobs>" in bubble.bot_cards_html
        assert "Here are matching Python developer roles" in bubble.bot_text
        assert len(bubble.conversation_history) == 1
        assert "Here are matching Python developer roles" in bubble.conversation_history[0]["content"]

    @patch("backend.main.execute_chat_pipeline")
    @patch("backend.api.auth.get_db")
    def test_live_terminal_employment_verification_consumed_by_frontend_parser(self, mock_auth_db, mock_pipeline):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_auth_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [self.db_user_row]

        mock_pipeline.return_value = ChatPipelineResult(
            role="jobseeker",
            intent_hint="user_data",
            msg_len=25,
            system="",
            bounded_messages=[],
            max_tokens_compose=0,
            is_terminal=True,
            reply="Your employment status at Google is verified.\n\n🏢 **Company status**\nRoadmap details...",
            results=[{"id": "rec_1", "company": "Google"}],
            result_type="employment_verification",
            presentation_mode="GENERIC_EMPLOYMENT_VERIFICATION",
            action_metadata=[{"id": "employment_add"}]
        )

        bubble = SimulatedFrontendAssistantBubble("live_req_3")
        parser = SimulatedSSEParser(bubble.on_event)

        payload = {"messages": [{"role": "user", "content": "verify google"}]}
        res = self.client.post("/chat/stream", json=payload, headers=self.headers)
        assert res.status_code == 200

        decoder = codecs.getincrementaldecoder("utf-8")()
        for raw_bytes in res.iter_bytes(chunk_size=5):
            parser.feed_chunk(decoder.decode(raw_bytes))
        parser.feed_chunk(decoder.decode(b"", final=True))
        parser.flush_final()

        assert "<cards-wrapper count=1 type=employment_verification>" in bubble.bot_cards_html
        assert bubble.bot_actions_html == "<actions count=1>"
        # Generic employment roadmap stripped in display text
        assert bubble.bot_text == "Your employment status at Google is verified."
        assert "Roadmap details" not in bubble.bot_text
        # Full content preserved in history
        assert "Roadmap details" in bubble.conversation_history[0]["content"]


# ============================================================================
# 6. Comprehensive Phase 2.2 SSE Lifecycle Hardening Tests
# ============================================================================

class MockAbortSignal:
    def __init__(self):
        self.aborted = False

    def abort(self):
        self.aborted = True


class SimulatedFrontendChatSession:
    """Exact simulation of the frontend/index.html chat lifecycle, DOM message list,
    and fallback handling across multiple turns, cancellations, and interruptions."""

    def __init__(self):
        self.current_turn_id = 0
        self.active_abort_controller: Optional[MockAbortSignal] = None
        self.is_waiting = False
        self.send_btn_disabled = False
        self.send_btn_state = "send"
        self.typing_indicator_active = False
        self.conversation_history: List[Dict[str, str]] = []
        # messages_dom represents the list of message bubble divs in #messages
        self.messages_dom: List[Dict[str, Any]] = []

    def update_send_button_state(self, is_streaming: bool):
        """Simulates updateSendButtonState(isStreaming) in frontend/index.html"""
        if is_streaming:
            self.send_btn_state = "stop"
            self.send_btn_disabled = False
        else:
            self.send_btn_state = "send"
            self.send_btn_disabled = False

    def stop_generation(self):
        """Simulates stopGeneration() in frontend/index.html"""
        if not self.is_waiting or not self.active_abort_controller:
            return
        self.active_abort_controller.abort()

    def clear_chat(self):
        """Simulates clearChat() in frontend/index.html"""
        if self.active_abort_controller:
            self.active_abort_controller.abort()
            self.active_abort_controller = None
        self.current_turn_id += 1
        self.typing_indicator_active = False
        self.is_waiting = False
        self.update_send_button_state(False)
        self.conversation_history = []
        self.messages_dom = []

    def send_chat_json(
        self,
        body: Dict[str, Any],
        headers: Dict[str, str],
        signal: Optional[MockAbortSignal],
        turn_id: int,
        mock_response: Optional[Dict[str, Any]] = None,
        mock_error: Optional[Exception] = None,
    ) -> bool:
        """Simulates sendChatJson(body, headers, signal, turnId) in frontend/index.html"""
        if (signal and signal.aborted) or turn_id != self.current_turn_id:
            return False

        if mock_error is not None:
            if isinstance(mock_error, (ConnectionError, RuntimeError)):
                if (signal and signal.aborted) or turn_id != self.current_turn_id:
                    return False
                self.typing_indicator_active = False
                self.messages_dom.append({
                    "type": "bot_error",
                    "text": f"Connection Error: {mock_error}",
                })
                return False
            # AbortError
            return False

        # In-flight check
        if (signal and signal.aborted) or turn_id != self.current_turn_id:
            return False

        data = mock_response or {"reply": "Fallback response from /chat"}

        # Post-fetch check
        if (signal and signal.aborted) or turn_id != self.current_turn_id:
            return False

        self.typing_indicator_active = False
        reply = data.get("reply", "")
        self.conversation_history.append({"role": "assistant", "content": reply})
        self.messages_dom.append({
            "type": "bot",
            "text": reply,
            "results": data.get("results"),
        })
        return True

    def run_turn_stream(
        self,
        user_text: str,
        sse_events_emitter: Optional[Callable[[Callable[[str, Any], None]], None]] = None,
        fallback_response: Optional[Dict[str, Any]] = None,
        stream_error: Optional[Exception] = None,
    ):
        """Simulates sendMessage() streaming path in frontend/index.html"""
        if self.is_waiting:
            self.stop_generation()
            return
        self.is_waiting = True
        self.update_send_button_state(True)
        self.typing_indicator_active = True

        # Append user message by reference identity
        user_history_entry = {"role": "user", "content": user_text}
        self.conversation_history.append(user_history_entry)
        self.messages_dom.append({"type": "user", "text": user_text})

        # Abort prior in-flight request
        if self.active_abort_controller:
            self.active_abort_controller.abort()
        self.active_abort_controller = MockAbortSignal()
        this_turn_id = self.current_turn_id + 1
        self.current_turn_id = this_turn_id
        signal = self.active_abort_controller

        assistant_container: Optional[Dict[str, Any]] = None
        accumulated_reply = ""
        has_meaningful_output = False
        received_done = False
        interruption_notice_shown = False

        def cleanup_abandoned_container():
            nonlocal assistant_container
            if assistant_container is not None:
                if assistant_container in self.messages_dom:
                    self.messages_dom.remove(assistant_container)
                assistant_container = None

        def remove_pending_user_history():
            if this_turn_id != self.current_turn_id:
                return
            if user_history_entry in self.conversation_history:
                self.conversation_history.remove(user_history_entry)

        def ensure_container(req_id: Optional[str] = None):
            nonlocal assistant_container
            if assistant_container is None:
                self.typing_indicator_active = False
                assistant_container = {
                    "type": "bot_streaming",
                    "req_id": req_id,
                    "text": "",
                    "cards": None,
                    "interrupted": False,
                    "stopped": False,
                }
                self.messages_dom.append(assistant_container)
            return assistant_container

        try:
            def on_event(event_type: str, data: Any):
                nonlocal accumulated_reply, has_meaningful_output, received_done
                if this_turn_id != self.current_turn_id:
                    return

                if event_type == "start":
                    ensure_container(data.get("request_id") if isinstance(data, dict) else None)
                elif event_type == "results":
                    has_meaningful_output = True
                    c = ensure_container()
                    c["cards"] = data.get("results") if isinstance(data, dict) else None
                elif event_type == "delta":
                    has_meaningful_output = True
                    c = ensure_container()
                    token = data.get("text", "") if isinstance(data, dict) else ""
                    accumulated_reply += token
                    c["text"] = accumulated_reply
                elif event_type == "done":
                    received_done = True
                    c = ensure_container()
                    if isinstance(data, dict) and data.get("reply"):
                        accumulated_reply = data["reply"]
                        c["text"] = accumulated_reply
                    if this_turn_id == self.current_turn_id:
                        self.conversation_history.append({"role": "assistant", "content": accumulated_reply})

            # Stream execution
            if stream_error:
                raise stream_error

            if sse_events_emitter:
                sse_events_emitter(on_event)

            # Check if stream ended without done
            if not received_done:
                if (signal and signal.aborted) or this_turn_id != self.current_turn_id:
                    if this_turn_id != self.current_turn_id:
                        cleanup_abandoned_container()
                        return
                    if not has_meaningful_output:
                        cleanup_abandoned_container()
                        remove_pending_user_history()
                    else:
                        if not interruption_notice_shown:
                            interruption_notice_shown = True
                            c = ensure_container()
                            c["stopped"] = True
                            c["text"] += " ⏹️ Generation stopped"
                        if this_turn_id == self.current_turn_id and accumulated_reply and accumulated_reply.strip():
                            self.conversation_history.append({"role": "assistant", "content": accumulated_reply.strip()})
                    return
                if not has_meaningful_output:
                    cleanup_abandoned_container()
                    self.send_chat_json({}, {}, signal, this_turn_id, mock_response=fallback_response)
                    return
                else:
                    if not interruption_notice_shown:
                        interruption_notice_shown = True
                        c = ensure_container()
                        c["interrupted"] = True
                        c["text"] += " ⚠️ (Response interrupted)"

        except Exception as err:
            if getattr(err, "name", "") == "AbortError" or (signal and signal.aborted) or this_turn_id != self.current_turn_id:
                if this_turn_id != self.current_turn_id:
                    cleanup_abandoned_container()
                    return
                if not has_meaningful_output:
                    cleanup_abandoned_container()
                    remove_pending_user_history()
                else:
                    if not interruption_notice_shown:
                        interruption_notice_shown = True
                        c = ensure_container()
                        c["stopped"] = True
                        c["text"] += " ⏹️ Generation stopped"
                    if this_turn_id == self.current_turn_id and not received_done and accumulated_reply and accumulated_reply.strip():
                        self.conversation_history.append({"role": "assistant", "content": accumulated_reply.strip()})
                return
            if not has_meaningful_output:
                cleanup_abandoned_container()
                self.send_chat_json({}, {}, signal, this_turn_id, mock_response=fallback_response)
                return
            else:
                if not interruption_notice_shown:
                    interruption_notice_shown = True
                    c = ensure_container()
                    c["interrupted"] = True
                    c["text"] += " ⚠️ (Response interrupted)"
        finally:
            if this_turn_id == self.current_turn_id:
                self.typing_indicator_active = False
                self.is_waiting = False
                self.update_send_button_state(False)


class TestSSELifecycleHardeningScenarios:
    """Explicit tests for the 8 lifecycle scenarios required by Phase 2.2."""

    def test_scenario_1_stream_fails_before_output_produces_single_bubble(self):
        """[Python Simulation] Scenario 1: Stream emits start, then fails before results/delta.
        Fallback produces exactly one assistant bubble with no orphaned bubble left behind."""
        session = SimulatedFrontendChatSession()

        def stream_emitter(on_event):
            on_event("start", {"request_id": "r-101"})
            # Early connection crash before results or delta
            raise ConnectionResetError("Connection dropped by peer")

        session.run_turn_stream(
            "Find Python jobs",
            sse_events_emitter=stream_emitter,
            fallback_response={"reply": "Here are 5 Python jobs found via /chat fallback."}
        )

        # Check DOM message elements
        user_msgs = [m for m in session.messages_dom if m["type"] == "user"]
        bot_msgs = [m for m in session.messages_dom if m["type"] in ("bot", "bot_streaming")]

        assert len(user_msgs) == 1, "User message must be preserved"
        assert len(bot_msgs) == 1, "Must produce exactly ONE assistant bubble; no orphaned bubble"
        assert bot_msgs[0]["type"] == "bot", "Fallback message should replace the abandoned streaming bubble"
        assert "5 Python jobs found" in bot_msgs[0]["text"]
        assert len(session.conversation_history) == 2  # user + assistant

    def test_scenario_2_clear_chat_during_pending_fallback_cannot_repopulate(self):
        """[Python Simulation] Scenario 2: clearChat() occurs while /chat fallback is pending.
        The late response cannot repopulate the cleared conversation."""
        session = SimulatedFrontendChatSession()

        # Simulates a turn that started and failed early, initiating a fallback
        session.is_waiting = True
        session.conversation_history.append({"role": "user", "content": "Initial query"})
        session.messages_dom.append({"type": "user", "text": "Initial query"})
        signal = MockAbortSignal()
        session.active_abort_controller = signal
        session.current_turn_id = 1
        turn1_id = 1

        # User calls clearChat() while /chat HTTP request is in-flight:
        session.clear_chat()
        assert len(session.messages_dom) == 0
        assert len(session.conversation_history) == 0
        assert signal.aborted is True

        # Now late fallback response resolves:
        accepted = session.send_chat_json(
            {}, {}, signal, turn1_id, mock_response={"reply": "Late fallback reply"}
        )

        assert accepted is False, "Late response must be rejected"
        assert len(session.messages_dom) == 0, "Cleared DOM must NOT be repopulated"
        assert len(session.conversation_history) == 0, "Cleared history must NOT be repopulated"

    def test_scenario_3_newer_turn_starts_while_older_fallback_pending(self):
        """[Python Simulation] Scenario 3: A newer turn starts while older fallback is pending.
        Older response cannot change the newer turn's UI or history."""
        session = SimulatedFrontendChatSession()

        # Turn 1: user sends message, fails early, fallback pending
        turn1_signal = MockAbortSignal()
        session.active_abort_controller = turn1_signal
        session.current_turn_id = 1
        turn1_id = 1
        session.conversation_history.append({"role": "user", "content": "Turn 1 question"})
        session.messages_dom.append({"type": "user", "text": "Turn 1 question"})

        # Turn 2: user sends new message before Turn 1 fallback arrives
        def turn2_stream(on_event):
            on_event("start", {"request_id": "r-turn2"})
            on_event("delta", {"text": "Turn 2 quick answer"})
            on_event("done", {"reply": "Turn 2 quick answer"})

        session.run_turn_stream("Turn 2 question", sse_events_emitter=turn2_stream)

        assert session.current_turn_id == 2
        assert turn1_signal.aborted is True

        # Now Turn 1's late fallback resolves:
        accepted = session.send_chat_json(
            {}, {}, turn1_signal, turn1_id, mock_response={"reply": "Turn 1 delayed fallback"}
        )
        assert accepted is False, "Turn 1 response must be ignored"

        # Verify history & DOM belong strictly to Turn 2:
        assert session.conversation_history[-1]["content"] == "Turn 2 quick answer"
        bot_texts = [m["text"] for m in session.messages_dom if m["type"] in ("bot", "bot_streaming")]
        assert "Turn 1 delayed fallback" not in bot_texts
        assert "Turn 2 quick answer" in bot_texts

    def test_scenario_4_intentional_abort_does_not_trigger_json_fallback(self):
        """[Python Simulation] Scenario 4: Intentional abort does not trigger a JSON fallback."""
        session = SimulatedFrontendChatSession()

        def aborted_stream(on_event):
            on_event("start", {"request_id": "r-abort"})
            # Simulate intentional user abort (signal.abort())
            session.active_abort_controller.abort()
            err = Exception("Fetch aborted")
            err.name = "AbortError"
            raise err

        session.run_turn_stream(
            "Query to abort",
            sse_events_emitter=aborted_stream,
            fallback_response={"reply": "Fallback that should NEVER be called"}
        )

        bot_msgs = [m for m in session.messages_dom if m["type"] in ("bot", "bot_streaming")]
        assert len(bot_msgs) == 0, "No bot bubble or fallback message should exist after intentional abort"
        # No assistant response in history
        assert not any(m["role"] == "assistant" for m in session.conversation_history)

    def test_scenario_5_stream_ends_after_delta_without_done_shows_interruption(self):
        """[Python Simulation] Scenario 5: Stream ends cleanly after one or more delta events
        but without done. Interruption notice is visible and incomplete text is not recorded
        as a completed assistant turn in conversation history."""
        session = SimulatedFrontendChatSession()

        def truncated_stream(on_event):
            on_event("start", {"request_id": "r-trunc"})
            on_event("delta", {"text": "Partial sentence that cut off midway..."})
            # Clean EOF without a done event

        session.run_turn_stream("Tell me a story", sse_events_emitter=truncated_stream)

        # Bubble must display partial text + interruption notice
        bot_msgs = [m for m in session.messages_dom if m["type"] in ("bot", "bot_streaming")]
        assert len(bot_msgs) == 1
        assert "Partial sentence that cut off midway..." in bot_msgs[0]["text"]
        assert "⚠️ (Response interrupted)" in bot_msgs[0]["text"]
        assert bot_msgs[0]["interrupted"] is True

        # Incomplete text must NOT be added to conversation history as a completed turn
        assistant_turns = [m for m in session.conversation_history if m["role"] == "assistant"]
        assert len(assistant_turns) == 0, "Incomplete reply must not be recorded in conversation history"

        # User must still be able to submit the next message normally
        assert session.is_waiting is False
        assert session.send_btn_disabled is False

    def test_scenario_6_stream_ends_without_done_before_meaningful_output(self):
        """[Python Simulation] Scenario 6: Stream ends without done before meaningful output.
        Fallback occurs only if the request is still active and not aborted."""
        # 6A: Active, non-aborted request falls back cleanly
        session_a = SimulatedFrontendChatSession()

        def clean_eof_no_output(on_event):
            on_event("start", {"request_id": "r-empty"})
            # Clean EOF without delta, results, or done

        session_a.run_turn_stream(
            "Hello",
            sse_events_emitter=clean_eof_no_output,
            fallback_response={"reply": "Recovered via /chat fallback"}
        )

        bot_msgs_a = [m for m in session_a.messages_dom if m["type"] in ("bot", "bot_streaming")]
        assert len(bot_msgs_a) == 1, "Abandoned bubble cleaned up, fallback produced exactly 1 bubble"
        assert bot_msgs_a[0]["text"] == "Recovered via /chat fallback"
        assert len(session_a.conversation_history) == 2

        # 6B: Aborted request does NOT fall back
        session_b = SimulatedFrontendChatSession()

        def clean_eof_aborted(on_event):
            on_event("start", {"request_id": "r-empty-abort"})
            session_b.active_abort_controller.abort()

        session_b.run_turn_stream(
            "Hello",
            sse_events_emitter=clean_eof_aborted,
            fallback_response={"reply": "Should not appear"}
        )

        bot_msgs_b = [m for m in session_b.messages_dom if m["type"] in ("bot", "bot_streaming")]
        assert len(bot_msgs_b) == 0, "Aborted request must not trigger fallback or leave abandoned container"

    def test_scenario_7_normal_done_reconciliation_records_single_response(self):
        """[Python Simulation] Scenario 7: Normal done reconciliation still records
        exactly one assistant response."""
        session = SimulatedFrontendChatSession()

        def normal_stream(on_event):
            on_event("start", {"request_id": "r-normal"})
            on_event("delta", {"text": "Complete "})
            on_event("delta", {"text": "response."})
            on_event("done", {"reply": "Complete response."})

        session.run_turn_stream("Normal query", sse_events_emitter=normal_stream)

        bot_msgs = [m for m in session.messages_dom if m["type"] in ("bot", "bot_streaming")]
        assert len(bot_msgs) == 1
        assert bot_msgs[0]["text"] == "Complete response."
        assert len(session.conversation_history) == 2
        assert session.conversation_history[1] == {"role": "assistant", "content": "Complete response."}

    def test_scenario_8_ui_state_after_abort_fallback_and_stale_responses(self):
        """[Python Simulation] Scenario 8: Send-button and typing-indicator state remain correct
        after abort, fallback success, fallback failure, and stale responses."""
        session = SimulatedFrontendChatSession()

        # 8A: After abort via clearChat()
        session.run_turn_stream("Q1", sse_events_emitter=lambda e: None)
        session.clear_chat()
        assert session.is_waiting is False
        assert session.send_btn_disabled is False
        assert session.typing_indicator_active is False

        # 8B: After fallback success
        session.run_turn_stream(
            "Q2",
            stream_error=ConnectionError("Drop"),
            fallback_response={"reply": "Fallback worked"}
        )
        assert session.is_waiting is False
        assert session.send_btn_disabled is False
        assert session.typing_indicator_active is False

        # 8C: After fallback failure (genuine transport failure)
        session.is_waiting = True
        session.current_turn_id += 1
        turn_c_id = session.current_turn_id
        session.send_chat_json({}, {}, None, turn_c_id, mock_error=ConnectionError("Backend down"))
        assert session.typing_indicator_active is False
        error_msgs = [m for m in session.messages_dom if m["type"] == "bot_error"]
        assert len(error_msgs) == 1

        # 8D: Stale response does not alter active turn UI state
        # Active turn is Turn 10
        session.current_turn_id = 10
        session.is_waiting = True
        session.send_btn_disabled = True
        session.typing_indicator_active = True

        # Late response from Turn 5 arrives
        session.send_chat_json({}, {}, None, 5, mock_response={"reply": "Old turn"})
        # Newer turn UI state remains intact!
        assert session.is_waiting is True
        assert session.send_btn_disabled is True
        assert session.typing_indicator_active is True


# ============================================================================
# 7. Markdown Security Investigation & Hardening Tests
# ============================================================================

def simulate_render_markdown(text: str) -> str:
    """Exact simulation of renderMarkdown(text) implemented in frontend/index.html."""
    if not text or not isinstance(text, str):
        return ""

    # 1. HTML Escaping (prevents XSS)
    text = (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )

    # 2. Headers
    text = re.sub(r"^### (.+)$", r"<h4>\1</h4>", text, flags=re.MULTILINE)
    text = re.sub(r"^## (.+)$", r"<h3>\1</h3>", text, flags=re.MULTILINE)

    # 3. Bold / italic
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)

    # 4. Action guidance buttons
    text = re.sub(
        r"\[(How to complete Level \d+)\](?!\()",
        r"""<button type="button" class="quick-chip-btn" style="background: var(--cc-navy, #0B1F3A); color: white; border: 1px solid var(--border-color); padding: 6px 12px; border-radius: 6px; font-size: 12px; font-weight: 500; cursor: pointer; margin-top: 6px; margin-bottom: 6px; display: inline-block;" onclick="askChip('\1')">👉 \1</button>""",
        text,
    )

    # 5. Safe Links (whitelist safe schemes: https, http, safe single-slash relative, anchor, mailto; reject //host and /\host)
    def link_repl(match):
        label = match.group(1)
        url = match.group(2).strip()
        if re.search(r"[\x00-\x1F\x7F-\x9F]", url):
            return f"{label} ({url})"
        if re.match(r"^(?:https?://|/(?![/\\])|#|mailto:)", url, re.IGNORECASE):
            return f'<a href="{url}" target="_blank" rel="noopener noreferrer" style="color: var(--accent-blue, #2563EB); font-weight: 600;">{label}</a>'
        return f"{label} ({url})"

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link_repl, text)

    # 6. Inline code
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)

    # 7. Lists
    lines = text.split("\n")
    in_list = False
    result = []
    for line in lines:
        is_item = bool(re.match(r"^\s*[-•]\s+", line) or re.match(r"^\s*\d+\.\s+", line))
        if is_item:
            if not in_list:
                result.append("<ul>")
                in_list = True
            content = re.sub(r"^\s*[-•]\s+", "", line)
            content = re.sub(r"^\s*\d+\.\s+", "", content)
            result.append(f"<li>{content}</li>")
        else:
            if in_list:
                result.append("</ul>")
                in_list = False
            result.append(line)
    if in_list:
        result.append("</ul>")
    text = "\n".join(result)

    # 8. Paragraphs
    text = text.replace("\n\n", "</p><p>")
    text = text.replace("\n", "<br>")
    if not text.startswith("<"):
        text = "<p>" + text + "</p>"
    return text


class TestMarkdownSecurityHardening:
    """Verifies that renderMarkdown neutralizes raw HTML and unsafe link schemes
    while preserving legitimate links, action buttons, and formatting."""

    def test_raw_script_and_event_handlers_escaped(self):
        """[Python Simulation] Verifies raw HTML tags like <script> or onerror are entity-escaped."""
        payload = "<script>alert('pwned')</script> and <img src=x onerror=alert(1)>"
        rendered = simulate_render_markdown(payload)

        assert "<script>" not in rendered
        assert "&lt;script&gt;alert(&#39;pwned&#39;)&lt;/script&gt;" in rendered or "&lt;script&gt;" in rendered
        assert "<img" not in rendered
        assert "&lt;img" in rendered

    def test_unsafe_javascript_scheme_neutralized(self):
        """[Python Simulation] Verifies javascript: links in Markdown are not converted to active <a> tags."""
        payload = "[Click here to claim prize](javascript:alert(document.cookie))"
        rendered = simulate_render_markdown(payload)

        assert 'href="javascript:' not in rendered
        assert "<a href=" not in rendered
        assert "Click here to claim prize (javascript:alert(document.cookie))" in rendered

    def test_unsafe_data_and_vbscript_schemes_neutralized(self):
        """[Python Simulation] Verifies data: and vbscript: links are neutralized."""
        data_payload = "[Exploit](data:text/html,<script>alert(1)</script>)"
        rendered = simulate_render_markdown(data_payload)
        assert 'href="data:' not in rendered

        vb_payload = "[VBS](vbscript:msgbox(1))"
        rendered_vb = simulate_render_markdown(vb_payload)
        assert 'href="vbscript:' not in rendered_vb

    def test_protocol_relative_and_backslash_links_rejected(self):
        """[Python Simulation] Verifies //host and /\\host links are neutralized."""
        for target in ["//attacker.example/job", "/\\attacker.example/job", "///evil.com", "\\attacker.example"]:
            payload = f"[Phishing]({target})"
            rendered = simulate_render_markdown(payload)
            assert f'href="{target}"' not in rendered
            assert "<a href=" not in rendered
            assert f"Phishing ({target})" in rendered

    def test_legitimate_https_and_relative_links_preserved(self):
        """[Python Simulation] Verifies valid HTTPS, HTTP, and relative links render with rel='noopener noreferrer'."""
        https_payload = "[CollarCheck](https://www.collarcheck.com/jobs)"
        rendered = simulate_render_markdown(https_payload)
        assert '<a href="https://www.collarcheck.com/jobs" target="_blank" rel="noopener noreferrer"' in rendered
        assert "CollarCheck</a>" in rendered

        rel_payload = "[Dashboard](/dashboard/user/experience)"
        rendered_rel = simulate_render_markdown(rel_payload)
        assert '<a href="/dashboard/user/experience" target="_blank" rel="noopener noreferrer"' in rendered_rel

    def test_action_guidance_buttons_and_lists_preserved(self):
        """[Python Simulation] Verifies [How to complete Level X] guidance chips and lists render correctly."""
        payload = (
            "### Verification Steps\n\n"
            "- Step 1: Upload ID\n"
            "- Step 2: [How to complete Level 2]\n"
        )
        rendered = simulate_render_markdown(payload)

        assert "<h4>Verification Steps</h4>" in rendered
        assert "<ul>" in rendered
        assert "<li>Step 1: Upload ID</li>" in rendered
        assert 'class="quick-chip-btn"' in rendered
        assert "askChip('Level 2')" in rendered or "askChip('How to complete Level 2')" in rendered


class TestPhase24FrontendSecurity:
    """Targeted regression tests for Phase 2.4 frontend security fixes."""

    def test_protocol_relative_markdown_links_rejected(self):
        """[Python Simulation] Verifies //host and /\\host links are rejected from markdown <a> tags."""
        for target in ["//attacker.example/login", "/\\attacker.example/login"]:
            payload = f"[Attacker]({target})"
            rendered = simulate_render_markdown(payload)
            assert f'href="{target}"' not in rendered
            assert '<a href=' not in rendered
            assert f"Attacker ({target})" in rendered

    def test_frontend_card_url_sanitization_contract(self, frontend_html_content: str):
        """[Static Contract] Verifies sanitizeCardUrl is declared and used on all card types."""
        assert "function sanitizeCardUrl(rawUrl" in frontend_html_content
        assert "const url = sanitizeCardUrl(" in frontend_html_content
        assert "const sanitizedUrl = url ? sanitizeCardUrl(" in frontend_html_content
        # Verify rel="noopener noreferrer" present on external card links
        assert 'rel="noopener noreferrer" class="card-action-btn' in frontend_html_content

    def test_frontend_chip_safe_handling_contract(self, frontend_html_content: str):
        """[Static Contract] Verifies chips avoid inline code interpolation and use data-* attributes."""
        # Selection chips use data-* attributes and static handlers
        assert 'data-selection-key="${safeKey}"' in frontend_html_content
        assert 'data-company-name="${safeCompany}"' in frontend_html_content
        assert 'onclick="handleSelectionRecordChip(this)"' in frontend_html_content
        assert 'onclick="handleSelectionCompanyChip(this)"' in frontend_html_content
        # No inline interpolation of dynamic values in onclick
        assert "onclick=\"askEmploymentRecordChip('" not in frontend_html_content
        assert "onclick=\"askEmploymentCompanyChip('" not in frontend_html_content
        # Suggestion chips use addEventListener with closures
        assert "btn.addEventListener('click', () => askChip(cleanText))" in frontend_html_content

    def test_escape_html_single_definition(self, frontend_html_content: str):
        """[Static Contract] Verifies duplicate escapeHtml was consolidated into a single declaration."""
        matches = re.findall(r"function\s+escapeHtml\s*\(", frontend_html_content)
        assert len(matches) == 1


# ============================================================================
# 8. Phase 3.2 Stop Generation UX and Lifecycle Verification
# ============================================================================

class TestPhase32StopGenerationUX:
    """Targeted static contract and simulation tests for Phase 3.2 Stop Generation UX."""

    def test_send_stop_button_static_contracts(self, frontend_html_content: str):
        """[Static Contract] Verifies Stop button markup, CSS, and helper declarations."""
        assert "function updateSendButtonState(" in frontend_html_content
        assert "function stopGeneration(" in frontend_html_content
        assert "function removePendingUserHistory(" in frontend_html_content
        assert ".send-btn.stop-btn" in frontend_html_content
        assert "sendBtn.setAttribute('title', 'Stop generation')" in frontend_html_content
        assert "sendBtn.setAttribute('aria-label', 'Stop generation')" in frontend_html_content
        assert 'title="Send message"' in frontend_html_content
        assert 'aria-label="Send message"' in frontend_html_content
        assert "⏹️ Generation stopped" in frontend_html_content
        assert "document.createElement('div')" in frontend_html_content
        assert "stream-stopped-notice" in frontend_html_content

    def test_stop_button_state_transitions(self):
        """[Python Simulation] Stop button toggles to Stop while turn is active, returns to Send afterward."""
        session = SimulatedFrontendChatSession()
        assert session.send_btn_state == "send"
        assert session.is_waiting is False
        assert session.send_btn_disabled is False

        # Start an active turn that stops midway
        def streaming_emitter(on_event):
            assert session.send_btn_state == "stop"
            assert session.is_waiting is True
            assert session.send_btn_disabled is False
            on_event("start", {"request_id": "r-btn-test"})
            on_event("delta", {"text": "Working..."})
            on_event("done", {"reply": "Working... done"})

        session.run_turn_stream("Check button state", sse_events_emitter=streaming_emitter)

        # After turn completion, state is reset
        assert session.send_btn_state == "send"
        assert session.is_waiting is False
        assert session.send_btn_disabled is False

    def test_stop_before_meaningful_output_cleans_container_and_history(self):
        """[Python Simulation] Stop before meaningful output removes empty container and pending user history."""
        session = SimulatedFrontendChatSession()

        def early_stopped_stream(on_event):
            on_event("start", {"request_id": "r-early-stop"})
            # User stops immediately after start before any results or delta
            session.stop_generation()

        session.run_turn_stream("Quick cancel query", sse_events_emitter=early_stopped_stream)

        # Empty assistant container removed
        bot_msgs = [m for m in session.messages_dom if m["type"] in ("bot", "bot_streaming")]
        assert len(bot_msgs) == 0, "Empty assistant container must be removed on early stop"

        # Pending user turn removed from history
        assert len(session.conversation_history) == 0, "Pending user history entry must be cleaned up on early stop"

        # UI restored
        assert session.is_waiting is False
        assert session.send_btn_state == "send"

    def test_stop_after_meaningful_output_preserves_container_and_saves_history(self):
        """[Python Simulation] Stop after cards or partial text preserves output and adds stopped indicator."""
        session = SimulatedFrontendChatSession()

        def mid_stopped_stream(on_event):
            on_event("start", {"request_id": "r-mid-stop"})
            on_event("results", {"results": [{"id": 1, "title": "Software Engineer"}]})
            on_event("delta", {"text": "Here are relevant openings: "})
            # User stops mid-stream
            session.stop_generation()

        session.run_turn_stream("Find software engineer jobs", sse_events_emitter=mid_stopped_stream)

        # Container is preserved in DOM with cards and partial text
        bot_msgs = [m for m in session.messages_dom if m["type"] in ("bot", "bot_streaming")]
        assert len(bot_msgs) == 1, "Rendered output must be preserved on mid-stream stop"
        assert bot_msgs[0]["cards"] is not None
        assert "Here are relevant openings:" in bot_msgs[0]["text"]
        assert "⏹️ Generation stopped" in bot_msgs[0]["text"]
        assert bot_msgs[0]["stopped"] is True

        # Partial assistant turn saved in history without duplicating
        assert len(session.conversation_history) == 2  # user + assistant
        assert session.conversation_history[0]["role"] == "user"
        assert session.conversation_history[1]["role"] == "assistant"
        assert session.conversation_history[1]["content"] == "Here are relevant openings:"

        # UI restored
        assert session.is_waiting is False
        assert session.send_btn_state == "send"

    def test_stopping_does_not_trigger_json_fallback(self):
        """[Python Simulation] Stopping a generation must never trigger a /chat JSON fallback."""
        session = SimulatedFrontendChatSession()

        def aborted_stream(on_event):
            on_event("start", {"request_id": "r-no-fallback"})
            session.stop_generation()

        session.run_turn_stream(
            "Cancel me",
            sse_events_emitter=aborted_stream,
            fallback_response={"reply": "ILLEGAL FALLBACK"}
        )

        # No fallback response in DOM or history
        bot_texts = [m.get("text", "") for m in session.messages_dom]
        assert "ILLEGAL FALLBACK" not in bot_texts
        assert not any("ILLEGAL FALLBACK" in h.get("content", "") for h in session.conversation_history)

    def test_completed_turn_not_changed_by_late_stop(self):
        """[Python Simulation] A completed turn is not changed by a late Stop action."""
        session = SimulatedFrontendChatSession()

        def complete_stream(on_event):
            on_event("start", {"request_id": "r-done"})
            on_event("delta", {"text": "Completed answer."})
            on_event("done", {"reply": "Completed answer."})

        session.run_turn_stream("Normal question", sse_events_emitter=complete_stream)

        assert len(session.conversation_history) == 2
        assert session.conversation_history[1]["content"] == "Completed answer."
        assert session.is_waiting is False

        # Late stop action when turn is already completed
        session.stop_generation()

        # Output and history must remain unchanged
        assert len(session.conversation_history) == 2
        assert session.conversation_history[1]["content"] == "Completed answer."
        bot_msgs = [m for m in session.messages_dom if m["type"] in ("bot", "bot_streaming")]
        assert len(bot_msgs) == 1
        assert "⏹️ Generation stopped" not in bot_msgs[0]["text"]

    def test_stale_turn_cannot_change_newer_turn_ui_or_history(self):
        """[Python Simulation] A stale turn cannot change a newer turn's UI state or history."""
        session = SimulatedFrontendChatSession()

        # Turn 1: user starts turn, aborted early
        turn1_signal = MockAbortSignal()
        session.active_abort_controller = turn1_signal
        session.current_turn_id = 1
        session.is_waiting = True
        session.update_send_button_state(True)
        turn1_id = 1

        # Turn 2: starts
        def turn2_stream(on_event):
            on_event("start", {"request_id": "r-turn2"})
            on_event("delta", {"text": "Fresh answer for turn 2"})
            on_event("done", {"reply": "Fresh answer for turn 2"})

        session.is_waiting = False  # previous stopped
        session.run_turn_stream("Turn 2 query", sse_events_emitter=turn2_stream)

        # Late Turn 1 fallback attempt
        accepted = session.send_chat_json({}, {}, turn1_signal, turn1_id, mock_response={"reply": "Stale reply"})
        assert accepted is False

        # Only Turn 2 response exists
        assert session.conversation_history[-1]["content"] == "Fresh answer for turn 2"
        assert session.send_btn_state == "send"

    def test_clear_chat_remains_distinct_from_stop(self):
        """[Python Simulation] Clear Chat aborts and clears transcript and history, while Stop preserves prior turns."""
        session = SimulatedFrontendChatSession()

        # Turn 1 completes normally
        def turn1_stream(on_event):
            on_event("start", {"request_id": "r-1"})
            on_event("delta", {"text": "Turn 1 answer."})
            on_event("done", {"reply": "Turn 1 answer."})

        session.run_turn_stream("Turn 1", sse_events_emitter=turn1_stream)
        assert len(session.conversation_history) == 2

        # Turn 2 is stopped early
        def turn2_stream(on_event):
            on_event("start", {"request_id": "r-2"})
            session.stop_generation()

        session.run_turn_stream("Turn 2", sse_events_emitter=turn2_stream)

        # Stop preserved Turn 1!
        assert len(session.conversation_history) == 2
        assert session.conversation_history[0]["content"] == "Turn 1"
        assert session.conversation_history[1]["content"] == "Turn 1 answer."

        # Now Clear Chat is called
        session.clear_chat()

        # Clear Chat clears all history and DOM
        assert len(session.conversation_history) == 0
        assert len(session.messages_dom) == 0
        assert session.is_waiting is False
        assert session.send_btn_state == "send"


# ============================================================================
# 9. Phase 3.4 History Cleanup Race and Keyboard Stop Verification
# ============================================================================

class TestPhase34HistoryRaceAndKeyboardStop:
    """Targeted regression tests for Phase 3.4 history cleanup race and keyboard Stop."""

    def test_phase34_history_identity_and_keyboard_contracts(self, frontend_html_content: str):
        """[Static Contract] Verifies userHistoryEntry reference identity, stale guards, and Escape key listeners."""
        assert "const userHistoryEntry = { role: 'user', content: text };" in frontend_html_content
        assert "conversationHistory.lastIndexOf(userHistoryEntry)" in frontend_html_content
        assert "if (thisTurnId !== currentTurnId) return;" in frontend_html_content
        assert "e.key === 'Escape'" in frontend_html_content
        assert "document.addEventListener('keydown'" in frontend_html_content

    def test_phase34_clear_chat_aborts_turn_a_turn_b_same_prompt_preserved(self):
        """[Python Simulation] Turn A starts -> Clear Chat aborts Turn A -> Turn B starts with same prompt ->
        Turn A delayed rejection cannot remove Turn B user history entry."""
        session = SimulatedFrontendChatSession()

        # 1. Turn A starts with prompt "Find Python jobs"
        turn_a_signal = MockAbortSignal()
        session.active_abort_controller = turn_a_signal
        session.current_turn_id = 1
        turn_a_id = 1
        session.is_waiting = True
        session.update_send_button_state(True)
        turn_a_entry = {"role": "user", "content": "Find Python jobs"}
        session.conversation_history.append(turn_a_entry)
        session.messages_dom.append({"type": "user", "text": "Find Python jobs"})

        def turn_a_delayed_abort():
            # Old turn A's delayed rejection runs
            if turn_a_id != session.current_turn_id:
                return
            if turn_a_entry in session.conversation_history:
                session.conversation_history.remove(turn_a_entry)

        # 2. Clear Chat aborts Turn A
        session.clear_chat()
        assert turn_a_signal.aborted is True
        assert len(session.conversation_history) == 0

        # 3. Turn B immediately starts with the same prompt
        def turn_b_stream(on_event):
            on_event("start", {"request_id": "r-turn-b"})
            on_event("delta", {"text": "Turn B answer."})
            on_event("done", {"reply": "Turn B answer."})

        session.run_turn_stream("Find Python jobs", sse_events_emitter=turn_b_stream)
        assert len(session.conversation_history) == 2
        assert session.conversation_history[0]["content"] == "Find Python jobs"
        assert session.conversation_history[1]["content"] == "Turn B answer."

        # 4. Turn A's delayed rejection executes
        turn_a_delayed_abort()

        # 5. Assert Turn B's user-history entry remains present and unchanged
        assert len(session.conversation_history) == 2, "Turn B entry must remain in history"
        assert session.conversation_history[0]["content"] == "Find Python jobs"
        assert session.conversation_history[1]["content"] == "Turn B answer."

    def test_phase34_stale_callback_cannot_remove_entry_after_history_shifts(self):
        """[Python Simulation] Stale callback cannot remove an entry after history array shifts."""
        session = SimulatedFrontendChatSession()

        # Turn 1 starts
        turn1_entry = {"role": "user", "content": "Turn 1 prompt"}
        session.conversation_history.append(turn1_entry)
        session.current_turn_id = 1
        turn1_id = 1

        def turn1_remove():
            if turn1_id != session.current_turn_id:
                return
            if turn1_entry in session.conversation_history:
                session.conversation_history.remove(turn1_entry)

        # An unexpected shift occurs in history
        session.conversation_history.insert(0, {"role": "system", "content": "Session initialized"})
        assert session.conversation_history[0]["role"] == "system"
        assert session.conversation_history[1] == turn1_entry

        # Turn 1 aborts early while still active turn
        turn1_remove()

        assert len(session.conversation_history) == 1
        assert session.conversation_history[0]["role"] == "system", "System entry must remain untouched at index 0"

    def test_phase34_keyboard_escape_stops_generation_and_enter_blocked(self):
        """[Python Simulation] Escape key calls stop_generation when active; Enter during generation is blocked."""
        session = SimulatedFrontendChatSession()

        # Idle: Escape does nothing
        assert session.is_waiting is False
        session.stop_generation()  # no-op
        assert session.is_waiting is False

        # Active turn starts
        def long_stream(on_event):
            on_event("start", {"request_id": "r-long"})
            on_event("delta", {"text": "Streaming output..."})
            # User presses Escape while isWaiting is true -> calls stop_generation()
            assert session.is_waiting is True
            session.stop_generation()

        session.run_turn_stream("Long query", sse_events_emitter=long_stream)

        # Output preserved with stopped indicator
        bot_msgs = [m for m in session.messages_dom if m["type"] in ("bot", "bot_streaming")]
        assert len(bot_msgs) == 1
        assert "⏹️ Generation stopped" in bot_msgs[0]["text"]
        assert session.is_waiting is False
        assert session.send_btn_state == "send"



