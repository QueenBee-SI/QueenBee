"""The free demo project can be solved, and is unsolved as shipped."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(ROOT, "queenbee", "mother", "demo_free")
SOLVED = os.path.join(ROOT, "tests", "fixtures", "free_demo_solved", "notes")


def run_suite(folder):
    p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."], cwd=folder,
                       capture_output=True, text=True, timeout=120)
    return p.returncode, p.stderr


class FreeDemo(unittest.TestCase):
    def copy(self):
        tmp = tempfile.mkdtemp(prefix="free-demo-")
        self.addCleanup(shutil.rmtree, tmp, True)
        dest = os.path.join(tmp, "demo")
        shutil.copytree(DEMO, dest, ignore=shutil.ignore_patterns("__pycache__"))
        return dest

    def test_as_shipped_every_test_fails(self):
        code, err = run_suite(self.copy())
        self.assertNotEqual(code, 0)
        self.assertIn("Ran 10 tests", err)
        self.assertNotIn("OK", err.splitlines()[-1])

    def test_with_the_three_parts_built_the_whole_suite_passes(self):
        dest = self.copy()
        for name in ("store.py", "search.py", "export.py"):
            shutil.copy(os.path.join(SOLVED, name), os.path.join(dest, "notes", name))
        code, err = run_suite(dest)
        self.assertEqual(code, 0, err)
        self.assertIn("Ran 10 tests", err)

    def test_a_search_part_cannot_pass_without_agreeing_with_the_store(self):
        """Searching a different note shape than the store produces fails the search tests."""
        dest = self.copy()
        shutil.copy(os.path.join(SOLVED, "store.py"), os.path.join(dest, "notes", "store.py"))
        shutil.copy(os.path.join(SOLVED, "export.py"), os.path.join(dest, "notes", "export.py"))
        with open(os.path.join(dest, "notes", "search.py"), "w", encoding="utf-8") as fh:
            fh.write("def find(notes, query=None, tag=None):\n"
                     "    return [n for n in notes if (query is None or query.lower() in n[1].lower())]\n")   # a tuple shape
        code, err = run_suite(dest)
        self.assertNotEqual(code, 0)
