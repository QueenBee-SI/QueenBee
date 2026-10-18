"""Where the tokens go: a worker's transcript split into exploring (before its first write) and building (after)."""
import json
import os
import tempfile
import unittest
from unittest import mock

from queenbee.mother import economy


def claude_line(ts, mid, blocks, usage, kind="assistant", model="claude-sonnet"):
    return json.dumps({"type": kind, "timestamp": ts, "uuid": mid + "-u",
                       "message": {"id": mid, "model": model, "content": blocks, "usage": usage}})


class ClaudeProfile(unittest.TestCase):
    def test_tokens_before_the_first_write_are_exploring(self):
        usage = {"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000, "output_tokens": 50}
        lines = [
            json.dumps({"type": "user", "timestamp": "t0", "message": {"content": "You are a worker bee for job W2 (parser) in C:/p"}}),
            claude_line("t1", "m1", [{"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}], usage),
            claude_line("t1", "m1", [{"type": "text", "text": "..."}], usage),                      # same message, one more block
            claude_line("t2", "m2", [{"type": "tool_use", "name": "Bash", "input": {"command": "python -m pytest"}}], usage),
            claude_line("t3", "m3", [{"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}}], usage),
            claude_line("t4", "m4", [{"type": "tool_use", "name": "Bash", "input": {"command": "echo x > out.txt"}}], usage),
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        p = economy.claude_profile(fh.name)
        os.unlink(fh.name)
        self.assertEqual(p["job"], 2)
        self.assertEqual(p["tokens"], {"explore": 2 * 1160, "build": 2 * 1160})     # m1 and m2 explore; m3 (the write) and m4 build
        self.assertEqual(p["reads_before_write"], 2)
        self.assertEqual(p["first_write_at"], "t3")
        self.assertEqual(p["tools"]["Read"], 1)
        self.assertEqual(p["context_end"], 1110)
        self.assertEqual(p["model"], "claude-sonnet")

    def test_a_shell_redirection_counts_as_the_first_write(self):
        usage = {"input_tokens": 1, "output_tokens": 1}
        lines = [claude_line("t1", "m1", [{"type": "tool_use", "name": "Bash", "input": {"command": "cat > out.txt"}}], usage),
                 claude_line("t2", "m2", [{"type": "tool_use", "name": "Read", "input": {}}], usage)]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        p = economy.claude_profile(fh.name)
        os.unlink(fh.name)
        self.assertEqual(p["turns"], {"explore": 0, "build": 2})


class CodexProfile(unittest.TestCase):
    def test_turn_usage_is_split_at_the_first_writing_call(self):
        def ev(payload, kind="event_msg", ts="t"):
            return json.dumps({"type": kind, "timestamp": ts, "payload": payload})
        lines = [
            ev({"cwd": "C:/proj"}, "session_meta"),
            ev({"model": "gpt-5.6-terra"}, "turn_context"),
            ev({"type": "message", "content": [{"type": "input_text", "text": "You are a worker bee for job W3 (storage)"}]}, "response_item"),
            ev({"type": "custom_tool_call", "name": "exec", "input": "tools.exec_command({cmd: 'Get-Content a.py'})"}, "response_item"),
            ev({"type": "token_count", "info": {"last_token_usage": {"input_tokens": 500, "output_tokens": 20, "total_tokens": 520}}}),
            ev({"type": "custom_tool_call", "name": "exec", "input": "tools.exec_command({cmd: 'apply_patch <<EOF ...'})"}, "response_item"),
            ev({"type": "token_count", "info": {"last_token_usage": {"input_tokens": 700, "output_tokens": 30, "total_tokens": 730}}}),
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        p = economy.codex_profile(fh.name)
        os.unlink(fh.name)
        self.assertEqual(p["job"], 3)
        self.assertEqual(p["model"], "gpt-5.6-terra")
        self.assertEqual(p["tokens"], {"explore": 520, "build": 730})
        self.assertEqual(p["reads_before_write"], 1)
        self.assertEqual(p["cwd"], "C:/proj")


class TheTable(unittest.TestCase):
    def test_shares_and_markdown(self):
        with mock.patch.object(economy, "worker_profiles", return_value=[
                {"job": 1, "agent": "claude", "model": "sonnet", "tokens": {"explore": 300, "build": 700}, "turns": {"explore": 3, "build": 7},
                 "reads_before_write": 3, "context_end": 50000, "path": "x"}]):
            t = economy.team_table("C:/proj", 0, queen={"planner": 100, "look-ins": 0})
        self.assertEqual(t["total"], 1100)
        self.assertAlmostEqual(t["explore_share"], 0.273, places=3)
        md = economy.render(t)
        self.assertIn("| W1 | claude / sonnet |", md)
        self.assertIn("queen: planner", md)
        self.assertNotIn("look-ins", md)



