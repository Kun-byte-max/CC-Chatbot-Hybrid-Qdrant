"""
backend/tests/test_telemetry_aggregator.py

Targeted unit tests for CollarCheck Stage 3.4.2B Offline Structured Telemetry Aggregator.
Covers:
1. Valid chat_stream_turn record parsing.
2. Valid chat_turn record parsing.
3. Correct parsing of nested stages JSON.
4. Malformed stages JSON rejection.
5. Missing required fields rejection.
6. Negative and non-finite durations rejection.
7. Booleans and non-numeric stage values rejection.
8. Missing stages not counted as zero.
9. Recognized malformed records versus unrecognized lines.
10. Exact percentile calculations against documented examples.
11. Small-sample warnings.
12. Multiple records and outcome aggregation.
13. No raw identifiers or source lines in output.
14. Graceful processing after malformed input.
"""

import json
import unittest
from backend.tools.aggregate_telemetry import (
    aggregate_telemetry,
    compute_percentiles,
    evaluate_sample_status,
    parse_telemetry_line,
)


class TestTelemetryAggregator(unittest.TestCase):

    def test_01_valid_chat_stream_turn(self):
        line = (
            "2026-10-22 12:00:00,000 [INFO] backend.main: chat_stream_turn "
            "request_id=req_12345 role=employee intent_hint=search msg_len=42 "
            'stages={"session_load":15,"compose":400,"ttft":180,"session_write":25} '
            "total_ms=450 outcome=ok"
        )
        record, reason = parse_telemetry_line(line)
        self.assertIsNone(reason)
        self.assertIsNotNone(record)
        self.assertEqual(record["event_type"], "chat_stream_turn")
        self.assertEqual(record["endpoint"], "/chat/stream")
        self.assertEqual(record["total_ms"], 450.0)
        self.assertEqual(record["outcome"], "ok")
        self.assertEqual(record["role"], "employee")
        self.assertEqual(record["intent_hint"], "search")
        self.assertEqual(record["msg_len"], 42)
        self.assertEqual(record["stages"]["ttft"], 180.0)
        self.assertEqual(record["stages"]["session_load"], 15.0)
        self.assertEqual(record["stages"]["session_write"], 25.0)

    def test_02_valid_chat_turn(self):
        line = (
            "2026-10-22 12:01:00,123 [INFO] backend.main: chat_turn "
            "request_id=turn_abc role=employer intent_hint=user_data msg_len=20 "
            'stages={"session_load":10,"retrieval":80,"session_write":30} '
            "total_ms=150 outcome=ok profile_parse_ran=false search_parse_ran=true"
        )
        record, reason = parse_telemetry_line(line)
        self.assertIsNone(reason)
        self.assertIsNotNone(record)
        self.assertEqual(record["event_type"], "chat_turn")
        self.assertEqual(record["endpoint"], "/chat")
        self.assertEqual(record["total_ms"], 150.0)
        self.assertEqual(record["outcome"], "ok")
        self.assertEqual(record["stages"]["session_load"], 10.0)

    def test_03_correct_parsing_nested_stages_json(self):
        line = (
            "chat_stream_turn request_id=xyz role=unknown intent_hint=none msg_len=0 "
            'stages={"a_stage":10.5,"b_stage":0} total_ms=20 outcome=ok'
        )
        record, reason = parse_telemetry_line(line)
        self.assertIsNone(reason)
        self.assertEqual(record["stages"]["a_stage"], 10.5)
        self.assertEqual(record["stages"]["b_stage"], 0.0)

    def test_04_malformed_stages_json(self):
        # Unbalanced or invalid JSON in stages
        line = (
            "chat_stream_turn request_id=xyz role=unknown intent_hint=none msg_len=0 "
            "stages={unquoted_key:10 total_ms=20 outcome=ok"
        )
        record, reason = parse_telemetry_line(line)
        self.assertIsNone(record)
        self.assertEqual(reason, "malformed_stages_json")

    def test_05_missing_required_fields(self):
        # Missing total_ms
        line_no_total = (
            "chat_stream_turn request_id=xyz role=unknown "
            'stages={"s":1} outcome=ok'
        )
        rec, reason = parse_telemetry_line(line_no_total)
        self.assertIsNone(rec)
        self.assertEqual(reason, "missing_total_ms")

        # Missing outcome
        line_no_outcome = (
            "chat_stream_turn request_id=xyz role=unknown "
            'stages={"s":1} total_ms=10'
        )
        rec, reason = parse_telemetry_line(line_no_outcome)
        self.assertIsNone(rec)
        self.assertEqual(reason, "missing_outcome")

        # Missing stages
        line_no_stages = (
            "chat_stream_turn request_id=xyz role=unknown total_ms=10 outcome=ok"
        )
        rec, reason = parse_telemetry_line(line_no_stages)
        self.assertIsNone(rec)
        self.assertEqual(reason, "missing_stages")

    def test_06_negative_and_non_finite_durations(self):
        # Negative total_ms
        line_neg_total = (
            'chat_stream_turn request_id=xyz stages={"s":1} total_ms=-50 outcome=ok'
        )
        rec, reason = parse_telemetry_line(line_neg_total)
        self.assertIsNone(rec)
        self.assertEqual(reason, "negative_total_ms")

        # Negative stage duration
        line_neg_stage = (
            'chat_stream_turn request_id=xyz stages={"s":-10} total_ms=50 outcome=ok'
        )
        rec, reason = parse_telemetry_line(line_neg_stage)
        self.assertIsNone(rec)
        self.assertEqual(reason, "negative_duration_in_stages")

        # NaN / Infinity in total_ms
        line_nan_total = (
            'chat_stream_turn request_id=xyz stages={"s":10} total_ms=nan outcome=ok'
        )
        rec, reason = parse_telemetry_line(line_nan_total)
        self.assertIsNone(rec)
        self.assertEqual(reason, "non_finite_total_ms")

        line_inf_total = (
            'chat_stream_turn request_id=xyz stages={"s":10} total_ms=inf outcome=ok'
        )
        rec, reason = parse_telemetry_line(line_inf_total)
        self.assertIsNone(rec)
        self.assertEqual(reason, "non_finite_total_ms")

    def test_07_booleans_and_non_numeric_stage_values(self):
        # Boolean in stage duration
        line_bool = (
            'chat_stream_turn request_id=xyz stages={"s":true} total_ms=50 outcome=ok'
        )
        rec, reason = parse_telemetry_line(line_bool)
        self.assertIsNone(rec)
        self.assertEqual(reason, "boolean_duration_in_stages")

        # String in stage duration
        line_str = (
            'chat_stream_turn request_id=xyz stages={"s":"100ms"} total_ms=50 outcome=ok'
        )
        rec, reason = parse_telemetry_line(line_str)
        self.assertIsNone(rec)
        self.assertEqual(reason, "non_numeric_duration_in_stages")

    def test_08_missing_stages_not_counted_as_zero(self):
        lines = [
            'chat_stream_turn request_id=1 stages={"ttft":100} total_ms=200 outcome=ok',
            'chat_stream_turn request_id=2 stages={"other_stage":50} total_ms=300 outcome=ok',
        ]
        report = aggregate_telemetry(lines)
        stream_stages = report["endpoints"]["/chat/stream"]["stages"]

        # ttft was only in record 1, so sample_count must be exactly 1, not 2
        self.assertEqual(stream_stages["ttft"]["sample_count"], 1)
        self.assertEqual(stream_stages["ttft"]["percentiles"]["min"], 100.0)
        self.assertEqual(stream_stages["ttft"]["percentiles"]["max"], 100.0)

        # other_stage was only in record 2, sample_count must be 1
        self.assertEqual(stream_stages["other_stage"]["sample_count"], 1)
        self.assertEqual(stream_stages["other_stage"]["percentiles"]["min"], 50.0)

    def test_09_recognized_malformed_versus_unrecognized_lines(self):
        lines = [
            "2026-10-22 12:00:00 [INFO] app: Starting server on 0.0.0.0:8000",
            "Traceback (most recent call last): File main.py, line 50",
            'chat_stream_turn request_id=bad stages={"bad":-5} total_ms=10 outcome=ok',
            'chat_stream_turn request_id=good stages={"good":5} total_ms=10 outcome=ok',
        ]
        report = aggregate_telemetry(lines)
        summary = report["summary"]
        self.assertEqual(summary["total_lines_scanned"], 4)
        self.assertEqual(summary["recognized_telemetry_records"], 2)
        self.assertEqual(summary["valid_records"], 1)
        self.assertEqual(summary["rejected_records"], 1)
        self.assertIn("negative_duration_in_stages", summary["rejection_reasons"])

    def test_10_exact_percentile_calculations(self):
        # Test Nearest-Rank: rank = ceil(p / 100 * N)
        # Array of 10 items: 10, 20, 30, 40, 50, 60, 70, 80, 90, 100
        # N = 10:
        # p50: rank = ceil(0.50 * 10) = 5 -> index 4 -> 50
        # p90: rank = ceil(0.90 * 10) = 9 -> index 8 -> 90
        # p99: rank = ceil(0.99 * 10) = 10 -> index 9 -> 100
        data10 = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
        p10 = compute_percentiles(data10)
        self.assertEqual(p10["min"], 10.0)
        self.assertEqual(p10["p50"], 50.0)
        self.assertEqual(p10["p90"], 90.0)
        self.assertEqual(p10["p99"], 100.0)
        self.assertEqual(p10["max"], 100.0)

        # Single item: [42.0]
        p1 = compute_percentiles([42.0])
        self.assertEqual(p1["min"], 42.0)
        self.assertEqual(p1["p50"], 42.0)
        self.assertEqual(p1["p90"], 42.0)
        self.assertEqual(p1["p99"], 42.0)
        self.assertEqual(p1["max"], 42.0)

        # Empty array
        p0 = compute_percentiles([])
        self.assertIsNone(p0["min"])
        self.assertIsNone(p0["p50"])

    def test_11_small_sample_warnings(self):
        info_0 = evaluate_sample_status(0)
        self.assertEqual(info_0["status"], "NO_SAMPLES")

        info_10 = evaluate_sample_status(10)
        self.assertIn("INSUFFICIENT_SAMPLE", info_10["status"])
        self.assertIn("unstable", info_10["warning"])

        info_50 = evaluate_sample_status(50)
        self.assertIn("LOW_SAMPLE", info_50["status"])

        info_150 = evaluate_sample_status(150)
        self.assertIn("ADEQUATE_SAMPLE", info_150["status"])

    def test_12_multiple_records_and_outcome_aggregation(self):
        lines = [
            'chat_stream_turn request_id=1 stages={"ttft":100} total_ms=300 outcome=ok',
            'chat_stream_turn request_id=2 stages={"ttft":120} total_ms=350 outcome=ok',
            'chat_stream_turn request_id=3 stages={"ttft":90} total_ms=250 outcome=client_disconnected',
            'chat_stream_turn request_id=4 stages={} total_ms=50 outcome=error error_type=ValueError',
            'chat_turn request_id=5 stages={"load":10} total_ms=100 outcome=ok',
        ]
        report = aggregate_telemetry(lines)
        endpoints = report["endpoints"]

        stream_data = endpoints["/chat/stream"]
        self.assertEqual(stream_data["sample_count"], 4)
        self.assertEqual(stream_data["outcomes"]["ok"], 2)
        self.assertEqual(stream_data["outcomes"]["client_disconnected"], 1)
        self.assertEqual(stream_data["outcomes"]["error"], 1)

        chat_data = endpoints["/chat"]
        self.assertEqual(chat_data["sample_count"], 1)
        self.assertEqual(chat_data["outcomes"]["ok"], 1)

    def test_13_no_raw_identifiers_in_output(self):
        sensitive_request_id = "SECRET_REQ_UUID_99999"
        lines = [
            f'chat_stream_turn request_id={sensitive_request_id} stages={{"ttft":100}} total_ms=300 outcome=ok'
        ]
        report = aggregate_telemetry(lines)
        report_json = json.dumps(report)
        self.assertNotIn(sensitive_request_id, report_json)
        self.assertNotIn("request_id", report_json)

    def test_14_graceful_processing_after_malformed_input(self):
        lines = [
            'chat_stream_turn request_id=1 stages={"bad":-10} total_ms=100 outcome=ok',
            "garbage line 123",
            'chat_stream_turn request_id=2 stages={"ttft":200} total_ms=400 outcome=ok',
        ]
        report = aggregate_telemetry(lines)
        self.assertEqual(report["summary"]["total_lines_scanned"], 3)
        self.assertEqual(report["summary"]["recognized_telemetry_records"], 2)
        self.assertEqual(report["summary"]["valid_records"], 1)
        self.assertEqual(report["summary"]["rejected_records"], 1)
        self.assertEqual(report["endpoints"]["/chat/stream"]["sample_count"], 1)
        self.assertEqual(
            report["endpoints"]["/chat/stream"]["metrics"]["total_ms"]["percentiles"]["p50"],
            400.0,
        )


if __name__ == "__main__":
    unittest.main()
