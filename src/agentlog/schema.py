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

import re
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

# Timestamps and dates are measurements, not structure, and in a small corpus
# there may be few enough of them to slip under _MAX_EXAMPLES and look like an
# enumeration. Excluding them keeps a baseline stable across runs.
_TIMESTAMPish = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]|$)|^\d{2}:\d{2}:\d{2}")


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
    # A number is a quantity, and the declared type already says which kind.
    # Listing token counts or costs as though they enumerated something would
    # make the inventory churn on every corpus. Booleans do enumerate.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
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
        if value.isdigit() or _TIMESTAMPish.match(value):
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
        if isinstance(value, (dict, list)) or value is None:
            return  # structure and absence; `types` already describes these
        rendered = _example(value)
        if rendered is None:
            # A value the filters refuse to print — a uuid, a path, a number,
            # a timestamp. It is still evidence that this field is not an
            # enumeration, and saying so keeps drift reports from treating a
            # short id in some other corpus as a new enum member.
            self._values.clear()
            self.varies = True
            return
        if rendered in self._values:
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
            transcript.shape,
            {"logs": 0, "records": 0, "malformed": 0, "groups": {}, "versions": set()},
        )
        bucket["logs"] += 1
        for event in transcript.events:
            if event.kind == "malformed":
                bucket["malformed"] += 1
                continue
            if not event.raw:
                continue
            bucket["records"] += 1
            # Session transcripts stamp `version` on every record; stream
            # output carries `claude_code_version` on its system/init header.
            for key in ("version", "claude_code_version"):
                version = event.raw.get(key)
                if isinstance(version, str) and version:
                    bucket["versions"].add(version)
            name = _record_type(event.raw)
            group = bucket["groups"].setdefault(name, _Group(name))
            group.observe(event.raw)

    return {
        "shapes": {
            shape: {
                "logs": bucket["logs"],
                "records": bucket["records"],
                "malformed": bucket["malformed"],
                # Which Claude Code releases this corpus was written by, so a
                # stored baseline says what the format description is true of.
                "versions": sorted(bucket["versions"]),
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


# --- drift detection -------------------------------------------------------
#
# The log formats are undocumented internals, so a description of them is only
# true of the releases it was derived from. `compare` turns "keep the docs
# current" into something runnable: store an inventory as a baseline, re-run it
# against a fresh corpus, and get the list of differences.
#
# The two directions are not equally strong evidence. Something *new* — a field
# path, a record type, a value in an enumeration — is proof the format grew,
# because it was actually observed. Something *absent* may only mean this
# corpus did not happen to exercise it. Both are reported; only the reader can
# weigh them, so the distinction is carried in `signal` rather than hidden by
# dropping one side.

_NEW = "new"
_ABSENT = "absent"


def _index(shape: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """`{record type: {field path: field}}` for one shape of a report."""
    return {
        group["type"]: {field["path"]: field for field in group["fields"]}
        for group in shape.get("record_types", [])
    }


def _change(signal: str, kind: str, shape: str, **rest: Any) -> dict[str, Any]:
    return {"signal": signal, "kind": kind, "shape": shape, **rest}


def _compare_fields(
    shape: str, record_type: str, old: dict[str, Any], new: dict[str, Any]
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for path in sorted(set(new) - set(old)):
        changes.append(
            _change(
                _NEW, "field", shape, record_type=record_type, path=path,
                detail="|".join(new[path]["types"]),
            )
        )
    for path in sorted(set(old) - set(new)):
        changes.append(_change(_ABSENT, "field", shape, record_type=record_type, path=path))
    for path in sorted(set(old) & set(new)):
        before, after = old[path], new[path]
        if before["types"] != after["types"]:
            changes.append(
                _change(
                    _NEW, "field-type", shape, record_type=record_type, path=path,
                    detail=f"{'|'.join(before['types'])} -> {'|'.join(after['types'])}",
                )
            )
        # A value appearing in an enumeration is the highest-signal change
        # there is: a new subtype or stop reason is exactly what breaks a
        # parser that switches on it. `varies` fields carry no examples, so
        # only genuine enumerations are compared.
        fresh = [v for v in after["examples"] if v not in before["examples"]]
        if fresh and not after["varies"] and not before["varies"]:
            changes.append(
                _change(
                    _NEW, "value", shape, record_type=record_type, path=path,
                    detail=", ".join(sorted(fresh)),
                )
            )
    return changes


def compare(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Differences between a stored inventory and a freshly built one.

    Both arguments are `inventory()` reports; any other top-level keys are
    ignored, so a baseline file may carry extra provenance of its own.
    """
    old_shapes = baseline.get("shapes", {})
    new_shapes = current.get("shapes", {})
    changes: list[dict[str, Any]] = []

    for shape in sorted(set(new_shapes) - set(old_shapes)):
        if new_shapes[shape].get("records"):
            changes.append(_change(_NEW, "shape", shape))
    for shape in sorted(set(old_shapes) - set(new_shapes)):
        if old_shapes[shape].get("records"):
            changes.append(_change(_ABSENT, "shape", shape))

    for shape in sorted(set(old_shapes) & set(new_shapes)):
        old, new = old_shapes[shape], new_shapes[shape]
        if not old.get("records") or not new.get("records"):
            # An empty log contributes a shape with nothing in it, which
            # describes no format and would otherwise report every field as
            # having vanished.
            continue
        for version in sorted(set(new.get("versions", [])) - set(old.get("versions", []))):
            changes.append(_change(_NEW, "version", shape, detail=version))
        old_types, new_types = _index(old), _index(new)
        for record_type in sorted(set(new_types) - set(old_types)):
            changes.append(_change(_NEW, "record-type", shape, record_type=record_type))
        for record_type in sorted(set(old_types) - set(new_types)):
            changes.append(_change(_ABSENT, "record-type", shape, record_type=record_type))
        for record_type in sorted(set(old_types) & set(new_types)):
            changes.extend(
                _compare_fields(shape, record_type, old_types[record_type], new_types[record_type])
            )

    return {
        "drift": bool(changes),
        "new": sum(1 for change in changes if change["signal"] == _NEW),
        "absent": sum(1 for change in changes if change["signal"] == _ABSENT),
        "changes": changes,
    }


def format_drift(report: dict[str, Any]) -> str:
    if not report["drift"]:
        return "no drift: the corpus matches the baseline"

    lines: list[str] = []
    for signal, heading in (
        (_NEW, "new since the baseline (observed, so the format changed)"),
        (_ABSENT, "in the baseline but not in this corpus (may just be coverage)"),
    ):
        selected = [change for change in report["changes"] if change["signal"] == signal]
        if not selected:
            continue
        if lines:
            lines.append("")
        lines.append(heading + ":")
        for change in selected:
            where = change["shape"]
            if change.get("record_type"):
                where += f" {change['record_type']}"
            if change.get("path"):
                where += f" {change['path']}"
            row = f"  {change['kind']:<11} {where}"
            if change.get("detail"):
                row += f"  {change['detail']}"
            lines.append(row)
    return "\n".join(lines)
