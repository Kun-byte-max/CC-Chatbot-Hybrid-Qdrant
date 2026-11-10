"""
backend/tools/aggregate_telemetry.py

Offline CLI utility for aggregating structured timing telemetry from CollarCheck
chat endpoints (chat_stream_turn and chat_turn).

Usage:
    python -m backend.tools.aggregate_telemetry --file logs/app.log
    cat logs/app.log | python -m backend.tools.aggregate_telemetry
"""

import argparse
import json
import math
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple


# Regex pattern to locate the start of a recognized event
EVENT_PREFIX_RE = re.compile(r"\b(chat_stream_turn|chat_turn)\b")

# Regex to safely locate stages={...} using balanced braces (no nested objects in stages dict)
STAGES_KV_RE = re.compile(r"\bstages=(\{[^{}]*\})")

# Regex to capture individual key=value tokens
KEY_VALUE_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)=([^\s]+)")

ALLOWED_OUTCOMES = {"ok", "ok_terminal", "client_disconnected", "error", "cancelled"}


def compute_percentiles(values: List[float]) -> Dict[str, Optional[float]]:
    """
    Computes min, p50, p90, p99, max using the Nearest-Rank method (P = ceil(p/100 * N) - 1).
    
    Formula:
        For sample size N (sorted 0-indexed array V):
        index = ceil((p / 100) * N) - 1
        bound between 0 and N - 1.

    This method:
    - Never interpolates synthetic numbers; returns an actual observed measurement.
    - Is standard for latency SLO accounting (e.g. AWS CloudWatch, Datadog nearest rank).
    - If values is empty, returns all None.
    """
    if not values:
        return {
            "min": None,
            "p50": None,
            "p90": None,
            "p99": None,
            "max": None,
        }

    sorted_vals = sorted(values)
    n = len(sorted_vals)

    def _rank_percentile(p: float) -> float:
        rank = math.ceil((p / 100.0) * n)
        idx = max(0, min(rank - 1, n - 1))
        return sorted_vals[idx]

    return {
        "min": sorted_vals[0],
        "p50": _rank_percentile(50.0),
        "p90": _rank_percentile(90.0),
        "p99": _rank_percentile(99.0),
        "max": sorted_vals[-1],
    }


def parse_telemetry_line(line: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Parses a single log line into a validated telemetry record.

    Returns:
        (record, None) if valid.
        (None, reason) if recognized event marker present but record is invalid.
        (None, None) if line does not contain a recognized event marker.
    """
    match_event = EVENT_PREFIX_RE.search(line)
    if not match_event:
        return None, None

    event_type = match_event.group(1)
    event_start = match_event.start()
    event_text = line[event_start:]

    # 1. Extract stages={...}
    stages_match = STAGES_KV_RE.search(event_text)
    if not stages_match:
        # Check if "stages=" exists but is malformed/unbalanced
        if "stages=" in event_text:
            return None, "malformed_stages_json"
        return None, "missing_stages"

    stages_str = stages_match.group(1)
    try:
        raw_stages = json.loads(stages_str)
    except Exception:
        return None, "malformed_stages_json"

    if not isinstance(raw_stages, dict):
        return None, "invalid_stages_format"

    validated_stages: Dict[str, float] = {}
    for stage_name, duration in raw_stages.items():
        if not isinstance(stage_name, str):
            return None, "invalid_stage_name"
        # In Python, bool is a subclass of int (isinstance(True, int) is True). Must reject bool!
        if isinstance(duration, bool):
            return None, "boolean_duration_in_stages"
        if not isinstance(duration, (int, float)):
            return None, "non_numeric_duration_in_stages"
        if math.isnan(duration) or math.isinf(duration):
            return None, "non_finite_duration_in_stages"
        if duration < 0:
            return None, "negative_duration_in_stages"
        validated_stages[stage_name] = float(duration)

    # 2. Extract remaining key-value pairs outside stages
    # Replace stages={...} with whitespace so KEY_VALUE_RE does not break inside the braces
    text_without_stages = event_text[:stages_match.start()] + " " + event_text[stages_match.end():]
    tokens: Dict[str, str] = {}
    for k, v in KEY_VALUE_RE.findall(text_without_stages):
        tokens[k] = v

    # 3. Validate total_ms
    if "total_ms" not in tokens:
        return None, "missing_total_ms"
    try:
        total_ms_val = float(tokens["total_ms"])
    except ValueError:
        return None, "non_numeric_total_ms"

    if math.isnan(total_ms_val) or math.isinf(total_ms_val):
        return None, "non_finite_total_ms"
    if total_ms_val < 0:
        return None, "negative_total_ms"

    # 4. Validate outcome
    outcome = tokens.get("outcome")
    if not outcome:
        return None, "missing_outcome"

    # Optional metadata (whitelisted)
    role = tokens.get("role", "unknown")
    intent_hint = tokens.get("intent_hint")
    raw_msg_len = tokens.get("msg_len")
    msg_len = None
    if raw_msg_len is not None:
        try:
            msg_len = int(raw_msg_len)
        except ValueError:
            pass

    record: Dict[str, Any] = {
        "event_type": event_type,
        "endpoint": "/chat/stream" if event_type == "chat_stream_turn" else "/chat",
        "outcome": outcome,
        "total_ms": total_ms_val,
        "stages": validated_stages,
        "role": role,
        "intent_hint": intent_hint,
        "msg_len": msg_len,
    }

    if outcome == "error" and "error_type" in tokens:
        record["error_type"] = tokens["error_type"]

    return record, None


def evaluate_sample_status(count: int) -> Dict[str, Any]:
    """Provides sample status and statistical reliability warnings."""
    if count == 0:
        return {
            "status": "NO_SAMPLES",
            "warning": "No measurements recorded.",
        }
    elif count < 30:
        return {
            "status": "INSUFFICIENT_SAMPLE (N < 30)",
            "warning": "High variance. p90 and p99 percentiles are unstable and not statistically reliable.",
        }
    elif count < 100:
        return {
            "status": "LOW_SAMPLE (30 <= N < 100)",
            "warning": "p50 and p90 are indicative; p99 remains unstable for rare outlier tail estimation.",
        }
    else:
        return {
            "status": "ADEQUATE_SAMPLE (N >= 100)",
            "warning": "Sample count meets basic threshold, but p99 requires careful tail monitoring.",
        }


def aggregate_telemetry(lines: Iterable[str]) -> Dict[str, Any]:
    """Aggregates an iterable of log lines into a structured JSON-serializable dictionary."""
    total_lines = 0
    recognized_records = 0
    valid_records = 0
    rejected_records = 0
    rejection_reasons: Dict[str, int] = {}

    # Grouped storage: endpoint -> data
    endpoint_data: Dict[str, Dict[str, Any]] = {
        "/chat/stream": {
            "total_ms": [],
            "stages": {},
            "outcomes": {},
        },
        "/chat": {
            "total_ms": [],
            "stages": {},
            "outcomes": {},
        },
    }

    for line in lines:
        total_lines += 1
        record, reason = parse_telemetry_line(line)
        if reason is not None:
            recognized_records += 1
            rejected_records += 1
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
            continue

        if record is None:
            # Unrecognized line (general app log, traceback, etc.)
            continue

        recognized_records += 1
        valid_records += 1

        endpoint = record["endpoint"]
        data = endpoint_data[endpoint]

        data["total_ms"].append(record["total_ms"])

        outcome = record["outcome"]
        data["outcomes"][outcome] = data["outcomes"].get(outcome, 0) + 1

        for s_name, s_ms in record["stages"].items():
            if s_name not in data["stages"]:
                data["stages"][s_name] = []
            data["stages"][s_name].append(s_ms)

    # Build report
    endpoints_summary: Dict[str, Any] = {}
    for ep, data in endpoint_data.items():
        count = len(data["total_ms"])
        if count == 0:
            continue

        sample_info = evaluate_sample_status(count)
        ep_summary: Dict[str, Any] = {
            "sample_count": count,
            "sample_status": sample_info["status"],
            "reliability_warning": sample_info["warning"],
            "outcomes": data["outcomes"],
            "metrics": {
                "total_ms": {
                    "sample_count": len(data["total_ms"]),
                    "percentiles": compute_percentiles(data["total_ms"]),
                }
            },
        }

        # Stages metrics
        stages_metrics: Dict[str, Any] = {}
        for s_name, s_vals in sorted(data["stages"].items()):
            metric_entry: Dict[str, Any] = {
                "sample_count": len(s_vals),
                "percentiles": compute_percentiles(s_vals),
            }
            if s_name == "session_write":
                metric_entry["semantics"] = "LAST_RECORDED_WRITE_ONLY"
                metric_entry["note"] = (
                    "Stage 'session_write' records only the duration of the final write in a turn. "
                    "It is not cumulative session persistence time."
                )
            if s_name == "ttft":
                metric_entry["semantics"] = "STREAMING_TIME_TO_FIRST_TOKEN"
                metric_entry["note"] = "Recorded under stage key 'ttft' (in milliseconds)."

            stages_metrics[s_name] = metric_entry

        ep_summary["stages"] = stages_metrics
        endpoints_summary[ep] = ep_summary

    report = {
        "metadata": {
            "origin": "LOCAL_OFFLINE_TELEMETRY",
            "percentile_method": "Nearest-Rank (rank = ceil((p/100) * N) - 1)",
            "description": "Offline aggregated timing telemetry for CollarCheck chat turns.",
        },
        "summary": {
            "total_lines_scanned": total_lines,
            "recognized_telemetry_records": recognized_records,
            "valid_records": valid_records,
            "rejected_records": rejected_records,
            "rejection_reasons": rejection_reasons,
        },
        "endpoints": endpoints_summary,
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CollarCheck Offline Structured Telemetry Aggregator"
    )
    parser.add_argument(
        "--file",
        type=str,
        default=None,
        help="Path to local log file. If omitted, reads from stdin.",
    )
    args = parser.parse_args()

    try:
        if args.file:
            with open(args.file, "r", encoding="utf-8", errors="replace") as f:
                report = aggregate_telemetry(f)
        else:
            if sys.stdin.isatty():
                sys.stderr.write("Error: No file specified and stdin is an interactive terminal.\n")
                sys.stderr.write("Usage: python -m backend.tools.aggregate_telemetry --file <PATH>\n")
                sys.exit(1)
            report = aggregate_telemetry(sys.stdin)
    except FileNotFoundError:
        sys.stderr.write(f"Error: File not found: {args.file}\n")
        sys.exit(2)
    except Exception as e:
        sys.stderr.write(f"Error processing telemetry: {e}\n")
        sys.exit(3)

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
