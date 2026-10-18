"""Mother carrying one task end to end: a job is done only when its result is
written, a silent death restarts it from its saved state, agents on different
files work side by side, stepping aside is recorded, and a report is written."""

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

from queenbee.mother import report
from queenbee.mother.dispatch import Dispatch


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = self.tmp.name
        self.terms = {}
        self.output = {}
        self.opened = []
        host = mock.Mock(port=7877)

        def call(method, *a, **k):
            if method == "terminals":
                return list(self.terms.values())
            if method == "terminal_read":
                return {"data": self.output.get(a[0], "")}
            if method == "open_terminal":
                tid = f"term-{50 + len(self.opened)}"
                self.opened.append(tid)
                self.terms[tid] = {"id": tid, "running": True, "offset": 0, "started_at": time.time()}
                return {"ok": True, "id": tid}
            return {"ok": True}

        host._call.side_effect = call
        self.host = host

    def dispatch(self, jobs):
        d = Dispatch(jobs, {"models": {"claude": {}, "codex": {}}, "worker_model": "haiku",
                            "escalation": ["sonnet"]}, self.host)
        d._workloads = lambda: {}
        return d

    def job(self, n, **kw):
        cmd = (f'"{sys.executable}" -m queenbee.gpu --port 7877 submit --project-root "{self.folder}" '
               f'--name "j{n}" -- "{sys.executable}" train.py')
        base = {"priority": n, "title": f"j{n}", "goal": "g", "project_path": self.folder, "agent": "claude",
                "effort": "low", "same_as": None, "work_order": "1. run", "command": cmd,
                "metrics_path": f"runs/j{n}/metrics.json", "metric_key": "val_acc"}
        base.update(kw)
        return base

    def running(self, d, i, workload, age=600):
        self.terms[workload] = {"id": workload, "running": True, "offset": 10, "started_at": time.time() - age}
        d.jobs[i].update(terminal="term-1", workload=workload, status="GPU job submitted",
                         launched_at=time.time() - age, submitted_at=time.time() - age)


class FinishTests(Base):
    def test_done_only_when_the_result_is_written(self):
        d = self.dispatch([self.job(1)])
        self.running(d, 0, "term-2")
        os.makedirs(os.path.join(self.folder, "runs", "j1"))
        with open(os.path.join(self.folder, "runs", "j1", "metrics.json"), "w") as f:
            f.write('{"val_acc": 0.9}')
        self.terms["term-2"]["running"] = False
        self.output["term-2"] = "epoch 15/15 val_acc 0.9\ndone"
        d.step()
        self.assertEqual(d.jobs[0]["status"], "done")
        self.assertIn("result written", d.jobs[0]["events"][-1][1])

    def test_a_silent_death_restarts_from_the_saved_state_then_goes_to_an_agent(self):
        d = self.dispatch([self.job(1)])
        self.running(d, 0, "term-2")
        self.terms["term-2"]["running"] = False
        self.output["term-2"] = "epoch 7/15 val_acc 0.6"          # killed: no error, no result
        ran = mock.Mock(returncode=0, stdout='{"ok": true, "id": "term-3"}', stderr="")
        with mock.patch("queenbee.mother.dispatch.subprocess.run", return_value=ran) as run:
            d.step()
        run.assert_called_once()
        self.assertEqual((d.jobs[0]["status"], d.jobs[0]["workload"], d.jobs[0]["restarts"]),
                         ("GPU job submitted", "term-3", 1))
        self.assertTrue(any("from its saved state" in e[1] for e in d.jobs[0]["events"]))

        # It dies the same way again: now an agent looks at it.
        self.terms["term-3"] = {"id": "term-3", "running": False, "offset": 10, "started_at": time.time() - 600}
        self.output["term-3"] = "epoch 3/15"
        d.jobs[0]["launched_at"] = time.time() - 600
        d.step()
        self.assertEqual(d.jobs[0]["status"], "agent working")
        self.assertEqual(len(self.opened), 1)
        self.assertIn("ended without its result", d.jobs[0]["retry_note"])

    def test_a_crash_goes_to_an_agent_with_the_error(self):
        d = self.dispatch([self.job(1)])
        self.running(d, 0, "term-2")
        self.terms["term-2"]["running"] = False
        self.output["term-2"] = "Traceback (most recent call last):\nRuntimeError: CUDA error"
        d.step()
        self.assertEqual(d.jobs[0]["status"], "agent working")
        self.assertIn("CUDA error", d.jobs[0]["retry_note"])


class SideBySideTests(Base):
    def test_agents_on_different_files_start_together_same_file_waits(self):
        jobs = [self.job(1, files=["train_cnn.py"]), self.job(2, files=["train_mlp.py"]),
                self.job(3, files=["train_cnn.py"])]
        d = self.dispatch(jobs)
        d.step()
        self.assertEqual([j["status"] for j in d.jobs], ["agent working", "agent working", "queued"])
        self.assertEqual(d.jobs[2]["waiting_for"], 1)

    def test_unknown_files_still_wait(self):
        d = self.dispatch([self.job(1), self.job(2)])
        d.step()
        self.assertEqual([j["status"] for j in d.jobs], ["agent working", "queued"])


class StepAsideTests(Base):
    def test_stepping_aside_and_resuming_are_recorded_once(self):
        d = self.dispatch([self.job(1)])
        self.running(d, 0, "term-2")
        self.terms["term-2"]["offset"] = 100
        self.output["term-2"] = ("[queenbee] j1: preempted after saving its state; waiting for the GPU to resume\n"
                                 "[queenbee] j1: resuming (start 2)\n")
        d.step()
        d.step()                                                   # nothing new: not counted twice
        self.assertEqual((d.jobs[0].get("preempts"), d.jobs[0].get("resumes")), (1, 1))


class ReportTests(unittest.TestCase):
    def test_report_lists_results_gpu_approvals_cost_and_events(self):
        now = time.time()
        jobs = [{"priority": 1, "title": "cnn lr 0.05", "status": "done", "agent": "claude", "tier": "haiku",
                 "attempts": 1, "preempts": 1, "resumes": 1, "restarts": 1,
                 "events": [[now - 300, "agent started"], [now - 10, "finished; result written"]]},
                {"priority": 2, "title": "cnn lr 0.01", "status": "done", "direct": True, "attempts": 1}]
        results = {"metric": "val_acc", "goal": "max", "best": 1,
                   "rows": [{"priority": 1, "value": 0.91}, {"priority": 2, "value": 0.84}]}
        text = report.build(jobs, results, instruction="two runs", gpu={"busy_s": 240, "idle_s": 60},
                            approvals=[{"decision": "approved"}, {"decision": "left to you", "priority": 2,
                                                                  "question": "Edit x?", "why": "outside"}],
                            costs={"planner": {"tokens": 1000, "cost_usd": 0.01},
                                   "architect": {"tokens": 50000, "cost_usd": 0.5},
                                   "workers": {"tokens": 20000, "cost_usd": 0.05}},
                            started_at=now - 400, finished_at=now)
        self.assertIn("**2 of 2 jobs finished**", text)
        self.assertIn("0.9100 ★", text)
        self.assertIn("Mother (no agent)", text)
        self.assertIn("80% of the run busy", text)
        self.assertIn("left to you: 1", text)
        self.assertIn("71,000", text)
        self.assertIn("finished; result written", text)


if __name__ == "__main__":
    unittest.main()
