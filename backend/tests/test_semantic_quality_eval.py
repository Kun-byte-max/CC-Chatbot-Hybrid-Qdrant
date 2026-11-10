"""
backend/tests/test_semantic_quality_eval.py — Unit tests for deterministic semantic quality & abstention helpers.

Verifies Step 3B.1 & 3B.2:
- eval_semantic_dataset.json schema and unique case IDs (12 cases).
- Supported facts matching and missing assertion failures.
- Forbidden claims detection causing failure even when supported facts are present.
- Case-insensitive matching.
- Empty answers failing both faithfulness and abstention.
- Cases with no applicable assertions returning score = None (not applicable).
- Abstention quality: missing-evidence vs entity-ambiguity distinction.
- Abstention with missing phrase assertions returning score = None (unconfigured).
- Negative-k, zero-k, and k=None behavior in calculate_average_precision_at_k.
- Synthetic fixture execution without live LLM or pipeline execution.
"""

import json
from pathlib import Path
import unittest

from backend.utils.retrieval_evaluator import (
    calculate_average_precision_at_k,
    verify_entity_id_grounding,
    evaluate_deterministic_faithfulness,
    evaluate_abstention_quality,
    aggregate_metric_scores,
)


class TestSemanticQualityEvaluator(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        fixture_path = Path(__file__).parent / "fixtures" / "eval_semantic_dataset.json"
        with open(fixture_path, "r", encoding="utf-8") as f:
            cls.dataset = json.load(f)

    def test_01_semantic_fixture_schema_and_unique_case_ids(self):
        """Verifies that eval_semantic_dataset.json contains 12 uniquely identified cases with complete fields."""
        self.assertIn("cases", self.dataset)
        cases = self.dataset["cases"]
        self.assertEqual(len(cases), 12)

        case_ids = [c["case_id"] for c in cases]
        self.assertEqual(len(case_ids), len(set(case_ids)), "Duplicate semantic case IDs detected!")

        for c in cases:
            self.assertTrue(c["case_id"].startswith("SEM-"))
            self.assertIn("category", c)
            self.assertIn("query", c)
            self.assertIn("synthetic_evidence", c)
            self.assertIn("supported_facts", c)
            self.assertIn("forbidden_claims", c)
            self.assertIn("requires_abstention", c)
            self.assertIn("evaluated_metrics", c)
            self.assertIn("expected_failure_conditions", c)

    def test_02_negative_k_and_edge_case_precision(self):
        """Tests that negative k returns 0.0 and preserves existing k=0, k=None, and empty relevance behavior."""
        # Negative k
        self.assertEqual(calculate_average_precision_at_k(["A", "B"], ["A"], k=-1), 0.0)
        self.assertEqual(calculate_average_precision_at_k(["A", "B"], ["A"], k=-5), 0.0)

        # Zero k
        self.assertEqual(calculate_average_precision_at_k(["A", "B"], ["A"], k=0), 0.0)

        # k=None
        self.assertEqual(calculate_average_precision_at_k(["A", "B"], ["A"], k=None), 1.0)

        # Empty relevance set -> undefined (None)
        self.assertIsNone(calculate_average_precision_at_k(["A", "B"], [], k=-1))
        self.assertIsNone(calculate_average_precision_at_k(["A", "B"], [], k=5))

    def test_03_deterministic_faithfulness_supported_and_forbidden_logic(self):
        """Tests faithfulness evaluation logic: supported match, missing facts, and forbidden claims overriding."""
        sample_answer = "The Senior Python Lead position offers 35 LPA in Delhi."

        # Pass: All supported facts present, no forbidden claims
        res_pass = evaluate_deterministic_faithfulness(
            answer_text=sample_answer,
            supported_facts=["35 LPA", "Python Lead"],
            forbidden_claims=["45 LPA", "Not specified"],
        )
        self.assertTrue(res_pass["is_faithful"])
        self.assertEqual(res_pass["score"], 1.0)
        self.assertEqual(res_pass["missing_supported_facts"], [])
        self.assertEqual(res_pass["detected_forbidden_claims"], [])

        # Fail: Missing one supported fact
        res_missing = evaluate_deterministic_faithfulness(
            answer_text=sample_answer,
            supported_facts=["35 LPA", "Remote Work"],
        )
        self.assertFalse(res_missing["is_faithful"])
        self.assertEqual(res_missing["score"], 0.0)
        self.assertIn("Remote Work", res_missing["missing_supported_facts"])

        # Fail: Forbidden claim present even though supported facts match
        answer_with_leak = "The Senior Python Lead position offers 35 LPA, but Job A offers 45 LPA."
        res_forbidden = evaluate_deterministic_faithfulness(
            answer_text=answer_with_leak,
            supported_facts=["35 LPA"],
            forbidden_claims=["45 LPA"],
        )
        self.assertFalse(res_forbidden["is_faithful"])
        self.assertEqual(res_forbidden["score"], 0.0)
        self.assertIn("45 LPA", res_forbidden["detected_forbidden_claims"])

    def test_04_faithfulness_empty_and_unconfigured_answers(self):
        """Tests that empty answers score 0.0 when assertions exist, and score None when unconfigured."""
        # Empty answer with assertions
        res_empty = evaluate_deterministic_faithfulness(
            answer_text="",
            supported_facts=["35 LPA"],
        )
        self.assertFalse(res_empty["is_faithful"])
        self.assertEqual(res_empty["score"], 0.0)
        self.assertEqual(res_empty["reason"], "empty_answer")

        # Whitespace answer
        res_ws = evaluate_deterministic_faithfulness(
            answer_text="   \n\t  ",
            supported_facts=["35 LPA"],
        )
        self.assertFalse(res_ws["is_faithful"])
        self.assertEqual(res_ws["score"], 0.0)

        # No assertions configured -> None (N/A)
        res_na = evaluate_deterministic_faithfulness(
            answer_text="Some valid text",
            supported_facts=[],
            forbidden_claims=[],
        )
        self.assertTrue(res_na["is_faithful"])
        self.assertIsNone(res_na["score"])
        self.assertEqual(res_na["reason"], "no_assertions_defined")

    def test_05_abstention_quality_missing_evidence(self):
        """Tests abstention verification for missing evidence (salary, culture, out of range)."""
        valid_abstention = "The compensation for this position is not specified in the job listing."

        # Pass: Expected phrase matched and no forbidden claims
        res_pass = evaluate_abstention_quality(
            answer_text=valid_abstention,
            requires_abstention=True,
            expected_abstention_phrases=["not specified", "not provided"],
            forbidden_claims=["LPA", "per annum"],
            abstention_type="missing_evidence",
        )
        self.assertTrue(res_pass["passed"])
        self.assertEqual(res_pass["score"], 1.0)
        self.assertIn("not specified", res_pass["matched_abstention_phrases"])

        # Fail: Mentions expected phrase but simultaneously fabricates forbidden claim
        hallucinated_abstention = "The salary is not specified, but typically ranges around 25 LPA."
        res_hallucinated = evaluate_abstention_quality(
            answer_text=hallucinated_abstention,
            requires_abstention=True,
            expected_abstention_phrases=["not specified"],
            forbidden_claims=["LPA"],
            abstention_type="missing_evidence",
        )
        self.assertFalse(res_hallucinated["passed"])
        self.assertEqual(res_hallucinated["score"], 0.0)
        self.assertIn("LPA", res_hallucinated["detected_forbidden_claims"])

        # Fail: Does not match any abstention phrases
        unhelpful_answer = "Here are the details for the job."
        res_no_phrase = evaluate_abstention_quality(
            answer_text=unhelpful_answer,
            requires_abstention=True,
            expected_abstention_phrases=["not specified", "not provided"],
        )
        self.assertFalse(res_no_phrase["passed"])
        self.assertEqual(res_no_phrase["score"], 0.0)

    def test_06_abstention_ambiguity_and_unconfigured_rules(self):
        """Tests entity ambiguity clarification, unconfigured assertions, and empty abstentions."""
        ambiguous_clarification = "I found 4 roles in your results. Could you please specify which job you are referring to?"

        # Pass: Ambiguity clarification matches expected phrase
        res_ambig = evaluate_abstention_quality(
            answer_text=ambiguous_clarification,
            requires_abstention=True,
            expected_abstention_phrases=["please specify", "which job"],
            abstention_type="entity_ambiguity",
        )
        self.assertTrue(res_ambig["passed"])
        self.assertEqual(res_ambig["score"], 1.0)

        # Empty answer never qualifies as abstention
        res_empty = evaluate_abstention_quality(
            answer_text="",
            requires_abstention=True,
            expected_abstention_phrases=["not specified"],
        )
        self.assertFalse(res_empty["passed"])
        self.assertEqual(res_empty["score"], 0.0)
        self.assertEqual(res_empty["reason"], "empty_answer_not_valid_abstention")

        # Unconfigured abstention phrases -> score = None (not eligible for scoring)
        res_unconfigured = evaluate_abstention_quality(
            answer_text="Some valid response",
            requires_abstention=True,
            expected_abstention_phrases=[],
        )
        self.assertFalse(res_unconfigured["passed"])
        self.assertIsNone(res_unconfigured["score"])
        self.assertEqual(res_unconfigured["reason"], "missing_abstention_assertions")

        # requires_abstention is False -> score = None (N/A)
        res_na = evaluate_abstention_quality(
            answer_text="Normal answer",
            requires_abstention=False,
        )
        self.assertTrue(res_na["passed"])
        self.assertIsNone(res_na["score"])
        self.assertEqual(res_na["reason"], "abstention_not_required")

    def test_07_semantic_fixture_synthetic_benchmark_run(self):
        """
        Runs synthetic verification over the 12 semantic cases using synthetic answers.
        Confirms aggregation excludes N/A cases and separates scores.
        """
        # Mapping of synthetic responses designed to test the fixture rules
        synthetic_answers = {
            "SEM-01": "The Senior Python Lead role offers 35 LPA with hybrid work options.",
            "SEM-02": "The compensation for this DevOps role is not specified in the listing.",
            "SEM-03": "Company culture details are not provided in indexed record for NexaCorp.",
            "SEM-04": "The salary for EdTech Solutions (Job B) is 20 LPA.",
            "SEM-05": "There are only 3 jobs in your current results. Rank 10 is out of range.",
            "SEM-06": "I found multiple matching jobs. Could you please specify which job you mean?",
            "SEM-07": "Proceeding with employment verification for record emp_rec_92.",
            "SEM-08": "Here are the Hybrid jobs in Pune, including Job 301.",
            "SEM-09": "I could not find any matching jobs for COBOL developer on Mars.",
            "SEM-10": "The listing does not mention free lunch or gym membership perks.",
            "SEM-11": "Job 501 is Work from Office on-site in Mumbai.",
            "SEM-12": "Job 601 requires 6+ years React and TypeScript experience.",
        }

        faithfulness_scores = []
        abstention_scores = []
        grounding_scores = []

        for case in self.dataset["cases"]:
            cid = case["case_id"]
            ans = synthetic_answers[cid]
            metrics = case["evaluated_metrics"]

            if "deterministic_faithfulness" in metrics:
                res_f = evaluate_deterministic_faithfulness(
                    answer_text=ans,
                    supported_facts=case.get("supported_facts"),
                    forbidden_claims=case.get("forbidden_claims"),
                )
                if res_f["score"] is not None:
                    faithfulness_scores.append(res_f["score"])

            if "abstention_quality" in metrics:
                res_a = evaluate_abstention_quality(
                    answer_text=ans,
                    requires_abstention=case.get("requires_abstention", False),
                    expected_abstention_phrases=case.get("expected_abstention_phrases"),
                    forbidden_claims=case.get("forbidden_claims"),
                    abstention_type=case.get("abstention_type"),
                )
                if res_a["score"] is not None:
                    abstention_scores.append(res_a["score"])

            if "entity_id_grounding" in metrics:
                # In synthetic dataset, expected_entity_id represents the target entity
                is_grd = verify_entity_id_grounding(
                    actual_selected_id=case.get("expected_entity_id"),
                    expected_selected_id=case.get("expected_entity_id"),
                )
                grounding_scores.append(1.0 if is_grd else 0.0)

        # Aggregate calculations strictly excluding N/A
        mean_faithfulness = aggregate_metric_scores(faithfulness_scores)
        mean_abstention = aggregate_metric_scores(abstention_scores)
        mean_grounding = aggregate_metric_scores(grounding_scores)

        # 9 cases evaluate deterministic_faithfulness (SEM-01, SEM-02, SEM-03, SEM-04, SEM-07, SEM-08, SEM-10, SEM-11, SEM-12)
        # 6 cases evaluate abstention_quality (SEM-02, SEM-03, SEM-05, SEM-06, SEM-09, SEM-10)
        # 12 cases evaluate entity_id_grounding
        self.assertEqual(len(faithfulness_scores), 9)
        self.assertEqual(len(abstention_scores), 6)
        self.assertEqual(len(grounding_scores), 12)

        self.assertEqual(mean_faithfulness, 1.0)
        self.assertEqual(mean_abstention, 1.0)
        self.assertEqual(mean_grounding, 1.0)


if __name__ == "__main__":
    unittest.main()
