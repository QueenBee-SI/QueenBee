"""Mother ranks finished jobs from their output files, with no agent."""

import json
import tempfile
import unittest
from pathlib import Path

from queenbee.mother.planner import normalise_compare
from queenbee.mother.results import compare, read_value


class ResultsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "a").mkdir()
        (root / "a" / "metrics.json").write_text(json.dumps({"val": {"iou": 0.51}}))
        (root / "b.csv").write_text("epoch,iou\n1,0.40\n2,0.58\n")
        self.root = str(root)

    def test_json_nested_and_csv_last_row(self):
        self.assertAlmostEqual(read_value(self.root, "a/metrics.json", "val.iou"), 0.51)
        self.assertAlmostEqual(read_value(self.root, "b.csv", "iou"), 0.58)
        self.assertIsNone(read_value(self.root, "missing.json", "iou"))

    def test_best_by_goal(self):
        jobs = [{"priority": 1, "project_path": self.root, "metrics_path": "a/metrics.json", "metric_key": "val.iou"},
                {"priority": 2, "project_path": self.root, "metrics_path": "b.csv", "metric_key": "iou"},
                {"priority": 3, "project_path": self.root, "metrics_path": "none.json", "metric_key": "iou"}]
        r = compare(jobs, {"metric": "iou", "goal": "max"})
        self.assertEqual(r["best"], 2)
        self.assertFalse(r["complete"])
        self.assertEqual(compare(jobs, {"metric": "iou", "goal": "min"})["best"], 1)
        self.assertIsNone(compare(jobs, None))

    def test_planner_compare_spec(self):
        self.assertEqual(normalise_compare({"metric": "val_acc", "goal": "higher"}), {"metric": "val_acc", "goal": "max"})
        self.assertEqual(normalise_compare({"metric": "loss", "goal": "min"})["goal"], "min")
        self.assertIsNone(normalise_compare({"metric": ""}))


if __name__ == "__main__":
    unittest.main()
