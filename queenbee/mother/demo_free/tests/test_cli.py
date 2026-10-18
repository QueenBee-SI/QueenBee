import os
import subprocess
import sys

from tests._helpers import WithNotesFile


class CliTests(WithNotesFile):
    def run_cli(self, *args):
        p = subprocess.run([sys.executable, "-m", "notes", *args], capture_output=True, text=True,
                           env={**os.environ}, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return p.returncode, p.stdout.strip()

    def test_the_three_parts_work_together_end_to_end(self):
        self.assertEqual(self.run_cli("add", "buy milk", "--tag", "home", "--tag", "shop"), (0, "added 1"))
        self.assertEqual(self.run_cli("add", "call Ann"), (0, "added 2"))
        self.assertEqual(self.run_cli("list"), (0, "1  buy milk  [home, shop]\n2  call Ann"))
        self.assertEqual(self.run_cli("find", "MILK"), (0, "1  buy milk  [home, shop]"))
        self.assertEqual(self.run_cli("find", "--tag", "home"), (0, "1  buy milk  [home, shop]"))
        self.assertEqual(self.run_cli("find", "zzz"), (1, "no notes found"))
        out = os.path.join(os.path.dirname(self.path), "out.md")
        self.assertEqual(self.run_cli("export", out), (0, f"wrote 2 notes to {out}"))
        with open(out, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "# Notes\n\n- buy milk (home, shop)\n- call Ann\n")
