#!/usr/bin/env python3
"""Regenerate `docs/schema-baseline.json` and the inventory in `docs/log-format.md`.

The baseline is only true of the corpus and the writer version it was built
from, so both have to move together: the JSON baseline, the rendered inventory
block, and the provenance table that says what produced them. Doing that by
hand across three places is how a "generated" document quietly stops matching
its generator, so it is one command instead.

    python3 scripts/regenerate-inventory.py ~/.claude/projects <more paths...>
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agentlog import parse_file  # noqa: E402
from agentlog.stats import iter_log_paths  # noqa: E402
from agentlog.schema import compare, format_drift, format_inventory, inventory  # noqa: E402

BASELINE = ROOT / "docs" / "schema-baseline.json"
DOC = ROOT / "docs" / "log-format.md"


def render(report: dict) -> tuple[str, str]:
    """The inventory block, and the versions it was observed at."""
    versions = sorted(
        {v for shape in report["shapes"].values() for v in shape.get("versions", [])}
    )
    return format_inventory(report), ", ".join(versions) or "unknown"


def replace_inventory(text: str, block: str) -> str:
    """Swap the fenced block that follows the `## Inventory` heading."""
    pattern = re.compile(r"(## Inventory\n\n```\n).*?(\n```\n)", re.DOTALL)
    if not pattern.search(text):
        raise SystemExit("docs/log-format.md: could not find the '## Inventory' fenced block")
    return pattern.sub(lambda m: m.group(1) + block.rstrip("\n") + m.group(2), text, count=1)


def _describe(report: dict) -> str:
    """The provenance row is a claim about the corpus, so derive it, never type it."""
    parts = []
    for name, label in (("stream", "non-interactive stream logs"), ("session", "session transcripts")):
        shape = report["shapes"].get(name)
        if shape and shape.get("records"):
            parts.append(f"{shape['logs']} {label}")
    return " + ".join(parts) + " from a single machine"


def replace_row(text: str, label: str, value: str) -> str:
    pattern = re.compile(rf"^\| {re.escape(label)} \| .*? \|$", re.MULTILINE)
    if not pattern.search(text):
        raise SystemExit(f"docs/log-format.md: no provenance row named {label!r}")
    return pattern.sub(f"| {label} | {value} |", text, count=1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="log files or directories to inventory")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report whether the checked-in files are stale; write nothing",
    )
    args = parser.parse_args(argv)

    files = list(iter_log_paths([str(pathlib.Path(p).expanduser()) for p in args.paths]))
    if not files:
        raise SystemExit("no log files found in the given paths")
    report = inventory([parse_file(path) for path in files])
    block, versions = render(report)

    baseline_text = json.dumps(report, indent=2, sort_keys=True) + "\n"

    doc_text = DOC.read_text()
    doc_text = replace_inventory(doc_text, block)
    doc_text = replace_row(doc_text, "Generated", dt.datetime.now(dt.timezone.utc).date().isoformat())
    doc_text = replace_row(doc_text, "Claude Code version", versions)
    doc_text = replace_row(doc_text, "Corpus", _describe(report))

    if args.check:
        # Counts are corpus size, not structure, and this corpus is live — the
        # session log of the process running this script is inside it and grows
        # while it runs. A textual diff is therefore never clean. Staleness is
        # the same question `schema --baseline` answers: did the *shape* move?
        drift = compare(json.loads(BASELINE.read_text()), report)
        if drift["drift"]:
            print(format_drift(drift))
            return 1
        print("up to date: the checked-in baseline still describes this corpus")
        return 0

    BASELINE.write_text(baseline_text)
    DOC.write_text(doc_text)
    print(f"wrote {BASELINE.relative_to(ROOT)} and {DOC.relative_to(ROOT)} ({versions})")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
