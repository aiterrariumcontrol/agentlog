"""Empirical field inventory for Claude Code JSONL logs.

Claude Code's log formats are internal and undocumented, so the only honest
description of them is one derived from records that actually exist. This
module walks a corpus of logs and reports, per record ``type``, which field
paths appear, how often, with which JSON types, and with which observed
values.

Nothing here guesses. A field is reported only because it was seen, and the
sample size is always carried alongside so a reader can judge how much the
inventory is worth.
"""

from __future__ import annotations

from typing import Any, Iterable

from .model import Transcript

# Example values exist to document enumerations — the block types, subtypes and
# stop reasons you need in order to write a parser. They are deliberately not a
# window into log content: a log is somebody's transcript, and this command's
# output is meant to be pasteable into a bug report or a format document.
#
# Three filters enforce that. Fields whose leaf name is known free-form content
# never show examples at all; values that are long or contain line breaks are
# dropped; and a field with more distinct values than an enumeration plausibly
# has reports ``(varies)`` instead of listing them, which also removes
# high-cardinality identifiers like paths, ids and timestamps.
_OPAQUE_LEAVES = frozenset(
    {
        "text",
        "thinking",
        "signature",
        "content",
        "result",
        "input",
        "command",
        "description",
        "prompt",
        "summary",
        "snippet",
        "commit",
        "pr",
        "stdout",
        "stderr",
        "output",
        "old_string",
        "new_string",
        "oldString",
        "newString",
        "originalFile",
    }
)

_MAX_EXAMPLES = 5
_EXAMPLE_CHARS = 32
_MAX_DEPTH = 6


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _is_opaque(path: str) -> bool:
    leaf = path.rsplit(".", 1)[-1]
    if leaf.endswith("[]"):
        leaf = leaf[:-2]
    return leaf in _OPAQUE_LEAVES


def _example(value: Any) -> str | None:
    """Render ``value`` as an example, or ``None`` if it must not be shown."""
    if isinstance(value, (dict, list)) or value is None:
        return None
    if isinstance(value, str):
        if len(value) > _EXAMPLE_CHARS or value.strip() != value:
            return None
        if any(character in value for character in "\n\r\t"):
            return None
        # Paths, URLs and addresses are never the enumeration you are trying
        # to document, and they say more about the log's author than about
        # the format.
        if value.startswith(("/", "~/", ".")) or "://" in value or "@" in value:
            return None
        return value or None
    return str(value)


class _Field:
    __slots__ = ("count", "types", "_values", "varies")

    def __init__(self) -> None:
        self.count = 0
        self.types: set[str] = set()
        self._values: list[str] = []
        self.varies = False

    def observe(self, path: str, value: Any, counts: bool) -> None:
        # ``counts`` is false for repeat occurrences within one record, so
        # ``count`` stays "records containing this field" while types and
        # examples still see every list element.
        if counts:
            self.count += 1
        self.types.add(_json_type(value))
        if self.varies or _is_opaque(path):
            self.varies = self.varies or _is_opaque(path)
            return
        rendered = _example(value)
        if rendered is None or rendered in self._values:
            return
        if len(self._values) == _MAX_EXAMPLES:
            # More distinct values than an enumeration would have: this is
            # data, not structure.
            self._values.clear()
            self.varies = True
            return
        self._values.append(rendered)

    @property
    def examples(self) -> list[str]:
        return list(self._values)


class _Group:
    """Everything observed for one record ``type``."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.records = 0
        self.fields: dict[str, _Field] = {}

    def observe(self, record: dict[str, Any]) -> None:
        self.records += 1
        # A field seen twice in one record (via a list) counts once, so the
        # count/records ratio stays readable as "how often is this present".
        for path, value, first in _walk(record):
            self.fields.setdefault(path, _Field()).observe(path, value, first)


def _walk(record: dict[str, Any]) -> Iterable[tuple[str, Any, bool]]:
    """Yield ``(path, value, first)`` for every value in one record.

    Nested objects flatten with ``.``; list elements collapse into a single
    ``[]`` path, so a hundred content blocks describe one field rather than a
    hundred. ``first`` marks the first occurrence of a path within this
    record, which lets the caller count presence per record while still
    seeing every distinct value a repeated path takes.
    """
    seen: set[str] = set()
    stack: list[tuple[str, Any, int]] = [("", record, 0)]
    while stack:
        prefix, value, depth = stack.pop()
        if prefix:
            first = prefix not in seen
            seen.add(prefix)
            yield prefix, value, first
        if depth >= _MAX_DEPTH:
            continue
        if isinstance(value, dict):
            for key in sorted(value, reverse=True):
                child = f"{prefix}.{key}" if prefix else str(key)
                stack.append((child, value[key], depth + 1))
        elif isinstance(value, list):
            for item in reversed(value):
                stack.append((f"{prefix}[]", item, depth + 1))


def _record_type(record: dict[str, Any]) -> str:
    """The grouping key: ``type``, refined by ``subtype`` when one exists.

    ``system`` records in particular mean very different things depending on
    their subtype, so collapsing them together would hide the difference.
    """
    base = record.get("type")
    base = str(base) if isinstance(base, (str, int)) else "(no type)"
    subtype = record.get("subtype")
    if isinstance(subtype, str) and subtype:
        return f"{base}/{subtype}"
    return base


def inventory(transcripts: Iterable[Transcript]) -> dict[str, Any]:
    """Build a field inventory, split by log shape.

    Stream output and session transcripts are different formats that share
    record types, so merging them would invent a format that no file has.
    """
    shapes: dict[str, dict[str, Any]] = {}
    for transcript in transcripts:
        bucket = shapes.setdefault(
            transcript.shape, {"logs": 0, "records": 0, "malformed": 0, "groups": {}}
        )
        bucket["logs"] += 1
        for event in transcript.events:
            if event.kind == "malformed":
                bucket["malformed"] += 1
                continue
            if not event.raw:
                continue
            bucket["records"] += 1
            name = _record_type(event.raw)
            group = bucket["groups"].setdefault(name, _Group(name))
            group.observe(event.raw)

    return {
        "shapes": {
            shape: {
                "logs": bucket["logs"],
                "records": bucket["records"],
                "malformed": bucket["malformed"],
                "record_types": [
                    {
                        "type": group.name,
                        "records": group.records,
                        "fields": [
                            {
                                "path": path,
                                "count": field.count,
                                "always": field.count == group.records,
                                "types": sorted(field.types),
                                "examples": field.examples,
                                "varies": field.varies,
                            }
                            for path, field in sorted(group.fields.items())
                        ],
                    }
                    for group in sorted(
                        bucket["groups"].values(), key=lambda g: (-g.records, g.name)
                    )
                ],
            }
            for shape, bucket in sorted(shapes.items())
        }
    }


def format_inventory(report: dict[str, Any]) -> str:
    lines: list[str] = []
    for shape, bucket in report["shapes"].items():
        if lines:
            lines.append("")
        header = (
            f"shape: {shape}  ({bucket['logs']} logs, {bucket['records']:,} records"
        )
        if bucket["malformed"]:
            header += f", {bucket['malformed']} malformed"
        lines.append(header + ")")
        for group in bucket["record_types"]:
            lines.append("")
            lines.append(f"  {group['type']}  ×{group['records']:,}")
            width = max((len(f["path"]) for f in group["fields"]), default=0)
            width = min(width, 52)
            for field in group["fields"]:
                presence = "" if field["always"] else f" {field['count']}/{group['records']}"
                types = "|".join(field["types"])
                row = f"    {field['path']:<{width}}  {types}{presence}"
                if field["examples"]:
                    row += "  " + ", ".join(field["examples"])
                elif field["varies"]:
                    row += "  (varies)"
                lines.append(row)
    return "\n".join(lines)
