"""Mother's dispatch: order, shared folders and stopped agents."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from queenbee.mother.dispatch import Dispatch


def job(priority, path, same_as=None):
    return {"priority": priority, "title": f"job {priority}", "goal": "g", "project_path": path,
            "agent": "claude", "effort": "medium", "same_as": same_as}


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a = str(Path(self.tmp.name) / "a"); Path(self.a).mkdir()
        self.b = str(Path(self.tmp.name) / "b"); Path(self.b).mkdir()
        self.host = mock.Mock(port=7877)
        self.terms = []
        self.host._call.side_effect = lambda m, *a, **k: (
            self.terms if m == "terminals" else {"ok": True, "id": f"term-{len(self.launched) + 1}"})
        self.launched = []

    def make(self, jobs):
        d = Dispatch(jobs, {"models": {"claude": {}, "codex": {}}}, self.host)

        def launch(j, ref=None, same_folder=False):
            self.launched.append((j["priority"], ref and ref["priority"], same_folder))
            j["terminal"] = f"term-{j['priority']}"
            j["status"] = "agent working"
            self.terms.append({"id": j["terminal"], "running": True})
        d._launch = launch
        d._workloads = lambda: {}
        return d

    def test_jobs_in_one_folder_wait_for_the_one_before(self):
        d = self.make([job(1, self.a), job(2, self.a), job(3, self.b)])
        d.step()
        self.assertEqual([p for p, _, _ in self.launched], [1, 3])
        self.assertEqual(d.jobs[1]["waiting_for"], 1)
        d._workloads = lambda: {"term-1": "term-9"}
        d.step()
        self.assertIn((2, 1, True), self.launched)

    def test_closed_agent_is_stopped_and_later_jobs_in_its_folder_do_not_start(self):
        d = self.make([job(1, self.a), job(2, self.a), job(3, self.b)])
        d.step()
        self.terms.clear()                      # the user closed the agent's terminal
        d.step()
        self.assertEqual(d.jobs[0]["status"], "stopped")
        # A later job in that folder builds on it ("score the best run"):
        # it must not start on its own. Another folder is unaffected.
        self.assertEqual(d.jobs[1]["status"], "failed")
        self.assertNotIn(2, [p for p, _, _ in self.launched])
        self.assertIn(3, [p for p, _, _ in self.launched])

    def test_prepared_job_does_not_start_after_its_predecessor_stopped(self):
        d = self.make([dict(job(1, self.a), work_order="adapt"), dict(job(2, self.a), work_order="run only")])
        d.step()
        self.terms.clear()
        d.step()
        self.assertEqual(d.jobs[1]["status"], "failed")
        self.assertNotIn(2, [p for p, _, _ in self.launched])

    def test_same_as_a_stopped_job_fails(self):
        d = self.make([job(1, self.a), job(2, self.a, same_as=1)])
        d.step()
        self.terms.clear()
        d.step()
        self.assertEqual(d.jobs[1]["status"], "failed")

    def test_unprepared_job_is_never_escalated(self):
        d = self.make([job(1, self.a)])
        d.state_of = lambda t: ("waiting", "turn finished -- your move")
        d._escalate = mock.Mock()
        d.step()
        d.step()
        d._escalate.assert_not_called()

    def test_prepared_job_that_stops_without_reporting_is_escalated(self):
        j = dict(job(1, self.a), work_order="1. run it")
        d = self.make([j])
        d.state_of = lambda t: ("waiting", "turn finished -- your move")
        d._escalate = mock.Mock()
        d.step()
        d.step()
        d._escalate.assert_called_once()

    def test_restore_after_reopen(self):
        d = self.make([job(1, self.a)])
        with mock.patch.object(Dispatch, "state_path", staticmethod(lambda: Path(self.tmp.name) / "p.json")):
            d.step()
            d.save()
            back = Dispatch.restore({"models": {}}, self.host)
        self.assertEqual(back.jobs[0]["terminal"], "term-1")
        self.assertEqual(back.jobs[0]["status"], "agent working")


if __name__ == "__main__":
    unittest.main()
