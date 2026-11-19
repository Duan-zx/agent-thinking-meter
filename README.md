# agent-thinking-meter

Is your coding agent still reasoning before it calls tools?

`agent-thinking-meter` reads the session logs that coding agents keep on your machine and reports, per source, day and model, how often tool-calling steps carried reasoning, how much, and how many tool calls the agent made per user turn. Use it to check a hunch ("it feels lazier this week") against your own data before you file a bug report.

It started as a script for a [Gemini 4 regression in Google Antigravity](https://discuss.ai.google.dev/t/counter-data-tool-aware-minimal-thinking-is-measurably-degrading-output-quality-on-agy-gemini-4-carbon/200671) in November 2026, where models on the subscription path stopped showing reasoning on tool-use turns. The regression was fixed server-side on November 19; the [case study](docs/case-study-antigravity-2026-11.md) walks through the drop and the recovery.

## Supported agents

| Source | Logs read by default | How "reasoning" is measured |
|---|---|---|
| `claude-code` | `~/.claude/projects/**/*.jsonl` (or `$CLAUDE_CONFIG_DIR`) | thinking tokens recorded per API message |
| `codex` | `~/.codex/sessions/**/*.jsonl` (or `$CODEX_HOME`) | reasoning output tokens recorded per model response |
| `antigravity` | `~/.gemini/antigravity/brain/*/.system_generated/logs/transcript_full.jsonl` | thought-summary length (no token count is logged) |

By default every source found on the machine is read. Gemini CLI, Cursor and others are not supported yet; pull requests with a reader and a small synthetic fixture are welcome.

## Privacy

The tool prints aggregate counts only. It never prints or writes prompt text, code, thoughts, tool arguments or file paths, and it makes no network requests. Model names are shown only when they look like model identifiers; anything else is reported as `unknown`. Review the output before you share it, as you would any log.

## Usage

Python 3.9 or later, standard library only. No install step.

```bash
python agent_thinking_meter.py
```

Common options:

```bash
# one source, one model family, from a given day
python agent_thinking_meter.py --source antigravity --model carbon --since 2026-11-11

# hourly view of one day
python agent_thinking_meter.py --by hour --since 2026-11-17 --until 2026-11-18

# one row per source and model over a period
python agent_thinking_meter.py --since 2026-11-01 --by all

# machine-readable output
python agent_thinking_meter.py --format csv > reasoning.csv
```

Point at other locations with `--claude-code-root`, `--codex-root` and `--antigravity-root`.

## Example output

Excerpt from the author's Antigravity logs during the regression (some columns omitted):

```
Source       Period (UTC)  Model                 Tool steps  Reasoning %  Avg summary chars  Avg output tok  Median calls/turn
antigravity  2026-11-16    orchestrator-carbon   784         86.6         950                1612            6.5
antigravity  2026-11-17    gemini-4-carbon-high  1192        14.3         68                 382             1
antigravity  2026-11-17    orchestrator-carbon   261         76.6         892                1271            5.5
antigravity  2026-11-18    gemini-4-carbon-high  400         17.5         98                 305             1
```

## Columns

| Column | Meaning |
|---|---|
| Tool steps | Model steps (one API response) that issued at least one tool call |
| Reasoning % | Share of tool steps with reasoning: more than `--min-reasoning-tokens` reasoning tokens (default 0) where the log records tokens, otherwise a thought summary longer than `--min-thinking` characters (default 70; Antigravity placeholders such as "I'm thinking through how to approach this." are shorter) |
| Avg reasoning tok | Mean reasoning tokens per tool step, where recorded |
| Avg summary chars | Mean visible thought-summary length per tool step. Claude Code and Codex often keep reasoning hidden or encrypted, so this is usually near zero for them |
| Avg output tok | Mean output tokens over tool steps whose output tokens are recorded |
| Turns | Distinct user turns with at least one model step in the row |
| Median / Mean calls/turn | Tool calls per user turn |

## Notes and limits

* Compare a source with itself over time. The measures differ between sources, so a Claude Code percentage and an Antigravity percentage are not directly comparable.
* Visible reasoning is a proxy. Token counts and summaries do not measure how well a model reasoned.
* Subagent and sidechain steps (Claude Code sidechains, Codex subagent sessions) are counted in step totals but not in per-turn numbers. Antigravity subagent conversations have no user turns, so each counts as a single long turn.
* Claude Code copies earlier messages into a new file when a session is resumed. Fragments are merged across files by message id, tool calls by tool-use id, and turns by the originating user message id, so resumed sessions are neither double-counted nor split. Codex steps are de-duplicated by response id.
* Antigravity builds before v2.33.0 do not record the served model per step; the model is then taken from the app's model-selection notice, or reported as `unknown`.
* Malformed records are skipped and counted in a warning; one bad record does not stop the report.
* Log formats are undocumented and change between versions. If a source suddenly reports nothing, please open an issue with the app version.
* Every log file is read, so a first run over months of history can take a few minutes. Timestamps are compared at microsecond precision.

## License

MIT
