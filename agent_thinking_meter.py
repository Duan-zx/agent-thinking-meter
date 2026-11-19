#!/usr/bin/env python3
"""agent-thinking-meter: is your coding agent still reasoning before it calls tools?

Reads the session logs that coding agents keep on your machine and reports, per
source, day (UTC) and model:

  * how many model steps issued tool calls,
  * what share of those steps carried reasoning,
  * average reasoning tokens (when the log records them), thought-summary length
    and output tokens per tool step,
  * tool calls per user turn (median and mean).

Supported sources:
  claude-code  ~/.claude/projects/**/*.jsonl           reasoning = thinking tokens
  codex        ~/.codex/sessions/**/*.jsonl            reasoning = reasoning output tokens
  antigravity  ~/.gemini/antigravity/brain/*/.system_generated/logs/transcript_full.jsonl
                                                       reasoning = thought summary length

Only aggregate counts are printed. Prompt text, code, thoughts, tool arguments
and file paths are never printed or written anywhere, and model names are only
shown when they look like model identifiers. No network access.

Standard library only. Python 3.9+.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Hashable, Iterator, Optional

__version__ = "0.2.0"

# Antigravity placeholder summaries such as "I'm thinking through how to approach this."
# are 43 characters; longer summaries count as reasoning.
DEFAULT_MIN_THINKING_CHARS = 70
# For sources that record reasoning tokens, a step counts as reasoning when it used more than this.
DEFAULT_MIN_REASONING_TOKENS = 0

MODEL_ID_RE = re.compile(r"[a-z0-9][a-z0-9._\-]{0,63}")
MODEL_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .()\-]{0,47}")
MAX_TOKENS = 10 ** 9
RECORD_ERRORS = (TypeError, AttributeError, KeyError, ValueError, IndexError)


# --------------------------------------------------------------------------- data


@dataclass
class Step:
    source: str
    turn: Optional[Hashable]  # None: not attributable to a user turn (subagents, kickoff steps)
    time: datetime
    model: str
    tool_calls: int
    thinking_chars: int
    reasoning_tokens: Optional[int]
    output_tokens: Optional[int]


@dataclass
class Bucket:
    tool_steps: int = 0
    with_reasoning: int = 0
    thinking_chars: int = 0
    reasoning_tokens: int = 0
    reasoning_token_steps: int = 0
    output_tokens: int = 0
    output_token_steps: int = 0
    turns: dict = field(default_factory=lambda: defaultdict(int))


@dataclass
class Stats:
    files: int = 0
    unreadable_files: int = 0
    bad_lines: int = 0
    bad_records: int = 0


# --------------------------------------------------------------------------- helpers


def parse_time(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # fromisoformat on Python 3.9/3.10 needs exactly 3 or 6 fractional digits.
    text = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def token_count(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value < MAX_TOKENS else None


def text_length(value: object) -> int:
    return len(value.strip()) if isinstance(value, str) else 0


def as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def dict_items(value: object) -> list:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def model_id(value: object) -> str:
    if isinstance(value, str):
        value = value.strip()
        if MODEL_ID_RE.fullmatch(value):
            return value
    return ""


def model_from_label(label: str) -> Optional[str]:
    """'Gemini 4 Carbon (High)' -> 'gemini-4-carbon-high'; '' for an explicit None;
    None for anything that does not look like a selector label."""
    label = label.strip()
    if label.lower() == "none":
        return ""
    if not MODEL_LABEL_RE.fullmatch(label):
        return None
    key = re.sub(r"[^a-z0-9.]+", "-", label.lower()).strip("-")
    return key or None


def read_jsonl(path: str, stats: Stats) -> Iterator[dict]:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                stats.bad_lines += 1
                continue
            if isinstance(record, dict):
                yield record


def find_files(root: str, pattern: str) -> list:
    base = glob.escape(os.path.expanduser(root))
    return sorted(glob.glob(os.path.join(base, pattern), recursive=True))


def records_of(files: list, stats: Stats) -> Iterator[tuple]:
    """Yield (file_index, record) for every readable record; unreadable files are counted."""
    for index, path in enumerate(files):
        try:
            for record in read_jsonl(path, stats):
                yield index, record
        except OSError:
            stats.unreadable_files += 1


# --------------------------------------------------------------------------- Antigravity

# The app appends this block after the user's request. The whole block must match.
AGY_SELECTION_RE = re.compile(
    r"<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from ([^\n]+?) to ([^\n]+?)\. "
    r"No need to comment on this change[^\n]*\n</USER_SETTINGS_CHANGE>"
)


def agy_tool_calls(value: object) -> int:
    if isinstance(value, list):
        return sum(1 for item in value if isinstance(item, dict) and item)
    return 1 if isinstance(value, dict) and value else 0


def agy_settings_part(content: object) -> str:
    """Only the app-appended part of a well-formed user input, after the user's own request."""
    if not isinstance(content, str) or not content.lstrip().startswith("<USER_REQUEST>"):
        return ""
    end = content.rfind("</USER_REQUEST>")
    return content[end + len("</USER_REQUEST>"):] if end >= 0 else ""


def read_antigravity(files: list, stats: Stats) -> list:
    steps: list = []
    for index, path in enumerate(files):
        selected, turn, has_user, file_steps = "", 0, False, []
        try:
            for record in read_jsonl(path, stats):
                try:
                    kind = record.get("type")
                    if kind == "USER_INPUT":
                        turn += 1
                        has_user = True
                        for match in AGY_SELECTION_RE.finditer(agy_settings_part(record.get("content"))):
                            new = model_from_label(match.group(2))
                            selected = new if new is not None else "unknown"
                        continue
                    if kind != "PLANNER_RESPONSE":
                        continue
                    when = parse_time(record.get("created_at"))
                    if when is None:
                        continue
                    file_steps.append(Step(
                        source="antigravity", turn=(index, turn), time=when,
                        model=model_id(record.get("model")) or selected or "unknown",
                        tool_calls=agy_tool_calls(record.get("tool_calls")),
                        thinking_chars=text_length(record.get("thinking")),
                        reasoning_tokens=None,
                        output_tokens=token_count(record.get("output_tokens")),
                    ))
                except RECORD_ERRORS:
                    stats.bad_records += 1
        except OSError:
            stats.unreadable_files += 1
            continue
        if has_user:  # steps before the first user input belong to no user turn
            for step in file_steps:
                if step.turn == (index, 0):
                    step.turn = None
        steps.extend(file_steps)
    return steps


# --------------------------------------------------------------------------- Claude Code

CLAUDE_INPUT_BLOCKS = {"text", "image", "document"}


def claude_user_turn_key(record: dict) -> Optional[Hashable]:
    """A stable key for a human-originated user input, or None for tool results, meta and sidechains."""
    if record.get("isMeta") is True or record.get("isSidechain") is True:
        return None
    content = as_dict(record.get("message")).get("content")
    if isinstance(content, str):
        is_input = bool(content.strip())
    else:
        is_input = any(
            b.get("type") in CLAUDE_INPUT_BLOCKS and (b.get("type") != "text" or text_length(b.get("text")))
            for b in dict_items(content) if isinstance(b.get("type"), str)
        )
    if not is_input:
        return None
    uuid = record.get("uuid")
    return ("claude-code", uuid) if isinstance(uuid, str) and uuid else ("claude-code", id(record))


def read_claude_code(files: list, stats: Stats) -> list:
    """Merge message fragments across all files by message id, so that resumed sessions
    (which copy earlier records into a new file) neither double-count nor lose fragments."""
    merged: dict = {}
    order: list = []
    current_turn: dict = {}
    for index, record in records_of(files, stats):
        try:
            kind = record.get("type")
            if kind == "user":
                key = claude_user_turn_key(record)
                if key is not None:
                    current_turn[index] = key
                continue
            if kind != "assistant":
                continue
            message = as_dict(record.get("message"))
            raw_model = message.get("model")
            if raw_model == "<synthetic>":
                continue
            mid = message.get("id")
            if not isinstance(mid, str) or not mid:
                mid = record.get("uuid")
            if not isinstance(mid, str) or not mid:
                stats.bad_records += 1
                continue
            when = parse_time(record.get("timestamp"))
            if when is None:
                continue
            entry = merged.get(mid)
            if entry is None:
                entry = merged[mid] = {
                    "time": when, "model": model_id(raw_model) or "unknown",
                    "turn": None if record.get("isSidechain") is True else current_turn.get(index),
                    "tools": set(), "thinking": {}, "output": None, "reasoning": None,
                }
                order.append(mid)
            elif entry["turn"] is None and record.get("isSidechain") is not True:
                entry["turn"] = current_turn.get(index)
            record_id = record.get("uuid") if isinstance(record.get("uuid"), str) else f"{index}:{id(record)}"
            for pos, block in enumerate(dict_items(message.get("content"))):
                block_id = block.get("id") if isinstance(block.get("id"), str) else f"{record_id}:{pos}"
                if block.get("type") == "tool_use":
                    entry["tools"].add(block_id)
                elif block.get("type") == "thinking":
                    entry["thinking"][block_id] = text_length(block.get("thinking"))
            usage = as_dict(message.get("usage"))
            for field_name, value in (("output", usage.get("output_tokens")),
                                      ("reasoning", as_dict(usage.get("output_tokens_details")).get("thinking_tokens"))):
                value = token_count(value)
                if value is not None and (entry[field_name] is None or value > entry[field_name]):
                    entry[field_name] = value
        except RECORD_ERRORS:
            stats.bad_records += 1
    return [
        Step(source="claude-code", turn=e["turn"], time=e["time"], model=e["model"],
             tool_calls=len(e["tools"]), thinking_chars=sum(e["thinking"].values()),
             reasoning_tokens=e["reasoning"], output_tokens=e["output"])
        for e in (merged[mid] for mid in order)
    ]


# --------------------------------------------------------------------------- Codex CLI

CODEX_TOOL_ITEMS = {"function_call", "custom_tool_call", "local_shell_call", "web_search_call",
                    "tool_search_call", "image_generation_call"}


def codex_usage(value: object) -> Optional[dict]:
    usage = as_dict(value)
    if token_count(usage.get("output_tokens")) is None and token_count(usage.get("reasoning_output_tokens")) is None:
        return None
    return usage


def codex_is_subagent(payload: dict) -> bool:
    return payload.get("thread_source") == "subagent" or "subagent" in as_dict(payload.get("source"))


def read_codex(files: list, stats: Stats) -> list:
    steps: list = []
    seen_responses: set = set()
    for index, path in enumerate(files):
        model, turn, fresh_task, subagent = "unknown", 0, False, False
        pending: Optional[dict] = None

        def close(usage: Optional[dict], response_id: object = None) -> None:
            nonlocal pending
            if pending is None:
                return
            if isinstance(response_id, str) and response_id:
                if response_id in seen_responses:
                    pending = None
                    return
                seen_responses.add(response_id)
            usage = usage or {}
            steps.append(Step(
                source="codex", turn=None if subagent or not pending["turn"] else (index, pending["turn"]),
                time=pending["time"], model=pending["model"],
                tool_calls=pending["tool_calls"], thinking_chars=pending["thinking_chars"],
                reasoning_tokens=token_count(usage.get("reasoning_output_tokens")),
                output_tokens=token_count(usage.get("output_tokens")),
            ))
            pending = None

        try:
            for record in read_jsonl(path, stats):
                try:
                    kind = record.get("type")
                    payload = as_dict(record.get("payload"))
                    ptype = payload.get("type") if isinstance(payload.get("type"), str) else None
                    if kind == "session_meta":
                        subagent = codex_is_subagent(payload)
                    elif kind == "turn_context":
                        if "model" in payload:
                            model = model_id(payload.get("model")) or "unknown"
                    elif kind == "event_msg" and ptype == "task_started":
                        close(None)
                        turn += 1
                        fresh_task = True
                    elif kind == "event_msg" and ptype == "user_message":
                        close(None)
                        if not fresh_task:
                            turn += 1
                        fresh_task = False
                    elif kind == "response_item" and (ptype in CODEX_TOOL_ITEMS or ptype == "reasoning"
                                                      or (ptype == "message" and payload.get("role") == "assistant")):
                        when = parse_time(record.get("timestamp"))
                        if when is None:
                            continue
                        if pending is None:
                            pending = {"time": when, "model": model, "turn": turn, "tool_calls": 0, "thinking_chars": 0}
                        if ptype in CODEX_TOOL_ITEMS:
                            pending["tool_calls"] += 1
                        elif ptype == "reasoning":
                            pending["thinking_chars"] += sum(text_length(p.get("text")) for p in dict_items(payload.get("summary")))
                    elif kind == "token_usage_record":
                        usage = codex_usage(payload.get("usage"))
                        if usage is not None:
                            close(usage, payload.get("response_id"))
                    elif kind == "event_msg" and ptype == "token_count":
                        usage = codex_usage(as_dict(payload.get("info")).get("last_token_usage"))
                        if usage is not None:
                            close(usage)
                except RECORD_ERRORS:
                    stats.bad_records += 1
            close(None)
        except OSError:
            stats.unreadable_files += 1
    return steps


# --------------------------------------------------------------------------- sources

SOURCES = {
    "claude-code": ("CLAUDE_CONFIG_DIR", os.path.join("~", ".claude"), os.path.join("projects", "**", "*.jsonl"),
                    read_claude_code),
    "codex": ("CODEX_HOME", os.path.join("~", ".codex"), os.path.join("sessions", "**", "*.jsonl"), read_codex),
    "antigravity": (None, os.path.join("~", ".gemini", "antigravity"),
                    os.path.join("brain", "*", ".system_generated", "logs", "transcript_full.jsonl"),
                    read_antigravity),
}


def source_root(name: str, override: Optional[str]) -> str:
    env, default, _, _ = SOURCES[name]
    if override:
        return override
    if env and os.environ.get(env):
        return os.environ[env]
    return default


# --------------------------------------------------------------------------- aggregation


def bucket_key(step: Step, by: str) -> str:
    if by == "hour":
        return step.time.strftime("%Y-%m-%d %H:00")
    if by == "all":
        return "all"
    return step.time.strftime("%Y-%m-%d")


def has_reasoning(step: Step, min_tokens: int, min_chars: int) -> bool:
    if step.reasoning_tokens is not None:
        return step.reasoning_tokens > min_tokens
    return step.thinking_chars > min_chars


def aggregate(steps: list, by: str, per_model: bool, min_tokens: int, min_chars: int) -> dict:
    buckets: dict = defaultdict(Bucket)
    for step in steps:
        key = (step.source, bucket_key(step, by), step.model if per_model else "*")
        b = buckets[key]
        if step.turn is not None:
            b.turns[step.turn] += step.tool_calls
        if step.tool_calls == 0:
            continue
        b.tool_steps += 1
        b.thinking_chars += step.thinking_chars
        if has_reasoning(step, min_tokens, min_chars):
            b.with_reasoning += 1
        if step.reasoning_tokens is not None:
            b.reasoning_tokens += step.reasoning_tokens
            b.reasoning_token_steps += 1
        if step.output_tokens is not None:
            b.output_tokens += step.output_tokens
            b.output_token_steps += 1
    return buckets


def rows_from(buckets: dict) -> list:
    rows = []
    for (source, period, model), b in sorted(buckets.items()):
        calls = list(b.turns.values())
        n = b.tool_steps
        rows.append({
            "source": source,
            "period": period,
            "model": model,
            "tool_steps": n,
            "with_reasoning_pct": round(100.0 * b.with_reasoning / n, 1) if n else None,
            "avg_reasoning_tokens": round(b.reasoning_tokens / b.reasoning_token_steps) if b.reasoning_token_steps else None,
            "avg_summary_chars": round(b.thinking_chars / n) if n else None,
            "avg_output_tokens": round(b.output_tokens / b.output_token_steps) if b.output_token_steps else None,
            "turns": len(calls),
            "median_tool_calls_per_turn": statistics.median(calls) if calls else None,
            "mean_tool_calls_per_turn": round(statistics.mean(calls), 1) if calls else None,
        })
    return rows


COLUMNS = [
    ("source", "Source"),
    ("period", "Period (UTC)"),
    ("model", "Model"),
    ("tool_steps", "Tool steps"),
    ("with_reasoning_pct", "Reasoning %"),
    ("avg_reasoning_tokens", "Avg reasoning tok"),
    ("avg_summary_chars", "Avg summary chars"),
    ("avg_output_tokens", "Avg output tok"),
    ("turns", "Turns"),
    ("median_tool_calls_per_turn", "Median calls/turn"),
    ("mean_tool_calls_per_turn", "Mean calls/turn"),
]


def fmt(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def print_table(rows: list, out=None) -> None:
    out = out or sys.stdout
    cols = COLUMNS if any(r["model"] != "*" for r in rows) else [c for c in COLUMNS if c[0] != "model"]
    cells = [[fmt(r[k]) for k, _ in cols] for r in rows]
    widths = [max([len(h)] + [len(c[i]) for c in cells]) for i, (_, h) in enumerate(cols)]
    print("  ".join(h.ljust(w) for (_, h), w in zip(cols, widths)), file=out)
    print("  ".join("-" * w for w in widths), file=out)
    for c in cells:
        print("  ".join(v.ljust(w) for v, w in zip(c, widths)), file=out)


# --------------------------------------------------------------------------- CLI


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-thinking-meter",
        description="Measure how often your coding agent reasons on tool-call steps, from local session logs. "
                    "Prints aggregate counts only, never content.",
    )
    parser.add_argument("--source", action="append", choices=sorted(SOURCES),
                        help="source to read (repeatable). Default: every source found on this machine")
    for name in SOURCES:
        env, default, _, _ = SOURCES[name]
        hint = f" (default: ${env} or {default})" if env else f" (default: {default})"
        parser.add_argument(f"--{name}-root", dest=f"root_{name.replace('-', '_')}", help=f"{name} data directory{hint}")
    parser.add_argument("--by", choices=["day", "hour", "all"], default="day", help="time bucket (UTC)")
    parser.add_argument("--no-model", action="store_true", help="do not split rows by model")
    parser.add_argument("--model", action="append", default=[],
                        help="only include models containing this text (repeatable), e.g. --model opus")
    parser.add_argument("--since", help="only include steps at or after this UTC date/time, e.g. 2026-11-11")
    parser.add_argument("--until", help="only include steps before this UTC date/time")
    parser.add_argument("--min-reasoning-tokens", type=int, default=DEFAULT_MIN_REASONING_TOKENS,
                        help="for sources with token counts, a step counts as reasoning above this many "
                             f"reasoning tokens (default: {DEFAULT_MIN_REASONING_TOKENS})")
    parser.add_argument("--min-thinking", type=int, default=DEFAULT_MIN_THINKING_CHARS,
                        help="for sources without token counts, a step counts as reasoning when its thought "
                             f"summary is longer than this many characters (default: {DEFAULT_MIN_THINKING_CHARS})")
    parser.add_argument("--format", choices=["table", "json", "csv"], default="table")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    since = parse_time(args.since) if args.since is not None else None
    until = parse_time(args.until) if args.until is not None else None
    if (args.since is not None and since is None) or (args.until is not None and until is None):
        parser.error("--since/--until must be ISO dates such as 2026-11-11 or 2026-11-11T09:00:00Z")
    if args.min_thinking < 0 or args.min_reasoning_tokens < 0:
        parser.error("--min-thinking and --min-reasoning-tokens must be 0 or more")

    chosen = list(dict.fromkeys(args.source)) if args.source else list(SOURCES)
    plan: list = []
    for name in chosen:
        root = source_root(name, getattr(args, f"root_{name.replace('-', '_')}"))
        _, _, pattern, reader = SOURCES[name]
        files = find_files(root, pattern)
        if files:
            plan.append((name, files, reader))
        elif args.source:
            print(f"warning: no {name} logs found under the given root", file=sys.stderr)
    if not plan:
        print("No session logs found. Use --source and --<source>-root to point at them.", file=sys.stderr)
        return 1

    stats = Stats(files=sum(len(f) for _, f, _ in plan))
    wanted = [m.lower() for m in args.model]
    steps = [
        step
        for _, files, reader in plan
        for step in reader(files, stats)
        if not (since and step.time < since)
        and not (until and step.time >= until)
        and not (wanted and not any(w in step.model.lower() for w in wanted))
    ]
    rows = rows_from(aggregate(steps, args.by, not args.no_model, args.min_reasoning_tokens, args.min_thinking))
    if stats.unreadable_files:
        print(f"warning: skipped {stats.unreadable_files} unreadable file(s)", file=sys.stderr)
    if stats.bad_lines or stats.bad_records:
        print(f"warning: skipped {stats.bad_lines} unreadable line(s) and {stats.bad_records} malformed record(s)",
              file=sys.stderr)
    if not rows:
        print("No matching model steps.", file=sys.stderr)
        return 1

    if args.format == "json":
        json.dump(rows, sys.stdout, indent=2)
        print()
    elif args.format == "csv":
        try:
            sys.stdout.reconfigure(newline="")
        except (AttributeError, ValueError):
            pass
        writer = csv.DictWriter(sys.stdout, fieldnames=[k for k, _ in COLUMNS])
        writer.writeheader()
        writer.writerows(rows)
    else:
        print_table(rows)
        sources = ", ".join(name for name, _, _ in plan)
        print(f"\n{stats.files} log file(s) from: {sources}. 'Reasoning %' uses reasoning tokens where the log "
              f"records them (claude-code, codex) and thought-summary length otherwise (antigravity). "
              f"Visible reasoning is a proxy; it does not measure internal effort.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
