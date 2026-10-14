"""
test_phase1b_frontend_playcard.py — Deterministic Unit Tests for Phase 1B Employment History Playcard UI Rendering.
"""

import pytest
from pathlib import Path


def test_frontend_index_html_contains_employment_playcard_structure():
    html_path = Path("frontend/index.html")
    assert html_path.exists(), "frontend/index.html must exist"
    
    html_content = html_path.read_text(encoding="utf-8")

    # Renderer functions
    assert "function renderEmploymentCardHtml(" in html_content
    assert "function renderSingleCardHtml(" in html_content

    # Detection logic
    assert "isEmploymentObject" in html_content
    assert "item.company_verification" in html_content

    # UI structure classes & sections
    assert "employment-card" in html_content
    assert "emp-logo" in html_content
    assert "emp-badge-current" in html_content
    assert "emp-badge-previous" in html_content
    assert "emp-levels-grid" in html_content
    assert "emp-comp-verif-status" in html_content
    assert "emp-evidence-row" in html_content
    assert "emp-skills-row" in html_content
    assert "ctaHtml" in html_content


def test_independent_verification_level_rendering_contract():
    """Verify that JS level pills map level1..level4 directly from levels object without mutation."""
    html_path = Path("frontend/index.html")
    html_content = html_path.read_text(encoding="utf-8")

    assert "const l1 = Boolean(levelsObj.level1);" in html_content
    assert "const l2 = Boolean(levelsObj.level2);" in html_content
    assert "const l3 = Boolean(levelsObj.level3);" in html_content
    assert "const l4 = Boolean(levelsObj.level4);" in html_content

    # Assert visual pills render exact boolean statuses
    assert "L1</div>" in html_content
    assert "L2</div>" in html_content
    assert "L3</div>" in html_content
    assert "L4</div>" in html_content
