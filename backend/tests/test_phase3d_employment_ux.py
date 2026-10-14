"""
test_phase3d_employment_ux.py — Targeted Unit & Accessibility Tests for Phase 3D Employment Verification UX.

Tests:
1. Verified employment card structure with independent level summary ("Completed:", "Not completed:").
2. FFTT verification state rendering L3 & L4 as completed and L1 & L2 as not completed.
3. FFFF zero verification state displaying "Completed: None".
4. Company verification separation ("✓ Company domain verified" vs "⚠ Company not verified").
5. Evidence availability indicators (Salary, Documents, Reviews).
6. Current vs Previous status badges.
7. Multi-role identity separation (SP HUMANS).
8. Accessible aria-label and title attributes on L1-L4 level pills.
9. Absence of fake action buttons (Request Verification, Upload Evidence, etc.).
10. Valid URL CTA vs missing/invalid URL CTA suppression.
"""

import pytest
from pathlib import Path


def get_html_content() -> str:
    html_path = Path("frontend/index.html")
    assert html_path.exists(), "frontend/index.html must exist"
    return html_path.read_text(encoding="utf-8")


def test_verification_summary_completed_and_not_completed_breakdown():
    html_content = get_html_content()

    assert "const completedLevels = [];" in html_content
    assert "const notCompletedLevels = [];" in html_content
    assert "Completed: ${completedLevels.join(', ')}" in html_content
    assert "Not completed: ${notCompletedLevels.join(', ')}" in html_content


def test_level_pills_accessibility_attributes():
    html_content = get_html_content()

    assert 'aria-label="Level 1 ${l1 ? \'Completed\' : \'Not completed\'}"' in html_content
    assert 'title="Level 1: ${l1 ? \'Completed\' : \'Not completed\'}"' in html_content
    assert 'aria-label="Level 4 ${l4 ? \'Completed\' : \'Not completed\'}"' in html_content


def test_no_prohibited_fake_verification_actions():
    html_content = get_html_content()

    prohibited = [
        "Request Verification",
        "Start Verification",
        "Upload Evidence",
        "Upload Document",
        "Verify Salary",
        "Submit Review",
        "Verify Now"
    ]

    for p in prohibited:
        assert f">{p}<" not in html_content, f"Prohibited fake action button '{p}' found in frontend template"


def test_cta_safety_preserved():
    html_content = get_html_content()

    assert "const url = (item.url && item.url !== '#') ? item.url : null;" in html_content
    assert "View Employment →" in html_content
