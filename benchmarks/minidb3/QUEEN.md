# QUEEN.md: how a team works on this project

Rules the queen and her workers follow.

## Scope
- Work only inside this folder. Edit files and run the tests and checks here; nothing outside it.
- Never delete files that are not your own, never install packages, never touch the network. Python 3.11, standard library only.
- `minidb/errors.py` and `minidb/result.py` are finished: do not change them. `minidb/ast.py` belongs to the parser's owner,
  who adds the new nodes and posts their shapes on the hive board before the others need them.

## How to split the work
- All workers start at once. Do not plan a senior, a skeleton or a "foundation" worker: the interfaces between the parts are
  already fixed in `TASK.md` section 30, `docs/ARCHITECTURE.md` and `.queenbee/FINDINGS.md`.
- Cut the work into parts of about the same size: the slowest part sets the finish time, so no worker should carry the executor
  and also the storage and the documentation. Rule of thumb for three workers: (1) lexer, parser, ast, expressions, functions
  and windows; (2) storage, dml, database (constraints, RETURNING for INSERT, savepoints, CREATE TABLE AS); (3) the executor's
  select side (NULLS ordering, FILTER, recursive CTEs, UPDATE ... FROM, RETURNING for UPDATE and DELETE).
- Give the hardest parts (the executor, and the parser with its expressions) to Claude Sonnet 5.5; give the self-contained
  storage/DML/DDL part to Codex (its medium model is enough); documentation to Claude Haiku 5.5. Use more than one kind of
  agent when they are installed. Nothing above Sonnet 5.5.
- Each worker edits only the files of its own part, and writes its own tests (`tests/test_<part>.py`).

## Talking to each other
- A worker that finds the interfaces need a change, or learns something another part depends on, posts it on the hive board
  before it changes any code, naming who it concerns. The queen passes it on and may change another worker's task because of it.
- Report a part as done when its own unit tests pass; the queen runs the whole check.

## Done means
- `python check_minidb3.py` passes all 219 checks (the 154 of versions 1 and 2 among them), and
  `python -m unittest discover -s tests` is green.

## What the queen answers by herself
- Permission to edit files and run tests or checks inside this folder: yes.
- Anything that deletes, installs, downloads or leaves this folder: no, and tell the worker to carry on without it.
