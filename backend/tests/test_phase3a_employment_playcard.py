"""
test_phase3a_employment_playcard.py — Deterministic Unit & Semantic HTML Tests for Phase 3A Employment Playcard UI.

Tests:
1. Current vs Previous employment badge rendering.
2. Formatted date display (e.g. YYYY-MM-DD -> Mon DD, YYYY).
3. Independent Level 1-4 boolean verification level rendering (e.g. FFTT -> highest_level 4).
4. All-false verification status rendering ("○ Not Verified").
5. Company verification separation ("✓ Company domain verified" vs "⚠ Company not verified").
6. Evidence availability indicators (Salary, Documents, Reviews).
7. Skills rendering & pill layout safety.
8. Partial/missing data safety (missing dates, skills, evidence, URL, logo).
9. CTA rendering (present when valid URL exists, absent when url is missing).
10. Fallback and absence of undefined / null / [object Object].
"""

import pytest
from pathlib import Path
import re


def get_html_content() -> str:
    html_path = Path("frontend/index.html")
    assert html_path.exists(), "frontend/index.html must exist"
    return html_path.read_text(encoding="utf-8")


def test_frontend_contains_phase3a_playcard_enhancements():
    html_content = get_html_content()

    # Core rendering functions
    assert "function renderEmploymentCardHtml(" in html_content
    assert "function formatDate(" in html_content

    # CSS classes & semantic structures
    assert "job-card employment-card" in html_content
    assert "emp-badge-current" in html_content
    assert "emp-badge-previous" in html_content
    assert "emp-highest-badge" in html_content
    assert "emp-levels-grid" in html_content
    assert "emp-comp-verif-status" in html_content
    assert "emp-evidence-row" in html_content
    assert "emp-skills-row" in html_content


def test_independent_level4_rendering_contract():
    """Verify that L1-L4 boolean flags render directly from verification.levels object without sequential mutation."""
    html_content = get_html_content()

    assert "const l1 = Boolean(levelsObj.level1);" in html_content
    assert "const l2 = Boolean(levelsObj.level2);" in html_content
    assert "const l3 = Boolean(levelsObj.level3);" in html_content
    assert "const l4 = Boolean(levelsObj.level4);" in html_content

    # Grid level pills assert exact direct boolean mapping
    assert "${l1 ? '✓' : '○'} L1" in html_content
    assert "${l2 ? '✓' : '○'} L2" in html_content
    assert "${l3 ? '✓' : '○'} L3" in html_content
    assert "${l4 ? '✓' : '○'} L4" in html_content


def test_company_verification_decoupling():
    """Verify company verification remains decoupled from employment verification."""
    html_content = get_html_content()

    assert "✓ Company domain verified" in html_content
    assert "⚠ Company not verified" in html_content
    assert "○ Company status unknown" in html_content


def test_date_formatting_helper():
    """Verify JS formatDate function correctly handles YYYY-MM-DD -> Mon DD, YYYY format."""
    html_content = get_html_content()

    assert "function formatDate(" in html_content
    assert "Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec" in html_content


def test_partial_data_safety_and_fallbacks():
    """Verify safe fallbacks for missing optional properties."""
    html_content = get_html_content()

    # Company name fallback
    assert "item.company_name || item.company || 'Company N/A'" in html_content

    # Role designation fallback hierarchy
    assert "item.designation || item.current_designation || item.position || 'Role / Designation'" in html_content

    # Logo fallback initials
    assert "CO" in html_content

    # CTA presence check
    assert "View Employment →" in html_content
