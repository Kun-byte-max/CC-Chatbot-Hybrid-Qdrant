"""
test_phase3_6a_action_metadata.py — Dedicated unit, integration, security and contract tests for Phase 3.6A Structured Action Metadata.
"""

import pytest
from pathlib import Path
from backend.services.user_data_intent_parser import UserIntent, parse_user_data_intents
from backend.services.user_data_service import handle_query, ServiceResponse
from backend.schemas.schemas import ChatResponse


@pytest.fixture
def frontend_html_content() -> str:
    html_path = Path(__file__).parent.parent.parent / "frontend" / "index.html"
    assert html_path.exists(), "frontend/index.html must exist"
    return html_path.read_text(encoding="utf-8")


class TestPhase36ABackendActionMetadataGeneration:
    """Backend tests for structured action metadata generation."""

    def test_add_employment_intent_generates_metadata(self):
        import asyncio
        res = asyncio.run(handle_query("How do I add my employment?", token=None))
        assert res.handled is True
        assert res.action_metadata is not None
        assert len(res.action_metadata) == 1
        meta = res.action_metadata[0]
        assert meta["id"] == "employment_add"
        assert meta["label"] == "Add Employment"
        assert meta["type"] == "nav_link"
        assert meta["destination"] == "experience_dashboard"

    def test_employer_verification_intent_generates_metadata(self):
        import asyncio
        res = asyncio.run(handle_query("How do I invite my employer for verification?", token=None))
        assert res.handled is True
        assert res.action_metadata is not None
        assert len(res.action_metadata) == 1
        meta = res.action_metadata[0]
        assert meta["id"] == "employer_verification"
        assert meta["label"] == "Verify Employment"
        assert meta["type"] == "nav_link"
        assert meta["destination"] == "experience_dashboard"

    def test_salary_verification_intent_generates_metadata(self):
        import asyncio
        res = asyncio.run(handle_query("How do I complete Level 4 salary verification?", token=None))
        assert res.handled is True
        assert res.action_metadata is not None
        assert len(res.action_metadata) == 1
        meta = res.action_metadata[0]
        assert meta["id"] == "salary_verification"
        assert meta["label"] == "Complete Salary Verification"
        assert meta["type"] == "chat_action"
        assert meta["message"] == "How do I complete Level 4 salary verification?"

    def test_ordinary_verification_query_has_no_action_metadata(self):
        import asyncio
        res = asyncio.run(handle_query("What is my employment verification status?", token=None))
        assert res.handled is True
        assert res.action_metadata is None or len(res.action_metadata) == 0

    def test_action_metadata_deduplication(self):
        import asyncio
        res = asyncio.run(handle_query("Add employment info and add new job record", token=None))
        assert res.handled is True
        if res.action_metadata:
            ids = [item["id"] for item in res.action_metadata]
            assert len(ids) == len(set(ids))

    def test_chat_response_schema_compatibility(self):
        resp = ChatResponse(
            reply="Test reply",
            success=True,
            action_metadata=[
                {
                    "id": "employment_add",
                    "label": "Add Employment",
                    "type": "nav_link",
                    "destination": "experience_dashboard"
                }
            ]
        )
        data = resp.model_dump()
        assert "action_metadata" in data
        assert data["action_metadata"][0]["id"] == "employment_add"


class TestPhase36AFrontendMetadataConsumption:
    """Frontend static checks and Javascript simulation tests for metadata consumption."""

    def test_frontend_does_not_use_markdown_text_matching(self, frontend_html_content: str):
        assert "replyText.includes('Add Employment Information')" not in frontend_html_content
        assert "replyText.includes('Employer Verification')" not in frontend_html_content
        assert "replyText.includes('Salary Verification')" not in frontend_html_content

    def test_frontend_uses_structured_action_metadata(self, frontend_html_content: str):
        assert "renderActionNavControls(data.action_metadata)" in frontend_html_content
        assert "DESTINATION_MAP" in frontend_html_content
        assert "ACTION_REGISTRY" in frontend_html_content
        assert "experience_dashboard" in frontend_html_content

    def test_security_allowlist_and_chat_action_registry(self, frontend_html_content: str):
        assert "https://www.collarcheck.com/dashboard/user/experience" in frontend_html_content
        assert "askChip(" in frontend_html_content
        assert "target=\"_blank\"" in frontend_html_content
        assert "rel=\"noopener noreferrer\"" in frontend_html_content


class TestPythonSimulatedFrontendMetadataRenderer:
    """Simulates the new JS renderActionNavControls(metadata) in Python to test metadata consumption cases."""

    DESTINATION_MAP = {
        "experience_dashboard": "https://www.collarcheck.com/dashboard/user/experience"
    }

    ALLOWED_NAV_URLS = {
        "https://www.collarcheck.com/dashboard/user/experience"
    }

    ACTION_REGISTRY = {
        "employment_add": {
            "type": "nav_link",
            "destination": "experience_dashboard",
            "label": "Add Employment",
            "icon": "💼"
        },
        "employer_verification": {
            "type": "nav_link",
            "destination": "experience_dashboard",
            "label": "Verify Employment",
            "icon": "🛡️"
        },
        "salary_verification": {
            "type": "chat_action",
            "message": "How do I complete Level 4 salary verification?",
            "label": "Complete Salary Verification",
            "icon": "💰"
        }
    }

    def render_action_nav_controls(self, action_metadata) -> str:
        if not action_metadata or not isinstance(action_metadata, list):
            return ""

        out_html = ""
        seen_ids = set()

        for item in action_metadata:
            if not isinstance(item, dict):
                continue
            action_id = item.get("id")
            if not action_id or action_id in seen_ids:
                continue

            config = self.ACTION_REGISTRY.get(action_id)
            if not config:
                continue

            item_type = item.get("type") or config.get("type")
            if item_type != config.get("type"):
                continue

            seen_ids.add(action_id)

            label = item.get("label") or config.get("label", "")
            icon = config.get("icon", "🔗")

            if item_type == "chat_action":
                trusted_msg = config.get("message", "")
                return f'<div class="action-nav-container"><button type="button" onclick="askChip(\'{trusted_msg}\')" class="action-nav-btn"><span>{icon} {label}</span><span class="action-nav-arrow">→</span></button></div>'

            elif item_type == "nav_link":
                logical_dest = item.get("destination") or config.get("destination")
                target_url = self.DESTINATION_MAP.get(logical_dest)
                if not target_url or target_url not in self.ALLOWED_NAV_URLS or not target_url.startswith("https://"):
                    continue

                out_html += f'<div class="action-nav-container"><a href="{target_url}" target="_blank" rel="noopener noreferrer" class="action-nav-btn"><span>{icon} {label}</span><span class="action-nav-arrow">→</span></a></div>'

        return out_html

    def test_rendering_employment_add_metadata(self):
        metadata = [{
            "id": "employment_add",
            "label": "Add Employment",
            "type": "nav_link",
            "destination": "experience_dashboard"
        }]
        out = self.render_action_nav_controls(metadata)
        assert "Add Employment" in out
        assert "https://www.collarcheck.com/dashboard/user/experience" in out
        assert "rel=\"noopener noreferrer\"" in out

    def test_rendering_employer_verification_metadata(self):
        metadata = [{
            "id": "employer_verification",
            "label": "Verify Employment",
            "type": "nav_link",
            "destination": "experience_dashboard"
        }]
        out = self.render_action_nav_controls(metadata)
        assert "Verify Employment" in out
        assert "https://www.collarcheck.com/dashboard/user/experience" in out

    def test_rendering_salary_verification_metadata(self):
        metadata = [{
            "id": "salary_verification",
            "label": "Complete Salary Verification",
            "type": "chat_action",
            "message": "How do I complete Level 4 salary verification?"
        }]
        out = self.render_action_nav_controls(metadata)
        assert "Complete Salary Verification" in out
        assert "askChip('How do I complete Level 4 salary verification?')" in out

    def test_null_or_empty_metadata_produces_empty_string(self):
        assert self.render_action_nav_controls(None) == ""
        assert self.render_action_nav_controls([]) == ""
        assert self.render_action_nav_controls("invalid_type") == ""

    def test_unknown_action_id_or_type_ignored(self):
        metadata = [
            {"id": "unknown_action", "type": "nav_link"},
            {"id": "employment_add", "type": "unknown_type"}
        ]
        out = self.render_action_nav_controls(metadata)
        assert out == ""

    def test_unallowlisted_destination_rejected(self):
        metadata = [{
            "id": "employment_add",
            "label": "Add Employment",
            "type": "nav_link",
            "destination": "malicious_dashboard"
        }]
        out = self.render_action_nav_controls(metadata)
        assert out == ""
