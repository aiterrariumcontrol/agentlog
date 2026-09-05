"""Cost, token, and tool-usage aggregation, for one transcript or many.

:func:`summarize` reports a single parsed transcript. :func:`aggregate` rolls
several of those summaries into one report over a whole directory of logs.
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

from .model import Transcript

_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _add_usage(total: dict[str, int], usage: Any) -> None:
    if not isinstance(usage, dict):
        return
    for name in _USAGE_FIELDS:
        value = usage.get(name)
        if isinstance(value, int):
            total[name] = total.get(name, 0) + value
    details = usage.get("output_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("thinking_tokens"), int):
        total["thinking_tokens"] = total.get("thinking_tokens", 0) + details["thinking_tokens"]


def summarize(transcript: Transcript) -> dict[str, Any]:
    """Return a JSON-serializable summary of a transcript.

    Token totals come from the final ``result`` record when present, because
    that is authoritative for a stream run. Session transcripts have no such
    record, so per-message ``usage`` is summed instead. The ``token_source``
    field says which path was taken.
    """
    result = transcript.result
    tools = Counter(call.name for call in transcript.tool_calls)
    failed = Counter(call.name for call in transcript.tool_calls if call.is_error)

    tokens: dict[str, int] = {}
    if result and isinstance(result.get("usage"), dict):
        _add_usage(tokens, result["usage"])
        token_source = "result"
    else:
        for event in transcript.events:
            if event.kind == "assistant":
                message = event.raw.get("message")
                if isinstance(message, dict):
                    _add_usage(tokens, message.get("usage"))
        token_source = "messages"

    summary: dict[str, Any] = {
        "path": transcript.path,
        "shape": transcript.shape,
        "complete": transcript.complete,
        "records": len(transcript.events),
        "assistant_turns": sum(1 for e in transcript.events if e.kind == "assistant"),
        "user_turns": sum(1 for e in transcript.events if e.kind == "user"),
        "malformed_records": sum(1 for e in transcript.events if e.kind == "malformed"),
        "tool_calls": sum(tools.values()),
        "tool_errors": sum(failed.values()),
        "tools": dict(tools.most_common()),
        "failing_tools": dict(failed.most_common()),
        "token_source": token_source,
        "tokens": tokens,
    }

    first = next((e.timestamp for e in transcript.events if e.timestamp), None)
    last = next((e.timestamp for e in reversed(transcript.events) if e.timestamp), None)
    summary["started_at"] = first
    summary["ended_at"] = last

    if result:
        summary["session_id"] = result.get("session_id")
        summary["cost_usd"] = result.get("total_cost_usd")
        summary["duration_ms"] = result.get("duration_ms")
        summary["duration_api_ms"] = result.get("duration_api_ms")
        summary["num_turns"] = result.get("num_turns")
        summary["is_error"] = result.get("is_error")
        summary["stop_reason"] = result.get("stop_reason")
        summary["permission_denials"] = len(result.get("permission_denials") or [])
        model_usage = result.get("modelUsage")
        if isinstance(model_usage, dict):
            summary["models"] = {
                name: {
                    "cost_usd": info.get("costUSD"),
                    "input_tokens": info.get("inputTokens"),
                    "output_tokens": info.get("outputTokens"),
                    "cache_read_input_tokens": info.get("cacheReadInputTokens"),
                    "cache_creation_input_tokens": info.get("cacheCreationInputTokens"),
                }
                for name, info in model_usage.items()
                if isinstance(info, dict)
            }

    system = transcript.system
    if system:
        summary.setdefault("session_id", system.get("session_id"))
        summary["model"] = system.get("model")
        summary["cwd"] = system.get("cwd")
        summary["permission_mode"] = system.get("permissionMode")
        summary["claude_code_version"] = system.get("claude_code_version")

    return summary


def format_summary(summary: dict[str, Any]) -> str:
    """Render :func:`summarize` output as an aligned plain-text report."""
    rows: list[tuple[str, str]] = []

    def add(label: str, value: Any, suffix: str = "") -> None:
        if value is None or value == "" or value == {}:
            return
        rows.append((label, f"{value}{suffix}"))

    add("file", summary.get("path"))
    add("shape", summary.get("shape"))
    if summary.get("complete") is False:
        add("complete", "no (run truncated or still in progress)")
    add("session", summary.get("session_id"))
    add("model", summary.get("model"))
    add("started", summary.get("started_at"))
    add("ended", summary.get("ended_at"))
    duration = summary.get("duration_ms")
    if isinstance(duration, (int, float)):
        add("duration", f"{duration / 1000:.1f}", "s")
    api_duration = summary.get("duration_api_ms")
    if isinstance(api_duration, (int, float)):
        add("api time", f"{api_duration / 1000:.1f}", "s")
    cost = summary.get("cost_usd")
    if isinstance(cost, (int, float)):
        add("cost", f"${cost:.6f}")
    add("stop reason", summary.get("stop_reason"))
    if summary.get("is_error"):
        add("errored", "yes")
    add("records", summary.get("records"))
    add("assistant turns", summary.get("assistant_turns"))
    add("tool calls", summary.get("tool_calls"))
    if summary.get("tool_errors"):
        add("tool errors", summary.get("tool_errors"))
    if summary.get("permission_denials"):
        add("permission denials", summary.get("permission_denials"))
    if summary.get("malformed_records"):
        add("malformed records", summary.get("malformed_records"))

    width = max((len(label) for label, _ in rows), default=0)
    lines = [f"{label.rjust(width)}  {value}" for label, value in rows]

    tokens = summary.get("tokens") or {}
    if tokens:
        lines.append("")
        lines.append(f"tokens ({summary.get('token_source')})")
        token_width = max(len(name) for name in tokens)
        for name, value in tokens.items():
            lines.append(f"  {name.rjust(token_width)}  {value:,}")

    models = summary.get("models") or {}
    if models:
        lines.append("")
        lines.append("per model")
        for name, info in models.items():
            cost = info.get("cost_usd")
            cost_text = f"${cost:.6f}" if isinstance(cost, (int, float)) else "?"
            lines.append(
                f"  {name}: {cost_text}  in={info.get('input_tokens')} "
                f"out={info.get('output_tokens')} cache_read={info.get('cache_read_input_tokens')}"
            )

    tools = summary.get("tools") or {}
    if tools:
        lines.append("")
        lines.append("tools")
        tool_width = max(len(name) for name in tools)
        failing = summary.get("failing_tools") or {}
        for name, count in tools.items():
            note = f"  ({failing[name]} failed)" if failing.get(name) else ""
            lines.append(f"  {name.rjust(tool_width)}  {count}{note}")

    return "\n".join(lines)


# --- multi-file aggregation -------------------------------------------------

_JSONL_SUFFIX = ".jsonl"


def iter_log_paths(paths: Iterable[str]) -> list[str]:
    """Expand a mix of files and directories into a sorted list of log files.

    Directories are walked recursively and contribute every ``*.jsonl`` file
    inside them. Explicitly named files are kept whatever their suffix, so an
    oddly named log can still be inspected on purpose.
    """
    found: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if os.path.isdir(path):
            matches: list[str] = []
            for root, dirs, files in os.walk(path):
                dirs.sort()
                matches.extend(
                    os.path.join(root, name)
                    for name in files
                    if name.endswith(_JSONL_SUFFIX)
                )
            candidates = sorted(matches)
        else:
            candidates = [path]
        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                found.append(candidate)
    return found


def _run_time(summary: dict[str, Any]) -> tuple[str | None, str]:
    """Return ``(iso_timestamp, source)`` for ordering and grouping a run.

    Stream logs carry per-message timestamps, so the first one is used. Logs
    with no usable timestamp at all — an empty or header-only file — fall back
    to the file's modification time, and the source is reported so the caller
    knows the value is an approximation rather than log content.
    """
    started = summary.get("started_at")
    if isinstance(started, str) and started:
        return started, "log"
    path = summary.get("path")
    if isinstance(path, str) and os.path.exists(path):
        stamp = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
        return stamp.isoformat(timespec="seconds").replace("+00:00", "Z"), "mtime"
    return None, "none"


def in_range(when: str | None, since: str | None = None, until: str | None = None) -> bool:
    """Prefix-compare an ISO timestamp against ``since``/``until`` bounds.

    Both bounds may be any ISO prefix: ``2026-09`` selects a month and
    ``2026-09-04T23`` selects an hour. Both ends are inclusive. A run with no
    timestamp is excluded whenever a bound is given, because it cannot be
    shown to fall inside it.
    """
    if since is None and until is None:
        return True
    if not when:
        return False
    if since is not None and when[: len(since)] < since:
        return False
    if until is not None and when[: len(until)] > until:
        return False
    return True


def filter_summaries(
    summaries: Iterable[dict[str, Any]], since: str | None = None, until: str | None = None
) -> list[dict[str, Any]]:
    """Keep only the summaries whose run time falls within the bounds."""
    return [s for s in summaries if in_range(_run_time(s)[0], since, until)]


def aggregate(summaries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Roll up :func:`summarize` outputs from several logs into one report.

    Cost is summed only over runs that actually reported one; ``cost_known``
    and ``cost_missing`` say how many runs each. This deliberately never
    estimates the cost of a run that did not state it, so the total is a
    floor, not a guess.
    """
    runs: list[dict[str, Any]] = []
    tools: Counter[str] = Counter()
    failing: Counter[str] = Counter()
    tokens: dict[str, int] = {}
    models: dict[str, dict[str, Any]] = {}
    by_day: dict[str, dict[str, Any]] = {}
    cost_total = 0.0
    cost_known = 0
    cost_missing = 0
    duration_ms = 0
    incomplete = 0
    errored = 0
    malformed = 0

    for summary in summaries:
        when, when_source = _run_time(summary)
        cost = summary.get("cost_usd")
        has_cost = isinstance(cost, (int, float))
        if has_cost:
            cost_total += float(cost)
            cost_known += 1
        else:
            cost_missing += 1

        tools.update(summary.get("tools") or {})
        failing.update(summary.get("failing_tools") or {})
        for name, value in (summary.get("tokens") or {}).items():
            if isinstance(value, int):
                tokens[name] = tokens.get(name, 0) + value
        for name, info in (summary.get("models") or {}).items():
            slot = models.setdefault(name, {"runs": 0, "cost_usd": 0.0})
            slot["runs"] += 1
            if isinstance(info.get("cost_usd"), (int, float)):
                slot["cost_usd"] += float(info["cost_usd"])
            for field_name in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
                value = info.get(field_name)
                if isinstance(value, int):
                    slot[field_name] = slot.get(field_name, 0) + value

        if isinstance(summary.get("duration_ms"), (int, float)):
            duration_ms += int(summary["duration_ms"])
        if summary.get("complete") is False:
            incomplete += 1
        if summary.get("is_error"):
            errored += 1
        malformed += summary.get("malformed_records") or 0

        run_tokens = summary.get("tokens") or {}
        total_tokens = sum(v for v in run_tokens.values() if isinstance(v, int))
        if when:
            day = by_day.setdefault(
                when[:10],
                {"runs": 0, "cost_usd": 0.0, "cost_missing": 0, "tokens": 0, "tool_calls": 0},
            )
            day["runs"] += 1
            if has_cost:
                day["cost_usd"] += float(cost)
            else:
                day["cost_missing"] += 1
            day["tokens"] += total_tokens
            day["tool_calls"] += summary.get("tool_calls") or 0

        runs.append(
            {
                "path": summary.get("path"),
                "when": when,
                "when_source": when_source,
                "shape": summary.get("shape"),
                "complete": summary.get("complete"),
                "session_id": summary.get("session_id"),
                "model": summary.get("model"),
                "cost_usd": cost if has_cost else None,
                "duration_ms": summary.get("duration_ms"),
                "tokens": total_tokens,
                "tool_calls": summary.get("tool_calls") or 0,
                "tool_errors": summary.get("tool_errors") or 0,
                "is_error": summary.get("is_error"),
            }
        )

    runs.sort(key=lambda run: (run["when"] or "", run["path"] or ""))
    dated = [run["when"] for run in runs if run["when"]]
    return {
        "files": len(runs),
        "range": {"from": dated[0] if dated else None, "to": dated[-1] if dated else None},
        "cost_usd": round(cost_total, 6),
        "cost_known": cost_known,
        "cost_missing": cost_missing,
        "duration_ms": duration_ms,
        "incomplete": incomplete,
        "errored": errored,
        "malformed_records": malformed,
        "tokens": tokens,
        "tool_calls": sum(tools.values()),
        "tool_errors": sum(failing.values()),
        "tools": dict(tools.most_common()),
        "failing_tools": dict(failing.most_common()),
        "models": models,
        "by_day": dict(sorted(by_day.items())),
        "runs": runs,
    }


def format_aggregate(report: dict[str, Any], show_runs: bool = True) -> str:
    """Render :func:`aggregate` output as a plain-text report."""
    lines: list[str] = []

    if show_runs and report["runs"]:
        header = ("when", "cost", "tokens", "tools", "log")
        table: list[tuple[str, str, str, str, str]] = [header]
        for run in report["runs"]:
            when = run["when"] or "?"
            if run["when_source"] == "mtime":
                when += " (mtime)"
            cost = run["cost_usd"]
            cost_text = f"${cost:.4f}" if isinstance(cost, (int, float)) else "-"
            flags = ""
            if run["is_error"]:
                flags += " !err"
            if run["complete"] is False:
                flags += " !partial"
            if run["tool_errors"]:
                flags += f" ({run['tool_errors']} tool err)"
            table.append(
                (
                    when,
                    cost_text,
                    f"{run['tokens']:,}",
                    str(run["tool_calls"]),
                    f"{run['path']}{flags}",
                )
            )
        widths = [max(len(row[i]) for row in table) for i in range(4)]
        for index, row in enumerate(table):
            lines.append(
                f"{row[0].ljust(widths[0])}  {row[1].rjust(widths[1])}  "
                f"{row[2].rjust(widths[2])}  {row[3].rjust(widths[3])}  {row[4]}"
            )
            if index == 0:
                lines.append("-" * (sum(widths) + 8 + len(row[4])))
        lines.append("")

    rows: list[tuple[str, str]] = [("logs", str(report["files"]))]
    span = report["range"]
    if span["from"]:
        rows.append(("range", f"{span['from']} .. {span['to']}"))
    cost_note = ""
    if report["cost_missing"]:
        cost_note = f"  (from {report['cost_known']}/{report['files']} logs; rest report no cost)"
    rows.append(("cost", f"${report['cost_usd']:.6f}{cost_note}"))
    if report["duration_ms"]:
        rows.append(("wall time", f"{report['duration_ms'] / 1000:.1f}s"))
    rows.append(("tool calls", str(report["tool_calls"])))
    if report["tool_errors"]:
        rows.append(("tool errors", str(report["tool_errors"])))
    if report["incomplete"]:
        rows.append(("incomplete logs", str(report["incomplete"])))
    if report["errored"]:
        rows.append(("errored runs", str(report["errored"])))
    if report["malformed_records"]:
        rows.append(("malformed records", str(report["malformed_records"])))
    width = max(len(label) for label, _ in rows)
    lines.extend(f"{label.rjust(width)}  {value}" for label, value in rows)

    tokens = report["tokens"]
    if tokens:
        lines.append("")
        lines.append("tokens")
        token_width = max(len(name) for name in tokens)
        for name, value in tokens.items():
            lines.append(f"  {name.rjust(token_width)}  {value:,}")

    by_day = report["by_day"]
    if len(by_day) > 1:
        lines.append("")
        lines.append("by day")
        for day, info in by_day.items():
            note = f"  ({info['cost_missing']} without cost)" if info["cost_missing"] else ""
            lines.append(
                f"  {day}  {info['runs']} {'run' if info['runs'] == 1 else 'runs'}  "
                f"${info['cost_usd']:.4f}  "
                f"{info['tokens']:,} tokens  {info['tool_calls']} tool calls{note}"
            )

    models = report["models"]
    if models:
        lines.append("")
        lines.append("per model")
        for name, info in sorted(models.items()):
            lines.append(
                f"  {name}: ${info['cost_usd']:.6f} over {info['runs']} "
                f"{'run' if info['runs'] == 1 else 'runs'}  "
                f"in={info.get('input_tokens', 0):,} out={info.get('output_tokens', 0):,} "
                f"cache_read={info.get('cache_read_input_tokens', 0):,}"
            )

    tools = report["tools"]
    if tools:
        lines.append("")
        lines.append("tools")
        tool_width = max(len(name) for name in tools)
        failing = report["failing_tools"]
        for name, count in tools.items():
            note = f"  ({failing[name]} failed)" if failing.get(name) else ""
            lines.append(f"  {name.rjust(tool_width)}  {count}{note}")

    return "\n".join(lines)
