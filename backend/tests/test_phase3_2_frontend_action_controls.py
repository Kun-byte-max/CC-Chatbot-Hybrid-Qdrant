"""
test_phase3_2_frontend_action_controls.py — Unit and integration tests for Phase 3.4 & Phase 3.6A employee action navigation controls.
"""

import re
import pytest
from pathlib import Path


@pytest.fixture
def frontend_html_content() -> str:
    html_path = Path(__file__).parent.parent.parent / "frontend" / "index.html"
    assert html_path.exists(), "frontend/index.html must exist"
    return html_path.read_text(encoding="utf-8")


class TestFrontendActionNavigationSecurityAndAllowlist:
    """Tests verifying frontend security allowlists and action nav rendering in index.html."""

    def test_allowed_navigation_urls_in_frontend(self, frontend_html_content: str):
        assert "https://www.collarcheck.com/dashboard/user/experience" in frontend_html_content
        assert "https://www.collarcheck.com/dashboard/user/skills" not in frontend_html_content

    def test_allowed_destinations_configuration_in_js(self, frontend_html_content: str):
        assert "ACTION_REGISTRY" in frontend_html_content
        assert "DESTINATION_MAP" in frontend_html_content
        assert "ALLOWED_NAV_URLS" in frontend_html_content
        assert "renderActionNavControls" in frontend_html_content

    def test_security_guards_in_frontend(self, frontend_html_content: str):
        assert "rel=\"noopener noreferrer\"" in frontend_html_content
        assert "target=\"_blank\"" in frontend_html_content
        assert "ALLOWED_NAV_URLS.has(" in frontend_html_content

    def test_action_button_labels_in_frontend(self, frontend_html_content: str):
        assert "Add Employment" in frontend_html_content
        assert "Verify Employment" in frontend_html_content
        assert "Complete Salary Verification" in frontend_html_content

    def test_existing_quick_chips_and_sendMessage_preserved(self, frontend_html_content: str):
        assert "function askChip(" in frontend_html_content
        assert "async function sendMessage(" in frontend_html_content
        assert "document.getElementById('sendBtn').disabled" in frontend_html_content


class TestPythonSimulatedFrontendActionNavRenderer:
    """Simulates the JavaScript renderActionNavControls logic in Python to test Phase 3.6A action metadata rendering."""

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

    def render_action_nav_controls(self, metadata) -> str:
        if not metadata or not isinstance(metadata, list):
            return ""

        out_html = ""
        seen_ids = set()

        for item in metadata:
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
                out_html += f'<div class="action-nav-container"><button type="button" onclick="askChip(\'{trusted_msg}\')" class="action-nav-btn"><span>{icon} {label}</span><span class="action-nav-arrow">→</span></button></div>'

            elif item_type == "nav_link":
                logical_dest = item.get("destination") or config.get("destination")
                target_url = self.DESTINATION_MAP.get(logical_dest)
                if not target_url or target_url not in self.ALLOWED_NAV_URLS or not target_url.startswith("https://"):
                    continue

                out_html += f'<div class="action-nav-container"><a href="{target_url}" target="_blank" rel="noopener noreferrer" class="action-nav-btn"><span>{icon} {label}</span><span class="action-nav-arrow">→</span></a></div>'

        return out_html

    def test_employment_add_response_renders_experience_nav(self):
        metadata = [{"id": "employment_add", "label": "Add Employment", "type": "nav_link", "destination": "experience_dashboard"}]
        out = self.render_action_nav_controls(metadata)
        assert "Add Employment" in out
        assert "https://www.collarcheck.com/dashboard/user/experience" in out
        assert "rel=\"noopener noreferrer\"" in out

    def test_employer_verification_response_renders_experience_nav(self):
        metadata = [{"id": "employer_verification", "label": "Verify Employment", "type": "nav_link", "destination": "experience_dashboard"}]
        out = self.render_action_nav_controls(metadata)
        assert "Verify Employment" in out
        assert "https://www.collarcheck.com/dashboard/user/experience" in out

    def test_salary_verification_response_renders_chat_action(self):
        metadata = [{"id": "salary_verification", "label": "Complete Salary Verification", "type": "chat_action", "message": "How do I complete Level 4 salary verification?"}]
        out = self.render_action_nav_controls(metadata)
        assert "Complete Salary Verification" in out
        assert "askChip('How do I complete Level 4 salary verification?')" in out

    def test_definition_response_does_not_render_nav(self):
        out = self.render_action_nav_controls(None)
        assert out == ""

    def test_status_response_does_not_render_nav(self):
        out = self.render_action_nav_controls([])
        assert out == ""

    def test_unallowlisted_url_rejection(self):
        bad_meta = [{"id": "employment_add", "type": "nav_link", "destination": "invalid_url"}]
        out = self.render_action_nav_controls(bad_meta)
        assert out == ""
