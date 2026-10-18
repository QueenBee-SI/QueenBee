"""The Changes tab's data: a snapshot when a plan starts, a diff against it later, and who wrote which file."""
import json
import os
import tempfile
import time
import unittest

from queenbee.mother import changes


def write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


class Changes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        write(self.root, "a.py", "one\ntwo\nthree\n")
        write(self.root, "pkg/b.py", "x = 1\n")
        write(self.root, "gone.txt", "bye\n")
        write(self.root, "node_modules/lib.js", "ignored\n")
        write(self.root, ".queenbee/W1.json", "{}")
        with open(os.path.join(self.root, "img.bin"), "wb") as fh:
            fh.write(b"\0\1\2")

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_snapshot_means_no_changes_and_a_note(self):
        out = changes.compare(self.root)
        self.assertEqual(out["files"], [])
        self.assertIn("snapshot", out["note"])

    def test_added_modified_deleted_and_unchanged_files(self):
        self.assertTrue(changes.save_baseline(self.root, 0.0))
        write(self.root, "a.py", "one\nTWO\nthree\nfour\n")
        write(self.root, "new.py", "print(1)\n")
        os.remove(os.path.join(self.root, "gone.txt"))
        out = changes.compare(self.root)
        rows = {r["path"]: r for r in out["files"]}
        self.assertEqual(set(rows), {"a.py", "new.py", "gone.txt"})
        self.assertEqual((rows["a.py"]["status"], rows["a.py"]["added"], rows["a.py"]["removed"]), ("modified", 2, 1))
        self.assertEqual(rows["new.py"]["status"], "added")
        self.assertEqual(rows["gone.txt"]["status"], "deleted")
        self.assertTrue(any(l.startswith("+TWO") for l in rows["a.py"]["diff"]))

    def test_skipped_folders_and_binary_files(self):
        self.assertTrue(changes.save_baseline(self.root, 0.0))
        write(self.root, "node_modules/lib.js", "changed\n")
        write(self.root, ".queenbee/W2.json", "{}")
        with open(os.path.join(self.root, "img.bin"), "wb") as fh:
            fh.write(b"\0\3\4")
        rows = {r["path"]: r for r in changes.compare(self.root)["files"]}
        self.assertEqual(set(rows), {"img.bin"})
        self.assertTrue(rows["img.bin"]["binary"])

    def test_a_plan_picked_up_again_keeps_its_first_snapshot(self):
        started = time.time() - 100
        self.assertTrue(changes.save_baseline(self.root, started))
        write(self.root, "a.py", "changed\n")
        self.assertFalse(changes.save_baseline(self.root, started))                  # same plan: kept
        self.assertEqual(len(changes.compare(self.root)["files"]), 1)
        self.assertTrue(changes.save_baseline(self.root, time.time() + 5))           # a new plan: a fresh snapshot
        self.assertEqual(changes.compare(self.root)["files"], [])

    def test_a_long_diff_is_cut(self):
        write(self.root, "big.txt", "".join(f"line {i}\n" for i in range(2000)))
        changes.save_baseline(self.root, 0.0)
        write(self.root, "big.txt", "".join(f"LINE {i}\n" for i in range(2000)))
        row = changes.compare(self.root)["files"][0]
        self.assertLessEqual(len(row["diff"]), changes.MAX_DIFF_LINES + 1)
        self.assertIn("more lines not shown", row["diff"][-1])

    def test_touched_by_reads_the_tool_calls_of_a_transcript(self):
        t = os.path.join(self.root, "t.jsonl")
        calls = [("Write", os.path.join(self.root, "pkg", "b.py")), ("Edit", os.path.join(self.root, "a.py")),
                 ("Read", os.path.join(self.root, "gone.txt")), ("Write", r"C:\elsewhere\x.py")]
        with open(t, "w", encoding="utf-8") as fh:
            for name, target in calls:
                fh.write(json.dumps({"message": {"content": [{"type": "tool_use", "name": name,
                                                              "input": {"file_path": target}}]}}) + "\n")
            fh.write("not json but mentions \"tool_use\"\n")
        self.assertEqual(changes.touched_by(t, self.root), {"pkg/b.py", "a.py"})


if __name__ == "__main__":
    unittest.main()
