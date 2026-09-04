"""Normalized event model for Claude Code JSONL logs.

Two on-disk shapes are supported and normalized into the same ``Event``:

``stream``
    Output of ``claude -p --output-format stream-json --verbose``. Contains a
    ``system``/``init`` header, ``assistant``/``user`` turns, and a final
    ``result`` record carrying cost and usage totals.

``session``
    The transcript files Claude Code writes under
    ``~/.claude/projects/<slug>/<session-id>.jsonl``. Same message blocks, but
    wrapped with editor bookkeeping records (``attachment``, ``ai-title``,
    ``queue-operation``, ...) and no ``result`` record.

Records that cannot be parsed are surfaced as ``Event(kind="malformed")``
rather than raising, so a truncated or partially written log still renders.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

# Record types that carry no conversational content. They are parsed but
# marked ``noise`` so renderers can drop them by default.
_BOOKKEEPING = frozenset(
    {
        "attachment",
        "ai-title",
        "atis-latch",
        "last-prompt",
        "queue-operation",
        "summary",
        "file-history-snapshot",
    }
)


@dataclass
class ToolCall:
    """A single ``tool_use`` block, joined to its result if one was found."""

    id: str
    name: str
    input: dict[str, Any]
    # Filled in by :func:`link_tool_results`.
    result: str | None = None
    is_error: bool = False

    @property
    def target(self) -> str:
        """A short human-readable summary of what the call acted on.

        Falls back to the first string-valued input field so unknown tools
        still render something useful.
        """
        for key in ("file_path", "path", "command", "pattern", "url", "prompt"):
            value = self.input.get(key)
            if isinstance(value, str) and value:
                return value
        for value in self.input.values():
            if isinstance(value, str) and value:
                return value
        return ""


@dataclass
class Event:
    """One normalized log record."""

    kind: str  # system | assistant | user | result | noise | malformed
    index: int  # 0-based position in the file
    timestamp: str | None = None
    text: str = ""  # concatenated text blocks
    thinking: str = ""  # concatenated thinking blocks
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_sidechain(self) -> bool:
        """True when the record belongs to a subagent, not the main thread."""
        return bool(self.raw.get("isSidechain") or self.raw.get("parent_tool_use_id"))


@dataclass
class Transcript:
    """A parsed log file."""

    events: list[Event]
    shape: str  # "stream" | "session" | "unknown"
    path: str | None = None

    @property
    def complete(self) -> bool:
        """Whether the log looks finished.

        A stream log is finished only once its ``result`` record is written, so
        a run that is still going, was killed, or crashed is detectably
        truncated. Session transcripts have no terminator, so they are always
        reported complete.
        """
        if self.shape == "stream":
            return self.result is not None
        return True

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [call for event in self.events for call in event.tool_calls]

    @property
    def result(self) -> dict[str, Any] | None:
        for event in reversed(self.events):
            if event.kind == "result":
                return event.raw
        return None

    @property
    def system(self) -> dict[str, Any] | None:
        for event in self.events:
            if event.kind == "system":
                return event.raw
        return None


def _blocks(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the content blocks of a message record.

    Claude's API allows ``content`` to be a bare string; normalize that to a
    single text block so callers only handle one shape.
    """
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _parse_record(record: dict[str, Any], index: int) -> Event:
    record_type = record.get("type", "")
    if record_type in _BOOKKEEPING:
        return Event(kind="noise", index=index, timestamp=record.get("timestamp"), raw=record)

    if record_type in ("system", "result", "rate_limit_event"):
        kind = record_type if record_type in ("system", "result") else "noise"
        text = record.get("result", "") if record_type == "result" else ""
        return Event(
            kind=kind,
            index=index,
            timestamp=record.get("timestamp"),
            text=text if isinstance(text, str) else "",
            raw=record,
        )

    if record_type not in ("assistant", "user"):
        return Event(kind="noise", index=index, timestamp=record.get("timestamp"), raw=record)

    event = Event(kind=record_type, index=index, timestamp=record.get("timestamp"), raw=record)
    texts: list[str] = []
    thoughts: list[str] = []
    for block in _blocks(record):
        block_type = block.get("type")
        if block_type == "text":
            texts.append(str(block.get("text", "")))
        elif block_type == "thinking":
            thoughts.append(str(block.get("thinking", "")))
        elif block_type == "tool_use":
            raw_input = block.get("input")
            event.tool_calls.append(
                ToolCall(
                    id=str(block.get("id", "")),
                    name=str(block.get("name", "?")),
                    input=raw_input if isinstance(raw_input, dict) else {},
                )
            )
        elif block_type == "tool_result":
            event.tool_results.append(block)
    event.text = "\n".join(t for t in texts if t)
    event.thinking = "\n".join(t for t in thoughts if t)
    return event


def _result_text(block: dict[str, Any]) -> str:
    """Flatten a ``tool_result`` block's content into plain text."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, dict):
                parts.append(f"[{item.get('type', 'block')}]")
        return "\n".join(parts)
    return ""


def link_tool_results(events: Iterable[Event]) -> None:
    """Attach each ``tool_result`` to the ``tool_use`` that produced it."""
    events = list(events)
    by_id = {call.id: call for event in events for call in event.tool_calls if call.id}
    for event in events:
        for block in event.tool_results:
            call = by_id.get(str(block.get("tool_use_id", "")))
            if call is None:
                continue
            call.result = _result_text(block)
            call.is_error = bool(block.get("is_error"))


def detect_shape(events: list[Event]) -> str:
    """Classify a log as stream output or a session transcript.

    A ``result`` record is conclusive but only appears once a run finishes, so
    a live or killed run must be recognized from its ``system``/``init``
    header instead. Session transcripts carry per-record editor metadata
    (``cwd`` plus ``version``) that stream output never has.
    """
    for event in events:
        if event.kind == "result":
            return "stream"
        if event.kind == "system" and event.raw.get("subtype") == "init":
            return "stream"
    for event in events:
        if "version" in event.raw and "cwd" in event.raw:
            return "session"
    return "unknown"


def parse_lines(lines: Iterable[str]) -> Transcript:
    events: list[Event] = []
    for index, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            events.append(Event(kind="malformed", index=index, text=str(exc), raw={"line": line}))
            continue
        if not isinstance(record, dict):
            events.append(Event(kind="malformed", index=index, text="record is not an object"))
            continue
        events.append(_parse_record(record, index))
    link_tool_results(events)
    return Transcript(events=events, shape=detect_shape(events))


def parse_file(path: str) -> Transcript:
    """Parse a JSONL log. ``-`` reads stdin."""
    if path == "-":
        import sys

        transcript = parse_lines(sys.stdin)
        transcript.path = "-"
        return transcript
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        transcript = parse_lines(handle)
    transcript.path = path
    return transcript


def iter_events(transcript: Transcript, include_noise: bool = False) -> Iterator[Event]:
    for event in transcript.events:
        if event.kind == "noise" and not include_noise:
            continue
        yield event
