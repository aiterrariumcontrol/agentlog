"""``agentlog`` command line interface."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Sequence

from . import __version__
from .model import Transcript, parse_file
from .render import render, to_json
from .schema import format_inventory, inventory
from .stats import (
    aggregate,
    filter_summaries,
    format_aggregate,
    format_summary,
    iter_log_paths,
    summarize,
)

DESCRIPTION = """\
Read Claude Code JSONL logs.

Accepts both `claude -p --output-format stream-json` output and the session
transcripts under ~/.claude/projects/<slug>/<session-id>.jsonl. Use `-` to
read from stdin.
"""


def _use_color(choice: str, stream=sys.stdout) -> bool:
    if choice == "always":
        return True
    if choice == "never":
        return False
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", help="JSONL log file, or - for stdin")
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="emit JSON instead of text"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentlog",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"agentlog {__version__}")
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="colorize output (default: auto)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    show = subparsers.add_parser("show", help="render the conversation")
    _add_common(show)
    show.add_argument(
        "--limit",
        type=int,
        default=800,
        help="truncate each text or tool result to N characters (0 = unlimited)",
    )
    show.add_argument("--thinking", action="store_true", help="include thinking blocks")
    show.add_argument("--tool-input", action="store_true", help="include full tool inputs")
    show.add_argument(
        "--all", dest="include_noise", action="store_true", help="include bookkeeping records"
    )

    stats = subparsers.add_parser(
        "stats",
        help="cost, token, and tool totals for one log or a directory of logs",
    )
    stats.add_argument(
        "file",
        nargs="+",
        help="JSONL log files or directories to search recursively, or - for stdin",
    )
    stats.add_argument(
        "--json", action="store_true", dest="as_json", help="emit JSON instead of text"
    )
    stats.add_argument("--since", help="only runs at or after this ISO prefix, e.g. 2026-09-04")
    stats.add_argument("--until", help="only runs at or before this ISO prefix (inclusive)")
    stats.add_argument(
        "--no-runs", dest="show_runs", action="store_false", help="totals only, no per-log table"
    )

    tools = subparsers.add_parser("tools", help="list tool calls")
    _add_common(tools)
    tools.add_argument("--name", action="append", help="only this tool (repeatable)")
    tools.add_argument("--failed", action="store_true", help="only calls that returned an error")
    tools.add_argument(
        "--limit", type=int, default=400, help="truncate results to N characters (0 = unlimited)"
    )
    tools.add_argument("--tool-input", action="store_true", help="include full tool inputs")

    schema = subparsers.add_parser(
        "schema",
        help="field inventory of the record types present in one log or many",
    )
    schema.add_argument(
        "file",
        nargs="+",
        help="JSONL log files or directories to search recursively, or - for stdin",
    )
    schema.add_argument(
        "--json", action="store_true", dest="as_json", help="emit JSON instead of text"
    )

    errors = subparsers.add_parser(
        "errors", help="show failed tool calls, denials, and malformed records"
    )
    _add_common(errors)
    errors.add_argument(
        "--limit", type=int, default=2000, help="truncate results to N characters (0 = unlimited)"
    )

    return parser


def _cmd_show(args: argparse.Namespace, transcript: Transcript) -> str:
    if args.as_json:
        return to_json(
            [
                {
                    "kind": event.kind,
                    "index": event.index,
                    "timestamp": event.timestamp,
                    "text": event.text,
                    "thinking": event.thinking if args.thinking else "",
                    "sidechain": event.is_sidechain,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "name": call.name,
                            "input": call.input,
                            "result": call.result,
                            "is_error": call.is_error,
                        }
                        for call in event.tool_calls
                    ],
                }
                for event in transcript.events
                if args.include_noise or event.kind != "noise"
            ]
        )
    return render(
        transcript,
        color=_use_color(args.color),
        limit=args.limit,
        show_thinking=args.thinking,
        show_input=args.tool_input,
        include_noise=args.include_noise,
    )


def _cmd_stats(args: argparse.Namespace, transcript: Transcript) -> str:
    summary = summarize(transcript)
    return to_json(summary) if args.as_json else format_summary(summary)


def _run_stats(args: argparse.Namespace) -> tuple[str, int]:
    """``stats`` handles its own I/O because it may read many files."""
    paths = iter_log_paths(args.file)
    filtering = args.since is not None or args.until is not None
    if len(paths) == 1 and not filtering:
        transcript = parse_file(paths[0])
        return _cmd_stats(args, transcript), 0

    summaries = []
    failures = 0
    for path in paths:
        try:
            summaries.append(summarize(parse_file(path)))
        except OSError as exc:
            print(f"agentlog: {exc}", file=sys.stderr)
            failures += 1
    if filtering:
        summaries = filter_summaries(summaries, args.since, args.until)
    report = aggregate(summaries)
    return (
        to_json(report) if args.as_json else format_aggregate(report, args.show_runs),
        2 if failures and not summaries else 0,
    )


def _run_schema(args: argparse.Namespace) -> tuple[str, int]:
    """``schema`` handles its own I/O because it may read many files."""
    transcripts = []
    failures = 0
    for path in iter_log_paths(args.file):
        try:
            transcripts.append(parse_file(path))
        except OSError as exc:
            print(f"agentlog: {exc}", file=sys.stderr)
            failures += 1
    report = inventory(transcripts)
    return (
        to_json(report) if args.as_json else format_inventory(report),
        2 if failures and not transcripts else 0,
    )


def _selected_calls(args: argparse.Namespace, transcript: Transcript):
    wanted = {name.lower() for name in (args.name or [])}
    for call in transcript.tool_calls:
        if wanted and call.name.lower() not in wanted:
            continue
        if getattr(args, "failed", False) and not call.is_error:
            continue
        yield call


def _cmd_tools(args: argparse.Namespace, transcript: Transcript) -> str:
    calls = list(_selected_calls(args, transcript))
    if args.as_json:
        return to_json(
            [
                {
                    "id": call.id,
                    "name": call.name,
                    "input": call.input,
                    "result": call.result,
                    "is_error": call.is_error,
                }
                for call in calls
            ]
        )
    from .render import Style, format_tool_call

    style = Style(_use_color(args.color))
    return "\n\n".join(
        format_tool_call(call, style, args.limit, args.tool_input) for call in calls
    )


def _cmd_errors(args: argparse.Namespace, transcript: Transcript) -> str:
    failed = [call for call in transcript.tool_calls if call.is_error]
    malformed = [event for event in transcript.events if event.kind == "malformed"]
    result = transcript.result or {}
    denials = result.get("permission_denials") or []
    report = {
        "failed_tool_calls": [
            {"name": call.name, "target": call.target, "result": call.result} for call in failed
        ],
        "permission_denials": denials,
        "malformed_records": [
            {"line": event.index + 1, "error": event.text} for event in malformed
        ],
        "run_errored": result.get("is_error"),
        "api_error_status": result.get("api_error_status"),
    }
    if args.as_json:
        return to_json(report)

    from .render import Style, _clip, format_tool_call

    style = Style(_use_color(args.color))
    sections: list[str] = []
    if failed:
        sections.append(
            "failed tool calls:\n\n"
            + "\n\n".join(format_tool_call(call, style, args.limit, False) for call in failed)
        )
    if denials:
        sections.append("permission denials:\n" + _clip(to_json(denials), args.limit))
    if malformed:
        sections.append(
            "malformed records:\n"
            + "\n".join(f"  line {event.index + 1}: {event.text}" for event in malformed)
        )
    if result.get("is_error"):
        sections.append(f"run errored: api_error_status={result.get('api_error_status')}")
    return "\n\n".join(sections) if sections else "no errors found"


def _emit(output: str) -> int:
    if output:
        try:
            print(output)
        except BrokenPipeError:  # e.g. piping into `head`
            sys.stdout.close()
    return 0


_COMMANDS = {"show": _cmd_show, "stats": _cmd_stats, "tools": _cmd_tools, "errors": _cmd_errors}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in ("stats", "schema"):
        runner = _run_stats if args.command == "stats" else _run_schema
        try:
            output, code = runner(args)
        except OSError as exc:
            print(f"agentlog: {exc}", file=sys.stderr)
            return 2
        return _emit(output) or code
    try:
        transcript = parse_file(args.file)
    except OSError as exc:
        print(f"agentlog: {exc}", file=sys.stderr)
        return 2

    return _emit(_COMMANDS[args.command](args, transcript))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
