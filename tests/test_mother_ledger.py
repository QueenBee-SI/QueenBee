"""The ledger prices transcripts per model and counts each message once."""

import json
import tempfile
import unittest
from pathlib import Path

from queenbee.mother.ledger import message_cost, transcript_cost


class LedgerTests(unittest.TestCase):
    def test_prices(self):
        self.assertAlmostEqual(message_cost("claude-haiku-4-5-20251001", {"input_tokens": 1_000_000}), 1.0)
        self.assertAlmostEqual(message_cost("claude-sonnet-5", {"output_tokens": 1_000_000}), 10.0)
        self.assertAlmostEqual(message_cost("claude-opus-5-5", {"cache_read_input_tokens": 1_000_000}), 0.20)
        # 1-hour cache writes cost 2x input, 5-minute ones 1.25x.
        usage = {"cache_creation_input_tokens": 2_000_000,
                 "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000, "ephemeral_5m_input_tokens": 1_000_000}}
        self.assertAlmostEqual(message_cost("claude-sonnet-5", usage), 2 * 2.0 + 2 * 1.25)

    def test_one_message_split_over_lines_is_counted_once(self):
        usage = {"input_tokens": 1000, "output_tokens": 100}
        lines = [
            {"type": "user", "message": {"content": "go"}, "timestamp": "2026-09-27T09:00:00Z"},
            {"type": "assistant", "message": {"id": "m1", "model": "claude-haiku-4-5", "usage": usage,
                                              "content": [{"type": "text", "text": "a"}]}},
            {"type": "assistant", "message": {"id": "m1", "model": "claude-haiku-4-5", "usage": usage,
                                              "content": [{"type": "tool_use", "name": "Bash"}]}},
        ]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            p.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
            c = transcript_cost(p)
        self.assertEqual(c["turns"], 1)
        self.assertAlmostEqual(c["cost_usd"], round((1000 * 1 + 100 * 5) / 1e6, 4))


if __name__ == "__main__":
    unittest.main()
