"""The long job: MiniDB 2 and 3 in one go. The MiniDB 2 start with both work packages, scored by check_minidb3.py (219 checks).
Writes ~/QueenBeeDemo/big-N and prints the path; bench.py calls it with TASK=23."""
import os, re, shutil, sys
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "minidb2")
M3 = os.path.join(HERE, "minidb3")
base = os.path.join(os.path.expanduser("~"), "QueenBeeDemo")
k = 1
while os.path.exists(os.path.join(base, f"big-{k}")):
    k += 1
dest = os.path.join(base, f"big-{k}")
shutil.copytree(SRC, dest, ignore=shutil.ignore_patterns("__pycache__"))
shutil.copy(os.path.join(M3, "check_minidb3.py"), dest)
shutil.copy(os.path.join(dest, "TASK.md"), os.path.join(dest, "docs", "TASK2.md"))
t3 = open(os.path.join(M3, "TASK.md"), encoding="utf-8").read()
t3 = t3.replace("# Task: MiniDB 3, nine more features",
    "# Task, second part: MiniDB 3, nine more features\n\n"
    "**This part comes on top of `TASK.md` (version 2), which is built in the same job.** Where this document says version 2 "
    "is finished, read: it is built as part of this job, and its 154 checks must pass at the end together with these.", 1)
open(os.path.join(dest, "TASK3.md"), "w", encoding="utf-8").write(t3)
q = open(os.path.join(dest, "QUEEN.md"), encoding="utf-8").read()
q = q.replace("- `minidb/ast.py`, `minidb/errors.py` and `minidb/result.py` are finished: do not change them.",
    "- `minidb/errors.py` and `minidb/result.py` are finished: do not change them. `minidb/ast.py` is finished for `TASK.md`; "
    "`TASK3.md` adds nodes and fields to it (section 30: the frontend's owner adds them, each new field with a default).")
q = q.replace("- `python check_minidb.py` passes all 154 checks, and `python -m unittest discover -s tests` is green.",
    "- `python check_minidb3.py` passes all 219 checks (the 154 of version 2 among them), and `python -m unittest discover -s tests` is green.")
open(os.path.join(dest, "QUEEN.md"), "w", encoding="utf-8").write(q)
print(dest)
