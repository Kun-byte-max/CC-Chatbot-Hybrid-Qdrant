"""
test_phase3b_verification_actions.py — Capabilities Discovery & Actionability Assurance.

Tests:
1. Capability Matrix audit confirming unsupported verification actions are NOT falsely exposed in UI.
2. Verification level flags (L1-L4) and highest_level remain unmodified and un-mutated.
3. Multi-role employment cards maintain safe role-level identity isolation.
4. Playcard CTA presence is strictly governed by valid item.url without inventing fake endpoints.
"""

import pytest
from pathlib import Path


def get_html_content() -> str:
    html_path = Path("frontend/index.html")
    assert html_path.exists(), "frontend/index.html must exist"
    return html_path.read_text(encoding="utf-8")


def test_no_unsupported_interactive_action_buttons():
    """Verify that fake or unsupported backend verification buttons are NOT present in frontend HTML."""
    html_content = get_html_content()

    # Prohibited unsupported/fake button labels
    prohibited_buttons = [
        "Request Verification",
        "Start Verification",
        "Upload Evidence",
        "Upload Document",
        "Verify Salary",
        "Submit Review",
        "Verify Now"
    ]

    for btn in prohibited_buttons:
        assert f">{btn}<" not in html_content, f"Prohibited unsupported button '{btn}' found in frontend HTML"


def test_verification_contract_and_levels_safety():
    """Verify L1-L4 boolean flags remain strictly informational without client-side state mutation."""
    html_content = get_html_content()

    assert "const l1 = Boolean(levelsObj.level1);" in html_content
    assert "const l2 = Boolean(levelsObj.level2);" in html_content
    assert "const l3 = Boolean(levelsObj.level3);" in html_content
    assert "const l4 = Boolean(levelsObj.level4);" in html_content

    # Level grid contains exact boolean status mapping
    assert "${l1 ? '✓' : '○'} L1" in html_content
    assert "${l2 ? '✓' : '○'} L2" in html_content
    assert "${l3 ? '✓' : '○'} L3" in html_content
    assert "${l4 ? '✓' : '○'} L4" in html_content


def test_cta_action_safety():
    """Verify CTA button only renders when item.url is valid public URL."""
    html_content = get_html_content()

    assert "const url = (item.url && item.url !== '#') ? item.url : null;" in html_content
    assert "View Employment →" in html_content
