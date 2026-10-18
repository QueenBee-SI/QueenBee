"""The Timeline tab's data: when each worker ran, who finished last, and where the tokens come from."""
import tempfile
import unittest
from unittest import mock

from queenbee.mother import config
from queenbee.mother.dispatch import Dispatch
from queenbee.mother.server import Mother


def job(n, status, start, end, agent="claude", tier="sonnet"):
    return {"priority": n, "kind": "task", "title": f"w{n}", "status": status, "project_path": ".",
            "launched_at": start, "agent": agent, "tier": tier, "events": [[end, "done"]] if end else []}


class Timeline(unittest.TestCase):
    def make(self, jobs):
        host = mock.MagicMock()
        with mock.patch("queenbee.mother.server.threading.Thread"):
            q = Mother(host, queen="q9", name="T", primary=False)
        d = Dispatch([], config.load(), host, 7878)
        d.jobs.extend(jobs)
        d.started_at = 1000.0
        q.dispatch = d
        return q

    def test_no_plan_no_workers(self):
        host = mock.MagicMock()
        with mock.patch("queenbee.mother.server.threading.Thread"):
            q = Mother(host, queen="q9", name="T", primary=False)
        self.assertEqual(q.timeline()["workers"], [])

    def test_the_last_worker_to_end_is_the_critical_one(self):
        q = self.make([job(1, "done", 1001, 1100), job(2, "done", 1001, 1400, tier="opus"), job(3, "done", 1002, 1200)])
        with mock.patch("queenbee.mother.ledger.find_worker_transcript", return_value=""):
            t = q.timeline()
        self.assertEqual(t["critical"], 2)
        self.assertEqual([w["end"] for w in t["workers"]], [1100, 1400, 1200])
        self.assertTrue(all(w["tokens"] is None for w in t["workers"]))

    def test_nobody_is_critical_while_a_worker_is_still_running(self):
        q = self.make([job(1, "done", 1001, 1100), job(2, "agent working", 1001, None)])
        with mock.patch("queenbee.mother.ledger.find_worker_transcript", return_value=""):
            t = q.timeline()
        self.assertIsNone(t["critical"])
        self.assertFalse(t["workers"][1]["ended"])

    def test_claude_tokens_come_from_the_transcript_and_codex_ones_are_not_told_apart(self):
        q = self.make([job(1, "done", 1001, 1100), job(2, "done", 1001, 1100, agent="codex", tier="gpt-5.6-terra")])
        with mock.patch("queenbee.mother.ledger.find_worker_transcript", return_value="t.jsonl"), \
             mock.patch("queenbee.mother.ledger._cached", return_value={"tokens": 1234}):
            t = q.timeline()
        self.assertEqual([w["tokens"] for w in t["workers"]], [1234, None])


if __name__ == "__main__":
    unittest.main()
