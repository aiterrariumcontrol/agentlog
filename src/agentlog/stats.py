"""Cost, token, and tool-usage aggregation over a parsed transcript."""

from __future__ import annotations

from collections import Counter
from typing import Any

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
