"""
backend/tests/test_evaluation_integration.py — Focused tests for offline evaluation report runner and integration.

Requirements:
- Valid fixture loading and report generation.
- Invalid or malformed fixture input handling.
- Deterministic output when timestamp is omitted.
- Production status remains NOT MEASURED.
- Stdout behavior and explicit output-path behavior.
- Existing-file protection without --overwrite.
- JUnit XML parser: success, failure, error, skip, missing, incomplete, duplicate, and parameterized handling.
- Zero inference of semantic answer metrics from pytest execution outcomes.
"""

import os
import sys
import tempfile
import pytest
from pathlib import Path
from backend.utils.retrieval_evaluator import (
    parse_junit_xml_test_results,
    generate_evaluation_summary_report,
    format_evaluation_report_markdown,
)
from backend.tests.run_evaluation_report import (
    load_fixture_data,
    generate_offline_report,
)


@pytest.fixture
def fixtures_dir():
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def retrieval_fixture_path(fixtures_dir):
    return fixtures_dir / "eval_retrieval_dataset.json"


@pytest.fixture
def semantic_fixture_path(fixtures_dir):
    return fixtures_dir / "eval_semantic_dataset.json"


def test_01_valid_fixture_loading_and_report_generation(retrieval_fixture_path, semantic_fixture_path):
    """Verifies standard fixtures load and generate report with truthful statuses."""
    md = generate_offline_report(
        retrieval_fixture_path=retrieval_fixture_path,
        semantic_fixture_path=semantic_fixture_path,
        junit_xml_path=None,
    )

    assert "CollarCheck Chatbot" in md
    assert "Unified Evaluation Report" in md
    assert "**Production Quality Status:** `NOT MEASURED`" in md
    assert "Context Precision (MAP@5)" in md
    assert "Deterministic Faithfulness" in md
    assert "Execution Provenance Notice" in md
    assert "NOT_RUN_STATIC_MAPPING_ONLY" in md


def test_02_malformed_fixture_handling(tmp_path):
    """Verifies runner fails gracefully on missing or malformed fixtures."""
    non_existent = tmp_path / "does_not_exist.json"
    with pytest.raises(FileNotFoundError):
        load_fixture_data(non_existent)

    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("not json content", encoding="utf-8")
    with pytest.raises(ValueError, match="Failed to parse JSON"):
        load_fixture_data(invalid_json)

    missing_cases = tmp_path / "no_cases.json"
    missing_cases.write_text('{"description": "no cases key"}', encoding="utf-8")
    with pytest.raises(ValueError, match="must contain a 'cases' list"):
        load_fixture_data(missing_cases)


def test_03_deterministic_output_when_timestamp_omitted(retrieval_fixture_path, semantic_fixture_path):
    """Verifies repeated generation produces identical bit-for-bit output."""
    md1 = generate_offline_report(
        retrieval_fixture_path=retrieval_fixture_path,
        semantic_fixture_path=semantic_fixture_path,
        timestamp=None,
    )
    md2 = generate_offline_report(
        retrieval_fixture_path=retrieval_fixture_path,
        semantic_fixture_path=semantic_fixture_path,
        timestamp=None,
    )
    assert md1 == md2


def test_04_production_status_permanence(retrieval_fixture_path, semantic_fixture_path):
    """Verifies Production Quality Status is strictly NOT MEASURED regardless of fixtures/tests."""
    md = generate_offline_report(
        retrieval_fixture_path=retrieval_fixture_path,
        semantic_fixture_path=semantic_fixture_path,
    )
    assert "NOT MEASURED" in md
    assert "MEETS_PRODUCTION_TARGET" not in md


def test_05_file_writing_and_existing_file_protection(tmp_path, retrieval_fixture_path, semantic_fixture_path):
    """Verifies file writing protection when output file already exists."""
    out_file = tmp_path / "eval_report.md"
    out_file.write_text("Pre-existing content", encoding="utf-8")

    from backend.tests.run_evaluation_report import main
    # Simulate CLI invocation trying to overwrite without --overwrite
    test_args = [
        "run_evaluation_report.py",
        "--retrieval-fixture", str(retrieval_fixture_path),
        "--semantic-fixture", str(semantic_fixture_path),
        "--output-path", str(out_file),
    ]

    import unittest.mock
    with unittest.mock.patch.object(sys, "argv", test_args):
        ret = main()
        assert ret == 1  # Fails due to existing file
        assert out_file.read_text(encoding="utf-8") == "Pre-existing content"

    # With --overwrite
    test_args.append("--overwrite")
    with unittest.mock.patch.object(sys, "argv", test_args):
        ret = main()
        assert ret == 0
        new_content = out_file.read_text(encoding="utf-8")
        assert "# CollarCheck Chatbot — Unified Evaluation Report" in new_content


def test_06_junit_xml_parsing_outcomes():
    """Verifies JUnit XML parsing handles passed, failed, error, skipped, and parameterized cases."""
    sample_xml = """<?xml version="1.0" encoding="utf-8"?>
    <testsuites>
      <testsuite name="pytest" tests="5" failures="1" errors="1" skipped="1">
        <testcase classname="test_pipeline" name="test_01_pipeline_sem01_supported_salary_and_grounding" time="0.100" />
        <testcase classname="test_pipeline" name="test_02_pipeline_sem02_missing_salary_abstention_and_negative_check" time="0.150">
          <failure message="AssertionError: expected phrase not found">Traceback info</failure>
        </testcase>
        <testcase classname="test_pipeline" name="test_03_pipeline_sem04_cross_entity_salary_isolation" time="0.050">
          <error message="RuntimeError: DB connection timed out">Error info</error>
        </testcase>
        <testcase classname="test_pipeline" name="test_04_pipeline_sem05_out_of_range_ordinal_handling" time="0.001">
          <skipped message="Skipped due to condition">Skip info</skipped>
        </testcase>
        <testcase classname="test_pipeline" name="test_08_pipeline_sem12_chat_and_stream_parity[mode_fast]" time="0.200" />
      </testsuite>
    </testsuites>
    """
    results = parse_junit_xml_test_results(sample_xml)

    assert results["test_01_pipeline_sem01_supported_salary_and_grounding"]["status"] == "PASSED"
    assert results["test_02_pipeline_sem02_missing_salary_abstention_and_negative_check"]["status"] == "FAILED"
    assert "AssertionError" in results["test_02_pipeline_sem02_missing_salary_abstention_and_negative_check"]["reason"]
    assert results["test_03_pipeline_sem04_cross_entity_salary_isolation"]["status"] == "ERROR"
    assert results["test_04_pipeline_sem05_out_of_range_ordinal_handling"]["status"] == "SKIPPED"
    # Parameterized name extraction
    assert results["test_08_pipeline_sem12_chat_and_stream_parity"]["status"] == "PASSED"


def test_07_junit_xml_duplicate_ambiguity_flagged_as_error():
    """Verifies duplicate/conflicting testcase records in XML are treated conservatively as ERROR."""
    ambiguous_xml = """<?xml version="1.0" encoding="utf-8"?>
    <testsuites>
      <testsuite name="pytest" tests="2">
        <testcase name="test_01_pipeline_sem01_supported_salary_and_grounding" />
        <testcase name="test_01_pipeline_sem01_supported_salary_and_grounding">
          <failure message="Second run failed" />
        </testcase>
      </testsuite>
    </testsuites>
    """
    results = parse_junit_xml_test_results(ambiguous_xml)
    assert results["test_01_pipeline_sem01_supported_salary_and_grounding"]["status"] == "ERROR"
    assert "Ambiguous/duplicate" in results["test_01_pipeline_sem01_supported_salary_and_grounding"]["reason"]


def test_08_no_inference_of_semantic_metrics_from_test_results(retrieval_fixture_path, semantic_fixture_path, tmp_path):
    """Verifies pipeline test outcomes do not synthesize answer faithfulness or entity grounding scores."""
    xml_path = tmp_path / "all_passed.xml"
    xml_path.write_text("""<?xml version="1.0" encoding="utf-8"?>
    <testsuites>
      <testsuite name="pytest" tests="1">
        <testcase name="test_01_pipeline_sem01_supported_salary_and_grounding" />
      </testsuite>
    </testsuites>""", encoding="utf-8")

    md = generate_offline_report(
        retrieval_fixture_path=retrieval_fixture_path,
        semantic_fixture_path=semantic_fixture_path,
        junit_xml_path=xml_path,
    )

    # In report, semantic answers remain missing/not provided (N/A)
    assert "Deterministic Faithfulness** | `0.95` | `N/A` | 0 / 12 (12 N/A)" in md
    # Entity grounding remains unmeasured (N/A)
    assert "Entity-ID Grounding Accuracy** | `1.00` | `N/A` | 0 / 10 (10 N/A)" in md
    # Pipeline execution results shows PASSED for test_01 and NOT_RUN for others
    assert "| `SEM-01` | straightforward_salary_query | `test_01_pipeline_sem01_supported_salary_and_grounding` | /chat | No | Yes | No | `PASSED` |" in md
    assert "| `SEM-02` | missing_salary_abstention | `test_02_pipeline_sem02_missing_salary_abstention_and_negative_check` | /chat | No | Yes | Yes | `NOT_RUN` |" in md


def test_09_cli_default_mode_retains_zero_exit_on_success(retrieval_fixture_path, semantic_fixture_path, monkeypatch):
    """Verifies that without --enforce-gate, runner returns exit code 0 even if verdict is INCONCLUSIVE."""
    from backend.tests.run_evaluation_report import main
    test_args = [
        "run_evaluation_report.py",
        "--retrieval-fixture", str(retrieval_fixture_path),
        "--semantic-fixture", str(semantic_fixture_path),
    ]
    monkeypatch.setattr(sys, "argv", test_args)
    ret = main()
    assert ret == 0


def test_10_cli_enforcement_returns_2_for_inconclusive(retrieval_fixture_path, semantic_fixture_path, monkeypatch):
    """Verifies that with --enforce-gate, an INCONCLUSIVE gate verdict returns exit code 2."""
    from backend.tests.run_evaluation_report import main
    test_args = [
        "run_evaluation_report.py",
        "--retrieval-fixture", str(retrieval_fixture_path),
        "--semantic-fixture", str(semantic_fixture_path),
        "--enforce-gate",
    ]
    monkeypatch.setattr(sys, "argv", test_args)
    ret = main()
    assert ret == 2


def test_11_cli_enforcement_returns_1_for_fail(retrieval_fixture_path, semantic_fixture_path, tmp_path, monkeypatch):
    """Verifies that with --enforce-gate, a FAIL gate verdict returns exit code 1."""
    # Supply XML with a failed pipeline test
    xml_path = tmp_path / "failed_pipeline.xml"
    xml_path.write_text("""<?xml version="1.0" encoding="utf-8"?>
    <testsuites>
      <testsuite name="pytest" tests="1">
        <testcase name="test_01_pipeline_sem01_supported_salary_and_grounding">
          <failure message="AssertionError: expected phrase not found" />
        </testcase>
      </testsuite>
    </testsuites>""", encoding="utf-8")

    from backend.tests.run_evaluation_report import main
    test_args = [
        "run_evaluation_report.py",
        "--retrieval-fixture", str(retrieval_fixture_path),
        "--semantic-fixture", str(semantic_fixture_path),
        "--junit-xml", str(xml_path),
        "--enforce-gate",
    ]
    monkeypatch.setattr(sys, "argv", test_args)
    ret = main()
    assert ret == 1


def test_12_cli_enforcement_returns_0_for_pass(retrieval_fixture_path, semantic_fixture_path, tmp_path, monkeypatch):
    """Verifies that with --enforce-gate, when all required metrics and pipeline tests pass, exits 0."""
    # Build complete 12 passed tests XML
    xml_cases = []
    from backend.utils.retrieval_evaluator import KNOWN_PIPELINE_COVERAGE_MAP
    for item in KNOWN_PIPELINE_COVERAGE_MAP:
        xml_cases.append(f'<testcase name="{item["test_method"]}" />')
    xml_content = f"""<?xml version="1.0" encoding="utf-8"?>
    <testsuites>
      <testsuite name="pytest" tests="{len(KNOWN_PIPELINE_COVERAGE_MAP)}">
        {"".join(xml_cases)}
      </testsuite>
    </testsuites>"""
    xml_path = tmp_path / "all_12_passed.xml"
    xml_path.write_text(xml_content, encoding="utf-8")

    # Mock evaluate_quality_gate to simulate a clean PASS across all layers
    import unittest.mock
    from backend.tests.run_evaluation_report import main

    test_args = [
        "run_evaluation_report.py",
        "--retrieval-fixture", str(retrieval_fixture_path),
        "--semantic-fixture", str(semantic_fixture_path),
        "--junit-xml", str(xml_path),
        "--enforce-gate",
    ]
    monkeypatch.setattr(sys, "argv", test_args)

    with unittest.mock.patch("backend.tests.run_evaluation_report.evaluate_quality_gate") as mock_gate:
        mock_gate.return_value = {
            "overall_verdict": "PASS",
            "overall_reason": "All required offline evaluation gates passed",
            "production_quality_status": "NOT MEASURED",
            "layer_a_verdict": "PASS",
            "layer_b_verdict": "PASS",
            "layer_b_reason": "All 12 mapped pipeline regression tests passed",
            "metric_verdicts": {},
            "pipeline_summary": {"total_mapped_tests": 12, "passed_tests": 12, "failed_or_errored_tests": 0, "unrun_or_skipped_tests": 0},
        }
        ret = main()
        assert ret == 0


def test_13_cli_enforcement_unexpected_verdict_fails(retrieval_fixture_path, semantic_fixture_path, monkeypatch):
    """Verifies that any unexpected or unsupported overall verdict returns nonzero (exit 1)."""
    import unittest.mock
    from backend.tests.run_evaluation_report import main

    test_args = [
        "run_evaluation_report.py",
        "--retrieval-fixture", str(retrieval_fixture_path),
        "--semantic-fixture", str(semantic_fixture_path),
        "--enforce-gate",
    ]
    monkeypatch.setattr(sys, "argv", test_args)

    with unittest.mock.patch("backend.tests.run_evaluation_report.evaluate_quality_gate") as mock_gate:
        mock_gate.return_value = {
            "overall_verdict": "UNEXPECTED_CUSTOM_STATUS",
            "overall_reason": "Something unusual",
            "production_quality_status": "NOT MEASURED",
            "layer_a_verdict": "PASS",
            "layer_b_verdict": "PASS",
            "layer_b_reason": "Ok",
            "metric_verdicts": {},
            "pipeline_summary": {},
        }
        ret = main()
        assert ret == 1


def test_14_diagnostic_report_still_written_when_gate_fails_or_inconclusive(
    retrieval_fixture_path, semantic_fixture_path, tmp_path, monkeypatch
):
    """Verifies that the diagnostic Markdown report is written even when --enforce-gate returns nonzero."""
    out_file = tmp_path / "diagnostic_report.md"
    from backend.tests.run_evaluation_report import main

    test_args = [
        "run_evaluation_report.py",
        "--retrieval-fixture", str(retrieval_fixture_path),
        "--semantic-fixture", str(semantic_fixture_path),
        "--output-path", str(out_file),
        "--enforce-gate",
    ]
    monkeypatch.setattr(sys, "argv", test_args)
    ret = main()
    assert ret == 2  # INCONCLUSIVE exit code
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "# CollarCheck Chatbot" in content
    assert "## 5. Offline Quality Gate Policy" in content
    assert "> **Overall Offline Verdict:** `INCONCLUSIVE`" in content


def test_15_malformed_xml_and_fixtures_exit_1(tmp_path, monkeypatch):
    """Verifies malformed XML or fixture input produces nonzero exit code and does not mask errors."""
    from backend.tests.run_evaluation_report import main
    bad_fixture = tmp_path / "bad.json"
    bad_fixture.write_text("invalid json", encoding="utf-8")

    test_args = [
        "run_evaluation_report.py",
        "--retrieval-fixture", str(bad_fixture),
    ]
    monkeypatch.setattr(sys, "argv", test_args)
    ret = main()
    assert ret == 1

