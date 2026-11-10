"""
backend/tests/run_evaluation_report.py — Dedicated standalone offline evaluation runner.

Loads retrieval and semantic datasets, optionally accepts completed JUnit XML test
results, generates the unified evaluation report, and renders Markdown output.

Usage:
  python -m backend.tests.run_evaluation_report [--junit-xml PATH] [--output-path PATH] [--overwrite]

Safety guarantees:
- Zero live network, LLM, embedding, DB, or Meilisearch calls.
- Never executes pytest automatically; only ingests already completed runs if provided.
- Does not call system clock internally (timestamp remains None by default).
- Prominently displays 'Production quality: NOT MEASURED'.
- Does not overwrite existing files unless explicitly specified.
"""

import os
import sys
import json
import argparse
from pathlib import Path
from typing import Optional, Dict, Any

from backend.utils.retrieval_evaluator import (
    generate_evaluation_summary_report,
    format_evaluation_report_markdown,
    parse_junit_xml_test_results,
    evaluate_quality_gate,
)


def load_fixture_data(file_path: Path) -> Dict[str, Any]:
    """Loads and validates an offline evaluation fixture JSON file."""
    if not file_path.exists() or not file_path.is_file():
        raise FileNotFoundError(f"Fixture file not found: {file_path}")

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise ValueError(f"Failed to parse JSON from {file_path}: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(f"Fixture root must be a JSON object, got {type(data).__name__}: {file_path}")

    if "cases" not in data or not isinstance(data["cases"], list):
        raise ValueError(f"Fixture must contain a 'cases' list: {file_path}")

    return data


def generate_offline_report(
    retrieval_fixture_path: Optional[Path] = None,
    semantic_fixture_path: Optional[Path] = None,
    junit_xml_path: Optional[Path] = None,
    timestamp: Optional[str] = None,
    return_gate_result: bool = False,
) -> Any:
    """Generates the Markdown evaluation report string from fixtures and optional JUnit results."""
    fixtures_dir = Path(__file__).parent / "fixtures"

    ret_path = retrieval_fixture_path or (fixtures_dir / "eval_retrieval_dataset.json")
    sem_path = semantic_fixture_path or (fixtures_dir / "eval_semantic_dataset.json")

    retrieval_data = load_fixture_data(ret_path)
    semantic_data = load_fixture_data(sem_path)

    test_results = None
    if junit_xml_path is not None:
        test_results = parse_junit_xml_test_results(str(junit_xml_path))

    report_data = generate_evaluation_summary_report(
        retrieval_fixture=retrieval_data,
        semantic_fixture=semantic_data,
        case_answers=None,  # Not synthesizing model answers
        actual_selected_ids=None,  # Not synthesizing selected IDs
        test_execution_results=test_results,
        timestamp=timestamp,
    )

    report_md = format_evaluation_report_markdown(report_data)

    # Evaluate quality gates
    gate_res = evaluate_quality_gate(report_data, baseline=None, require_layer_b=True)

    gate_lines = [
        "",
        "## 5. Offline Quality Gate Policy",
        "",
        f"> **Overall Offline Verdict:** `{gate_res['overall_verdict']}`",
        f"> **Production Quality Status:** `{gate_res['production_quality_status']}`",
        f"> **Verdict Reason:** {gate_res['overall_reason']}",
        "",
        f"- **Layer A (Synthetic Benchmarks):** `{gate_res['layer_a_verdict']}`",
        f"- **Layer B (Pipeline Regression):** `{gate_res['layer_b_verdict']}` ({gate_res['layer_b_reason']})",
        "",
        "| Metric | Verdict | Reason | Score / Target | Baseline Comparison |",
        "| :--- | :---: | :--- | :---: | :--- |",
    ]

    for m_k, m_v in gate_res["metric_verdicts"].items():
        v = m_v["verdict"]
        reas = m_v["reason"]
        sc = f"{m_v['score']:.4f}" if m_v["score"] is not None else "N/A"
        tgt = f"{m_v['target']:.2f}" if m_v["target"] is not None else "N/A"
        b_comp = m_v["baseline_comparison"]
        gate_lines.append(f"| **{m_k}** | `{v}` | {reas} | {sc} / {tgt} | `{b_comp}` |")

    gate_lines.append("")

    final_md = report_md + "\n".join(gate_lines)
    if return_gate_result:
        return final_md, gate_res
    return final_md


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CollarCheck Offline Evaluation Report Generator"
    )
    parser.add_argument(
        "--retrieval-fixture",
        type=Path,
        default=None,
        help="Path to retrieval dataset JSON (defaults to backend/tests/fixtures/eval_retrieval_dataset.json)",
    )
    parser.add_argument(
        "--semantic-fixture",
        type=Path,
        default=None,
        help="Path to semantic dataset JSON (defaults to backend/tests/fixtures/eval_semantic_dataset.json)",
    )
    parser.add_argument(
        "--junit-xml",
        type=Path,
        default=None,
        help="Path to pre-computed JUnit XML test execution report",
    )
    parser.add_argument(
        "--output-path",
        type=Path,
        default=None,
        help="Path to write the Markdown report. If omitted, outputs to stdout.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow overwriting an existing file at --output-path",
    )
    parser.add_argument(
        "--timestamp",
        type=str,
        default=None,
        help="Optional explicit timestamp string for report header (defaults to None for determinism)",
    )
    parser.add_argument(
        "--enforce-gate",
        action="store_true",
        help="Enforce offline quality gates: return 0 for PASS, 1 for FAIL, 2 for INCONCLUSIVE",
    )

    args = parser.parse_args()

    try:
        report_md, gate_res = generate_offline_report(
            retrieval_fixture_path=args.retrieval_fixture,
            semantic_fixture_path=args.semantic_fixture,
            junit_xml_path=args.junit_xml,
            timestamp=args.timestamp,
            return_gate_result=True,
        )

        if args.output_path:
            out_file = args.output_path
            if out_file.exists() and not args.overwrite:
                sys.stderr.write(f"Error: Output file already exists: {out_file}. Use --overwrite to replace.\n")
                return 1

            out_file.parent.mkdir(parents=True, exist_ok=True)
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(report_md)
            sys.stderr.write(f"Report written to: {out_file}\n")
        else:
            sys.stdout.write(report_md + "\n")

        # If --enforce-gate is enabled, determine exit code from gate verdict
        if getattr(args, "enforce_gate", False):
            verdict = gate_res.get("overall_verdict")
            if verdict == "PASS":
                return 0
            elif verdict == "FAIL":
                return 1
            elif verdict == "INCONCLUSIVE":
                return 2
            else:
                # Any unexpected or unsupported verdict
                return 1

        return 0
    except Exception as e:
        sys.stderr.write(f"Error generating evaluation report: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())

