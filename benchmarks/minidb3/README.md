# MiniDB

A small SQL database engine in Python 3.11, standard library only.

- `docs/MINIDB.md`: the specification of the engine as it is (90 checks pass).
- `docs/TASK2.md`: the nine features of version 2 (done; 154 checks pass).
- `TASK.md`: what is to be added now: nine more features (NULLS FIRST/LAST, GROUP_CONCAT and FILTER, new functions, CHECK
  and composite UNIQUE, RETURNING, savepoints, CREATE TABLE AS, recursive CTEs, UPDATE ... FROM). 219 checks in all.
- `docs/ARCHITECTURE.md`: how the modules fit together.
- `QUEEN.md`: how a team of agents is to work on this project.
- `python check_minidb3.py` runs every check (`check_minidb.py` the earlier 154 alone); `python -m unittest discover -s tests` runs the unit tests.
