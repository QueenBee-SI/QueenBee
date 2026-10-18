"""A sweep from both ends: lanes advance, stop past their peak or where they
meet, a clearly losing run is stopped early, and only a full log is done."""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

from queenbee.mother import sweep
from queenbee.mother.dispatch import Dispatch

SPEC = {"param": "--lambda-reward", "min": 0.1, "max": 1.0, "epochs": 4,
        "lanes": [{"name": "up", "start": 0.1, "step": 0.1}, {"name": "down", "start": 1.0, "step": -0.1}]}


class LogicTests(unittest.TestCase):
    def test_lanes_from_both_ends(self):
        s = sweep.normalise(SPEC)
        self.assertEqual(s["lanes"][0]["values"][:3], [0.1, 0.2, 0.3])
        self.assertEqual(s["lanes"][1]["values"][:3], [1.0, 0.9, 0.8])
        self.assertIsNone(sweep.normalise({"param": ""}))

    def test_a_lane_stops_once_its_score_falls(self):
        s = sweep.normalise(SPEC)
        up = s["lanes"][0]
        up["runs"] = [[0.1, 0.60], [0.3, 0.62], [0.2, 0.64]]      # finished out of order
        self.assertIn("after 0.2", sweep.peak_passed(up))
        up["runs"] = [[0.1, 0.60], [0.3, 0.62]]                  # 0.2 still running: undecided
        self.assertIsNone(sweep.peak_passed(up))

    def test_an_early_stopped_score_is_not_a_trend(self):
        s = sweep.normalise(SPEC)
        down = s["lanes"][1]
        # 0.8 finished; 0.7 was stopped early with a lower partial best: the
        # lane has not passed its peak, it just has no full score for 0.7.
        down["runs"] = [[1.0, .55, True], [0.9, .55, True], [0.8, .557, False], [0.7, .553, True]]
        self.assertIsNone(sweep.peak_passed(down))
        down["runs"].append([0.6, .55, False])                    # a full run that fell
        self.assertIn("after 0.8", sweep.peak_passed(down))

    def test_next_value_skips_running_ones_and_stops_where_lanes_meet(self):
        s = sweep.normalise(SPEC)
        up, down = s["lanes"]
        up.update(runs=[[0.1, .5]], active={"0.2": 3})
        down.update(runs=[], active={})
        self.assertEqual(sweep.next_value(up, s["lanes"]), ((0.3, 1), ""))
        up.update(runs=[[0.1, .5], [0.2, .6], [0.3, .7], [0.4, .8]], active={})
        down.update(runs=[[1.0, .5], [0.9, .6], [0.8, .7], [0.7, .8], [0.6, .85]], active={"0.5": 9})
        self.assertEqual(sweep.next_value(up, s["lanes"]), (None, "met the other lane"))

    def test_fill_template(self):
        self.assertEqual(sweep.fill("--lambda-reward {value} --run-name s_{tag}", 0.3),
                         "--lambda-reward 0.3 --run-name s_0p3")

    def test_early_stop_against_the_best_finished_run(self):
        spec = {"min_epoch": 3, "margin": 0.05}
        best = [0.2, 0.4, 0.55, 0.6]
        self.assertIsNone(sweep.should_stop_early([0.1, 0.3], [best], spec))           # too early
        self.assertIsNone(sweep.should_stop_early([0.2, 0.38, 0.52], [best], spec))    # close enough
        self.assertIn("behind", sweep.should_stop_early([0.1, 0.2, 0.3], [best], spec))


class FirstValueTests(unittest.TestCase):
    def test_the_first_run_is_filled_from_the_template_not_trusted(self):
        proto = {"priority": 1, "title": "t", "goal": "g", "project_path": ".", "agent": "claude",
                 "effort": "low", "same_as": None,
                 "command": "py x.py --lam 1.0 --run-name r1.0",           # the script's default
                 "command_template": "py x.py --lam {value} --run-name r{tag}",
                 "metrics_template": "results/r{tag}.jsonl", "metrics_path": "results/r1.0.jsonl"}
        d = Dispatch([proto], {"models": {}}, mock.Mock())
        d.start_sweep(sweep.normalise(SPEC))
        self.assertEqual(d.jobs[0]["command"], "py x.py --lam 0.1 --run-name r0p1")
        self.assertEqual(d.jobs[0]["metrics_path"], "results/r0p1.jsonl")


class RepeatTests(unittest.TestCase):
    GRID = {"param": "--lambda-reward", "epochs": 30, "repeats": 3,
            "lanes": [{"name": "grid", "values": [0.1, 0.4, 0.7, 1.0]}]}

    def test_a_listed_grid_runs_every_value_three_times(self):
        s = sweep.normalise(self.GRID)
        lane = s["lanes"][0]
        self.assertTrue(lane["exhaustive"])
        self.assertEqual((s["repeats"], s["slots"], s["margin"]), (3, 2, 0.15))
        lane.update(runs=[[0.1, .55, False, 1]], active={"0.1#2": 2})
        self.assertEqual(sweep.next_value(lane, s["lanes"], 3), ((0.1, 3), ""))
        lane.update(runs=[[0.1, .55, False, 1], [0.1, .6, False, 2], [0.1, .58, False, 3],
                          [0.4, .5, False, 1], [0.4, .5, False, 2], [0.4, .5, False, 3]], active={})
        self.assertIsNone(sweep.peak_passed(lane, repeats=3))          # a listed grid runs whole
        self.assertEqual(sweep.next_value(lane, s["lanes"], 3), ((0.7, 1), ""))

    def test_best_by_mean_with_spread(self):
        s = sweep.normalise(self.GRID)
        lane = s["lanes"][0]
        lane["runs"] = [[1.0, .55, False, 1], [1.0, .61, False, 2], [1.0, .58, False, 3],
                        [0.4, .60, False, 1], [0.4, .59, False, 2], [0.4, 0.0, True, 3]]
        res = sweep.summary(s["lanes"], "max", 3)
        self.assertEqual(res["best"], 0.4)                              # early-stopped repeat left out
        self.assertAlmostEqual(res["score"], 0.595)
        self.assertEqual(res["n"], 2)

    def test_each_repeat_gets_its_own_output(self):
        self.assertEqual(sweep.fill("--lam {value} --run-name r{tag}", 0.4, 2, 3), "--lam 0.4 --run-name r0p4_r2")


class SlotTests(unittest.TestCase):
    def test_a_stopped_lane_hands_its_slot_to_the_other(self):
        s = sweep.normalise(SPEC)
        up, down = s["lanes"]
        up.update(runs=[[0.1, .6], [0.2, .5]], active={}, stopped=True)
        down.update(runs=[[1.0, .4]], active={"0.9": 5})
        self.assertEqual(sweep.next_value(down, s["lanes"]), ((0.8, 1), ""))


class DispatchSweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = self.tmp.name
        self.terms = {}
        self.closed = []
        self.submits = 0
        host = mock.Mock(port=7877)

        def call(method, *a, **k):
            if method == "terminals":
                return list(self.terms.values())
            if method == "terminal_read":
                return {"data": ""}
            if method == "terminal_close":
                self.closed.append(a[0])
                if a[0] in self.terms:
                    self.terms[a[0]]["running"] = False
                return {"ok": True}
            return {"ok": True}
        host._call.side_effect = call
        cmd = f'"{sys.executable}" -m queenbee.gpu submit --project-root "{self.folder}" -- x --lr {{value}} --run-name s_{{tag}}'
        proto = {"priority": 1, "title": "t", "goal": "g", "project_path": self.folder, "agent": "claude",
                 "effort": "low", "same_as": None, "work_order": "1.", "command": sweep.fill(cmd, 0.1),
                 "command_template": cmd, "metrics_template": "results/s_{tag}_log.jsonl",
                 "metrics_path": "results/s_0p1_log.jsonl", "metric_key": "iou"}
        self.d = Dispatch([proto], {"models": {"claude": {}, "codex": {}}}, host)
        self.d._workloads = lambda: {}
        self.d.start_sweep(sweep.normalise(SPEC))
        self.d.jobs[0].update(terminal="term-1", workload="term-2", status="GPU job submitted",
                              launched_at=time.time() - 5, submitted_at=time.time() - 600)
        self.terms["term-2"] = {"id": "term-2", "running": True, "offset": 1, "started_at": time.time() - 500}

    def log(self, tag, values):
        path = os.path.join(self.folder, "results", f"s_{tag}_log.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            for i, v in enumerate(values):
                f.write(json.dumps({"epoch": i, "iou": v}) + "\n")

    def submit(self, argv, *a, **k):
        self.submits += 1
        tid = f"term-{10 + self.submits}"
        self.terms[tid] = {"id": tid, "running": True, "offset": 1, "started_at": time.time() - 500}
        return mock.Mock(returncode=0, stdout=json.dumps({"ok": True, "id": tid}), stderr="")

    def test_the_other_lane_starts_with_no_agent_and_a_full_log_finishes(self):
        with mock.patch("queenbee.mother.dispatch.subprocess.run", side_effect=self.submit) as run:
            self.d.step()                                  # adds the down lane's first value
            self.d.step()                                  # submits it directly
            titles = [j["title"] for j in self.d.jobs]
            self.assertEqual(titles[1], "lambda-reward = 1 (down)")
            self.assertIn("--lr 1", " ".join(run.call_args[0][0]))
            self.assertEqual(self.d.jobs[1]["status"], "GPU job submitted")

            self.log("0p1", [0.1, 0.2])                   # half a run: not done
            self.terms["term-2"]["running"] = False
            self.d.jobs[0]["restarts"] = 1                 # no silent-death restart in this test
            self.d.step()
            self.assertNotEqual(self.d.jobs[0]["status"], "done")

    def test_a_losing_run_is_stopped_early_and_its_lane_moves_on(self):
        self.d.sweep["min_epoch"] = 3
        with mock.patch("queenbee.mother.dispatch.subprocess.run", side_effect=self.submit):
            self.d.step()
            self.d.step()
            self.log("0p1", [0.2, 0.4, 0.55, 0.6])          # P1 finished all 4 epochs
            self.terms["term-2"]["running"] = False
            self.d.step()
            self.assertEqual(self.d.jobs[0]["status"], "done")
            self.log("1", [0.05, 0.1, 0.15])               # the down lane trails far behind
            self.d.step()
            down = self.d.jobs[1]
            self.assertEqual(down["status"], "stopped early")
            self.assertIn(down["workload"], self.closed)
            self.d.step()
            lanes = {l["name"]: l for l in self.d.sweep["lanes"]}
            self.assertEqual(lanes["down"]["runs"][0][0], 1.0)
            self.assertTrue(any(k.startswith("0.9") for k in lanes["down"]["active"]))


if __name__ == "__main__":
    unittest.main()


class ScienceTests(unittest.TestCase):
    def test_every_repeat_has_its_own_recorded_seed_shared_across_values(self):
        spec = sweep.normalise({"param": "--lam", "lanes": [{"start": 0.1, "step": 0.1}], "min": 0.1, "max": 0.3,
                                "repeats": 3, "seed": 50, "seed_flag": "--seed"})
        seeds = [sweep.seed_for(spec, r) for r in (1, 2, 3)]
        self.assertEqual(seeds, [50, 51, 52])
        cmd = sweep.fill("python t.py --lam {value} --run-name r{tag}", 0.2, 2, 3, seeds[1], "--seed")
        self.assertTrue(cmd.endswith("--seed 51"))
        with_slot = sweep.fill("python t.py --seed {seed} --lam {value}", 0.2, 2, 3, 51, "--seed")
        self.assertEqual(with_slot, "python t.py --seed 51 --lam 0.2")

    def test_a_run_is_scored_where_it_ended_not_by_its_best_epoch(self):
        hist = [0.5, 0.9, 0.6, 0.6, 0.6]
        self.assertAlmostEqual(sweep.final_of(hist), 0.6)
        self.assertEqual(sweep.best_of(hist), 0.9)

    def test_margin_follows_the_metric_scale(self):
        spec = {"min_epoch": 3, "margin_rel": 0.05}
        self.assertIsNotNone(sweep.should_stop_early([40, 42, 44], [[30, 31, 32]], spec, "min"))   # a loss of ~30
        self.assertIsNone(sweep.should_stop_early([40, 42, 31.2], [[30, 31, 30.8]], spec, "min"))
        small = {"min_epoch": 3, "margin_rel": 0.05}
        self.assertIsNone(sweep.should_stop_early([0.0011, 0.0011, 0.00108], [[0.0011, 0.0011, 0.00107]], small, "min"))

    def test_a_fall_inside_the_noise_is_not_a_peak(self):
        lane = {"name": "up", "values": [0.1, 0.2], "runs": [
            [0.1, 0.60, False, 1], [0.1, 0.70, False, 2], [0.1, 0.50, False, 3],
            [0.2, 0.55, False, 1], [0.2, 0.68, False, 2], [0.2, 0.50, False, 3]], "active": {}}
        self.assertIsNone(sweep.peak_passed(lane, "max", 0.0, 3))
        lane["runs"] = [[0.1, 0.80, False, 1], [0.1, 0.81, False, 2], [0.1, 0.79, False, 3],
                        [0.2, 0.50, False, 1], [0.2, 0.51, False, 2], [0.2, 0.49, False, 3]]
        self.assertIn("fell", sweep.peak_passed(lane, "max", 0.0, 3))
