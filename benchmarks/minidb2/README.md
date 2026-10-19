# MiniDB

A small SQL database engine in Python 3.11, standard library only.

- `docs/MINIDB.md`: the specification of the engine as it is (90 checks pass).
- `TASK.md`: what is to be added now: nine features (CASE and CAST, correlated subqueries, CTEs, views, set operations,
  window functions, INSERT ... SELECT and upserts, ALTER TABLE, foreign keys). 154 checks in all.
- `docs/ARCHITECTURE.md`: how the modules fit together.
- `QUEEN.md`: how a team of agents is to work on this project.
- `python check_minidb.py` runs every check; `python -m unittest discover -s tests` runs the unit tests.
