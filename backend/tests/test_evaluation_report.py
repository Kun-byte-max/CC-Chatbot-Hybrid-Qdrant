"""
backend/tests/test_evaluation_report.py — Focused tests for unified evaluation report.

Stage 4.2.2 requirements:
1. Empty inputs and graceful N/A reporting.
2. Retrieval aggregation calculated from the real fixture.
3. Missing answers versus explicitly empty answers.
4. Supplied answers and independent metric eligibility.
5. Actual grounding accuracy remaining unmeasured when actual IDs are absent.
6. Supplied pipeline pass/fail/error results and unknown test names.
7. Static coverage mapping without execution results.
8. Markdown formatting and N/A display.
9. Count invariants and unimplemented metric status.
10. Deterministic report and Markdown output.

Zero live network calls, zero external dependencies, mock/local fixture only.
"""

import os
import json
import pytest
from backend.utils.retrieval_evaluator import (
    generate_evaluation_summary_report,
    format_evaluation_report_markdown,
    KNOWN_PIPELINE_COVERAGE_MAP,
)


@pytest.fixture
def fixtures_dir():
    return os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def retrieval_fixture_data(fixtures_dir):
    p = os.path.join(fixtures_dir, "eval_retrieval_dataset.json")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def semantic_fixture_data(fixtures_dir):
    p = os.path.join(fixtures_dir, "eval_semantic_dataset.json")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def test_01_empty_inputs_graceful_handling():
    """1. Empty inputs and graceful N/A reporting."""
    report = generate_evaluation_summary_report(
        retrieval_fixture=None,
        semantic_fixture=None,
        case_answers=None,
        actual_selected_ids=None,
        test_execution_results=None,
        timestamp=None,
    )

    assert report["report_schema_version"] == "1.0"
    assert report["timestamp"] is None
    assert report["production_quality_status"] == "NOT MEASURED"

    layers = report["layers"]
    ret_layer = layers["layer_a_retrieval_benchmarks"]
    assert ret_layer["context_precision"]["mean_score"] is None
    assert ret_layer["context_precision"]["eligible_cases"] == 0
    assert ret_layer["context_precision"]["total_cases"] == 0
    assert ret_layer["context_precision"]["status"] == "N/A_NO_ELIGIBLE_CASES"

    assert ret_layer["context_recall"]["mean_score"] is None
    assert ret_layer["context_recall"]["eligible_cases"] == 0
    assert ret_layer["context_recall"]["total_cases"] == 0
    assert ret_layer["context_recall"]["status"] == "N/A_NO_ELIGIBLE_CASES"

    assert ret_layer["entity_id_grounding"]["mean_score"] is None
    assert ret_layer["entity_id_grounding"]["status"] == "NOT_MEASURED_MISSING_ACTUAL_IDS"

    sem_layer = layers["layer_a_semantic_benchmarks"]
    assert sem_layer["deterministic_faithfulness"]["mean_score"] is None
    assert sem_layer["deterministic_faithfulness"]["status"] == "N/A_NO_ELIGIBLE_CASES"
    assert sem_layer["abstention_quality"]["mean_score"] is None
    assert sem_layer["abstention_quality"]["status"] == "N/A_NO_ELIGIBLE_CASES"
    assert sem_layer["response_relevance"]["mean_score"] is None
    assert sem_layer["response_relevance"]["status"] == "N/A_NO_ELIGIBLE_CASES"

    md = format_evaluation_report_markdown(report)
    assert "# CollarCheck Chatbot — Unified Evaluation Report" in md
    assert "NOT MEASURED" in md


def test_02_synthetic_retrieval_aggregation(retrieval_fixture_data):
    """2. Retrieval aggregation calculated from the real fixture."""
    report = generate_evaluation_summary_report(
        retrieval_fixture=retrieval_fixture_data,
        semantic_fixture=None,
    )

    ret_layer = report["layers"]["layer_a_retrieval_benchmarks"]
    prec = ret_layer["context_precision"]
    rec = ret_layer["context_recall"]

    # In eval_retrieval_dataset.json there are 10 cases:
    # CASE-08 and CASE-09 have empty expected_relevant_ids -> AP is None (eligible=8, na=2)
    assert prec["total_cases"] == 10
    assert prec["eligible_cases"] == 8
    assert prec["na_cases"] == 2
    assert prec["eligible_cases"] + prec["na_cases"] == prec["total_cases"]
    assert prec["mean_score"] is not None
    assert 0.0 <= prec["mean_score"] <= 1.0
    assert prec["status"] in {"MEETS_SYNTHETIC_TARGET", "BELOW_TARGET"}

    assert rec["total_cases"] == 10
    assert rec["eligible_cases"] == 8
    assert rec["na_cases"] == 2
    assert rec["eligible_cases"] + rec["na_cases"] == rec["total_cases"]
    assert rec["mean_score"] is not None
    assert 0.0 <= rec["mean_score"] <= 1.0


def test_03_missing_answers_versus_explicitly_empty_answers(semantic_fixture_data):
    """3. Missing answers versus explicitly empty answers."""
    # Case A: answers completely omitted -> missing answer payload, score=None
    report_missing = generate_evaluation_summary_report(
        semantic_fixture=semantic_fixture_data,
        case_answers=None,
    )
    faith_missing = report_missing["layers"]["layer_a_semantic_benchmarks"]["deterministic_faithfulness"]
    assert faith_missing["eligible_cases"] == 0
    assert faith_missing["na_cases"] == len(semantic_fixture_data["cases"])
    assert faith_missing["mean_score"] is None
    assert faith_missing["evaluation_scope"] == "GENERATED_ANSWERS_NOT_PROVIDED"
    for b in faith_missing["per_case_breakdown"]:
        assert b["status"] == "N/A_NO_ANSWER_SUPPLIED"
        assert b["score"] is None

    # Case B: explicitly empty string supplied for SEM-01
    report_empty = generate_evaluation_summary_report(
        semantic_fixture=semantic_fixture_data,
        case_answers={"SEM-01": ""},
    )
    faith_empty = report_empty["layers"]["layer_a_semantic_benchmarks"]["deterministic_faithfulness"]
    # SEM-01 with empty string: evaluate_deterministic_faithfulness with empty answer and facts -> score 0.0, evaluated
    sem01_entry = next(b for b in faith_empty["per_case_breakdown"] if b["case_id"] == "SEM-01")
    assert sem01_entry["status"] == "EVALUATED"
    assert sem01_entry["score"] == 0.0

    # Other cases in report_empty should still be missing
    sem02_entry = next(b for b in faith_empty["per_case_breakdown"] if b["case_id"] == "SEM-02")
    assert sem02_entry["status"] == "N/A_NO_ANSWER_SUPPLIED"
    assert sem02_entry["score"] is None


def test_04_supplied_answers_and_independent_metric_eligibility(semantic_fixture_data):
    """4. Supplied answers and independent metric eligibility."""
    # SEM-01 supported facts in fixture are: ["35 LPA", "Python Lead"]
    # SEM-02 requires abstention phrases: ["not specified", "not provided", "not disclosed"]
    answers = {
        "SEM-01": "The Python Lead role offers a salary of 35 LPA.",
        "SEM-02": "The compensation details for Job 102 are not specified in the listing.",
    }

    report = generate_evaluation_summary_report(
        semantic_fixture=semantic_fixture_data,
        case_answers=answers,
    )

    sem_layer = report["layers"]["layer_a_semantic_benchmarks"]
    faith = sem_layer["deterministic_faithfulness"]
    abstain = sem_layer["abstention_quality"]

    # For faithfulness, SEM-01 and SEM-02 are both evaluated
    sem01_faith = next(b for b in faith["per_case_breakdown"] if b["case_id"] == "SEM-01")
    assert sem01_faith["status"] == "EVALUATED"
    assert sem01_faith["score"] == 1.0

    # For abstention, SEM-01 does NOT require abstention -> should be N/A_ABSTENTION_NOT_REQUIRED, score=None
    sem01_abstain = next(b for b in abstain["per_case_breakdown"] if b["case_id"] == "SEM-01")
    assert sem01_abstain["status"] == "N/A_ABSTENTION_NOT_REQUIRED"
    assert sem01_abstain["score"] is None

    # SEM-02 DOES require abstention and provides valid abstention phrase -> evaluated, score=1.0
    sem02_abstain = next(b for b in abstain["per_case_breakdown"] if b["case_id"] == "SEM-02")
    assert sem02_abstain["status"] == "EVALUATED"
    assert sem02_abstain["score"] == 1.0


def test_05_actual_grounding_accuracy_distinction(retrieval_fixture_data):
    """5. Actual grounding accuracy remaining unmeasured when actual IDs are absent."""
    # Without actual_selected_ids
    report_unmeasured = generate_evaluation_summary_report(
        retrieval_fixture=retrieval_fixture_data,
        actual_selected_ids=None,
    )
    gr_unmeasured = report_unmeasured["layers"]["layer_a_retrieval_benchmarks"]["entity_id_grounding"]
    assert gr_unmeasured["mean_score"] is None
    assert gr_unmeasured["evaluation_scope"] == "NOT_MEASURED"
    assert gr_unmeasured["status"] == "NOT_MEASURED_MISSING_ACTUAL_IDS"
    assert "spec_integrity_validation" in gr_unmeasured
    assert gr_unmeasured["spec_integrity_validation"]["status"] == "SPEC_INTEGRITY_VALIDATED"

    # With actual_selected_ids supplied using real fixture case IDs ("CASE-01", "CASE-02", "CASE-03")
    actual_ids = {
        "CASE-01": None,
        "CASE-02": None,
        "CASE-03": "job_303",
    }
    report_measured = generate_evaluation_summary_report(
        retrieval_fixture=retrieval_fixture_data,
        actual_selected_ids=actual_ids,
    )
    gr_measured = report_measured["layers"]["layer_a_retrieval_benchmarks"]["entity_id_grounding"]
    assert gr_measured["evaluation_scope"] == "ACTUAL_EXECUTION_GROUNDING"
    assert gr_measured["eligible_cases"] == 3
    assert gr_measured["mean_score"] == 1.0


def test_06_supplied_pipeline_execution_results():
    """6. Supplied pipeline pass/fail/error results and unknown test names."""
    execution_results = {
        "test_01_pipeline_sem01_supported_salary_and_grounding": {"status": "PASSED"},
        "test_02_pipeline_sem02_missing_salary_abstention_and_negative_check": {
            "status": "FAILED",
            "reason": "AssertionError: expected abstention phrase not found",
        },
        "test_03_pipeline_sem04_cross_entity_salary_isolation": {
            "status": "ERROR",
            "reason": "RuntimeError: connection timeout",
        },
        "unknown_extra_test_method": {"status": "PASSED"},
    }

    report = generate_evaluation_summary_report(
        test_execution_results=execution_results,
    )

    pipe_layer = report["layers"]["layer_b_pipeline_regression"]
    assert pipe_layer["execution_results_supplied"] is True

    cases_map = {c["test_method"]: c for c in pipe_layer["cases"]}

    assert cases_map["test_01_pipeline_sem01_supported_salary_and_grounding"]["execution_status"] == "PASSED"
    assert cases_map["test_02_pipeline_sem02_missing_salary_abstention_and_negative_check"]["execution_status"] == "FAILED"
    assert "AssertionError" in cases_map["test_02_pipeline_sem02_missing_salary_abstention_and_negative_check"]["failure_reason"]
    assert cases_map["test_03_pipeline_sem04_cross_entity_salary_isolation"]["execution_status"] == "ERROR"

    # Any test method not in execution_results must be NOT_RUN
    assert cases_map["test_04_pipeline_sem05_out_of_range_ordinal_handling"]["execution_status"] == "NOT_RUN"

    # Unknown test name should not alter known pipeline case count
    assert len(pipe_layer["cases"]) == len(KNOWN_PIPELINE_COVERAGE_MAP)


def test_07_static_coverage_mapping_without_execution():
    """7. Static coverage mapping without execution results."""
    report = generate_evaluation_summary_report(test_execution_results=None)

    pipe_layer = report["layers"]["layer_b_pipeline_regression"]
    assert pipe_layer["execution_results_supplied"] is False
    assert len(pipe_layer["cases"]) == 12

    for c in pipe_layer["cases"]:
        assert c["execution_status"] == "NOT_RUN_STATIC_MAPPING_ONLY"
        assert c["failure_reason"] is None
        assert "route_tested" in c
        assert "prompt_boundary_inspection" in c
        assert "structured_card_grounding" in c
        assert "evaluator_counterfactual" in c
        assert "production_contradiction_blocking" in c


def test_08_markdown_formatting_and_na_display(retrieval_fixture_data):
    """8. Markdown formatting and N/A display."""
    report = generate_evaluation_summary_report(
        retrieval_fixture=retrieval_fixture_data,
        case_answers=None,
        actual_selected_ids=None,
        test_execution_results=None,
        timestamp="2026-10-21T12:00:00Z",
    )

    md = format_evaluation_report_markdown(report)

    # Truth notice
    assert "> [!IMPORTANT]" in md
    assert "NOT MEASURED" in md
    assert "`2026-10-21T12:00:00Z`" in md

    # N/A rendered cleanly, not 0.0
    assert "N/A" in md
    assert "0.0000" not in md  # Entity grounding and semantic answers should display N/A, not 0.0000

    # Execution provenance notice
    assert "Execution Provenance Notice" in md
    assert "static design coverage mapping" in md

    # Layer sections present
    assert "## 1. Retrieval Quality Benchmarks (Layer A)" in md
    assert "## 2. Semantic Answer Benchmarks (Layer A)" in md
    assert "## 3. Pipeline Regression Coverage (Layer B)" in md
    assert "## 4. Semantic LLM Judge Status (Layer C)" in md


def test_09_count_invariants_and_unimplemented_metrics(retrieval_fixture_data, semantic_fixture_data):
    """9. Count invariants and unimplemented metric status."""
    report = generate_evaluation_summary_report(
        retrieval_fixture=retrieval_fixture_data,
        semantic_fixture=semantic_fixture_data,
    )

    layers = report["layers"]

    # Retrieval metrics invariants
    ret_prec = layers["layer_a_retrieval_benchmarks"]["context_precision"]
    assert ret_prec["eligible_cases"] + ret_prec["na_cases"] == ret_prec["total_cases"]

    ret_rec = layers["layer_a_retrieval_benchmarks"]["context_recall"]
    assert ret_rec["eligible_cases"] + ret_rec["na_cases"] == ret_rec["total_cases"]

    ret_gr = layers["layer_a_retrieval_benchmarks"]["entity_id_grounding"]
    assert ret_gr["eligible_cases"] + ret_gr["na_cases"] == ret_gr["total_cases"]

    # Semantic metrics invariants
    sem_faith = layers["layer_a_semantic_benchmarks"]["deterministic_faithfulness"]
    assert sem_faith["eligible_cases"] + sem_faith["na_cases"] == sem_faith["total_cases"]

    sem_abst = layers["layer_a_semantic_benchmarks"]["abstention_quality"]
    assert sem_abst["eligible_cases"] + sem_abst["na_cases"] == sem_abst["total_cases"]

    # Response relevance metric
    sem_rel = layers["layer_a_semantic_benchmarks"]["response_relevance"]
    assert sem_rel["status"] == "N/A_NO_ELIGIBLE_CASES"
    assert sem_rel["mean_score"] is None
    assert sem_rel["eligible_cases"] == 0
    assert sem_rel["eligible_cases"] + sem_rel["na_cases"] == sem_rel["total_cases"]


def test_10_deterministic_output_identity(retrieval_fixture_data, semantic_fixture_data):
    """10. Deterministic report and Markdown output."""
    report1 = generate_evaluation_summary_report(
        retrieval_fixture=retrieval_fixture_data,
        semantic_fixture=semantic_fixture_data,
        case_answers={"SEM-01": "Some answer"},
        timestamp=None,
    )
    report2 = generate_evaluation_summary_report(
        retrieval_fixture=retrieval_fixture_data,
        semantic_fixture=semantic_fixture_data,
        case_answers={"SEM-01": "Some answer"},
        timestamp=None,
    )

    assert report1 == report2

    md1 = format_evaluation_report_markdown(report1)
    md2 = format_evaluation_report_markdown(report2)

    assert md1 == md2


def test_11_cli_runner_exit_code_contract(monkeypatch):
    """11. Test CLI runner exit-code contract:

    --enforce-gate:
      PASS -> 0
      FAIL -> 1
      INCONCLUSIVE -> 2
    """
    from backend.tests.run_evaluation_report import main

    # 1. When gate returns PASS -> exit code 0
    monkeypatch.setattr(
        "backend.tests.run_evaluation_report.generate_offline_report",
        lambda **kwargs: ("# Mock Report", {"overall_verdict": "PASS"}),
    )
    monkeypatch.setattr("sys.argv", ["run_evaluation_report", "--enforce-gate"])
    assert main() == 0

    # 2. When gate returns FAIL -> exit code 1
    monkeypatch.setattr(
        "backend.tests.run_evaluation_report.generate_offline_report",
        lambda **kwargs: ("# Mock Report", {"overall_verdict": "FAIL"}),
    )
    monkeypatch.setattr("sys.argv", ["run_evaluation_report", "--enforce-gate"])
    assert main() == 1

    # 3. When gate returns INCONCLUSIVE -> exit code 2
    monkeypatch.setattr(
        "backend.tests.run_evaluation_report.generate_offline_report",
        lambda **kwargs: ("# Mock Report", {"overall_verdict": "INCONCLUSIVE"}),
    )
    monkeypatch.setattr("sys.argv", ["run_evaluation_report", "--enforce-gate"])
    assert main() == 2
