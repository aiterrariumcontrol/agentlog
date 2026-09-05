# agentlog

Read Claude Code JSONL logs from the terminal.

Claude Code can emit a lot of JSONL, and none of it is pleasant to read raw:

- `claude -p --output-format stream-json --verbose` writes a stream of records
  to stdout, ending in a `result` record with cost and token totals;
- interactive sessions are archived under
  `~/.claude/projects/<slug>/<session-id>.jsonl`, interleaved with editor
  bookkeeping records.

`agentlog` turns either of those into something you can actually read, and
answers the questions you usually have about a run: what did it do, what did it
cost, which tools did it call, and what failed.

Zero dependencies, Python 3.10+, stdlib only.

## Install

```sh
pip install git+https://github.com/aiterrariumcontrol/agentlog
```

Or just run it from a checkout — there is nothing to build:

```sh
PYTHONPATH=src python3 -m agentlog stats run.jsonl
```

## Usage

```
agentlog show   FILE          # render the conversation
agentlog stats  FILE|DIR ...  # cost, token, and tool totals, for one log or many
agentlog tools  FILE          # list tool calls
agentlog errors FILE          # failed tool calls, permission denials, bad records
agentlog schema FILE|DIR ...  # which record types and fields a corpus contains
```

`FILE` may be `-` to read from stdin. Every command accepts `--json` for
machine-readable output.

### `show`

```console
$ agentlog show --limit 150 run.jsonl
— session start · model=claude-opus-5 · cwd=/home/agent/terrarium

[23:28:53] ◆ assistant
  I'll run the read-only checks.

[23:28:55] ◆ assistant
  ⚙ Bash id -un; echo "---"; pwd; echo "---"; gh api user --jq .login
      → agent
      ---
      /home/agent/terrarium
      ... [77 more characters]
```

Text, tool calls, and tool results are shown by default. Thinking blocks and
full tool inputs are opt-in, since they dominate the output otherwise:

```sh
agentlog show --thinking --tool-input --limit 0 run.jsonl
```

`--limit N` truncates each block to N characters and says how much was dropped;
`--limit 0` means unlimited. `--all` also prints the bookkeeping records that
are hidden by default.

### `stats`

```console
$ agentlog stats run.jsonl
           file  run.jsonl
          shape  stream
        session  96e8478e-cb5b-42b9-9e25-d4bd27c8edc0
          model  claude-opus-5
       duration  12.5s
       api time  10.9s
           cost  $0.073704
    stop reason  end_turn
        records  7
assistant turns  3
     tool calls  1

tokens (result)
                 input_tokens  4
                output_tokens  725
  cache_creation_input_tokens  4,155
      cache_read_input_tokens  25,574

per model
  claude-haiku-4-5-20251001: $0.001222  in=1157 out=13 cache_read=0
  claude-opus-5: $0.072482  in=4 out=725 cache_read=25574

tools
  Bash  1
```

Totals come from the final `result` record when there is one. Session
transcripts have no such record, so per-message `usage` is summed instead —
`token_source` in the JSON output tells you which happened, and `cost_usd` is
simply absent when the log does not contain it. `agentlog` never estimates a
price it was not given.

A stream log with no `result` record is reported as `complete: false`: the run
is either still going or was killed. That is often the single most useful thing
to know when triaging a batch of logs.

#### Many logs at once

Give `stats` several files, or a directory to walk recursively, and it rolls
them up instead:

```console
$ agentlog stats logs/
when                             cost     tokens  tools  log
------------------------------------------------------------
2026-09-04T23:01:56Z (mtime)        -          0      0  logs/20260904T230150Z/stream.jsonl
2026-09-04T23:10:01.418Z      $0.1755     30,382      1  logs/20260904T230940Z/stream.jsonl
2026-09-04T23:32:37.816Z      $2.7250  1,977,611     38  logs/20260904T233225Z/stream.jsonl (1 tool err)
2026-09-05T02:43:32.958Z            -    854,634     17  logs/20260905T024310Z/stream.jsonl !partial

           logs  4
          range  2026-09-04T23:01:56Z .. 2026-09-05T02:43:32.958Z
           cost  $2.900500  (from 2/4 logs; rest report no cost)
      wall time  604.3s
     tool calls  56
    tool errors  1
incomplete logs  1

by day
  2026-09-04  3 runs  $2.9005  2,007,993 tokens  39 tool calls  (1 without cost)
  2026-09-05  1 run   $0.0000    854,634 tokens  17 tool calls  (1 without cost)
```

The same honesty rule applies to the total: runs that reported no cost
contribute nothing to it, and the report says how many those were, so the
number is a floor rather than an estimate. A log whose records carry no
timestamp — an empty or killed-at-startup run — is placed by file mtime, and
marked `(mtime)` so you know the time did not come from the log.

Restrict the window with any ISO prefix; both ends are inclusive:

```sh
agentlog stats logs/ --since 2026-09-04 --until 2026-09-04  # one day
agentlog stats logs/ --since 2026-09                        # one month
agentlog stats logs/ --no-runs                              # totals only
agentlog stats logs/ --json | jq '.by_day'
```

Undated logs are dropped when a bound is given, since they cannot be shown to
fall inside it.

```sh
# which runs in this directory died?
agentlog stats --json logs/ | jq -r '.runs[] | select(.complete == false) | .path'
```

### `tools`

```sh
agentlog tools run.jsonl                      # every call
agentlog tools --name Bash --name Read f.jsonl
agentlog tools --failed run.jsonl             # only calls that errored
agentlog tools --json --tool-input run.jsonl  # full inputs, as JSON
```

### `errors`

One place to look when a run went wrong — failed tool calls, permission
denials, unparseable records, and the run's own error status:

```sh
agentlog errors --json run.jsonl | jq .
```

### `schema`

The log formats are undocumented and drift between releases, so `agentlog`
can describe them from evidence instead of from assumptions. `schema` walks a
corpus and reports, per record type, which field paths appeared, how often,
with which JSON types, and — where a field looks like an enumeration — which
values:

```console
$ agentlog schema ~/.claude/projects/ logs/
shape: stream  (6 logs, 582 records)

  assistant  ×174
    message.content[].type                str  text, tool_use, thinking
    message.content[].name                str 97/174  Bash, Write, Edit
    message.content[].thinking            str 49/174  (varies)
    message.model                         str  <synthetic>, claude-opus-5
    message.stop_reason                   null|str  stop_sequence
    message.usage.cache_read_input_tokens  int  (varies)
```

A bare type means every record of that type had the field; `49/174` means it
is optional. `(varies)` means the field held more distinct values than an
enumeration plausibly would — it is data, not structure.

Example values are for documenting formats, not for reading logs. Free-form
content, paths, URLs and addresses are never printed, and high-cardinality
fields collapse to `(varies)`, so `schema` output is safe to paste into a bug
report. Use `show` when you want the actual contents.

[`docs/log-format.md`](docs/log-format.md) is this output, generated from a
real corpus and annotated — useful if you are writing your own tooling against
these files.

#### Watching the format change

Save an inventory and `schema` will tell you what moved since:

```console
$ agentlog schema --json ~/.claude/projects/ logs/ > baseline.json
# ... a Claude Code release later ...
$ agentlog schema --baseline baseline.json ~/.claude/projects/ logs/
new since the baseline (observed, so the format changed):
  record-type stream  system/compact_boundary
  value       stream assistant  message.content[].type  server_tool_use
  version     stream  2.3.0
$ echo $?
1
```

It exits 1 on any difference, so a scheduled job can notice a release breaking
your parser before your users do. Additions are hard evidence; things listed as
absent may just be a corpus that did not exercise them. The checked-in
[`docs/schema-baseline.json`](docs/schema-baseline.json) is a usable starting
baseline.

## Robustness

Logs get truncated, killed mid-write, and tailed while still being appended.
`agentlog` never aborts on a bad record: unparseable lines are reported as
`malformed` and everything else still renders. `agentlog errors` counts them,
so a corrupted log is visible rather than silently short.

## Library use

```python
from agentlog import parse_file, summarize

transcript = parse_file("run.jsonl")
print(transcript.shape, transcript.complete)

for call in transcript.tool_calls:
    if call.is_error:
        print(call.name, call.target, call.result)

print(summarize(transcript)["cost_usd"])
```

`Transcript.events` holds normalized `Event` records (`kind` is one of
`system`, `assistant`, `user`, `result`, `noise`, `malformed`), each keeping
its original record in `.raw` so nothing is lost.

## Compatibility

The log formats above are not a documented, stable interface — they are
Claude Code implementation details and may change between releases. `agentlog`
is written defensively: unknown record types are ignored rather than fatal, and
missing fields are omitted from output rather than guessed. Verified against
Claude Code 2.x logs.

## Development

```sh
python3 -m unittest discover -s tests   # with src on PYTHONPATH
```

## License

MIT
