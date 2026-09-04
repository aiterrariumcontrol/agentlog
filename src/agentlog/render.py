"""Human-readable rendering of a parsed transcript."""

from __future__ import annotations

import json
import shutil
from typing import Any, Iterable

from .model import Event, ToolCall, Transcript, iter_events

_COLORS = {
    "assistant": "\033[36m",  # cyan
    "user": "\033[32m",  # green
    "tool": "\033[35m",  # magenta
    "error": "\033[31m",  # red
    "dim": "\033[2m",
    "reset": "\033[0m",
}


class Style:
    """Optional ANSI styling. Disabled writes plain text."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, name: str, text: str) -> str:
        if not self.enabled or name not in _COLORS:
            return text
        return f"{_COLORS[name]}{text}{_COLORS['reset']}"


def _clip(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` characters, noting what was dropped.

    ``limit <= 0`` means unlimited.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    dropped = len(text) - limit
    return f"{text[:limit]}\n... [{dropped} more characters]"


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def _short_time(timestamp: str | None) -> str:
    if not timestamp:
        return ""
    # ISO-8601: keep HH:MM:SS, drop date and sub-second precision.
    if "T" in timestamp and len(timestamp) >= 19:
        return timestamp[11:19]
    return timestamp


def format_tool_call(call: ToolCall, style: Style, limit: int, show_input: bool) -> str:
    header = style("tool", f"⚙ {call.name}")
    target = call.target.replace("\n", " ")
    if target:
        header += " " + style("dim", _clip(target, 120))
    lines = [header]
    if show_input:
        rendered = json.dumps(call.input, indent=2, ensure_ascii=False, default=str)
        lines.append(_indent(_clip(rendered, limit), "    "))
    if call.result is not None:
        label = "error" if call.is_error else "dim"
        marker = "✗" if call.is_error else "→"
        body = _clip(call.result.strip(), limit)
        if body:
            lines.append(_indent(style(label, f"{marker} {body}"), "    "))
        elif call.is_error:
            lines.append(_indent(style("error", f"{marker} (empty error)"), "    "))
    return "\n".join(lines)


def format_event(
    event: Event,
    style: Style,
    limit: int = 800,
    show_thinking: bool = False,
    show_input: bool = False,
) -> str:
    """Render one event, or ``""`` if it has nothing worth showing."""
    parts: list[str] = []
    stamp = _short_time(event.timestamp)
    prefix = style("dim", f"[{stamp}] ") if stamp else ""

    if event.kind == "malformed":
        return style("error", f"{prefix}! unparseable record at line {event.index + 1}: {event.text}")

    if event.kind == "system":
        model = event.raw.get("model", "?")
        cwd = event.raw.get("cwd", "?")
        return style("dim", f"{prefix}— session start · model={model} · cwd={cwd}")

    if event.kind == "result":
        cost = event.raw.get("total_cost_usd")
        cost_text = f" · ${cost:.4f}" if isinstance(cost, (int, float)) else ""
        status = "error" if event.raw.get("is_error") else "ok"
        return style("dim", f"{prefix}— result: {status}{cost_text}")

    role = "assistant" if event.kind == "assistant" else "user"
    tag = "◆ assistant" if role == "assistant" else "▶ user"
    if event.is_sidechain:
        tag += " (subagent)"

    if show_thinking and event.thinking:
        parts.append(style("dim", _indent(_clip(event.thinking, limit), "  │ ")))
    if event.text.strip():
        parts.append(_indent(_clip(event.text.strip(), limit)))
    for call in event.tool_calls:
        parts.append(_indent(format_tool_call(call, style, limit, show_input)))

    if not parts:
        return ""
    return "\n".join([prefix + style(role, tag), *parts])


def render(
    transcript: Transcript,
    color: bool = False,
    limit: int = 800,
    show_thinking: bool = False,
    show_input: bool = False,
    include_noise: bool = False,
    events: Iterable[Event] | None = None,
) -> str:
    style = Style(color)
    source = iter_events(transcript, include_noise) if events is None else events
    chunks = [
        formatted
        for event in source
        if (formatted := format_event(event, style, limit, show_thinking, show_input))
    ]
    return "\n\n".join(chunks)


def terminal_width(default: int = 100) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except OSError:  # pragma: no cover - environment dependent
        return default


def to_json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)
