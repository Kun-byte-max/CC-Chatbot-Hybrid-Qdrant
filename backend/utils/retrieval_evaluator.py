"""
backend/utils/retrieval_evaluator.py — Pure, deterministic metric helpers for CollarCheck retrieval quality evaluation.

Implements Step 3A:
- Context precision: Average Precision at K (AP@K) with deduplication.
- Context recall: Unique retrieved relevant / ground-truth relevant.
- Entity-ID grounding: Exact ID comparison, allowed-IDs validation, and out-of-range abstention.
- Deterministic answer grounding: Attribute isolation and forbidden facts detection.
- Aggregation helpers excluding undefined metrics.

Zero network calls, zero external dependencies, side-effect free.
"""

from typing import List, Set, Dict, Any, Optional, Union


def calculate_average_precision_at_k(
    retrieved_ids: List[Union[str, int]],
    relevant_ids: Union[List[Union[str, int]], Set[Union[str, int]]],
    k: Optional[int] = None,
) -> Optional[float]:
    """
    Computes Average Precision at K (AP@K) over a ranked list of retrieved document/entity IDs.

    Definition:
      AP@K = sum_{i=1..K} (P@i * rel(i)) / min(|Relevant|, K)
      where rel(i) is 1 if retrieved_ids[i] is in relevant_ids, else 0.
      P@i is the precision at rank i (number of relevant items observed up to i divided by i).

    Rules:
    - Duplicate IDs in retrieved_ids are ignored after their first appearance to avoid inflation.
    - If relevant_ids is empty, AP@K is undefined and returns None (must be excluded from MAP).
    - If retrieved_ids is empty but relevant_ids is non-empty, AP@K is 0.0.
    """
    if not relevant_ids:
        return None

    if k is not None and k <= 0:
        return 0.0

    clean_relevant: Set[str] = {str(r).strip() for r in relevant_ids if str(r).strip()}
    if not clean_relevant:
        return None

    # Deduplicate retrieved IDs preserving rank order
    seen: Set[str] = set()
    deduped_retrieved: List[str] = []
    for rid in retrieved_ids:
        s_id = str(rid).strip()
        if s_id and s_id not in seen:
            seen.add(s_id)
            deduped_retrieved.append(s_id)

    max_k = len(deduped_retrieved) if k is None else min(k, len(deduped_retrieved))
    denominator = min(len(clean_relevant), max_k) if k else len(clean_relevant)
    if denominator == 0:
        return 0.0

    hits = 0
    cumulative_precision = 0.0

    for i in range(max_k):
        doc_id = deduped_retrieved[i]
        if doc_id in clean_relevant:
            hits += 1
            precision_at_i = hits / (i + 1)
            cumulative_precision += precision_at_i

    return round(cumulative_precision / denominator, 4)


def calculate_context_recall(
    retrieved_ids: List[Union[str, int]],
    relevant_ids: Union[List[Union[str, int]], Set[Union[str, int]]],
) -> Optional[float]:
    """
    Computes Context Recall:
      Recall = |Retrieved ∩ Relevant| / |Relevant|

    Rules:
    - If relevant_ids is empty, recall is undefined and returns None.
    - If retrieved_ids is empty and relevant_ids is non-empty, recall is 0.0.
    """
    if not relevant_ids:
        return None

    clean_relevant: Set[str] = {str(r).strip() for r in relevant_ids if str(r).strip()}
    if not clean_relevant:
        return None

    clean_retrieved: Set[str] = {str(r).strip() for r in retrieved_ids if str(r).strip()}
    hits = clean_retrieved.intersection(clean_relevant)

    return round(len(hits) / len(clean_relevant), 4)


def verify_entity_id_grounding(
    actual_selected_id: Optional[Union[str, int]],
    expected_selected_id: Optional[Union[str, int]],
    allowed_ids: Optional[Set[Union[str, int]]] = None,
) -> bool:
    """
    Verifies that the resolved/selected entity ID matches the exact ground-truth expectation.

    Rules:
    - If an entity was expected, actual_selected_id must equal expected_selected_id (normalized string).
    - If allowed_ids is supplied, the selected ID must also be a member of allowed_ids.
    - If expected_selected_id is None (e.g. out-of-range, clarification), actual_selected_id must be None.
    """
    clean_actual = str(actual_selected_id).strip() if actual_selected_id is not None else None
    clean_expected = str(expected_selected_id).strip() if expected_selected_id is not None else None

    if clean_expected is None:
        return clean_actual is None

    if clean_actual != clean_expected:
        return False

    if allowed_ids is not None:
        clean_allowed = {str(aid).strip() for aid in allowed_ids}
        return clean_actual in clean_allowed

    return True


def check_deterministic_grounding_facts(
    text: str,
    expected_facts: Optional[List[str]] = None,
    forbidden_facts: Optional[List[str]] = None,
    case_insensitive: bool = True,
) -> Dict[str, Any]:
    """
    Performs narrow, deterministic fact and anti-fact verification on evidence/answer text.

    Returns:
      {
        "passed": bool,
        "missing_expected_facts": List[str],
        "found_forbidden_facts": List[str],
      }
    """
    target_text = text or ""
    if case_insensitive:
        target_text = target_text.lower()

    missing: List[str] = []
    if expected_facts:
        for f in expected_facts:
            chk = f.lower() if case_insensitive else f
            if chk not in target_text:
                missing.append(f)

    forbidden_found: List[str] = []
    if forbidden_facts:
        for f in forbidden_facts:
            chk = f.lower() if case_insensitive else f
            if chk in target_text:
                forbidden_found.append(f)

    passed = (len(missing) == 0) and (len(forbidden_found) == 0)
    return {
        "passed": passed,
        "missing_expected_facts": missing,
        "found_forbidden_facts": forbidden_found,
    }


def aggregate_metric_scores(scores: List[Optional[float]]) -> Optional[float]:
    """
    Computes arithmetic mean over metric scores, strictly excluding undefined (None) entries.
    Returns None if no defined scores exist.
    """
    valid_scores = [s for s in scores if s is not None]
    if not valid_scores:
        return None
    return round(sum(valid_scores) / len(valid_scores), 4)


def evaluate_deterministic_faithfulness(
    answer_text: Optional[str],
    supported_facts: Optional[List[str]] = None,
    forbidden_claims: Optional[List[str]] = None,
    case_insensitive: bool = True,
) -> Dict[str, Any]:
    """
    Evaluates an answer against explicit supported-fact expectations and forbidden-claim expectations.

    Note: This is a phrase-based regression assertion proxy, NOT general semantic entailment.

    Rules:
    - Empty/whitespace answer with assertions defined -> fails (score = 0.0).
    - If neither supported_facts nor forbidden_claims are provided, returns score = None (not applicable).
    - Forbidden claims ALWAYS fail the evaluation (score = 0.0), even if supported facts match.
    - If supported_facts are defined, all must appear in answer_text.
    - Returns structured outcome:
        {
          "is_faithful": bool,
          "score": Optional[float],  # 1.0, 0.0, or None (if N/A)
          "missing_supported_facts": List[str],
          "detected_forbidden_claims": List[str],
          "reason": str,
        }
    """
    clean_text = (answer_text or "").strip()
    s_facts = [f.strip() for f in (supported_facts or []) if f.strip()]
    f_claims = [c.strip() for c in (forbidden_claims or []) if c.strip()]

    if not s_facts and not f_claims:
        return {
            "is_faithful": True,
            "score": None,
            "missing_supported_facts": [],
            "detected_forbidden_claims": [],
            "reason": "no_assertions_defined",
        }

    if not clean_text:
        return {
            "is_faithful": False,
            "score": 0.0,
            "missing_supported_facts": s_facts,
            "detected_forbidden_claims": [],
            "reason": "empty_answer",
        }

    target = clean_text.lower() if case_insensitive else clean_text

    missing = []
    for fact in s_facts:
        f_cmp = fact.lower() if case_insensitive else fact
        if f_cmp not in target:
            missing.append(fact)

    forbidden_found = []
    for claim in f_claims:
        c_cmp = claim.lower() if case_insensitive else claim
        if c_cmp in target:
            forbidden_found.append(claim)

    if forbidden_found:
        return {
            "is_faithful": False,
            "score": 0.0,
            "missing_supported_facts": missing,
            "detected_forbidden_claims": forbidden_found,
            "reason": "forbidden_claims_detected",
        }

    if missing:
        return {
            "is_faithful": False,
            "score": 0.0,
            "missing_supported_facts": missing,
            "detected_forbidden_claims": [],
            "reason": "missing_supported_facts",
        }

    return {
        "is_faithful": True,
        "score": 1.0,
        "missing_supported_facts": [],
        "detected_forbidden_claims": [],
        "reason": "all_assertions_passed",
    }


def evaluate_abstention_quality(
    answer_text: Optional[str],
    requires_abstention: bool,
    expected_abstention_phrases: Optional[List[str]] = None,
    forbidden_claims: Optional[List[str]] = None,
    abstention_type: Optional[str] = None,  # "missing_evidence" | "entity_ambiguity" | None
    case_insensitive: bool = True,
) -> Dict[str, Any]:
    """
    Evaluates whether the answer properly abstains or requests clarification when evidence is missing.

    Rules:
    - If requires_abstention is False: returns score = None (not applicable).
    - If requires_abstention is True:
        - If expected_abstention_phrases is empty or None: returns score = None, reason = "missing_abstention_assertions".
        - An empty/whitespace answer NEVER qualifies as a valid abstention (score = 0.0).
        - If any forbidden_claims are detected (e.g. fabricated salary): fails (score = 0.0).
        - At least ONE expected abstention/clarification phrase must appear in the answer.
    """
    if not requires_abstention:
        return {
            "passed": True,
            "score": None,
            "detected_forbidden_claims": [],
            "matched_abstention_phrases": [],
            "reason": "abstention_not_required",
        }

    phrases = [p.strip() for p in (expected_abstention_phrases or []) if p.strip()]
    if not phrases:
        return {
            "passed": False,
            "score": None,
            "detected_forbidden_claims": [],
            "matched_abstention_phrases": [],
            "reason": "missing_abstention_assertions",
        }

    clean_text = (answer_text or "").strip()
    if not clean_text:
        return {
            "passed": False,
            "score": 0.0,
            "detected_forbidden_claims": [],
            "matched_abstention_phrases": [],
            "reason": "empty_answer_not_valid_abstention",
        }

    target = clean_text.lower() if case_insensitive else clean_text

    # Check forbidden claims first (e.g., claiming a salary even while saying "not specified")
    f_claims = [c.strip() for c in (forbidden_claims or []) if c.strip()]
    forbidden_found = []
    for claim in f_claims:
        c_cmp = claim.lower() if case_insensitive else claim
        if c_cmp in target:
            forbidden_found.append(claim)

    if forbidden_found:
        return {
            "passed": False,
            "score": 0.0,
            "detected_forbidden_claims": forbidden_found,
            "matched_abstention_phrases": [],
            "reason": "forbidden_claims_detected",
        }

    matched = []
    for phrase in phrases:
        p_cmp = phrase.lower() if case_insensitive else phrase
        if p_cmp in target:
            matched.append(phrase)

    if not matched:
        return {
            "passed": False,
            "score": 0.0,
            "detected_forbidden_claims": [],
            "matched_abstention_phrases": [],
            "reason": "no_abstention_phrases_matched",
        }

    return {
        "passed": True,
        "score": 1.0,
        "detected_forbidden_claims": [],
        "matched_abstention_phrases": matched,
        "reason": "valid_abstention_confirmed",
    }


def normalize_evaluation_text(text: Optional[str]) -> str:
    """
    Normalizes text for deterministic relevance evaluation:
    - Strips whitespace.
    - Converts to lowercase.
    - Replaces punctuation with single spaces and collapses multiple spaces.
    """
    import re
    if not text:
        return ""
    clean = re.sub(r"[^\w\s]", " ", str(text).lower())
    return " ".join(clean.split())


# Known intent signal keywords for deterministic relevance intent matching
INTENT_SIGNAL_MAP: Dict[str, List[str]] = {
    "provide_salary": ["lpa", "salary", "compensation", "package", "per annum", "fixed"],
    "abstain_missing_evidence": ["not specified", "not provided", "not disclosed", "not listed", "not mention", "no information"],
    "explain_out_of_range": ["only", "out of range", "don't have", "does not exist", "not available"],
    "request_clarification": ["which", "please specify", "clarify", "which role", "which job"],
    "confirm_employment_selection": ["employment verification", "emp_rec", "record", "option", "selected"],
    "provide_filtered_results": ["hybrid", "filter", "pune", "delhi", "bangalore", "remote", "office"],
    "explain_no_results": ["no matching", "could not find", "no jobs found", "adjusting"],
    "provide_work_mode": ["office", "remote", "hybrid", "work from home", "work from office", "on site"],
    "provide_job_requirements": ["years", "requirements", "experience", "react", "python", "skills"],
}


def evaluate_response_relevance(
    answer_text: Optional[str],
    expected_intent: Optional[str] = None,
    required_elements: Optional[List[str]] = None,
    query: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Evaluates whether an answer addresses the case's intended goal and required elements.

    Conceptually distinct from faithfulness:
    - Faithfulness measures factual truth against evidence (forbidden claims fail).
    - Relevance measures whether the response actually answers the query's goal
      (e.g. providing salary vs giving culture; asking for clarification vs guessing).

    Scoring model:
    - Empty or whitespace answer: score = 0.0 (reason: empty_answer).
    - If neither expected_intent nor required_elements is defined:
      returns score = None, reason = "missing_relevance_contract" (unevaluable).
    - Intent Match (50% weight): Checks if normalized answer contains signals for expected_intent.
      If expected_intent is None, intent match defaults to 1.0 (unconstrained).
    - Elements Match (50% weight): Fraction of required_elements found in answer.
      If required_elements is empty, elements match defaults to 1.0.
    - Final score = round(0.5 * intent_score + 0.5 * element_score, 4).
    - Returns structured outcome:
        {
          "is_relevant": bool,
          "score": Optional[float],
          "matched_intent": bool,
          "matched_elements": List[str],
          "missing_elements": List[str],
          "reason": str,
        }
    """
    clean_raw = (answer_text or "").strip()
    if not clean_raw:
        return {
            "is_relevant": False,
            "score": 0.0,
            "matched_intent": False,
            "matched_elements": [],
            "missing_elements": [str(e) for e in (required_elements or [])],
            "reason": "empty_answer",
        }

    norm_ans = normalize_evaluation_text(clean_raw)
    req_elems = [str(e).strip() for e in (required_elements or []) if str(e).strip()]

    if not expected_intent and not req_elems:
        return {
            "is_relevant": False,
            "score": None,
            "matched_intent": False,
            "matched_elements": [],
            "missing_elements": [],
            "reason": "missing_relevance_contract",
        }

    # 1. Intent Match
    # Requirement: "Do not award relevance merely because the response repeats query words."
    has_intent_contract = bool(expected_intent)
    intent_matched = False
    if has_intent_contract:
        clean_intent = expected_intent.strip().lower()
        signals = INTENT_SIGNAL_MAP.get(clean_intent, [clean_intent])
        
        # If query is provided, check if the signal is merely repeating query terms
        norm_query = normalize_evaluation_text(query) if query else ""
        query_words = set(norm_query.split()) if norm_query else set()
        
        # A signal is valid for intent matching if it matches the answer
        # and is either not entirely composed of words from the query or distinct intent signals
        matched_any = False
        for sig in signals:
            norm_sig = normalize_evaluation_text(sig)
            if not norm_sig:
                continue
            padded_ans = f" {norm_ans} "
            padded_sig = f" {norm_sig} "
            if padded_sig in padded_ans:
                sig_words = set(norm_sig.split())
                if query_words and sig_words.issubset(query_words):
                    continue
                matched_any = True
                break
        intent_matched = matched_any

    intent_score = 1.0 if intent_matched else 0.0

    # 2. Required Elements Match
    has_element_contract = bool(req_elems)
    matched_elems: List[str] = []
    missing_elems: List[str] = []
    if has_element_contract:
        for elem in req_elems:
            norm_elem = normalize_evaluation_text(elem)
            if norm_elem in norm_ans:
                matched_elems.append(elem)
            else:
                missing_elems.append(elem)
        element_score = len(matched_elems) / len(req_elems)
    else:
        element_score = 0.0

    # Composite scoring:
    # If both dimensions are contracted: 50% intent + 50% element.
    # If only intent is contracted: 100% intent_score.
    # If only elements are contracted: 100% element_score.
    if has_intent_contract and has_element_contract:
        composite_score = round((0.5 * intent_score) + (0.5 * element_score), 4)
    elif has_intent_contract:
        composite_score = round(intent_score, 4)
    else:
        composite_score = round(element_score, 4)

    is_relevant = composite_score >= 0.90

    reason = "relevant_intent_and_elements_matched"
    if has_intent_contract and not intent_matched and not matched_elems:
        reason = "intent_and_elements_mismatched"
    elif has_intent_contract and not intent_matched:
        reason = "intent_mismatched"
    elif missing_elems:
        reason = "partial_elements_missing"

    return {
        "is_relevant": is_relevant,
        "score": composite_score,
        "matched_intent": intent_matched,
        "matched_elements": matched_elems,
        "missing_elements": missing_elems,
        "reason": reason,
    }


# =========================================================================
# Stage 4.2 Unified Evaluation Report Contracts & Static Coverage Mapping
# =========================================================================

KNOWN_PIPELINE_COVERAGE_MAP: List[Dict[str, Any]] = [
    {
        "case_id": "SEM-01",
        "category": "straightforward_salary_query",
        "test_method": "test_01_pipeline_sem01_supported_salary_and_grounding",
        "route_tested": "/chat",
        "http_routing": True,
        "prompt_boundary_inspection": False,
        "structured_card_grounding": True,
        "evaluator_counterfactual": False,
        "production_contradiction_blocking": False,
    },
    {
        "case_id": "SEM-02",
        "category": "missing_salary_abstention",
        "test_method": "test_02_pipeline_sem02_missing_salary_abstention_and_negative_check",
        "route_tested": "/chat",
        "http_routing": True,
        "prompt_boundary_inspection": False,
        "structured_card_grounding": True,
        "evaluator_counterfactual": True,
        "production_contradiction_blocking": False,
    },
    {
        "case_id": "SEM-03",
        "category": "missing_culture_abstention",
        "test_method": "test_11_pipeline_sem03_missing_culture_abstention",
        "route_tested": "/chat",
        "http_routing": True,
        "prompt_boundary_inspection": True,
        "structured_card_grounding": True,
        "evaluator_counterfactual": True,
        "production_contradiction_blocking": False,
    },
    {
        "case_id": "SEM-04",
        "category": "cross_entity_salary_leakage",
        "test_method": "test_03_pipeline_sem04_cross_entity_salary_isolation",
        "route_tested": "/chat",
        "http_routing": True,
        "prompt_boundary_inspection": False,
        "structured_card_grounding": True,
        "evaluator_counterfactual": False,
        "production_contradiction_blocking": False,
    },
    {
        "case_id": "SEM-05",
        "category": "out_of_range_ordinal",
        "test_method": "test_04_pipeline_sem05_out_of_range_ordinal_handling",
        "route_tested": "/chat",
        "http_routing": True,
        "prompt_boundary_inspection": False,
        "structured_card_grounding": True,
        "evaluator_counterfactual": False,
        "production_contradiction_blocking": False,
    },
    {
        "case_id": "SEM-06",
        "category": "ambiguous_ordinal_reference",
        "test_method": "test_05_pipeline_sem06_ambiguous_entity_clarification",
        "route_tested": "/chat",
        "http_routing": True,
        "prompt_boundary_inspection": False,
        "structured_card_grounding": True,
        "evaluator_counterfactual": False,
        "production_contradiction_blocking": False,
    },
    {
        "case_id": "SEM-07",
        "category": "employment_precedence_collision",
        "test_method": "test_06_pipeline_sem07_employment_precedence_collision",
        "route_tested": "Component (QueryRewriter)",
        "http_routing": False,
        "prompt_boundary_inspection": False,
        "structured_card_grounding": True,
        "evaluator_counterfactual": False,
        "production_contradiction_blocking": False,
    },
    {
        "case_id": "SEM-08",
        "category": "multi_turn_filter_refinement",
        "test_method": "test_09_pipeline_sem08_multi_turn_filter_refinement",
        "route_tested": "/chat (Multi-turn)",
        "http_routing": True,
        "prompt_boundary_inspection": False,
        "structured_card_grounding": True,
        "evaluator_counterfactual": False,
        "production_contradiction_blocking": False,
    },
    {
        "case_id": "SEM-09",
        "category": "empty_retrieval_abstention",
        "test_method": "test_07_pipeline_sem09_empty_retrieval_grounding_directive",
        "route_tested": "/chat",
        "http_routing": True,
        "prompt_boundary_inspection": True,
        "structured_card_grounding": True,
        "evaluator_counterfactual": False,
        "production_contradiction_blocking": False,
    },
    {
        "case_id": "SEM-10",
        "category": "unsupported_perk_hallucination",
        "test_method": "test_12_pipeline_sem10_unsupported_perks_abstention",
        "route_tested": "/chat",
        "http_routing": True,
        "prompt_boundary_inspection": True,
        "structured_card_grounding": True,
        "evaluator_counterfactual": True,
        "production_contradiction_blocking": False,
    },
    {
        "case_id": "SEM-11",
        "category": "contradictory_claim_detection",
        "test_method": "test_10_pipeline_sem11_contradictory_claim_detection",
        "route_tested": "/chat",
        "http_routing": True,
        "prompt_boundary_inspection": True,
        "structured_card_grounding": True,
        "evaluator_counterfactual": True,
        "production_contradiction_blocking": False,
    },
    {
        "case_id": "SEM-12",
        "category": "endpoint_streaming_parity",
        "test_method": "test_08_pipeline_sem12_chat_and_stream_parity",
        "route_tested": "/chat & /chat/stream",
        "http_routing": True,
        "prompt_boundary_inspection": False,
        "structured_card_grounding": True,
        "evaluator_counterfactual": False,
        "production_contradiction_blocking": False,
    },
]


def generate_evaluation_summary_report(
    retrieval_fixture: Optional[Dict[str, Any]] = None,
    semantic_fixture: Optional[Dict[str, Any]] = None,
    case_answers: Optional[Dict[str, str]] = None,
    actual_selected_ids: Optional[Dict[str, Any]] = None,
    test_execution_results: Optional[Dict[str, Dict[str, Any]]] = None,
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generates a structured, deterministic summary report across retrieval benchmarks,
    semantic benchmarks, and pipeline regression coverage.

    Strict Design Guarantees:
    - Never converts N/A to zero.
    - Never calls live clocks or time functions internally (timestamp is injected or None).
    - Production quality is strictly labeled "NOT MEASURED".
    - Actual grounding accuracy is labeled "NOT MEASURED" unless actual_selected_ids are supplied.
    - Missing generated answers are explicitly labeled as missing answers, never scored as 0.0.
    """
    retrieval_cases = (retrieval_fixture or {}).get("cases", []) if isinstance(retrieval_fixture, dict) else []
    semantic_cases = (semantic_fixture or {}).get("cases", []) if isinstance(semantic_fixture, dict) else []

    # -------------------------------------------------------------------------
    # Layer A1: Retrieval Fixture Benchmarks
    # -------------------------------------------------------------------------
    prec_scores: List[Optional[float]] = []
    prec_cases: List[Dict[str, Any]] = []
    recall_scores: List[Optional[float]] = []
    recall_cases: List[Dict[str, Any]] = []
    spec_integrity_cases: List[Dict[str, Any]] = []

    for c in retrieval_cases:
        cid = c.get("case_id", "")
        ret_ids = c.get("retrieved_ids", [])
        rel_ids = c.get("expected_relevant_ids", [])
        exp_sel = c.get("expected_selected_id")

        # Precision (AP@5)
        ap = calculate_average_precision_at_k(ret_ids, rel_ids, k=5)
        prec_scores.append(ap)
        prec_cases.append({
            "case_id": cid,
            "score": ap,
            "status": "EVALUATED" if ap is not None else "N/A_EMPTY_RELEVANT",
        })

        # Recall
        rec = calculate_context_recall(ret_ids, rel_ids)
        recall_scores.append(rec)
        recall_cases.append({
            "case_id": cid,
            "score": rec,
            "status": "EVALUATED" if rec is not None else "N/A_EMPTY_RELEVANT",
        })

        # Spec integrity (format validation only, NOT system accuracy)
        is_valid_spec = (exp_sel is None) or (isinstance(exp_sel, (str, int)) and str(exp_sel).strip() != "")
        spec_integrity_cases.append({
            "case_id": cid,
            "expected_selected_id": exp_sel,
            "spec_valid": is_valid_spec,
        })

    # Precision Aggregates
    eligible_prec = [s for s in prec_scores if s is not None]
    mean_prec = aggregate_metric_scores(prec_scores)
    prec_summary = {
        "metric_name": "context_precision_map_at_k",
        "display_name": "Context Precision (MAP@5)",
        "target_threshold": 0.85,
        "mean_score": mean_prec,
        "eligible_cases": len(eligible_prec),
        "total_cases": len(retrieval_cases),
        "na_cases": len(retrieval_cases) - len(eligible_prec),
        "evaluation_scope": "SYNTHETIC_FIXTURE_BENCHMARK",
        "status": (
            "MEETS_SYNTHETIC_TARGET"
            if mean_prec is not None and mean_prec >= 0.85
            else ("BELOW_TARGET" if mean_prec is not None else "N/A_NO_ELIGIBLE_CASES")
        ),
        "per_case_breakdown": prec_cases,
    }

    # Recall Aggregates
    eligible_rec = [s for s in recall_scores if s is not None]
    mean_rec = aggregate_metric_scores(recall_scores)
    rec_summary = {
        "metric_name": "context_recall",
        "display_name": "Context Recall",
        "target_threshold": 0.80,
        "mean_score": mean_rec,
        "eligible_cases": len(eligible_rec),
        "total_cases": len(retrieval_cases),
        "na_cases": len(retrieval_cases) - len(eligible_rec),
        "evaluation_scope": "SYNTHETIC_FIXTURE_BENCHMARK",
        "status": (
            "MEETS_SYNTHETIC_TARGET"
            if mean_rec is not None and mean_rec >= 0.80
            else ("BELOW_TARGET" if mean_rec is not None else "N/A_NO_ELIGIBLE_CASES")
        ),
        "per_case_breakdown": recall_cases,
    }

    # Entity Grounding Summary: Actual vs Spec Integrity
    grounding_scores: List[Optional[float]] = []
    grounding_cases: List[Dict[str, Any]] = []

    if actual_selected_ids is not None:
        for c in retrieval_cases:
            cid = c.get("case_id", "")
            exp_sel = c.get("expected_selected_id")
            if cid in actual_selected_ids:
                act_sel = actual_selected_ids[cid]
                matched = verify_entity_id_grounding(act_sel, exp_sel)
                sc = 1.0 if matched else 0.0
                grounding_scores.append(sc)
                grounding_cases.append({"case_id": cid, "score": sc, "status": "EVALUATED"})
            else:
                grounding_scores.append(None)
                grounding_cases.append({"case_id": cid, "score": None, "status": "N/A_NO_ACTUAL_ID_SUPPLIED"})

        eligible_gr = [s for s in grounding_scores if s is not None]
        mean_gr = aggregate_metric_scores(grounding_scores)
        grounding_summary = {
            "metric_name": "entity_id_grounding",
            "display_name": "Entity-ID Grounding Accuracy",
            "target_threshold": 1.00,
            "mean_score": mean_gr,
            "eligible_cases": len(eligible_gr),
            "total_cases": len(retrieval_cases),
            "na_cases": len(retrieval_cases) - len(eligible_gr),
            "evaluation_scope": "ACTUAL_EXECUTION_GROUNDING",
            "status": (
                "MEETS_TARGET"
                if mean_gr is not None and mean_gr >= 1.0
                else ("BELOW_TARGET" if mean_gr is not None else "N/A_NO_ELIGIBLE_CASES")
            ),
            "per_case_breakdown": grounding_cases,
        }
    else:
        # No actual selected IDs supplied -> Grounding Accuracy remains strictly NOT MEASURED
        grounding_summary = {
            "metric_name": "entity_id_grounding",
            "display_name": "Entity-ID Grounding Accuracy",
            "target_threshold": 1.00,
            "mean_score": None,
            "eligible_cases": 0,
            "total_cases": len(retrieval_cases),
            "na_cases": len(retrieval_cases),
            "evaluation_scope": "NOT_MEASURED",
            "status": "NOT_MEASURED_MISSING_ACTUAL_IDS",
            "spec_integrity_validation": {
                "total_specs": len(spec_integrity_cases),
                "valid_specs": sum(1 for s in spec_integrity_cases if s["spec_valid"]),
                "status": "SPEC_INTEGRITY_VALIDATED" if spec_integrity_cases else "N/A",
            },
            "per_case_breakdown": [],
        }

    # -------------------------------------------------------------------------
    # Layer A2: Semantic Fixture Benchmarks
    # -------------------------------------------------------------------------
    faith_scores: List[Optional[float]] = []
    faith_cases: List[Dict[str, Any]] = []
    abstain_scores: List[Optional[float]] = []
    abstain_cases: List[Dict[str, Any]] = []
    relevance_scores: List[Optional[float]] = []
    relevance_cases: List[Dict[str, Any]] = []

    # Check for duplicate case_ids in semantic_cases
    seen_sem_ids = set()
    for sc in semantic_cases:
        cid = sc.get("case_id", "")
        if cid in seen_sem_ids:
            raise ValueError(f"Duplicate semantic case_id detected: {cid}")
        seen_sem_ids.add(cid)
        # Validate critical metadata if present
        if "critical" in sc:
            c_val = sc["critical"]
            if not isinstance(c_val, bool):
                raise ValueError(f"Malformed critical flag in case {cid}: expected bool, got {type(c_val).__name__}")

    for sc in semantic_cases:
        cid = sc.get("case_id", "")
        supp_facts = sc.get("supported_facts", [])
        forb_claims = sc.get("forbidden_claims", [])
        req_abstain = bool(sc.get("requires_abstention", False))
        abstain_phrases = sc.get("expected_abstention_phrases", [])
        abstain_type = sc.get("abstention_type")
        exp_intent = sc.get("expected_intent")
        req_elements = sc.get("required_elements", [])
        c_query = sc.get("query")
        is_critical = bool(sc.get("critical", False))

        if case_answers is not None and cid in case_answers:
            ans_text = case_answers[cid]  # May be empty string or text

            # Evaluate Faithfulness
            res_faith = evaluate_deterministic_faithfulness(
                answer_text=ans_text,
                supported_facts=supp_facts,
                forbidden_claims=forb_claims,
            )
            sc_f = res_faith.get("score")
            faith_scores.append(sc_f)
            faith_cases.append({
                "case_id": cid,
                "score": sc_f,
                "status": "EVALUATED" if sc_f is not None else "N/A_NO_ASSERTIONS",
                "reason": res_faith.get("reason"),
            })

            # Evaluate Abstention Quality
            res_abst = evaluate_abstention_quality(
                answer_text=ans_text,
                requires_abstention=req_abstain,
                expected_abstention_phrases=abstain_phrases,
                forbidden_claims=forb_claims,
                abstention_type=abstain_type,
            )
            sc_a = res_abst.get("score")
            abstain_scores.append(sc_a)
            abstain_cases.append({
                "case_id": cid,
                "score": sc_a,
                "status": "EVALUATED" if sc_a is not None else "N/A_ABSTENTION_NOT_REQUIRED",
                "reason": res_abst.get("reason"),
            })

            # Evaluate Response Relevance
            res_rel = evaluate_response_relevance(
                answer_text=ans_text,
                expected_intent=exp_intent,
                required_elements=req_elements,
                query=c_query,
            )
            sc_r = res_rel.get("score")
            relevance_scores.append(sc_r)
            relevance_cases.append({
                "case_id": cid,
                "score": sc_r,
                "status": "EVALUATED" if sc_r is not None else "N/A_MISSING_CONTRACT",
                "critical": is_critical,
                "reason": res_rel.get("reason"),
            })
        else:
            # Missing generated answer for this case
            faith_scores.append(None)
            faith_cases.append({
                "case_id": cid,
                "score": None,
                "status": "N/A_NO_ANSWER_SUPPLIED",
                "reason": "missing_answer_payload",
            })
            abstain_scores.append(None)
            abstain_cases.append({
                "case_id": cid,
                "score": None,
                "status": "N/A_NO_ANSWER_SUPPLIED",
                "reason": "missing_answer_payload",
            })
            relevance_scores.append(None)
            relevance_cases.append({
                "case_id": cid,
                "score": None,
                "status": "N/A_NO_ANSWER_SUPPLIED",
                "critical": is_critical,
                "reason": "missing_answer_payload",
            })

    # Faithfulness Aggregates
    eligible_faith = [s for s in faith_scores if s is not None]
    mean_faith = aggregate_metric_scores(faith_scores)
    faith_summary = {
        "metric_name": "deterministic_faithfulness",
        "display_name": "Deterministic Faithfulness",
        "target_threshold": 0.95,
        "mean_score": mean_faith,
        "eligible_cases": len(eligible_faith),
        "total_cases": len(semantic_cases),
        "na_cases": len(semantic_cases) - len(eligible_faith),
        "evaluation_scope": "SYNTHETIC_ANSWER_BENCHMARK" if eligible_faith else "GENERATED_ANSWERS_NOT_PROVIDED",
        "status": (
            "MEETS_TARGET"
            if mean_faith is not None and mean_faith >= 0.95
            else ("BELOW_TARGET" if mean_faith is not None else "N/A_NO_ELIGIBLE_CASES")
        ),
        "per_case_breakdown": faith_cases,
    }

    # Abstention Aggregates
    eligible_abstain = [s for s in abstain_scores if s is not None]
    mean_abstain = aggregate_metric_scores(abstain_scores)
    abstain_summary = {
        "metric_name": "abstention_quality",
        "display_name": "Abstention Quality",
        "target_threshold": 0.95,
        "mean_score": mean_abstain,
        "eligible_cases": len(eligible_abstain),
        "total_cases": len(semantic_cases),
        "na_cases": len(semantic_cases) - len(eligible_abstain),
        "evaluation_scope": "SYNTHETIC_ANSWER_BENCHMARK" if eligible_abstain else "GENERATED_ANSWERS_NOT_PROVIDED",
        "status": (
            "MEETS_TARGET"
            if mean_abstain is not None and mean_abstain >= 0.95
            else ("BELOW_TARGET" if mean_abstain is not None else "N/A_NO_ELIGIBLE_CASES")
        ),
        "per_case_breakdown": abstain_cases,
    }

    # Response Relevance Aggregates
    eligible_relevance = [s for s in relevance_scores if s is not None]
    mean_relevance = aggregate_metric_scores(relevance_scores)
    expected_crit_ids = sorted([sc.get("case_id") for sc in semantic_cases if sc.get("critical") is True and sc.get("case_id")])
    relevance_summary = {
        "metric_name": "response_relevance",
        "display_name": "Response Relevance",
        "target_threshold": 0.90,
        "mean_score": mean_relevance,
        "eligible_cases": len(eligible_relevance),
        "total_cases": len(semantic_cases),
        "na_cases": len(semantic_cases) - len(eligible_relevance),
        "evaluation_scope": "SYNTHETIC_ANSWER_BENCHMARK" if eligible_relevance else "GENERATED_ANSWERS_NOT_PROVIDED",
        "status": (
            "MEETS_TARGET"
            if mean_relevance is not None and mean_relevance >= 0.90
            else ("BELOW_TARGET" if mean_relevance is not None else "N/A_NO_ELIGIBLE_CASES")
        ),
        "per_case_breakdown": relevance_cases,
        "expected_critical_case_ids": expected_crit_ids,
    }

    # -------------------------------------------------------------------------
    # Layer B: Pipeline Regression Coverage & Execution Provenance
    # -------------------------------------------------------------------------
    pipeline_entries = []
    VALID_STATUSES = {"PASSED", "FAILED", "ERROR", "SKIPPED"}

    for item in KNOWN_PIPELINE_COVERAGE_MAP:
        method = item["test_method"]
        entry = dict(item)

        if test_execution_results is not None:
            if method in test_execution_results:
                raw_stat = str(test_execution_results[method].get("status", "")).upper()
                entry["execution_status"] = raw_stat if raw_stat in VALID_STATUSES else "UNKNOWN_STATUS"
                entry["failure_reason"] = test_execution_results[method].get("reason")
            else:
                entry["execution_status"] = "NOT_RUN"
                entry["failure_reason"] = None
        else:
            entry["execution_status"] = "NOT_RUN_STATIC_MAPPING_ONLY"
            entry["failure_reason"] = None

        pipeline_entries.append(entry)

    # -------------------------------------------------------------------------
    # Compose Final Report Payload
    # -------------------------------------------------------------------------
    return {
        "report_schema_version": "1.0",
        "timestamp": timestamp,
        "production_quality_status": "NOT MEASURED",
        "layers": {
            "layer_a_retrieval_benchmarks": {
                "context_precision": prec_summary,
                "context_recall": rec_summary,
                "entity_id_grounding": grounding_summary,
            },
            "layer_a_semantic_benchmarks": {
                "deterministic_faithfulness": faith_summary,
                "abstention_quality": abstain_summary,
                "response_relevance": relevance_summary,
            },
            "layer_b_pipeline_regression": {
                "coverage_mapping_total": len(KNOWN_PIPELINE_COVERAGE_MAP),
                "execution_results_supplied": test_execution_results is not None,
                "cases": pipeline_entries,
            },
            "layer_c_semantic_judge": {
                "status": "NOT_IMPLEMENTED",
                "notes": "Semantic LLM judge is not enabled. All evaluation remains deterministic and offline.",
            },
        },
    }


def format_evaluation_report_markdown(report_data: Dict[str, Any]) -> str:
    """
    Renders the evaluation report dictionary into clean GitHub Flavored Markdown.
    Guarantees deterministic output and safe N/A rendering.
    """
    lines: List[str] = []

    lines.append("# CollarCheck Chatbot — Unified Evaluation Report")
    lines.append("")
    ts = report_data.get("timestamp")
    if ts:
        lines.append(f"> **Report Timestamp:** `{ts}`")
    lines.append("> [!IMPORTANT]")
    lines.append("> **TRUTH IN REPORTING NOTICE:**")
    lines.append("> Synthetic fixture benchmarks and mock-based pipeline tests do **NOT** measure production quality.")
    lines.append("> **Production Quality Status:** `NOT MEASURED`.")
    lines.append("")

    layers = (report_data or {}).get("layers", {})

    # 1. Retrieval Benchmarks Table
    lines.append("## 1. Retrieval Quality Benchmarks (Layer A)")
    lines.append("")
    ret_layer = layers.get("layer_a_retrieval_benchmarks", {})
    if ret_layer:
        lines.append("| Metric | Target | Actual Score | Eligible / Total (N/A) | Status |")
        lines.append("| :--- | :---: | :---: | :---: | :--- |")
        for m_key in ["context_precision", "context_recall", "entity_id_grounding"]:
            m_data = ret_layer.get(m_key, {})
            disp = m_data.get("display_name", m_key)
            tgt = f"{m_data.get('target_threshold', 0.0):.2f}"
            sc = m_data.get("mean_score")
            sc_str = f"{sc:.4f}" if sc is not None else "N/A"
            el = m_data.get("eligible_cases", 0)
            tot = m_data.get("total_cases", 0)
            na = m_data.get("na_cases", 0)
            st = m_data.get("status", "N/A")
            lines.append(f"| **{disp}** | `{tgt}` | `{sc_str}` | {el} / {tot} ({na} N/A) | `{st}` |")
        lines.append("")
        spec_val = ret_layer.get("entity_id_grounding", {}).get("spec_integrity_validation")
        if spec_val:
            lines.append(f"*Spec Integrity Validation:* `{spec_val.get('status')}` ({spec_val.get('valid_specs')}/{spec_val.get('total_specs')} valid specs).")
            lines.append("")
    else:
        lines.append("*No retrieval fixture benchmark data available.*")
        lines.append("")

    # 2. Semantic Benchmarks Table
    lines.append("## 2. Semantic Answer Benchmarks (Layer A)")
    lines.append("")
    sem_layer = layers.get("layer_a_semantic_benchmarks", {})
    if sem_layer:
        lines.append("| Metric | Target | Actual Score | Eligible / Total (N/A) | Status |")
        lines.append("| :--- | :---: | :---: | :---: | :--- |")
        for m_key in ["deterministic_faithfulness", "abstention_quality", "response_relevance"]:
            m_data = sem_layer.get(m_key, {})
            disp = m_data.get("display_name", m_key)
            tgt = f"{m_data.get('target_threshold', 0.0):.2f}"
            sc = m_data.get("mean_score")
            sc_str = f"{sc:.4f}" if sc is not None else "N/A"
            el = m_data.get("eligible_cases", 0)
            tot = m_data.get("total_cases", 0)
            na = m_data.get("na_cases", 0)
            st = m_data.get("status", "N/A")
            lines.append(f"| **{disp}** | `{tgt}` | `{sc_str}` | {el} / {tot} ({na} N/A) | `{st}` |")
        lines.append("")

        # Render critical semantic cases summary if present
        rel_bk = sem_layer.get("response_relevance", {}).get("per_case_breakdown", [])
        crit_items = [c for c in rel_bk if isinstance(c, dict) and c.get("critical") is True]
        if crit_items:
            c_pass = [c for c in crit_items if c.get("status") == "EVALUATED" and c.get("score") is not None and c.get("score") >= 0.90]
            c_fail = [c for c in crit_items if c.get("status") == "EVALUATED" and c.get("score") is not None and c.get("score") < 0.90]
            c_uneval = [c for c in crit_items if c not in c_pass and c not in c_fail]
            crit_status_str = f"PASSED ({len(c_pass)}/{len(crit_items)})" if not c_fail and not c_uneval else (f"FAILED ({len(c_fail)} failed)" if c_fail else f"UNEVALUABLE ({len(c_uneval)} unevaluable)")
            lines.append(f"*Critical Case Enforcement:* `{crit_status_str}` ({len(c_pass)} passed, {len(c_fail)} failed, {len(c_uneval)} unevaluable).")
            lines.append("")
    else:
        lines.append("*No semantic fixture benchmark data available.*")
        lines.append("")

    # 3. Pipeline Coverage & Provenance Table
    lines.append("## 3. Pipeline Regression Coverage (Layer B)")
    lines.append("")
    pipe_layer = layers.get("layer_b_pipeline_regression", {})
    cases = pipe_layer.get("cases", [])
    exec_supplied = pipe_layer.get("execution_results_supplied", False)

    if not exec_supplied:
        lines.append("> [!NOTE]")
        lines.append("> **Execution Provenance Notice:** Live test execution results were not supplied for this report.")
        lines.append("> Test statuses below reflect **static design coverage mapping**, not test runner execution outcomes.")
        lines.append("")

    if cases:
        lines.append("| Case ID | Category | Test Method | Route Tested | Prompt Check | Card Check | Counterfactual | Execution Status |")
        lines.append("| :--- | :--- | :--- | :--- | :---: | :---: | :---: | :--- |")
        for c in cases:
            cid = c.get("case_id", "")
            cat = c.get("category", "")
            method = c.get("test_method", "")
            route = c.get("route_tested", "")
            pb = "Yes" if c.get("prompt_boundary_inspection") else "No"
            cg = "Yes" if c.get("structured_card_grounding") else "No"
            cf = "Yes" if c.get("evaluator_counterfactual") else "No"
            st = c.get("execution_status", "NOT_RUN")
            lines.append(f"| `{cid}` | {cat} | `{method}` | {route} | {pb} | {cg} | {cf} | `{st}` |")
        lines.append("")
    else:
        lines.append("*No pipeline coverage mapping available.*")
        lines.append("")

    # 4. Layer C Semantic Judge Status
    lines.append("## 4. Semantic LLM Judge Status (Layer C)")
    lines.append("")
    lines.append("- **Status:** `NOT IMPLEMENTED` (Opt-in evaluation disabled; zero external LLM calls).")
    lines.append("")

    return "\n".join(lines)


def parse_junit_xml_test_results(
    xml_content_or_path: str,
) -> Dict[str, Dict[str, Any]]:
    """
    Parses standard JUnit XML output to extract test execution outcomes.
    
    Robustness guarantees:
    - Accepts either raw XML string or valid file path.
    - Zero external dependencies (uses standard xml.etree.ElementTree).
    - Conservative normalization: Only explicit testcase elements are evaluated.
    - Accurately captures PASSED, FAILED, ERROR, and SKIPPED.
    - Detects ambiguous/duplicate testcase entries and flags them as ERROR.
    - Never infers success without an explicit testcase record.
    """
    import xml.etree.ElementTree as ET
    from pathlib import Path

    p = Path(xml_content_or_path)
    if p.exists() and p.is_file():
        try:
            tree = ET.parse(str(p))
            root = tree.getroot()
        except Exception as e:
            raise ValueError(f"Malformed JUnit XML file: {e}") from e
    else:
        try:
            root = ET.fromstring(xml_content_or_path)
        except Exception as e:
            raise ValueError(f"Malformed JUnit XML content: {e}") from e

    results: Dict[str, Dict[str, Any]] = {}
    seen_methods: Set[str] = set()

    for tc in root.iter("testcase"):
        raw_name = tc.get("name", "").strip()
        if not raw_name:
            continue

        # Extract base test method name if parameterized or qualified
        # e.g., 'test_method[param]' -> 'test_method'
        method_name = raw_name.split("[")[0].strip()

        # Handle duplicate / ambiguous records conservatively
        if method_name in seen_methods:
            results[method_name] = {
                "status": "ERROR",
                "reason": f"Ambiguous/duplicate testcase record for {method_name}",
            }
            continue
        seen_methods.add(method_name)

        failure_el = tc.find("failure")
        error_el = tc.find("error")
        skipped_el = tc.find("skipped")

        if failure_el is not None:
            results[method_name] = {
                "status": "FAILED",
                "reason": failure_el.get("message") or failure_el.text or "Assertion failed",
            }
        elif error_el is not None:
            results[method_name] = {
                "status": "ERROR",
                "reason": error_el.get("message") or error_el.text or "Execution error",
            }
        elif skipped_el is not None:
            results[method_name] = {
                "status": "SKIPPED",
                "reason": skipped_el.get("message") or skipped_el.text or "Test skipped",
            }
        else:
            results[method_name] = {
                "status": "PASSED",
                "reason": None,
            }

    return results


def evaluate_quality_gate(
    report_data: Dict[str, Any],
    baseline: Optional[Dict[str, Any]] = None,
    min_eligible_cases: Optional[int] = None,
    require_layer_b: bool = True,
) -> Dict[str, Any]:
    """
    Evaluates quality gates against an offline evaluation summary report.

    Precedence order for layer & overall verdicts:
    1. FAIL: Confirmed failure on threshold, regression against baseline, or pipeline test failure/error.
    2. INCONCLUSIVE: Data missing, zero eligible cases, minimum eligible cases not met, or missing pipeline execution.
    3. PASS: Explicitly evaluated and met targets across all required gates.

    Strict Quality Guarantees:
    - Never mutates input report_data or baseline.
    - Zero network, DB, or clock calls (pure and deterministic).
    - Production quality status remains strictly 'NOT MEASURED'.
    - Layer B cannot pass on static mapping or missing runs; requires explicit PASSED for all mapped tests.
    - Missing baselines report 'NO_BASELINE', never assumed as passing.
    """
    if not isinstance(report_data, dict):
        raise ValueError(f"report_data must be a dict, got {type(report_data).__name__}")

    layers = report_data.get("layers", {})
    ret_layer = layers.get("layer_a_retrieval_benchmarks", {})
    sem_layer = layers.get("layer_a_semantic_benchmarks", {})
    pipe_layer = layers.get("layer_b_pipeline_regression", {})

    baseline_metrics = (baseline or {}).get("metrics", {}) if isinstance(baseline, dict) else {}
    has_baseline = baseline is not None and isinstance(baseline, dict)

    metric_verdicts: Dict[str, Dict[str, Any]] = {}
    layer_a_verdicts: List[str] = []

    # Helper to evaluate an individual Layer A metric
    def _eval_layer_a_metric(m_key: str, m_dict: Dict[str, Any]):
        if not m_dict:
            metric_verdicts[m_key] = {
                "verdict": "INCONCLUSIVE",
                "reason": "Metric block missing from report",
                "score": None,
                "target": None,
                "eligible_cases": 0,
                "baseline_comparison": "NO_BASELINE" if not has_baseline else "INCONCLUSIVE_DATA_MISSING",
            }
            layer_a_verdicts.append("INCONCLUSIVE")
            return

        scope = m_dict.get("evaluation_scope")
        status = m_dict.get("status")
        score = m_dict.get("mean_score")
        target = m_dict.get("target_threshold")
        eligible = m_dict.get("eligible_cases", 0)

        # Baseline comparison block
        base_comp = "NO_BASELINE"
        if has_baseline:
            if m_key in baseline_metrics:
                b_val = baseline_metrics[m_key]
                if score is not None and isinstance(b_val, (int, float)):
                    if score < b_val:
                        base_comp = f"REGRESSION: score {score:.4f} < baseline {b_val:.4f}"
                    else:
                        base_comp = f"MAINTAINED: score {score:.4f} >= baseline {b_val:.4f}"
                else:
                    base_comp = "INCONCLUSIVE_DATA_MISSING"
            else:
                base_comp = "METRIC_NOT_IN_BASELINE"

        # Check for unimplemented
        if scope == "NOT_IMPLEMENTED" or status == "NOT_IMPLEMENTED":
            metric_verdicts[m_key] = {
                "verdict": "NOT_IMPLEMENTED",
                "reason": "Metric is not implemented in current evaluation harness",
                "score": None,
                "target": target,
                "eligible_cases": eligible,
                "baseline_comparison": base_comp,
            }
            return

        # Check for unmeasured entity grounding
        if m_key == "entity_id_grounding" and (scope == "NOT_MEASURED" or status == "NOT_MEASURED_MISSING_ACTUAL_IDS"):
            metric_verdicts[m_key] = {
                "verdict": "INCONCLUSIVE",
                "reason": "Actual selected entity IDs not supplied; actual grounding accuracy not measured",
                "score": None,
                "target": target,
                "eligible_cases": eligible,
                "baseline_comparison": base_comp,
            }
            layer_a_verdicts.append("INCONCLUSIVE")
            return

        # Check for missing answers
        if scope == "GENERATED_ANSWERS_NOT_PROVIDED" or status == "N/A_NO_ANSWER_SUPPLIED":
            metric_verdicts[m_key] = {
                "verdict": "INCONCLUSIVE",
                "reason": "Generated answers not supplied for evaluation",
                "score": None,
                "target": target,
                "eligible_cases": eligible,
                "baseline_comparison": base_comp,
            }
            layer_a_verdicts.append("INCONCLUSIVE")
            return

        # Check for zero eligible cases
        if eligible == 0 or score is None:
            metric_verdicts[m_key] = {
                "verdict": "INCONCLUSIVE",
                "reason": "Zero eligible cases for evaluation",
                "score": score,
                "target": target,
                "eligible_cases": eligible,
                "baseline_comparison": base_comp,
            }
            layer_a_verdicts.append("INCONCLUSIVE")
            return

        # Check configurable minimum eligible cases
        if min_eligible_cases is not None and eligible < min_eligible_cases:
            metric_verdicts[m_key] = {
                "verdict": "INCONCLUSIVE",
                "reason": f"Eligible cases ({eligible}) below minimum requirement ({min_eligible_cases})",
                "score": score,
                "target": target,
                "eligible_cases": eligible,
                "baseline_comparison": base_comp,
            }
            layer_a_verdicts.append("INCONCLUSIVE")
            return

        # Check baseline regression failure
        if has_baseline and base_comp.startswith("REGRESSION"):
            metric_verdicts[m_key] = {
                "verdict": "FAIL",
                "reason": f"Baseline regression detected: {base_comp}",
                "score": score,
                "target": target,
                "eligible_cases": eligible,
                "baseline_comparison": base_comp,
            }
            layer_a_verdicts.append("FAIL")
            return

        # Critical case zero-tolerance evaluation for semantic metrics (e.g. response_relevance)
        breakdown = m_dict.get("per_case_breakdown", [])
        expected_crit_ids = m_dict.get("expected_critical_case_ids")

        # Contract integrity: check if expected_critical_case_ids is provided or if critical cases exist in breakdown
        if breakdown and isinstance(breakdown, list):
            crit_failures = []
            crit_unevaluable = []
            seen_all_case_ids = set()
            duplicate_all_ids = set()
            crit_case_ids_in_breakdown = set()

            for item in breakdown:
                if not isinstance(item, dict):
                    continue
                cid = item.get("case_id", "")
                if cid in seen_all_case_ids:
                    duplicate_all_ids.add(cid)
                else:
                    seen_all_case_ids.add(cid)

                if item.get("critical") is True:
                    crit_case_ids_in_breakdown.add(cid)

            seen_crit_ids = set()
            for item in breakdown:
                if not isinstance(item, dict):
                    continue
                cid = item.get("case_id", "")
                is_crit = item.get("critical")

                if is_crit is True or cid in crit_case_ids_in_breakdown:
                    if cid in duplicate_all_ids:
                        crit_unevaluable.append(f"{cid} (duplicate entry in per_case_breakdown)")
                        continue
                    if cid in seen_crit_ids:
                        continue
                    seen_crit_ids.add(cid)

                    c_score = item.get("score")
                    c_status = item.get("status")

                    # Valid numeric check: supported number, not bool, finite, and strictly in [0.0, 1.0]
                    is_valid_num = (
                        c_score is not None
                        and isinstance(c_score, (int, float))
                        and not isinstance(c_score, bool)
                        and not (c_score != c_score)  # NaN check
                        and abs(c_score) != float("inf")
                        and 0.0 <= c_score <= 1.0
                    )

                    if c_status == "EVALUATED" and is_valid_num:
                        if c_score < 0.90:
                            crit_failures.append(f"{cid} (score {c_score:.4f} < 0.90)")
                    else:
                        crit_unevaluable.append(f"{cid} (status={c_status}, score={c_score})")

            # Check expected_critical_case_ids completeness and validity
            if expected_crit_ids is not None:
                if not isinstance(expected_crit_ids, list):
                    crit_unevaluable.append("expected_critical_case_ids must be a list")
                else:
                    seen_exp = set()
                    for exp_id in expected_crit_ids:
                        if not isinstance(exp_id, str) or not exp_id.strip():
                            crit_unevaluable.append(f"invalid expected_critical_case_id: {exp_id}")
                            continue
                        if exp_id in seen_exp:
                            crit_unevaluable.append(f"{exp_id} (duplicate expected critical ID)")
                            continue
                        seen_exp.add(exp_id)
                        if exp_id not in seen_crit_ids:
                            crit_unevaluable.append(f"{exp_id} (missing from per_case_breakdown)")
            elif m_key == "response_relevance" and not crit_case_ids_in_breakdown:
                # Direct gate call with neither expected_critical_case_ids nor critical cases in breakdown
                # If require_critical_contract is requested or if caller passes uncontracted report
                if m_dict.get("require_critical_contract", False):
                    crit_unevaluable.append("missing expected_critical_case_ids contract")

            # FAIL outranks INCONCLUSIVE
            if crit_failures:
                metric_verdicts[m_key] = {
                    "verdict": "FAIL",
                    "reason": f"Critical semantic case failure: {', '.join(crit_failures)}",
                    "score": score,
                    "target": target,
                    "eligible_cases": eligible,
                    "baseline_comparison": base_comp,
                    "critical_failures": crit_failures,
                    "critical_unevaluable": crit_unevaluable,
                }
                layer_a_verdicts.append("FAIL")
                return
            elif crit_unevaluable:
                metric_verdicts[m_key] = {
                    "verdict": "INCONCLUSIVE",
                    "reason": f"Critical semantic case unevaluable: {', '.join(crit_unevaluable)}",
                    "score": score,
                    "target": target,
                    "eligible_cases": eligible,
                    "baseline_comparison": base_comp,
                    "critical_failures": [],
                    "critical_unevaluable": crit_unevaluable,
                }
                layer_a_verdicts.append("INCONCLUSIVE")
                return
        elif m_dict.get("require_critical_contract", False) or (expected_crit_ids and len(expected_crit_ids) > 0):
            # Breakdown is missing or empty while expected critical IDs were declared or required
            missing_ids = expected_crit_ids if isinstance(expected_crit_ids, list) else ["unknown"]
            metric_verdicts[m_key] = {
                "verdict": "INCONCLUSIVE",
                "reason": f"Critical semantic case unevaluable: expected critical cases missing from breakdown ({', '.join(missing_ids)})",
                "score": score,
                "target": target,
                "eligible_cases": eligible,
                "baseline_comparison": base_comp,
                "critical_failures": [],
                "critical_unevaluable": missing_ids,
            }
            layer_a_verdicts.append("INCONCLUSIVE")
            return

        # Check threshold pass/fail
        if target is not None and score < target:
            metric_verdicts[m_key] = {
                "verdict": "FAIL",
                "reason": f"Score {score:.4f} below target threshold {target:.4f}",
                "score": score,
                "target": target,
                "eligible_cases": eligible,
                "baseline_comparison": base_comp,
            }
            layer_a_verdicts.append("FAIL")
        else:
            metric_verdicts[m_key] = {
                "verdict": "PASS",
                "reason": f"Score {score:.4f} meets target {target:.4f}",
                "score": score,
                "target": target,
                "eligible_cases": eligible,
                "baseline_comparison": base_comp,
            }
            layer_a_verdicts.append("PASS")

    # Evaluate Layer A retrieval metrics
    for r_k in ["context_precision", "context_recall", "entity_id_grounding"]:
        _eval_layer_a_metric(r_k, ret_layer.get(r_k, {}))

    # Evaluate Layer A semantic metrics
    for s_k in ["deterministic_faithfulness", "abstention_quality", "response_relevance"]:
        _eval_layer_a_metric(s_k, sem_layer.get(s_k, {}))

    # Aggregate Layer A verdict
    if "FAIL" in layer_a_verdicts:
        layer_a_verdict = "FAIL"
    elif "INCONCLUSIVE" in layer_a_verdicts:
        layer_a_verdict = "INCONCLUSIVE"
    elif all(v == "PASS" for v in layer_a_verdicts) and len(layer_a_verdicts) > 0:
        layer_a_verdict = "PASS"
    else:
        layer_a_verdict = "INCONCLUSIVE"

    # -------------------------------------------------------------------------
    # Layer B: Mock-based Pipeline Regression
    # -------------------------------------------------------------------------
    pipeline_cases = pipe_layer.get("cases", [])
    exec_supplied = pipe_layer.get("execution_results_supplied", False)

    pipe_failures = []
    pipe_unrun = []
    pipe_passed = []

    for pc in pipeline_cases:
        st = pc.get("execution_status")
        method = pc.get("test_method", pc.get("case_id", "unknown"))
        if st in {"FAILED", "ERROR"}:
            pipe_failures.append({"test_method": method, "status": st, "reason": pc.get("failure_reason")})
        elif st == "PASSED":
            pipe_passed.append(method)
        else:
            # SKIPPED, NOT_RUN, NOT_RUN_STATIC_MAPPING_ONLY, UNKNOWN_STATUS
            pipe_unrun.append({"test_method": method, "status": st})

    total_pipe = len(pipeline_cases)

    if pipe_failures:
        layer_b_verdict = "FAIL"
        layer_b_reason = f"{len(pipe_failures)} pipeline tests failed or errored"
    elif not exec_supplied or pipe_unrun or total_pipe == 0:
        layer_b_verdict = "INCONCLUSIVE"
        if not exec_supplied:
            layer_b_reason = "No test execution results supplied (static mapping only)"
        else:
            layer_b_reason = f"{len(pipe_unrun)} pipeline tests not run, skipped, or unknown"
    elif len(pipe_passed) == total_pipe:
        layer_b_verdict = "PASS"
        layer_b_reason = f"All {total_pipe} mapped pipeline regression tests passed"
    else:
        layer_b_verdict = "INCONCLUSIVE"
        layer_b_reason = "Incomplete pipeline test execution"

    # -------------------------------------------------------------------------
    # Overall Offline Quality Gate Verdict
    # -------------------------------------------------------------------------
    # Precedence:
    # 1. FAIL if either Layer A or Layer B (if required) fails.
    # 2. INCONCLUSIVE if either required layer is inconclusive.
    # 3. PASS only if all required layers pass.
    if layer_a_verdict == "FAIL" or (require_layer_b and layer_b_verdict == "FAIL"):
        overall_verdict = "FAIL"
        overall_reason = "One or more evaluation quality gates failed"
    elif layer_a_verdict == "INCONCLUSIVE" or (require_layer_b and layer_b_verdict == "INCONCLUSIVE"):
        overall_verdict = "INCONCLUSIVE"
        overall_reason = "Required evaluation data, answer payloads, or test execution results missing"
    elif layer_a_verdict == "PASS" and (not require_layer_b or layer_b_verdict == "PASS"):
        overall_verdict = "PASS"
        overall_reason = "All required offline evaluation gates passed"
    else:
        overall_verdict = "INCONCLUSIVE"
        overall_reason = "Inconclusive evaluation status"

    return {
        "overall_verdict": overall_verdict,
        "overall_reason": overall_reason,
        "production_quality_status": "NOT MEASURED",
        "has_baseline": has_baseline,
        "layer_a_verdict": layer_a_verdict,
        "layer_b_verdict": layer_b_verdict,
        "layer_b_reason": layer_b_reason,
        "metric_verdicts": metric_verdicts,
        "pipeline_summary": {
            "total_mapped_tests": total_pipe,
            "passed_tests": len(pipe_passed),
            "failed_or_errored_tests": len(pipe_failures),
            "unrun_or_skipped_tests": len(pipe_unrun),
        },
    }



