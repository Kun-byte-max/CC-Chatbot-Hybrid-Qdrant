"""
backend/tests/test_evaluation_quality_gate.py — Focused tests for offline quality gate policy evaluator.

Requirements:
- All synthetic eligible metrics pass.
- A metric below target fails.
- Zero eligible cases are inconclusive.
- Missing answers are inconclusive.
- Explicitly empty answers follow existing evaluator behavior.
- Missing actual entity IDs do not pass grounding.
- Pipeline failure/error fails Layer B.
- Skipped, missing, unknown, or static-only pipeline results prevent Layer B from passing.
- All required pipeline tests explicitly passed is the only route to a Layer B pass.
- Missing baseline is reported as NO_BASELINE.
- Explicit baseline regression is detected.
- Missing baseline metric or ineligible data makes that comparison inconclusive.
- Unimplemented relevance remains NOT_IMPLEMENTED.
- Production quality always remains NOT MEASURED.
- Input data is not mutated.
- Deterministic output.
- Overall verdict precedence when both a failure and inconclusive data are present.
"""

import copy
import pytest
from backend.utils.retrieval_evaluator import (
    evaluate_quality_gate,
    generate_evaluation_summary_report,
)


def _build_synthetic_report(
    prec_score=1.0,
    rec_score=1.0,
    gr_score=1.0,
    faith_score=1.0,
    abst_score=1.0,
    rel_score=1.0,
    pipe_status="PASSED",
    include_answers=True,
    include_actual_ids=True,
    total_pipe=12,
):
    """Builds a structured synthetic report payload matching the report schema."""
    pipe_cases = []
    for i in range(1, total_pipe + 1):
        pipe_cases.append({
            "case_id": f"SEM-{i:02d}",
            "test_method": f"test_{i:02d}_pipeline_sem{i:02d}",
            "execution_status": pipe_status,
            "failure_reason": "Assertion failed" if pipe_status in {"FAILED", "ERROR"} else None,
        })

    return {
        "report_schema_version": "1.0",
        "timestamp": None,
        "production_quality_status": "NOT MEASURED",
        "layers": {
            "layer_a_retrieval_benchmarks": {
                "context_precision": {
                    "metric_name": "context_precision_map_at_k",
                    "target_threshold": 0.85,
                    "mean_score": prec_score,
                    "eligible_cases": 8 if prec_score is not None else 0,
                    "total_cases": 10,
                    "evaluation_scope": "SYNTHETIC_FIXTURE_BENCHMARK",
                    "status": "MEETS_SYNTHETIC_TARGET" if prec_score and prec_score >= 0.85 else "BELOW_TARGET",
                },
                "context_recall": {
                    "metric_name": "context_recall",
                    "target_threshold": 0.80,
                    "mean_score": rec_score,
                    "eligible_cases": 8 if rec_score is not None else 0,
                    "total_cases": 10,
                    "evaluation_scope": "SYNTHETIC_FIXTURE_BENCHMARK",
                    "status": "MEETS_SYNTHETIC_TARGET" if rec_score and rec_score >= 0.80 else "BELOW_TARGET",
                },
                "entity_id_grounding": {
                    "metric_name": "entity_id_grounding",
                    "target_threshold": 1.00,
                    "mean_score": gr_score if include_actual_ids else None,
                    "eligible_cases": 10 if include_actual_ids else 0,
                    "total_cases": 10,
                    "evaluation_scope": "ACTUAL_EXECUTION_GROUNDING" if include_actual_ids else "NOT_MEASURED",
                    "status": ("MEETS_TARGET" if gr_score and gr_score >= 1.0 else "BELOW_TARGET")
                    if include_actual_ids
                    else "NOT_MEASURED_MISSING_ACTUAL_IDS",
                },
            },
            "layer_a_semantic_benchmarks": {
                "deterministic_faithfulness": {
                    "metric_name": "deterministic_faithfulness",
                    "target_threshold": 0.95,
                    "mean_score": faith_score if include_answers else None,
                    "eligible_cases": 12 if include_answers else 0,
                    "total_cases": 12,
                    "evaluation_scope": "SYNTHETIC_ANSWER_BENCHMARK" if include_answers else "GENERATED_ANSWERS_NOT_PROVIDED",
                    "status": ("MEETS_TARGET" if faith_score and faith_score >= 0.95 else "BELOW_TARGET")
                    if include_answers
                    else "N/A_NO_ANSWER_SUPPLIED",
                },
                "abstention_quality": {
                    "metric_name": "abstention_quality",
                    "target_threshold": 0.95,
                    "mean_score": abst_score if include_answers else None,
                    "eligible_cases": 6 if include_answers else 0,
                    "total_cases": 12,
                    "evaluation_scope": "SYNTHETIC_ANSWER_BENCHMARK" if include_answers else "GENERATED_ANSWERS_NOT_PROVIDED",
                    "status": ("MEETS_TARGET" if abst_score and abst_score >= 0.95 else "BELOW_TARGET")
                    if include_answers
                    else "N/A_NO_ANSWER_SUPPLIED",
                },
                "response_relevance": {
                    "metric_name": "response_relevance",
                    "target_threshold": 0.90,
                    "mean_score": rel_score if include_answers else None,
                    "eligible_cases": 12 if include_answers else 0,
                    "total_cases": 12,
                    "evaluation_scope": "SYNTHETIC_ANSWER_BENCHMARK" if include_answers else "GENERATED_ANSWERS_NOT_PROVIDED",
                    "status": ("MEETS_TARGET" if rel_score and rel_score >= 0.90 else "BELOW_TARGET")
                    if include_answers
                    else "N/A_NO_ANSWER_SUPPLIED",
                },
            },
            "layer_b_pipeline_regression": {
                "coverage_mapping_total": total_pipe,
                "execution_results_supplied": pipe_status != "NOT_RUN_STATIC_MAPPING_ONLY",
                "cases": pipe_cases,
            },
            "layer_c_semantic_judge": {
                "status": "NOT_IMPLEMENTED",
            },
        },
    }


def test_01_all_synthetic_eligible_metrics_pass():
    """All eligible metrics pass target and all pipeline tests passed -> PASS."""
    rep = _build_synthetic_report()
    gate = evaluate_quality_gate(rep, baseline=None, require_layer_b=True)

    assert gate["overall_verdict"] == "PASS"
    assert gate["layer_a_verdict"] == "PASS"
    assert gate["layer_b_verdict"] == "PASS"
    assert gate["production_quality_status"] == "NOT MEASURED"
    assert gate["metric_verdicts"]["context_precision"]["verdict"] == "PASS"
    assert gate["metric_verdicts"]["context_recall"]["verdict"] == "PASS"
    assert gate["metric_verdicts"]["entity_id_grounding"]["verdict"] == "PASS"
    assert gate["metric_verdicts"]["deterministic_faithfulness"]["verdict"] == "PASS"
    assert gate["metric_verdicts"]["abstention_quality"]["verdict"] == "PASS"
    assert gate["metric_verdicts"]["response_relevance"]["verdict"] == "PASS"


def test_02_metric_below_target_fails():
    """A metric below target threshold fails Layer A and overall verdict."""
    rep = _build_synthetic_report(prec_score=0.75)  # Target is 0.85
    gate = evaluate_quality_gate(rep, baseline=None, require_layer_b=True)

    assert gate["overall_verdict"] == "FAIL"
    assert gate["layer_a_verdict"] == "FAIL"
    assert gate["metric_verdicts"]["context_precision"]["verdict"] == "FAIL"
    assert "below target threshold" in gate["metric_verdicts"]["context_precision"]["reason"]


def test_03_zero_eligible_cases_is_inconclusive():
    """Zero eligible cases produces INCONCLUSIVE, not zero and not PASS."""
    rep = _build_synthetic_report(prec_score=None)
    gate = evaluate_quality_gate(rep, baseline=None, require_layer_b=False)

    assert gate["overall_verdict"] == "INCONCLUSIVE"
    assert gate["metric_verdicts"]["context_precision"]["verdict"] == "INCONCLUSIVE"
    assert "Zero eligible cases" in gate["metric_verdicts"]["context_precision"]["reason"]


def test_04_missing_answers_is_inconclusive():
    """Missing generated answers causes faithfulness and abstention to be INCONCLUSIVE."""
    rep = _build_synthetic_report(include_answers=False)
    gate = evaluate_quality_gate(rep, baseline=None, require_layer_b=False)

    assert gate["overall_verdict"] == "INCONCLUSIVE"
    assert gate["metric_verdicts"]["deterministic_faithfulness"]["verdict"] == "INCONCLUSIVE"
    assert gate["metric_verdicts"]["abstention_quality"]["verdict"] == "INCONCLUSIVE"
    assert "Generated answers not supplied" in gate["metric_verdicts"]["deterministic_faithfulness"]["reason"]


def test_05_missing_actual_entity_ids_does_not_pass_grounding():
    """Missing actual selected entity IDs leaves grounding accuracy INCONCLUSIVE, never PASS."""
    rep = _build_synthetic_report(include_actual_ids=False)
    gate = evaluate_quality_gate(rep, baseline=None, require_layer_b=False)

    assert gate["overall_verdict"] == "INCONCLUSIVE"
    assert gate["metric_verdicts"]["entity_id_grounding"]["verdict"] == "INCONCLUSIVE"
    assert "actual grounding accuracy not measured" in gate["metric_verdicts"]["entity_id_grounding"]["reason"]


def test_06_pipeline_failure_fails_layer_b():
    """Any FAILED or ERROR in Layer B pipeline causes Layer B and overall FAIL."""
    rep = _build_synthetic_report(pipe_status="FAILED")
    gate = evaluate_quality_gate(rep, baseline=None, require_layer_b=True)

    assert gate["overall_verdict"] == "FAIL"
    assert gate["layer_b_verdict"] == "FAIL"
    assert "failed or errored" in gate["layer_b_reason"]


def test_07_skipped_missing_or_static_only_pipeline_prevents_pass():
    """Static mapping or missing/skipped runs prevent Layer B from PASS (remains INCONCLUSIVE)."""
    # Static mapping only
    rep_static = _build_synthetic_report(pipe_status="NOT_RUN_STATIC_MAPPING_ONLY")
    gate_static = evaluate_quality_gate(rep_static, baseline=None, require_layer_b=True)

    assert gate_static["overall_verdict"] == "INCONCLUSIVE"
    assert gate_static["layer_b_verdict"] == "INCONCLUSIVE"
    assert "static mapping only" in gate_static["layer_b_reason"]

    # Skipped
    rep_skipped = _build_synthetic_report(pipe_status="SKIPPED")
    gate_skipped = evaluate_quality_gate(rep_skipped, baseline=None, require_layer_b=True)
    assert gate_skipped["layer_b_verdict"] == "INCONCLUSIVE"


def test_08_all_required_pipeline_tests_explicitly_passed_is_only_route_to_layer_b_pass():
    """Only 100% explicit PASSED produces Layer B PASS."""
    rep = _build_synthetic_report(pipe_status="PASSED")
    gate = evaluate_quality_gate(rep, baseline=None, require_layer_b=True)

    assert gate["layer_b_verdict"] == "PASS"
    assert "All 12 mapped pipeline regression tests passed" in gate["layer_b_reason"]


def test_09_missing_baseline_reported_as_no_baseline():
    """When baseline is None, baseline_comparison is NO_BASELINE, not assumed passed."""
    rep = _build_synthetic_report()
    gate = evaluate_quality_gate(rep, baseline=None)

    assert gate["has_baseline"] is False
    assert gate["metric_verdicts"]["context_precision"]["baseline_comparison"] == "NO_BASELINE"


def test_10_explicit_baseline_regression_detected():
    """If score < baseline, regression is flagged as FAIL."""
    rep = _build_synthetic_report(prec_score=0.88)  # Above target (0.85), but below baseline (0.95)
    baseline = {"metrics": {"context_precision": 0.95}}

    gate = evaluate_quality_gate(rep, baseline=baseline)

    assert gate["overall_verdict"] == "FAIL"
    assert gate["metric_verdicts"]["context_precision"]["verdict"] == "FAIL"
    assert "REGRESSION" in gate["metric_verdicts"]["context_precision"]["baseline_comparison"]


def test_11_missing_baseline_metric_or_ineligible_is_inconclusive():
    """If baseline is missing a specific metric or data is missing, comparison is marked accordingly."""
    rep = _build_synthetic_report(include_answers=False)
    # Baseline contains context_precision and deterministic_faithfulness, but not context_recall
    baseline = {"metrics": {"context_precision": 0.85, "deterministic_faithfulness": 0.95}}

    gate = evaluate_quality_gate(rep, baseline=baseline)
    assert gate["metric_verdicts"]["context_recall"]["baseline_comparison"] == "METRIC_NOT_IN_BASELINE"
    assert gate["metric_verdicts"]["deterministic_faithfulness"]["baseline_comparison"] == "INCONCLUSIVE_DATA_MISSING"


def test_12_unimplemented_relevance_remains_not_implemented():
    """response_relevance is NOT_IMPLEMENTED when configured as such and does not count as PASS or FAIL."""
    rep = _build_synthetic_report()
    rep["layers"]["layer_a_semantic_benchmarks"]["response_relevance"]["status"] = "NOT_IMPLEMENTED"
    rep["layers"]["layer_a_semantic_benchmarks"]["response_relevance"]["evaluation_scope"] = "NOT_IMPLEMENTED"
    rep["layers"]["layer_a_semantic_benchmarks"]["response_relevance"]["mean_score"] = None
    gate = evaluate_quality_gate(rep)
    assert gate["metric_verdicts"]["response_relevance"]["verdict"] == "NOT_IMPLEMENTED"


def test_13_production_quality_always_not_measured():
    """Production quality is strictly NOT MEASURED under all combinations."""
    rep = _build_synthetic_report()
    gate = evaluate_quality_gate(rep)
    assert gate["production_quality_status"] == "NOT MEASURED"


def test_14_input_data_not_mutated():
    """Evaluator does not mutate input report_data or baseline dictionary."""
    rep = _build_synthetic_report()
    rep_copy = copy.deepcopy(rep)
    baseline = {"metrics": {"context_precision": 0.90}}
    base_copy = copy.deepcopy(baseline)

    evaluate_quality_gate(rep, baseline=baseline)

    assert rep == rep_copy
    assert baseline == base_copy


def test_15_deterministic_output():
    """Repeated calls produce identical verdicts and reasoning."""
    rep = _build_synthetic_report()
    g1 = evaluate_quality_gate(rep)
    g2 = evaluate_quality_gate(rep)
    assert g1 == g2


def test_16_verdict_precedence_failure_over_inconclusive():
    """When both a failure and missing data are present, overall verdict is FAIL (failure takes precedence)."""
    # Precision failed (0.70 < 0.85), but answers are missing (INCONCLUSIVE)
    rep = _build_synthetic_report(prec_score=0.70, include_answers=False)
    gate = evaluate_quality_gate(rep, baseline=None, require_layer_b=False)

    assert gate["metric_verdicts"]["context_precision"]["verdict"] == "FAIL"
    assert gate["metric_verdicts"]["deterministic_faithfulness"]["verdict"] == "INCONCLUSIVE"
    # Overall verdict must be FAIL, not hidden by INCONCLUSIVE
    assert gate["overall_verdict"] == "FAIL"
    assert gate["layer_a_verdict"] == "FAIL"


def test_17_relevance_scorer_unit_scenarios():
    """Directly tests evaluate_response_relevance across all required behavioral cases."""
    from backend.utils.retrieval_evaluator import evaluate_response_relevance

    # 1. Clearly relevant answer (both intent and required elements satisfied -> 1.0)
    res_rel = evaluate_response_relevance(
        answer_text="The salary for Senior Software Engineer is 35 LPA fixed compensation.",
        expected_intent="provide_salary",
        required_elements=["35 LPA"],
        query="What is the salary for senior software engineer?",
    )
    assert res_rel["score"] == 1.0
    assert res_rel["matched_intent"] is True
    assert res_rel["matched_elements"] == ["35 LPA"]
    assert res_rel["is_relevant"] is True

    # 2. Query words repeated but wrong intent (e.g., query asked for salary, response only talks about company culture)
    res_wrong_intent = evaluate_response_relevance(
        answer_text="Regarding what is the salary for senior software engineer, our culture is highly collaborative.",
        expected_intent="provide_salary",
        required_elements=["35 LPA"],
        query="What is the salary for senior software engineer?",
    )
    # Intent signal not matched (0.0), required elements not matched (0.0) -> score 0.0
    assert res_wrong_intent["score"] == 0.0
    assert res_wrong_intent["matched_intent"] is False
    assert res_wrong_intent["is_relevant"] is False

    # 3. Partially relevant answer (intent matched, but only 1 of 2 required elements present)
    res_partial = evaluate_response_relevance(
        answer_text="The fixed compensation and salary is 35 LPA.",
        expected_intent="provide_salary",
        required_elements=["35 LPA", "Delhi"],
        query="What is the salary for senior software engineer in Delhi?",
    )
    # 0.5 * 1.0 (intent) + 0.5 * (1/2 elements = 0.5) = 0.75
    assert res_partial["score"] == 0.75
    assert res_partial["matched_intent"] is True
    assert res_partial["matched_elements"] == ["35 LPA"]
    assert res_partial["missing_elements"] == ["Delhi"]
    assert res_partial["is_relevant"] is False  # 0.75 < 0.90 threshold

    # 4. Correct clarification response for an ambiguous query
    res_clarif = evaluate_response_relevance(
        answer_text="Could you please clarify which job or role you are asking about?",
        expected_intent="request_clarification",
        required_elements=["which"],
        query="Tell me about that role",
    )
    assert res_clarif["score"] == 1.0
    assert res_clarif["matched_intent"] is True
    assert res_clarif["is_relevant"] is True

    # 5. Correct abstention / no-results response
    res_abstain = evaluate_response_relevance(
        answer_text="Salary details are not specified in the job listing.",
        expected_intent="abstain_missing_evidence",
        required_elements=["not specified"],
        query="What is the compensation for Job 102?",
    )
    assert res_abstain["score"] == 1.0
    assert res_abstain["matched_intent"] is True
    assert res_abstain["is_relevant"] is True

    # 6. Empty or whitespace-only answer
    res_empty = evaluate_response_relevance(
        answer_text="   ",
        expected_intent="provide_salary",
        required_elements=["35 LPA"],
        query="What is the salary?",
    )
    assert res_empty["score"] == 0.0
    assert res_empty["reason"] == "empty_answer"
    assert res_empty["is_relevant"] is False

    # 7. Missing expectations / unannotated contract -> unevaluable (score is None)
    res_missing_contract = evaluate_response_relevance(
        answer_text="Some valid answer.",
        expected_intent=None,
        required_elements=None,
        query="Some query",
    )
    assert res_missing_contract["score"] is None
    assert res_missing_contract["reason"] == "missing_relevance_contract"


def test_18_response_relevance_threshold_enforcement_in_gate():
    """Validates that response_relevance strictly enforces the 0.90 threshold."""
    # Score 0.88 (< 0.90) fails Layer A and overall gate
    rep_fail = _build_synthetic_report(rel_score=0.88)
    gate_fail = evaluate_quality_gate(rep_fail, baseline=None, require_layer_b=False)
    assert gate_fail["overall_verdict"] == "FAIL"
    assert gate_fail["metric_verdicts"]["response_relevance"]["verdict"] == "FAIL"
    assert "below target threshold 0.9000" in gate_fail["metric_verdicts"]["response_relevance"]["reason"]

    # Score 0.90 (== 0.90) passes
    rep_pass = _build_synthetic_report(rel_score=0.90)
    gate_pass = evaluate_quality_gate(rep_pass, baseline=None, require_layer_b=False)
    assert gate_pass["metric_verdicts"]["response_relevance"]["verdict"] == "PASS"

    # Score 0.95 (> 0.90) passes
    rep_pass_higher = _build_synthetic_report(rel_score=0.95)
    gate_pass_higher = evaluate_quality_gate(rep_pass_higher, baseline=None, require_layer_b=False)
    assert gate_pass_higher["metric_verdicts"]["response_relevance"]["verdict"] == "PASS"


def test_19_response_relevance_unevaluable_cases_in_report_and_gate():
    """Validates aggregate report and quality gate when answers or contracts are missing."""
    # Synthetic semantic fixture with one annotated and one missing-contract case
    mock_fixture = {
        "dataset_version": "1.0",
        "cases": [
            {
                "case_id": "MOCK-01",
                "evaluated_metrics": ["response_relevance"],
                "expected_intent": "provide_salary",
                "required_elements": ["35 LPA"],
            },
            {
                "case_id": "MOCK-02",
                "evaluated_metrics": ["response_relevance"],
                "expected_intent": None,  # Missing contract
                "required_elements": None,
            },
        ],
    }

    answers = {
        "MOCK-01": "Salary is 35 LPA per annum.",
        "MOCK-02": "Some unannotated answer.",
    }

    report = generate_evaluation_summary_report(
        semantic_fixture=mock_fixture,
        case_answers=answers,
    )

    rel_summary = report["layers"]["layer_a_semantic_benchmarks"]["response_relevance"]
    # 1 eligible, 1 unevaluable (N/A) out of 2 total
    assert rel_summary["eligible_cases"] == 1
    assert rel_summary["na_cases"] == 1
    assert rel_summary["total_cases"] == 2
    assert rel_summary["mean_score"] == 1.0

    # Gate evaluation on this report
    gate = evaluate_quality_gate(report, baseline=None, require_layer_b=False)
    # The eligible case scored 1.0 >= 0.90, so metric verdict is PASS
    assert gate["metric_verdicts"]["response_relevance"]["verdict"] == "PASS"
    assert gate["metric_verdicts"]["response_relevance"]["eligible_cases"] == 1


def test_20_false_pass_prevention_audit_scenarios():
    """Validates that false-pass risks identified in Phase 1.1.1 audit are blocked."""
    from backend.utils.retrieval_evaluator import evaluate_response_relevance

    # 1. Answer with WRONG intent but containing ALL required elements
    # Even if all elements match (element_score = 1.0), wrong intent (intent_score = 0.0)
    # caps composite score at 0.50, which strictly FAILS the 0.90 threshold.
    res_wrong_intent_all_elements = evaluate_response_relevance(
        answer_text="Our company culture is great and the salary is 35 LPA.",
        expected_intent="abstain_missing_evidence",  # expected abstention, but got factual salary answer
        required_elements=["35 LPA"],
        query="What is the compensation for Job 102?",
    )
    assert res_wrong_intent_all_elements["score"] == 0.50
    assert res_wrong_intent_all_elements["is_relevant"] is False  # Cannot pass 0.90 threshold

    # 2. When only intent is contracted and answer fails intent, score is 0.0, NOT 0.50 or 1.0
    res_intent_only_fail = evaluate_response_relevance(
        answer_text="We have a collaborative office environment.",
        expected_intent="provide_salary",
        required_elements=None,
        query="What is the salary?",
    )
    assert res_intent_only_fail["score"] == 0.0
    assert res_intent_only_fail["is_relevant"] is False

    # 3. When only intent is contracted and answer matches, score is 1.0
    res_intent_only_pass = evaluate_response_relevance(
        answer_text="The compensation package is fixed per annum.",
        expected_intent="provide_salary",
        required_elements=None,
        query="What is the salary?",
    )
    assert res_intent_only_pass["score"] == 1.0
    assert res_intent_only_pass["is_relevant"] is True

    # 4. When only elements are contracted and answer misses element, score is 0.0, NOT unearned 1.0
    res_element_only_fail = evaluate_response_relevance(
        answer_text="We have a great opportunity.",
        expected_intent=None,
        required_elements=["35 LPA"],
        query="Tell me about Job 101",
    )
    assert res_element_only_fail["score"] == 0.0
    assert res_element_only_fail["is_relevant"] is False


def test_21_twelve_cases_scenario_one_zero_eleven_ones():
    """Phase 1.1.2 Audit Scenario:

    12 eligible cases: one response-relevance score of 0.0 and eleven scores of 1.0.
    - Mean score: 11 / 12 = 0.9167 (>= 0.90 target threshold).
    - Under the existing aggregate-only gate policy, the metric verdict is PASS.
    - The failed case is visibly tracked in per_case_breakdown and Markdown.
    """
    from backend.utils.retrieval_evaluator import format_evaluation_report_markdown

    # Build 12 mock cases
    mock_cases = []
    answers = {}
    for i in range(1, 13):
        cid = f"CASE-{i:02d}"
        mock_cases.append({
            "case_id": cid,
            "evaluated_metrics": ["response_relevance"],
            "expected_intent": "provide_salary",
            "required_elements": [f"ELEM_{i}"],
        })
        if i == 1:
            # Case 1 fails both intent and element -> 0.0
            answers[cid] = "Irrelevant answer."
        else:
            # Cases 2-12 satisfy both intent and element -> 1.0
            answers[cid] = f"Fixed salary compensation is ELEM_{i} per annum."

    report = generate_evaluation_summary_report(
        semantic_fixture={"version": "1.0", "cases": mock_cases},
        case_answers=answers,
    )

    rel_summary = report["layers"]["layer_a_semantic_benchmarks"]["response_relevance"]
    assert rel_summary["eligible_cases"] == 12
    assert rel_summary["total_cases"] == 12
    assert rel_summary["na_cases"] == 0
    # Mean score: 11 / 12 = 0.9167
    assert rel_summary["mean_score"] == pytest.approx(0.9167, abs=1e-4)
    assert rel_summary["status"] == "MEETS_TARGET"

    # Verify per-case breakdown exposes the failed case
    breakdown = rel_summary["per_case_breakdown"]
    assert len(breakdown) == 12
    failed_case = next(c for c in breakdown if c["case_id"] == "CASE-01")
    assert failed_case["score"] == 0.0
    assert failed_case["reason"] == "intent_and_elements_mismatched"

    # Verify gate verdict under aggregate-only policy
    gate = evaluate_quality_gate(report, baseline=None, require_layer_b=False)
    # The aggregate mean (0.9167) meets the 0.90 threshold, so response_relevance verdict is PASS
    assert gate["metric_verdicts"]["response_relevance"]["verdict"] == "PASS"
    # Note: overall_verdict is INCONCLUSIVE here solely because retrieval metrics were omitted from mock fixture
    assert gate["metric_verdicts"]["context_precision"]["verdict"] == "INCONCLUSIVE"
    assert gate["overall_verdict"] == "INCONCLUSIVE"

    # Now verify in a full synthetic report where all other metrics pass:
    full_report = _build_synthetic_report(rel_score=0.9167)
    full_gate = evaluate_quality_gate(full_report, baseline=None, require_layer_b=False)
    assert full_gate["metric_verdicts"]["response_relevance"]["verdict"] == "PASS"
    assert full_gate["overall_verdict"] == "PASS"

    # Verify Markdown report exposes the metrics
    md = format_evaluation_report_markdown(report)
    assert "Response Relevance" in md
    assert "0.9167" in md
    assert "12 / 12 (0 N/A)" in md


def test_22_critical_case_zero_tolerance_evaluation():
    """Phase 1.2: Tests critical-case zero-tolerance policy in evaluate_quality_gate."""
    from backend.utils.retrieval_evaluator import format_evaluation_report_markdown

    # 1. One critical case fails while aggregate >= 0.90 -> expected FAIL
    # 1 critical case scoring 0.0, 11 non-critical cases scoring 1.0 -> mean = 0.9167
    cases_crit_fail = []
    answers_crit_fail = {}
    for i in range(1, 13):
        cid = f"CASE-{i:02d}"
        is_crit = (i == 1)
        cases_crit_fail.append({
            "case_id": cid,
            "evaluated_metrics": ["response_relevance"],
            "expected_intent": "provide_salary",
            "required_elements": [f"ELEM_{i}"],
            "critical": is_crit,
        })
        if i == 1:
            answers_crit_fail[cid] = "Irrelevant answer."
        else:
            answers_crit_fail[cid] = f"Fixed salary compensation is ELEM_{i} per annum."

    rep_crit_fail = generate_evaluation_summary_report(
        semantic_fixture={"version": "1.0", "cases": cases_crit_fail},
        case_answers=answers_crit_fail,
    )
    # Aggregate mean is 0.9167 (>= 0.90 target)
    assert rep_crit_fail["layers"]["layer_a_semantic_benchmarks"]["response_relevance"]["mean_score"] == pytest.approx(0.9167, abs=1e-4)

    gate_crit_fail = evaluate_quality_gate(rep_crit_fail, baseline=None, require_layer_b=False)
    # Critical failure forces FAIL regardless of passing mean
    assert gate_crit_fail["metric_verdicts"]["response_relevance"]["verdict"] == "FAIL"
    assert "Critical semantic case failure" in gate_crit_fail["metric_verdicts"]["response_relevance"]["reason"]
    assert "CASE-01" in gate_crit_fail["metric_verdicts"]["response_relevance"]["reason"]

    # 2. A non-critical case fails while aggregate >= 0.90 and all critical cases pass -> PASS
    # Case 2 is critical and passes (1.0). Case 1 is non-critical and fails (0.0). Cases 3-12 pass (1.0).
    cases_noncrit_fail = []
    answers_noncrit_fail = {}
    for i in range(1, 13):
        cid = f"CASE-{i:02d}"
        is_crit = (i == 2)  # only Case 2 is critical
        cases_noncrit_fail.append({
            "case_id": cid,
            "evaluated_metrics": ["response_relevance"],
            "expected_intent": "provide_salary",
            "required_elements": [f"ELEM_{i}"],
            "critical": is_crit,
        })
        if i == 1:
            answers_noncrit_fail[cid] = "Irrelevant answer."
        else:
            answers_noncrit_fail[cid] = f"Fixed salary compensation is ELEM_{i} per annum."

    rep_noncrit_fail = generate_evaluation_summary_report(
        semantic_fixture={"version": "1.0", "cases": cases_noncrit_fail},
        case_answers=answers_noncrit_fail,
    )
    gate_noncrit_fail = evaluate_quality_gate(rep_noncrit_fail, baseline=None, require_layer_b=False)
    # Critical case CASE-02 passed, and aggregate mean 0.9167 >= 0.90 -> metric PASS
    assert gate_noncrit_fail["metric_verdicts"]["response_relevance"]["verdict"] == "PASS"

    # 3. All critical cases pass and all aggregate checks pass -> PASS
    cases_all_pass = []
    answers_all_pass = {}
    for i in range(1, 13):
        cid = f"CASE-{i:02d}"
        cases_all_pass.append({
            "case_id": cid,
            "evaluated_metrics": ["response_relevance"],
            "expected_intent": "provide_salary",
            "required_elements": [f"ELEM_{i}"],
            "critical": (i in {2, 4, 10}),
        })
        answers_all_pass[cid] = f"Fixed salary compensation is ELEM_{i} per annum."

    rep_all_pass = generate_evaluation_summary_report(
        semantic_fixture={"version": "1.0", "cases": cases_all_pass},
        case_answers=answers_all_pass,
    )
    gate_all_pass = evaluate_quality_gate(rep_all_pass, baseline=None, require_layer_b=False)
    assert gate_all_pass["metric_verdicts"]["response_relevance"]["verdict"] == "PASS"

    # 4. A critical case is unevaluable (e.g. missing contract or unevaluated) -> INCONCLUSIVE
    cases_uneval = [
        {
            "case_id": "CASE-01",
            "evaluated_metrics": ["response_relevance"],
            "expected_intent": None,  # Missing contract makes it unevaluable
            "required_elements": None,
            "critical": True,
        },
        {
            "case_id": "CASE-02",
            "evaluated_metrics": ["response_relevance"],
            "expected_intent": "provide_salary",
            "required_elements": ["ELEM_2"],
            "critical": False,
        },
    ]
    answers_uneval = {
        "CASE-01": "Some answer.",
        "CASE-02": "Fixed salary compensation is ELEM_2 per annum.",
    }
    rep_uneval = generate_evaluation_summary_report(
        semantic_fixture={"version": "1.0", "cases": cases_uneval},
        case_answers=answers_uneval,
    )
    gate_uneval = evaluate_quality_gate(rep_uneval, baseline=None, require_layer_b=False)
    assert gate_uneval["metric_verdicts"]["response_relevance"]["verdict"] == "INCONCLUSIVE"
    assert "Critical semantic case unevaluable" in gate_uneval["metric_verdicts"]["response_relevance"]["reason"]

    # 5. A confirmed critical failure and a separate unevaluable critical case coexist -> FAIL (precedence)
    cases_mixed = [
        {
            "case_id": "CASE-01",
            "evaluated_metrics": ["response_relevance"],
            "expected_intent": "provide_salary",
            "required_elements": ["ELEM_1"],
            "critical": True,
        },
        {
            "case_id": "CASE-02",
            "evaluated_metrics": ["response_relevance"],
            "expected_intent": None,  # Unevaluable
            "required_elements": None,
            "critical": True,
        },
    ]
    answers_mixed = {
        "CASE-01": "Irrelevant culture answer.",  # Fails -> 0.0
        "CASE-02": "Some text.",
    }
    rep_mixed = generate_evaluation_summary_report(
        semantic_fixture={"version": "1.0", "cases": cases_mixed},
        case_answers=answers_mixed,
    )
    gate_mixed = evaluate_quality_gate(rep_mixed, baseline=None, require_layer_b=False)
    # Confirmed failure outranks unevaluable
    assert gate_mixed["metric_verdicts"]["response_relevance"]["verdict"] == "FAIL"

    # 6. Malformed critical annotation (non-bool type like "yes") raises ValueError
    with pytest.raises(ValueError, match="Malformed critical flag in case"):
        generate_evaluation_summary_report(
            semantic_fixture={"version": "1.0", "cases": [{"case_id": "BAD-01", "critical": "true_string"}]},
            case_answers={},
        )

    # 7. Duplicate semantic case IDs raises ValueError
    with pytest.raises(ValueError, match="Duplicate semantic case_id detected"):
        generate_evaluation_summary_report(
            semantic_fixture={"version": "1.0", "cases": [{"case_id": "DUP-01"}, {"case_id": "DUP-01"}]},
            case_answers={},
        )

    # 8. Markdown report rendering of critical case status
    md_fail = format_evaluation_report_markdown(rep_crit_fail)
    assert "*Critical Case Enforcement:*" in md_fail
    assert "FAILED (1 failed)" in md_fail

    # 9. Boolean score rejection: Python bool (e.g. True) must NOT be accepted as numeric 1.0
    rep_bool_score = {
        "layers": {
            "layer_a_semantic_benchmarks": {
                "response_relevance": {
                    "mean_score": 1.0,
                    "target_threshold": 0.90,
                    "eligible_cases": 1,
                    "per_case_breakdown": [
                        {
                            "case_id": "CRIT-01",
                            "critical": True,
                            "status": "EVALUATED",
                            "score": True,  # Boolean subclass of int!
                        }
                    ],
                }
            }
        }
    }
    gate_bool = evaluate_quality_gate(rep_bool_score, baseline=None, require_layer_b=False)
    # Boolean score must be rejected as unevaluable, not treated as pass
    assert gate_bool["metric_verdicts"]["response_relevance"]["verdict"] == "INCONCLUSIVE"
    assert "Critical semantic case unevaluable" in gate_bool["metric_verdicts"]["response_relevance"]["reason"]

    # 10. Disappeared critical case: expected critical case omitted from per_case_breakdown
    rep_missing_from_breakdown = {
        "layers": {
            "layer_a_semantic_benchmarks": {
                "response_relevance": {
                    "mean_score": 1.0,
                    "target_threshold": 0.90,
                    "eligible_cases": 1,
                    "expected_critical_case_ids": ["CRIT-01", "CRIT-02"],
                    "per_case_breakdown": [
                        {
                            "case_id": "CRIT-01",
                            "critical": True,
                            "status": "EVALUATED",
                            "score": 1.0,
                        }
                        # CRIT-02 is omitted from breakdown!
                    ],
                }
            }
        }
    }
    gate_disappeared = evaluate_quality_gate(rep_missing_from_breakdown, baseline=None, require_layer_b=False)
    assert gate_disappeared["metric_verdicts"]["response_relevance"]["verdict"] == "INCONCLUSIVE"
    assert "CRIT-02 (missing from per_case_breakdown)" in gate_disappeared["metric_verdicts"]["response_relevance"]["reason"]

    # 11. Finding 1: Out-of-range critical scores must be rejected as unevaluable, not treated as pass or fail
    for bad_score in [1.50, -0.10, float("nan"), float("inf"), float("-inf"), "invalid_str", None]:
        rep_out_of_range = {
            "layers": {
                "layer_a_semantic_benchmarks": {
                    "response_relevance": {
                        "mean_score": 1.0,
                        "target_threshold": 0.90,
                        "eligible_cases": 1,
                        "per_case_breakdown": [
                            {
                                "case_id": "CRIT-01",
                                "critical": True,
                                "status": "EVALUATED",
                                "score": bad_score,
                            }
                        ],
                    }
                }
            }
        }
        gate_oor = evaluate_quality_gate(rep_out_of_range, baseline=None, require_layer_b=False)
        assert gate_oor["metric_verdicts"]["response_relevance"]["verdict"] == "INCONCLUSIVE"
        assert "Critical semantic case unevaluable" in gate_oor["metric_verdicts"]["response_relevance"]["reason"]

    # 12. Finding 3: Conflicting duplicate breakdown entries (both orders: False then True, and True then False)
    # Order A: False then True
    rep_dup_false_true = {
        "layers": {
            "layer_a_semantic_benchmarks": {
                "response_relevance": {
                    "mean_score": 1.0,
                    "target_threshold": 0.90,
                    "eligible_cases": 2,
                    "per_case_breakdown": [
                        {"case_id": "CASE-DUP", "critical": False, "status": "EVALUATED", "score": 1.0},
                        {"case_id": "CASE-DUP", "critical": True, "status": "EVALUATED", "score": 1.0},
                    ],
                }
            }
        }
    }
    gate_dup_a = evaluate_quality_gate(rep_dup_false_true, baseline=None, require_layer_b=False)
    assert gate_dup_a["metric_verdicts"]["response_relevance"]["verdict"] == "INCONCLUSIVE"
    assert "duplicate entry in per_case_breakdown" in gate_dup_a["metric_verdicts"]["response_relevance"]["reason"]

    # Order B: True then False
    rep_dup_true_false = {
        "layers": {
            "layer_a_semantic_benchmarks": {
                "response_relevance": {
                    "mean_score": 1.0,
                    "target_threshold": 0.90,
                    "eligible_cases": 2,
                    "per_case_breakdown": [
                        {"case_id": "CASE-DUP", "critical": True, "status": "EVALUATED", "score": 1.0},
                        {"case_id": "CASE-DUP", "critical": False, "status": "EVALUATED", "score": 1.0},
                    ],
                }
            }
        }
    }
    gate_dup_b = evaluate_quality_gate(rep_dup_true_false, baseline=None, require_layer_b=False)
    assert gate_dup_b["metric_verdicts"]["response_relevance"]["verdict"] == "INCONCLUSIVE"
    assert "duplicate entry in per_case_breakdown" in gate_dup_b["metric_verdicts"]["response_relevance"]["reason"]

    # 13. Finding 2: Direct gate invocation requiring critical contract with missing contract metadata
    rep_missing_contract = {
        "layers": {
            "layer_a_semantic_benchmarks": {
                "response_relevance": {
                    "mean_score": 1.0,
                    "target_threshold": 0.90,
                    "eligible_cases": 1,
                    "require_critical_contract": True,
                    "per_case_breakdown": [
                        {"case_id": "CASE-01", "critical": False, "status": "EVALUATED", "score": 1.0}
                    ],
                }
            }
        }
    }
    gate_mc = evaluate_quality_gate(rep_missing_contract, baseline=None, require_layer_b=False)
    assert gate_mc["metric_verdicts"]["response_relevance"]["verdict"] == "INCONCLUSIVE"
    assert "missing expected_critical_case_ids contract" in gate_mc["metric_verdicts"]["response_relevance"]["reason"]
