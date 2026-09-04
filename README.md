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
agentlog show   FILE   # render the conversation
agentlog stats  FILE   # cost, token, and tool totals
agentlog tools  FILE   # list tool calls
agentlog errors FILE   # failed tool calls, permission denials, bad records
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

```sh
# which runs in this directory died?
for f in logs/*/stream.jsonl; do
  agentlog stats --json "$f" | jq -r --arg f "$f" 'select(.complete|not) | $f'
done
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
