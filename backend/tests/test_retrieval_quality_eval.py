"""
backend/tests/test_retrieval_quality_eval.py — Deterministic retrieval quality & grounding evaluation tests.

Verifies:
1. JSON fixture structure, schema integrity, and unique case IDs.
2. Context Precision (AP@K) ranking math, deduplication, and edge cases.
3. Context Recall math, empty pools, and undefined behavior.
4. Entity-ID grounding exact matching, allowed-IDs validation, and out-of-range rejection.
5. Deterministic grounding checks (missing evidence, attribute isolation, forbidden facts).
6. Complete synthetic benchmark execution over all 10 evaluation cases.
"""

import json
from pathlib import Path
import unittest

from backend.utils.retrieval_evaluator import (
    calculate_average_precision_at_k,
    calculate_context_recall,
    verify_entity_id_grounding,
    check_deterministic_grounding_facts,
    aggregate_metric_scores,
)


class TestRetrievalQualityEvaluator(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        fixture_path = Path(__file__).parent / "fixtures" / "eval_retrieval_dataset.json"
        with open(fixture_path, "r", encoding="utf-8") as f:
            cls.dataset = json.load(f)

    def test_01_fixture_integrity_and_unique_case_ids(self):
        """Verifies that the synthetic fixture has valid schema and 10 strictly unique case IDs."""
        self.assertIn("cases", self.dataset)
        cases = self.dataset["cases"]
        self.assertEqual(len(cases), 10)

        case_ids = [c["case_id"] for c in cases]
        self.assertEqual(len(case_ids), len(set(case_ids)), "Duplicate case IDs detected!")

        for c in cases:
            self.assertTrue(c["case_id"].startswith("CASE-"))
            self.assertIn("query", c)
            self.assertIn("retrieved_ids", c)
            self.assertIn("expected_relevant_ids", c)
            self.assertIn("evaluated_metrics", c)

    def test_02_context_precision_ranking_and_deduplication(self):
        """Tests Average Precision at K calculation with rank weighting and duplicate-ID discounting."""
        # Relevant: {A, B}. Retrieved: [A, C, B]
        # Rank 1 (A): hit 1, P@1 = 1/1 = 1.0
        # Rank 2 (C): hit 1, irrelevant
        # Rank 3 (B): hit 2, P@3 = 2/3 = 0.6667
        # AP = (1.0 + 0.6667) / 2 = 0.8333
        ap = calculate_average_precision_at_k(
            retrieved_ids=["A", "C", "B"],
            relevant_ids={"A", "B"},
        )
        self.assertAlmostEqual(ap, 0.8333, places=3)

        # Perfect ranking: [A, B, C] -> AP = (1.0 + 1.0) / 2 = 1.0
        ap_perf = calculate_average_precision_at_k(
            retrieved_ids=["A", "B", "C"],
            relevant_ids={"A", "B"},
        )
        self.assertEqual(ap_perf, 1.0)

        # Duplicate ID in retrieved list must not inflate hits or rank
        ap_dup = calculate_average_precision_at_k(
            retrieved_ids=["A", "A", "C", "B"],
            relevant_ids={"A", "B"},
        )
        self.assertEqual(ap_dup, ap)

        # Empty ground truth: undefined (None)
        self.assertIsNone(calculate_average_precision_at_k(["A", "B"], []))

        # Empty retrieval with relevant items present: 0.0
        self.assertEqual(calculate_average_precision_at_k([], ["A", "B"]), 0.0)

    def test_03_context_recall_calculations(self):
        """Tests Context Recall calculations and empty relevance handling."""
        # 2 out of 3 retrieved -> 2/3 = 0.6667
        rec = calculate_context_recall(
            retrieved_ids=["A", "B"],
            relevant_ids=["A", "B", "C"],
        )
        self.assertAlmostEqual(rec, 0.6667, places=3)

        # Perfect recall
        self.assertEqual(calculate_context_recall(["A", "B", "C", "D"], ["A", "B"]), 1.0)

        # Zero retrieved
        self.assertEqual(calculate_context_recall([], ["A", "B"]), 0.0)

        # Empty ground truth is undefined
        self.assertIsNone(calculate_context_recall(["A"], []))

    def test_04_entity_id_grounding_verification(self):
        """Tests exact entity-ID verification, allowed_ids membership, and out-of-range abstention."""
        # Exact match inside allowed_ids
        self.assertTrue(verify_entity_id_grounding(101, "101", allowed_ids={101, 102}))
        # ID mismatch
        self.assertFalse(verify_entity_id_grounding(101, 102, allowed_ids={101, 102}))
        # Not in allowed_ids
        self.assertFalse(verify_entity_id_grounding(101, 101, allowed_ids={102, 103}))
        # Abstention expected (None) and observed (None)
        self.assertTrue(verify_entity_id_grounding(None, None))
        # Guessing entity when abstention was expected
        self.assertFalse(verify_entity_id_grounding(101, None))

    def test_05_deterministic_grounding_facts_and_forbidden_checks(self):
        """Tests keyword/attribute isolation and forbidden-fact detection."""
        evidence_text = "NexusTech Solutions offers 25 LPA for Full Stack Engineer. Culture details are not provided."

        # Pass: expected facts present, forbidden facts absent
        chk1 = check_deterministic_grounding_facts(
            text=evidence_text,
            expected_facts=["25 LPA", "NexusTech Solutions"],
            forbidden_facts=["50 LPA", "OldTech Legacy"],
        )
        self.assertTrue(chk1["passed"])
        self.assertEqual(chk1["missing_expected_facts"], [])
        self.assertEqual(chk1["found_forbidden_facts"], [])

        # Fail: missing expected fact
        chk2 = check_deterministic_grounding_facts(
            text=evidence_text,
            expected_facts=["100 LPA"],
        )
        self.assertFalse(chk2["passed"])
        self.assertIn("100 LPA", chk2["missing_expected_facts"])

        # Fail: contains forbidden leaked fact
        chk3 = check_deterministic_grounding_facts(
            text=evidence_text,
            forbidden_facts=["NexusTech"],
        )
        self.assertFalse(chk3["passed"])
        self.assertIn("NexusTech", chk3["found_forbidden_facts"])

    def test_06_synthetic_dataset_benchmark_execution(self):
        """
        Executes the benchmark harness across all 10 synthetic test cases.
        Computes precision, recall, grounding accuracy, and verifies aggregation.
        """
        precision_scores = []
        recall_scores = []
        grounding_results = []
        case_summary = []

        for case in self.dataset["cases"]:
            cid = case["case_id"]
            metrics = case["evaluated_metrics"]
            res = {"case_id": cid, "status": "PASS"}

            # Context Precision
            if "context_precision" in metrics:
                ap = calculate_average_precision_at_k(
                    retrieved_ids=case["retrieved_ids"],
                    relevant_ids=case["expected_relevant_ids"],
                )
                if ap is not None:
                    precision_scores.append(ap)
                res["precision"] = ap

            # Context Recall
            if "context_recall" in metrics:
                rec = calculate_context_recall(
                    retrieved_ids=case["retrieved_ids"],
                    relevant_ids=case["expected_relevant_ids"],
                )
                if rec is not None:
                    recall_scores.append(rec)
                res["recall"] = rec

            # Entity-ID Grounding
            if "entity_id_grounding" in metrics:
                # In synthetic labeled fixture, retrieved_ids represents the resolved pool
                resolved_id = case["retrieved_ids"][0] if case["retrieved_ids"] else None
                is_grounded = verify_entity_id_grounding(
                    actual_selected_id=resolved_id,
                    expected_selected_id=case["expected_selected_id"],
                )
                grounding_results.append(1.0 if is_grounded else 0.0)
                res["grounding"] = 1.0 if is_grounded else 0.0
                if not is_grounded:
                    res["status"] = "FAIL"

            case_summary.append(res)

        mean_precision = aggregate_metric_scores(precision_scores)
        mean_recall = aggregate_metric_scores(recall_scores)
        mean_grounding = aggregate_metric_scores(grounding_results)

        # Assert all 10 benchmark cases passed deterministic evaluation
        for s in case_summary:
            self.assertEqual(s["status"], "PASS", f"Case {s['case_id']} failed benchmark!")

        # High synthetic baseline verification
        self.assertIsNotNone(mean_precision)
        self.assertGreaterEqual(mean_precision, 0.85)

        self.assertIsNotNone(mean_recall)
        self.assertGreaterEqual(mean_recall, 0.80)

        self.assertIsNotNone(mean_grounding)
        self.assertEqual(mean_grounding, 1.0)


if __name__ == "__main__":
    unittest.main()
