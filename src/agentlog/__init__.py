"""agentlog — read Claude Code JSONL logs."""

__version__ = "0.4.0"

from .model import Event, ToolCall, Transcript, parse_file, parse_lines
from .stats import aggregate, summarize

__all__ = ["Event", "ToolCall", "Transcript", "parse_file", "parse_lines", "summarize", "aggregate", "__version__"]
