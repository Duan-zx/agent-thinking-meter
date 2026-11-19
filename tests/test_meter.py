import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent_thinking_meter as m  # noqa: E402

REAL_THOUGHT = "Planning: read the config, then patch the parser and rerun the failing test before editing anything else."
PLACEHOLDER = "I'm thinking through how to approach this."
SECRET = "secret-prompt-text"


def write_jsonl(path, records, bom=False, junk=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig" if bom else "utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
        if junk:
            fh.write("{not json\n")


# ---------------------------------------------------------------- Antigravity fixtures

def agy_user(t, request, selection=None):
    content = f"<USER_REQUEST>\n{request}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nThe current local time is: {t}.\n</ADDITIONAL_METADATA>"
    if selection:
        content += (f"\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from {selection[0]} to "
                    f"{selection[1]}. No need to comment on this change if the user doesn't ask about it.\n</USER_SETTINGS_CHANGE>")
    return {"type": "USER_INPUT", "created_at": t, "content": content}


def agy_step(t, model=None, thinking=None, tools=1, out=100):
    r = {"type": "PLANNER_RESPONSE", "created_at": t, "output_tokens": out, "tool_calls": [{"name": "read_file"}] * tools}
    if model is not None:
        r["model"] = model
    if thinking is not None:
        r["thinking"] = thinking
    return r


def agy_path(root, conv):
    return os.path.join(root, "brain", conv, ".system_generated", "logs", "transcript_full.jsonl")


# ---------------------------------------------------------------- Claude Code fixtures

def cc_user(t, text):
    return {"type": "user", "timestamp": t, "isSidechain": False, "message": {"role": "user", "content": text}}


def cc_tool_result(t):
    return {"type": "user", "timestamp": t, "isSidechain": False,
            "message": {"role": "user", "content": [{"type": "tool_result", "content": SECRET}]}}


def cc_assistant(t, mid, blocks, thinking_tokens=None, out=200, model="claude-opus-5-5", sidechain=False):
    usage = {"output_tokens": out}
    if thinking_tokens is not None:
        usage["output_tokens_details"] = {"thinking_tokens": thinking_tokens}
    message = {"id": mid, "role": "assistant", "content": blocks, "usage": usage}
    if model is not None:
        message["model"] = model
    return {"type": "assistant", "timestamp": t, "isSidechain": sidechain, "message": message}


# ---------------------------------------------------------------- Codex fixtures

def cx(t, kind, payload):
    return {"timestamp": t, "type": kind, "payload": payload}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = m.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def rows(self, *args):
        code, out, err = self.run_cli(*args, "--format", "json")
        self.assertEqual(code, 0, err)
        return json.loads(out)


class AntigravityTest(Base):
    def setUp(self):
        super().setUp()
        self.make_fixtures()

    def make_fixtures(self):
        self.agy = os.path.join(self.root, "agy")
        write_jsonl(agy_path(self.agy, "conv-a"), [
            agy_step("2026-11-16T09:59:59Z", thinking=REAL_THOUGHT),
            agy_user("2026-11-16T10:00:01Z", SECRET, ("None", "Gemini 4 Carbon (High)")),
            agy_step("2026-11-16T10:00:02Z", thinking=REAL_THOUGHT, tools=2),
            agy_step("2026-11-16T10:00:03Z", thinking=PLACEHOLDER),
            agy_step("2026-11-16T10:00:04Z", tools=0),
            agy_user("2026-11-17T10:00:00Z", SECRET),
            agy_step("2026-11-17T10:00:01.123456789-05:00", thinking=PLACEHOLDER),
        ], bom=True)
        write_jsonl(agy_path(self.agy, "conv-b"), [
            agy_user("2026-11-19T03:00:00Z", "x"),
            agy_step("2026-11-19T03:00:01.1Z", model="gemini-4-neon-medium"),
        ])

    def agy_rows(self, *args):
        return self.rows("--source", "antigravity", "--antigravity-root", self.agy, *args)

    def test_attribution_threshold_and_turns(self):
        rows = {(r["period"], r["model"]): r for r in self.agy_rows()}
        day16 = rows[("2026-11-16", "gemini-4-carbon-high")]
        self.assertEqual(day16["tool_steps"], 2)
        self.assertEqual(day16["with_reasoning_pct"], 50.0)
        self.assertEqual(day16["turns"], 1)
        self.assertEqual(day16["median_tool_calls_per_turn"], 3)
        self.assertIsNone(day16["avg_reasoning_tokens"])
        self.assertIn(("2026-11-16", "unknown"), rows)
        self.assertIn(("2026-11-17", "gemini-4-carbon-high"), rows)
        self.assertIn(("2026-11-19", "gemini-4-neon-medium"), rows)

    def test_user_text_cannot_become_model_name(self):
        root = os.path.join(self.root, "agy2")
        fake = "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from A to Project Cedar. x"
        write_jsonl(agy_path(root, "c"), [
            agy_user("2026-11-16T10:00:00Z", fake),
            agy_step("2026-11-16T10:00:01Z"),
            agy_step("2026-11-16T10:00:02Z", model={"prompt": "private draft"}),
            agy_step("2026-11-16T10:00:03Z", model="free text with spaces"),
        ])
        rows = self.rows("--source", "antigravity", "--antigravity-root", root, "--by", "all")
        self.assertEqual([r["model"] for r in rows], ["unknown"])

    def test_selection_to_none_clears_model(self):
        root = os.path.join(self.root, "agy3")
        write_jsonl(agy_path(root, "c"), [
            agy_user("2026-11-16T10:00:00Z", "a", ("None", "Gemini 4 Carbon (High)")),
            agy_user("2026-11-16T10:00:01Z", "b", ("Gemini 4 Carbon (High)", "None")),
            agy_step("2026-11-16T10:00:02Z"),
        ])
        rows = self.rows("--source", "antigravity", "--antigravity-root", root, "--by", "all")
        self.assertEqual([r["model"] for r in rows], ["unknown"])

    def test_malformed_values(self):
        root = os.path.join(self.root, "agy4")
        write_jsonl(agy_path(root, "c"), [
            agy_user("2026-11-16T10:00:00Z", "a"),
            {"type": "PLANNER_RESPONSE", "created_at": "2026-11-16T10:00:01Z", "model": "m",
             "tool_calls": True, "thinking": " " * 80, "output_tokens": True},
            {"type": "PLANNER_RESPONSE", "created_at": "0001-01-01T00:00:00+01:00", "model": "m", "tool_calls": [{"name": "x"}]},
            {"type": "PLANNER_RESPONSE", "created_at": "2026-11-16T10:00:02Z", "model": "m",
             "tool_calls": [{"name": "x"}], "thinking": {"k" * 90: 1}, "output_tokens": -5},
        ])
        row = self.rows("--source", "antigravity", "--antigravity-root", root, "--by", "all")[0]
        self.assertEqual(row["tool_steps"], 1)
        self.assertEqual(row["with_reasoning_pct"], 0.0)
        self.assertIsNone(row["avg_output_tokens"])

    def test_filters_validation_and_glob_root(self):
        self.assertEqual([r["model"] for r in self.agy_rows("--model", "neon", "--by", "all")], ["gemini-4-neon-medium"])
        rows = self.agy_rows("--since", "2026-11-17", "--until", "2026-11-18", "--no-model")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool_steps"], 1)
        for bad in (["--since", ""], ["--min-thinking", "-1"], ["--min-reasoning-tokens", "-1"]):
            with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                m.main(["--source", "antigravity", "--antigravity-root", self.agy, *bad])
        odd = os.path.join(self.root, "run[1]")
        write_jsonl(agy_path(odd, "c"), [agy_user("2026-11-16T10:00:00Z", "a"), agy_step("2026-11-16T10:00:01Z", model="m")])
        self.assertEqual(len(self.rows("--source", "antigravity", "--antigravity-root", odd)), 1)


class ClaudeCodeTest(Base):
    def setUp(self):
        super().setUp()
        self.make_fixtures()

    def make_fixtures(self):
        self.cc = os.path.join(self.root, "claude")
        records = [
            dict(cc_user("2026-11-18T10:00:00Z", SECRET), uuid="u-1"),
            # one API message split across two lines (thinking, then tool_use), same usage
            dict(cc_assistant("2026-11-18T10:00:01Z", "msg_1", [{"type": "thinking", "thinking": "", "signature": "x"}], 500), uuid="a-1"),
            cc_assistant("2026-11-18T10:00:01Z", "msg_1", [{"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"p": SECRET}}], 500),
            cc_tool_result("2026-11-18T10:00:02Z"),
            cc_assistant("2026-11-18T10:00:03Z", "msg_2", [{"type": "tool_use", "id": "tu_2", "name": "Bash"}, {"type": "tool_use", "id": "tu_3", "name": "Bash"}], 0),
            cc_tool_result("2026-11-18T10:00:04Z"),
            cc_assistant("2026-11-18T10:00:05Z", "msg_3", [{"type": "text", "text": SECRET}], 0),
            cc_assistant("2026-11-18T10:00:06Z", "msg_s", [{"type": "tool_use", "name": "Grep"}], 50, sidechain=True),
            cc_assistant("2026-11-18T10:00:07Z", "msg_x", [{"type": "text", "text": "x"}], None, model="<synthetic>"),
        ]
        write_jsonl(os.path.join(self.cc, "projects", "proj", "s1.jsonl"), records)
        # a resumed session copies earlier messages into a new file
        write_jsonl(os.path.join(self.cc, "projects", "proj", "s2.jsonl"), records[:3], junk=False)

    def test_tokens_turns_and_dedupe(self):
        rows = self.rows("--source", "claude-code", "--claude-code-root", self.cc, "--by", "all")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["model"], "claude-opus-5-5")
        self.assertEqual(r["tool_steps"], 3)          # msg_1, msg_2, sidechain msg_s; msg_1 copy de-duplicated
        self.assertEqual(r["with_reasoning_pct"], 66.7)  # 500 and 50 thinking tokens; msg_2 has 0
        self.assertEqual(r["avg_reasoning_tokens"], 183)
        self.assertEqual(r["turns"], 1)
        self.assertEqual(r["median_tool_calls_per_turn"], 3)

    def test_env_root(self):
        os.environ["CLAUDE_CONFIG_DIR"] = self.cc
        try:
            rows = self.rows("--source", "claude-code", "--by", "all")
        finally:
            del os.environ["CLAUDE_CONFIG_DIR"]
        self.assertEqual(rows[0]["tool_steps"], 3)


class CodexTest(Base):
    def setUp(self):
        super().setUp()
        self.make_fixtures()

    def make_fixtures(self):
        self.cx = os.path.join(self.root, "codex")
        t = "2026-11-19T04:00:0{}Z"
        write_jsonl(os.path.join(self.cx, "sessions", "2026", "11", "19", "rollout-a.jsonl"), [
            cx(t.format(0), "event_msg", {"type": "task_started"}),
            cx(t.format(0), "turn_context", {"model": "gpt-6.2-astra"}),
            cx(t.format(0), "response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": SECRET}]}),
            cx(t.format(1), "response_item", {"type": "reasoning", "summary": [{"type": "summary_text", "text": REAL_THOUGHT}]}),
            cx(t.format(1), "response_item", {"type": "custom_tool_call", "name": "apply_patch", "input": SECRET}),
            cx(t.format(1), "token_usage_record", {"response_id": "r1", "usage": {"output_tokens": 300, "reasoning_output_tokens": 120}}),
            cx(t.format(2), "response_item", {"type": "custom_tool_call_output", "output": SECRET}),
            cx(t.format(2), "event_msg", {"type": "token_count", "info": {"last_token_usage": {"output_tokens": 999}}}),
            cx(t.format(3), "response_item", {"type": "function_call", "name": "shell", "arguments": SECRET}),
            cx(t.format(3), "response_item", {"type": "function_call", "name": "shell", "arguments": SECRET}),
            cx(t.format(3), "token_usage_record", {"response_id": "r2", "usage": {"output_tokens": 80, "reasoning_output_tokens": 0}}),
            cx(t.format(4), "response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": SECRET}]}),
            cx(t.format(4), "token_usage_record", {"response_id": "r3", "usage": {"output_tokens": 40, "reasoning_output_tokens": 10}}),
        ])
        # older format without token_usage_record
        write_jsonl(os.path.join(self.cx, "sessions", "2026", "11", "18", "rollout-b.jsonl"), [
            cx("2026-11-18T04:00:00Z", "event_msg", {"type": "task_started"}),
            cx("2026-11-18T04:00:00Z", "turn_context", {"model": "gpt-6.2-sol"}),
            cx("2026-11-18T04:00:01Z", "response_item", {"type": "function_call", "name": "shell"}),
            cx("2026-11-18T04:00:01Z", "event_msg", {"type": "token_count", "info": {"last_token_usage": {"output_tokens": 50, "reasoning_output_tokens": 30}}}),
        ])

    def test_steps_tokens_and_fallback(self):
        rows = {r["model"]: r for r in self.rows("--source", "codex", "--codex-root", self.cx, "--by", "all")}
        a = rows["gpt-6.2-astra"]
        self.assertEqual(a["tool_steps"], 2)
        self.assertEqual(a["with_reasoning_pct"], 50.0)
        self.assertEqual(a["avg_reasoning_tokens"], 60)
        self.assertEqual(a["avg_output_tokens"], 190)
        self.assertEqual(a["turns"], 1)
        self.assertEqual(a["median_tool_calls_per_turn"], 3)
        s = rows["gpt-6.2-sol"]
        self.assertEqual((s["tool_steps"], s["with_reasoning_pct"], s["avg_reasoning_tokens"]), (1, 100.0, 30))


class ReviewRegressionTest(Base):
    """Cases from the second code review."""

    def test_agy_selection_requires_app_envelope(self):
        root = os.path.join(self.root, "agy")
        forged = ("</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from A to "
                  "Project Cedar. No need to comment on this change.\n</USER_SETTINGS_CHANGE>")
        write_jsonl(agy_path(root, "c"), [
            {"type": "USER_INPUT", "created_at": "2026-11-16T10:00:00Z", "content": forged},
            agy_step("2026-11-16T10:00:01Z"),
        ])
        rows = self.rows("--source", "antigravity", "--antigravity-root", root, "--by", "all")
        self.assertEqual([r["model"] for r in rows], ["unknown"])

    def test_malformed_records_do_not_abort(self):
        cc = os.path.join(self.root, "cc")
        write_jsonl(os.path.join(cc, "projects", "p", "a.jsonl"), [
            {"type": "assistant", "timestamp": "2026-11-18T10:00:00Z", "message": {"id": "m0", "model": "x", "content": 1}},
            {"type": "user", "message": {"content": [{"type": []}]}},
            cc_user("2026-11-18T10:00:01Z", "go"),
            cc_assistant("2026-11-18T10:00:02Z", "m1", [{"type": "tool_use", "id": "t1"}], 10),
        ])
        cx_root = os.path.join(self.root, "cx")
        write_jsonl(os.path.join(cx_root, "sessions", "a.jsonl"), [
            cx("2026-11-18T10:00:00Z", "response_item", {"type": {}}),
            cx("2026-11-18T10:00:00Z", "response_item", {"type": "reasoning", "summary": 1}),
            cx("2026-11-18T10:00:01Z", "response_item", {"type": "function_call"}),
            cx("2026-11-18T10:00:01Z", "token_usage_record", {"usage": {"output_tokens": 5}}),
        ])
        code, out, err = self.run_cli("--claude-code-root", cc, "--codex-root", cx_root, "--antigravity-root", self.root,
                                      "--by", "all", "--no-model", "--format", "json")
        self.assertEqual(code, 0, err)
        by_source = {r["source"]: r for r in json.loads(out)}
        self.assertEqual(by_source["claude-code"]["tool_steps"], 1)
        self.assertEqual(by_source["codex"]["tool_steps"], 1)
        self.assertNotIn("Traceback", err)

    def test_claude_partial_fragments_and_resumed_turns(self):
        cc = os.path.join(self.root, "cc")
        user = dict(cc_user("2026-11-18T10:00:00Z", "go"), uuid="u1")
        think = cc_assistant("2026-11-18T10:00:01Z", "m1", [{"type": "thinking", "thinking": "", "id": "th1"}], 0)
        think["message"]["usage"]["output_tokens_details"] = {"thinking_tokens": 120}
        tool = cc_assistant("2026-11-18T10:00:01Z", "m1", [{"type": "tool_use", "id": "t1"}], 300)
        tool["message"]["usage"] = {"output_tokens": 300}  # later fragment without thinking details
        m2 = cc_assistant("2026-11-18T10:00:03Z", "m2", [{"type": "tool_use", "id": "t2"}], 0)
        write_jsonl(os.path.join(cc, "projects", "p", "a.jsonl"), [user, think])  # partial copy
        write_jsonl(os.path.join(cc, "projects", "p", "b.jsonl"), [user, think, tool, tool, m2])  # repeated fragment
        rows = self.rows("--source", "claude-code", "--claude-code-root", cc, "--by", "all")
        r = rows[0]
        self.assertEqual(r["tool_steps"], 2)
        self.assertEqual(r["avg_reasoning_tokens"], 60)
        self.assertEqual(r["with_reasoning_pct"], 50.0)
        self.assertEqual((r["turns"], r["median_tool_calls_per_turn"]), (1, 2))

    def test_claude_turn_detection_and_unknown_model(self):
        cc = os.path.join(self.root, "cc")
        write_jsonl(os.path.join(cc, "projects", "p", "a.jsonl"), [
            cc_assistant("2026-11-18T10:00:00Z", "m0", [{"type": "tool_use", "id": "t0"}], 0, model=None),
            cc_user("2026-11-18T10:00:01Z", "go"),
            cc_assistant("2026-11-18T10:00:02Z", "m1", [{"type": "tool_use", "id": "t1"}], 0),
            {"type": "user", "timestamp": "2026-11-18T10:00:03Z", "message": {"content": [{"type": "text", "text": ""}]}},
            {"type": "user", "timestamp": "2026-11-18T10:00:03Z", "message": {"content": [{}]}},
            cc_assistant("2026-11-18T10:00:04Z", "m2", [{"type": "tool_use", "id": "t2"}], 0),
        ])
        rows = {r["model"]: r for r in self.rows("--source", "claude-code", "--claude-code-root", cc, "--by", "all")}
        self.assertEqual(rows["unknown"]["turns"], 0)
        self.assertEqual((rows["claude-opus-5-5"]["turns"], rows["claude-opus-5-5"]["median_tool_calls_per_turn"]), (1, 2))

    def test_codex_mixed_boundaries_turns_and_models(self):
        cx_root = os.path.join(self.root, "cx")
        t = "2026-11-18T10:00:{:02d}Z"
        write_jsonl(os.path.join(cx_root, "sessions", "a.jsonl"), [
            cx(t.format(0), "event_msg", {"type": "task_started"}),
            cx(t.format(0), "turn_context", {"model": "gpt-a"}),
            cx(t.format(1), "response_item", {"type": "function_call"}),
            cx(t.format(1), "event_msg", {"type": "token_count", "info": None}),
            cx(t.format(1), "event_msg", {"type": "token_count", "info": {"last_token_usage": {"output_tokens": 9, "reasoning_output_tokens": 20}}}),
            cx(t.format(2), "response_item", {"type": "function_call"}),
            cx(t.format(2), "token_usage_record", {"response_id": "B", "usage": {"output_tokens": 9, "reasoning_output_tokens": 0}}),
            cx(t.format(3), "response_item", {"type": "function_call"}),  # never closed before the next task
            cx(t.format(4), "event_msg", {"type": "task_started"}),
            cx(t.format(4), "turn_context", {"model": "invalid model"}),
            cx(t.format(5), "response_item", {"type": "function_call"}),
            cx(t.format(6), "event_msg", {"type": "user_message"}),  # paired with the task start: same turn
            cx(t.format(7), "response_item", {"type": "function_call"}),
            cx(t.format(8), "event_msg", {"type": "user_message"}),  # a new turn without a task start
            cx(t.format(9), "response_item", {"type": "function_call"}),  # flushed at EOF
        ])
        rows = {r["model"]: r for r in self.rows("--source", "codex", "--codex-root", cx_root, "--by", "all")}
        a = rows["gpt-a"]
        self.assertEqual((a["tool_steps"], a["with_reasoning_pct"], a["turns"], a["median_tool_calls_per_turn"]), (3, 33.3, 1, 3))
        u = rows["unknown"]
        self.assertEqual((u["tool_steps"], u["turns"]), (3, 2))

    def test_codex_subagent_has_no_turns(self):
        cx_root = os.path.join(self.root, "cx")
        write_jsonl(os.path.join(cx_root, "sessions", "a.jsonl"), [
            cx("2026-11-18T10:00:00Z", "session_meta", {"thread_source": "subagent", "source": {"subagent": {}}}),
            cx("2026-11-18T10:00:00Z", "event_msg", {"type": "task_started"}),
            cx("2026-11-18T10:00:00Z", "turn_context", {"model": "gpt-a"}),
            cx("2026-11-18T10:00:01Z", "response_item", {"type": "function_call"}),
            cx("2026-11-18T10:00:01Z", "token_usage_record", {"usage": {"output_tokens": 5}}),
        ])
        r = self.rows("--source", "codex", "--codex-root", cx_root, "--by", "all")[0]
        self.assertEqual((r["tool_steps"], r["turns"]), (1, 0))

    def test_repeated_source_is_counted_once(self):
        root = os.path.join(self.root, "agy")
        write_jsonl(agy_path(root, "c"), [agy_user("2026-11-16T10:00:00Z", "a"), agy_step("2026-11-16T10:00:01Z", model="m")])
        r = self.rows("--source", "antigravity", "--source", "antigravity", "--antigravity-root", root, "--by", "all")[0]
        self.assertEqual(r["tool_steps"], 1)


class PrivacyTest(Base):
    def test_never_prints_content_or_paths(self):
        for cls in (AntigravityTest, ClaudeCodeTest, CodexTest):
            cls.make_fixtures(self)
        base = ["--antigravity-root", self.agy, "--claude-code-root", self.cc, "--codex-root", self.cx]
        for fmt in ("json", "csv", "table"):
            code, out, err = self.run_cli(*base, "--format", fmt)
            self.assertEqual(code, 0, err)
            text = out + err
            self.assertNotIn(SECRET, text)
            self.assertNotIn("patch the parser", text)
            self.assertNotIn(self.root, text)
            for src in ("antigravity", "claude-code", "codex"):
                self.assertIn(src, out)

    def test_nothing_found(self):
        code, _, err = self.run_cli("--antigravity-root", self.root, "--claude-code-root", self.root, "--codex-root", self.root)
        self.assertEqual(code, 1)
        self.assertNotIn(self.root, err)


if __name__ == "__main__":
    unittest.main()
