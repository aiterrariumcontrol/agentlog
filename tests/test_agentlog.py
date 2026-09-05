import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agentlog import parse_file, parse_lines
from agentlog.cli import main
from agentlog.render import Style, format_event, render
from agentlog.stats import (
    aggregate,
    filter_summaries,
    format_aggregate,
    format_summary,
    in_range,
    iter_log_paths,
    summarize,
)
import os
import tempfile

FIXTURES = Path(__file__).parent / "fixtures"
STREAM = str(FIXTURES / "stream.jsonl")
SESSION = str(FIXTURES / "session.jsonl")
BROKEN = str(FIXTURES / "broken.jsonl")


def run(*argv) -> str:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(list(argv))
    assert code == 0, f"exit code {code}"
    return buffer.getvalue()


class TestParsing(unittest.TestCase):
    def test_stream_shape_and_counts(self):
        transcript = parse_file(STREAM)
        self.assertEqual(transcript.shape, "stream")
        self.assertEqual(sum(1 for e in transcript.events if e.kind == "assistant"), 3)
        self.assertIsNotNone(transcript.result)
        self.assertIsNotNone(transcript.system)

    def test_session_shape_and_noise_filtering(self):
        transcript = parse_file(SESSION)
        self.assertEqual(transcript.shape, "session")
        kinds = [e.kind for e in transcript.events]
        # queue-operation / attachment / ai-title are bookkeeping.
        self.assertEqual(kinds.count("noise"), 3)
        self.assertEqual(kinds.count("assistant"), 1)

    def test_string_content_is_normalized_to_text(self):
        transcript = parse_file(SESSION)
        user = next(e for e in transcript.events if e.kind == "user")
        self.assertEqual(user.text, "summarize the repo")

    def test_tool_results_are_linked_to_calls(self):
        transcript = parse_file(STREAM)
        calls = {call.name: call for call in transcript.tool_calls}
        self.assertEqual(set(calls), {"Bash", "Read"})
        self.assertIn("README.md", calls["Bash"].result)
        self.assertFalse(calls["Bash"].is_error)
        self.assertTrue(calls["Read"].is_error)
        self.assertEqual(calls["Read"].result, "File does not exist.")

    def test_tool_target_prefers_meaningful_field(self):
        transcript = parse_file(STREAM)
        calls = {call.name: call for call in transcript.tool_calls}
        self.assertEqual(calls["Bash"].target, "ls /work/demo")
        self.assertEqual(calls["Read"].target, "/work/demo/missing.py")

    def test_thinking_is_captured(self):
        transcript = parse_file(STREAM)
        first = next(e for e in transcript.events if e.kind == "assistant")
        self.assertIn("wants the file listed", first.thinking)

    def test_malformed_lines_do_not_abort_parsing(self):
        transcript = parse_file(BROKEN)
        kinds = [e.kind for e in transcript.events]
        self.assertEqual(kinds.count("assistant"), 2)
        self.assertEqual(kinds.count("malformed"), 2)  # truncated JSON + non-object

    def test_blank_lines_are_skipped(self):
        transcript = parse_lines(["", "   ", '{"type":"assistant","message":{"content":"hi"}}'])
        self.assertEqual(len(transcript.events), 1)

    def test_incomplete_stream_is_still_detected_as_stream(self):
        # A run that is still going, or was killed, has no result record.
        lines = [l for l in open(STREAM) if '"type":"result"' not in l]
        transcript = parse_lines(lines)
        self.assertEqual(transcript.shape, "stream")
        self.assertFalse(transcript.complete)

    def test_finished_stream_and_session_are_complete(self):
        self.assertTrue(parse_file(STREAM).complete)
        self.assertTrue(parse_file(SESSION).complete)

    def test_unknown_shape(self):
        self.assertEqual(parse_lines(['{"type":"assistant","message":{}}']).shape, "unknown")


class TestStats(unittest.TestCase):
    def test_stream_totals_come_from_result_record(self):
        summary = summarize(parse_file(STREAM))
        self.assertEqual(summary["token_source"], "result")
        self.assertEqual(summary["cost_usd"], 0.0125)
        self.assertEqual(summary["tokens"]["output_tokens"], 61)
        self.assertEqual(summary["tokens"]["thinking_tokens"], 7)
        self.assertEqual(summary["tool_calls"], 2)
        self.assertEqual(summary["tool_errors"], 1)
        self.assertEqual(summary["failing_tools"], {"Read": 1})
        self.assertEqual(summary["permission_denials"], 1)
        self.assertEqual(summary["model"], "claude-opus-5")
        self.assertIn("claude-opus-5", summary["models"])

    def test_session_totals_fall_back_to_per_message_usage(self):
        summary = summarize(parse_file(SESSION))
        self.assertEqual(summary["token_source"], "messages")
        self.assertEqual(summary["tokens"]["input_tokens"], 50)
        self.assertEqual(summary["tokens"]["output_tokens"], 8)
        self.assertNotIn("cost_usd", summary)

    def test_timestamps_span_the_file(self):
        summary = summarize(parse_file(STREAM))
        self.assertEqual(summary["started_at"], "2026-01-02T03:04:05.000Z")
        self.assertEqual(summary["ended_at"], "2026-01-02T03:04:09.000Z")

    def test_format_summary_is_plain_text(self):
        text = format_summary(summarize(parse_file(STREAM)))
        self.assertIn("$0.012500", text)
        self.assertIn("Bash", text)
        self.assertIn("(1 failed)", text)
        self.assertNotIn("\033", text)


class TestRender(unittest.TestCase):
    def test_render_includes_text_and_tools_but_not_thinking(self):
        output = render(parse_file(STREAM))
        self.assertIn("I'll list the directory.", output)
        self.assertIn("Bash", output)
        self.assertNotIn("wants the file listed", output)

    def test_thinking_opt_in(self):
        output = render(parse_file(STREAM), show_thinking=True)
        self.assertIn("wants the file listed", output)

    def test_limit_truncates_and_reports_the_remainder(self):
        output = render(parse_file(STREAM), limit=10)
        self.assertIn("more characters]", output)

    def test_zero_limit_is_unlimited(self):
        output = render(parse_file(STREAM), limit=0)
        self.assertNotIn("more characters]", output)

    def test_color_only_when_enabled(self):
        self.assertNotIn("\033", render(parse_file(STREAM), color=False))
        self.assertIn("\033", render(parse_file(STREAM), color=True))

    def test_empty_events_render_as_nothing(self):
        transcript = parse_lines(['{"type":"assistant","message":{"content":[]}}'])
        self.assertEqual(format_event(transcript.events[0], Style(False)), "")

    def test_noise_hidden_unless_requested(self):
        transcript = parse_file(SESSION)
        self.assertNotIn("attachment", render(transcript))


class TestCli(unittest.TestCase):
    def test_show(self):
        self.assertIn("I'll list the directory.", run("show", STREAM))

    def test_show_json_is_valid_and_excludes_noise(self):
        events = json.loads(run("show", "--json", SESSION))
        self.assertTrue(all(e["kind"] != "noise" for e in events))

    def test_stats_json(self):
        summary = json.loads(run("stats", "--json", STREAM))
        self.assertEqual(summary["cost_usd"], 0.0125)
        self.assertEqual(summary["shape"], "stream")
        self.assertTrue(summary["complete"])

    def test_tools_filter_by_name(self):
        calls = json.loads(run("tools", "--json", "--name", "bash", STREAM))
        self.assertEqual([c["name"] for c in calls], ["Bash"])

    def test_tools_failed_only(self):
        calls = json.loads(run("tools", "--json", "--failed", STREAM))
        self.assertEqual([c["name"] for c in calls], ["Read"])

    def test_errors_reports_failures_denials_and_malformed(self):
        report = json.loads(run("errors", "--json", STREAM))
        self.assertEqual(len(report["failed_tool_calls"]), 1)
        self.assertEqual(len(report["permission_denials"]), 1)
        report = json.loads(run("errors", "--json", BROKEN))
        self.assertEqual(len(report["malformed_records"]), 2)

    def test_errors_text_when_clean(self):
        self.assertIn("no errors found", run("errors", SESSION))

    def test_missing_file_exits_nonzero(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(main(["stats", str(FIXTURES / "nope.jsonl")]), 2)

    def test_color_never_suppresses_ansi(self):
        self.assertNotIn("\033", run("--color", "never", "show", STREAM))


class TestAggregate(unittest.TestCase):
    """Multi-log aggregation: directory expansion, filtering, and roll-up."""

    def summaries(self):
        return [summarize(parse_file(str(path))) for path in (STREAM, SESSION, BROKEN)]

    def test_iter_log_paths_walks_directories_and_dedupes(self):
        paths = iter_log_paths([str(FIXTURES), str(STREAM)])
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(paths, sorted(paths))
        self.assertIn(str(STREAM), paths)

    def test_iter_log_paths_keeps_explicit_non_jsonl_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            odd = os.path.join(tmp, "log.txt")
            open(odd, "w").close()
            self.assertEqual(iter_log_paths([odd]), [odd])
            self.assertEqual(iter_log_paths([tmp]), [])

    def test_cost_sums_only_reported_costs(self):
        report = aggregate(self.summaries())
        self.assertEqual(report["files"], 3)
        # Only the stream fixture carries a result record with a cost.
        self.assertEqual(report["cost_known"], 1)
        self.assertEqual(report["cost_missing"], 2)
        self.assertAlmostEqual(report["cost_usd"], 0.0125)

    def test_totals_combine_tools_and_tokens(self):
        report = aggregate(self.summaries())
        per_file = [summarize(parse_file(str(p))) for p in (STREAM, SESSION, BROKEN)]
        self.assertEqual(report["tool_calls"], sum(s["tool_calls"] for s in per_file))
        self.assertEqual(report["tool_errors"], sum(s["tool_errors"] for s in per_file))
        self.assertEqual(
            report["tokens"]["output_tokens"],
            sum(s["tokens"].get("output_tokens", 0) for s in per_file),
        )
        self.assertEqual(report["malformed_records"], 2)

    def test_runs_are_sorted_and_dated(self):
        report = aggregate(self.summaries())
        whens = [run["when"] for run in report["runs"]]
        self.assertEqual(whens, sorted(whens))
        self.assertTrue(all(run["when"] for run in report["runs"]))

    def test_undated_log_falls_back_to_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = os.path.join(tmp, "empty.jsonl")
            open(empty, "w").close()
            report = aggregate([summarize(parse_file(empty))])
            self.assertEqual(report["runs"][0]["when_source"], "mtime")
            self.assertTrue(report["runs"][0]["when"].endswith("Z"))

    def test_in_range_accepts_any_iso_prefix(self):
        self.assertTrue(in_range("2026-09-04T23:00:00Z", since="2026-09"))
        self.assertTrue(in_range("2026-09-04T23:00:00Z", until="2026-09-04"))
        self.assertFalse(in_range("2026-09-05T00:00:00Z", until="2026-09-04"))
        self.assertFalse(in_range("2026-09-03T23:59:59Z", since="2026-09-04"))
        self.assertTrue(in_range(None))
        # An undated run cannot be shown to be inside a bound, so it is dropped.
        self.assertFalse(in_range(None, since="2026-09-04"))

    def test_filter_summaries_by_date(self):
        summaries = self.summaries()
        self.assertEqual(filter_summaries(summaries, since="2030-01-01"), [])
        self.assertEqual(len(filter_summaries(summaries, since="2000-01-01")), 3)

    def test_by_day_grouping(self):
        report = aggregate(self.summaries())
        for day, info in report["by_day"].items():
            self.assertRegex(day, r"^\d{4}-\d{2}-\d{2}$")
            self.assertGreaterEqual(info["runs"], 1)
        self.assertEqual(sum(i["runs"] for i in report["by_day"].values()), 3)

    def test_format_aggregate_mentions_partial_cost_coverage(self):
        text = format_aggregate(aggregate(self.summaries()))
        self.assertIn("report no cost", text)
        self.assertIn(str(STREAM), text)
        self.assertNotIn(str(STREAM), format_aggregate(aggregate(self.summaries()), show_runs=False))

    def test_cli_directory_produces_aggregate(self):
        text = run("stats", str(FIXTURES))
        self.assertIn("logs  3", text)
        report = json.loads(run("stats", "--json", str(FIXTURES)))
        self.assertEqual(report["files"], 3)

    def test_cli_single_file_still_reports_one_run(self):
        summary = json.loads(run("stats", "--json", str(STREAM)))
        self.assertEqual(summary["shape"], "stream")

    def test_cli_since_filter_switches_to_aggregate(self):
        report = json.loads(run("stats", "--json", "--since", "2030-01-01", str(STREAM)))
        self.assertEqual(report["files"], 0)

    def test_cli_all_files_unreadable_exits_nonzero(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["stats", str(FIXTURES / "nope.jsonl"), str(FIXTURES / "nope2.jsonl")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
