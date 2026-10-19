# MiniDB architecture

Pipeline: SQL text -> `lexer.tokenize` -> `parser.parse` (ast nodes) -> `Database` -> `Executor` -> `Storage`.

- `lexer.py`, `parser.py` (+ `parser_base.py`, the token helpers): recursive descent. The parser attaches the source
  text of each select item as `item.text` (used to name unaliased expression columns).
- `values.py`: SQL typing rules shared by the rest (`compare` raises SQLError on mixed types, `hash_key`).
- `functions.py`: scalar functions. `expr.py`: `evaluate`, three-valued logic, arithmetic, LIKE, IN, subquery hooks.
  Aggregates are rejected there; the executor computes them and substitutes literals.
- `storage.py`: `Storage` -> `Table` (rows in an insertion-ordered dict by rowid) -> `Index` (value -> rowids; NULLs not
  indexed). `Table.update_rows` judges uniqueness on the final state and undoes itself on failure. `snapshot`/`restore`
  clone tables; `dump`/`load` give the JSON form.
- `plan.py`: tree walking/rewriting and `Scope` (column -> position in the combined join row, ambiguity checks).
  Every column in a statement is qualified with its table alias before evaluation.
- `executor.py`: `_analyze` resolves names, expands `*`, resolves ORDER BY aliases/ordinals and checks grouping;
  `_select` runs scan/index lookup, nested-loop joins, WHERE, grouping, projection, DISTINCT, ORDER BY, LIMIT.
  An index is used when a top-level AND conjunct of WHERE is `column = literal` (also for joined tables; WHERE is
  still applied afterwards, so results are the same). `EXPLAIN` uses the same `_plan`.
- `database.py`: one snapshot per mutating statement (restored on error), BEGIN/COMMIT/ROLLBACK, JSON save after each
  committed change (write to a temp file, then replace).
- `cli.py`: splits stdin at top-level semicolons, prints tables, `OK` lines and `Error:` lines.

Choices where the task left room: `||` renders booleans as `true`/`false`; AND/OR/NOT need booleans or NULL; a table
alias hides the table name; `OFFSET` is only accepted after `LIMIT`; `*` is lexed as an OP token.
