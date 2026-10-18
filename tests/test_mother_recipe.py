import json
import os
import tempfile
import unittest
from unittest import mock

from queenbee.mother import architect


def _fake_run(calls):
    def run(argv, **kw):
        calls.append(argv)
        out = {"result": json.dumps({"jobs": [{"priority": 1, "work_order": "1. run it", "command": "x",
                                               "files": ["train.py"]}], "questions": [], "notes": ""}),
               "usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.4, "duration_ms": 1000}
        return mock.Mock(stdout=json.dumps(out), stderr="", returncode=0)
    return run


class RecipeCache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.folder = self.dir.name
        with open(os.path.join(self.folder, "train.py"), "w") as fh:
            fh.write("print('train')\n")
        self.jobs = [{"priority": 1, "title": "Run train.py", "goal": "run train.py", "project_path": self.folder}]

    def tearDown(self):
        self.dir.cleanup()

    def prepare(self, calls):
        with mock.patch.object(architect, "binary_for", return_value="claude"), \
                mock.patch.object(architect.subprocess, "run", side_effect=_fake_run(calls)):
            return architect.prepare(self.folder, self.jobs, "sonnet")

    def test_second_identical_call_costs_no_model(self):
        calls = []
        first = self.prepare(calls)
        second = self.prepare(calls)
        self.assertEqual(len(calls), 1)
        self.assertEqual(second["orders"], first["orders"])
        self.assertEqual(second["usage"]["cost_usd"], 0.0)
        self.assertTrue(second["usage"]["cached"])

    def test_changed_script_is_a_new_call(self):
        calls = []
        self.prepare(calls)
        with open(os.path.join(self.folder, "train.py"), "a") as fh:
            fh.write("print('edited')\n")
        self.prepare(calls)
        self.assertEqual(len(calls), 2)

    def test_default_architect_is_not_opus(self):
        from queenbee.mother.config import DEFAULTS
        self.assertNotEqual(DEFAULTS["architect_model"], "opus")


if __name__ == "__main__":
    unittest.main()


class ModelCeiling(unittest.TestCase):
    def test_a_ceiling_set_in_the_file_holds_even_if_a_role_asks_for_more(self):
        import tempfile
        from pathlib import Path
        from queenbee.mother import config
        with tempfile.TemporaryDirectory() as d:
            toml = Path(d) / "mother.toml"
            toml.write_text('max_model = "sonnet"\narchitect_model = "opus"\nescalation = ["opus"]\n'
                            '[models.claude]\nhigh = "opus"\n')
            cfg = config.load(toml)
        self.assertEqual(cfg["architect_model"], "sonnet")
        self.assertEqual(cfg["escalation"], ["sonnet"])
        self.assertEqual(cfg["models"]["claude"]["high"], "sonnet")

    def test_ceiling_can_be_lifted(self):
        import tempfile
        from pathlib import Path
        from queenbee.mother import config
        with tempfile.TemporaryDirectory() as d:
            toml = Path(d) / "mother.toml"
            toml.write_text('max_model = "opus"\narchitect_model = "opus"\n')
            self.assertEqual(config.load(toml)["architect_model"], "opus")

    def test_a_worker_asked_for_opus_runs_on_sonnet(self):
        from queenbee.mother.config import clamp
        self.assertEqual(clamp("opus", "sonnet"), "sonnet")
        self.assertEqual(clamp("haiku", "sonnet"), "haiku")

    def test_by_default_the_user_may_choose_opus_for_the_queen(self):
        from pathlib import Path
        from queenbee.mother import config
        base = config.load(Path("nonexistent.toml"))
        cfg = config.capped({**base, "planner_model": "opus", "strong_model": "opus"})
        self.assertEqual((cfg["planner_model"], cfg["strong_model"]), ("opus", "opus"))
        self.assertEqual(base["strong_model"], "sonnet")                   # the default stays Sonnet

    def test_the_window_can_set_the_queens_model_per_queen(self):
        from queenbee.mother.server import ROLE_KEYS
        for key in ("planner_model", "architect_model", "strong_model", "max_model"):
            self.assertIn(key, ROLE_KEYS)


class Prices(unittest.TestCase):
    def test_prices_come_from_the_settings_file_and_reach_the_planner_as_one_line(self):
        import tempfile
        from pathlib import Path
        from queenbee.mother import config, planner
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "mother.toml")
            path.write_text('[prices]\n"claude/haiku" = 1\n"claude/sonnet" = 3\n"codex/gpt-5.6-terra" = 2\n', encoding="utf-8")
            cfg = config.load(path)
        self.assertEqual(cfg["prices"]["claude/sonnet"], 3.0)
        hint = planner.price_hint(cfg["prices"])
        self.assertIn("claude/haiku 1, codex/gpt-5.6-terra 2, claude/sonnet 3", hint)
        self.assertIn("take the cheaper one", hint)
        self.assertEqual(planner.price_hint({}), "")
        self.assertEqual(config.load(Path(tmp, "none.toml"))["prices"], {})

