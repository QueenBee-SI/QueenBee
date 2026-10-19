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
- `windows.py`: `compute_window(func, rows, eval_expr)` returns the value of one window function for every row
  (ROW_NUMBER, RANK, DENSE_RANK, LAG, LEAD, and SUM/COUNT/AVG/MIN/MAX over a window). The executor calls it after
  WHERE/GROUP BY/HAVING and substitutes the results as literals before ORDER BY, DISTINCT and LIMIT. Windows in
  WHERE, GROUP BY, HAVING, ON or UPDATE/DELETE are an `SQLError`.
- `dml.py`: INSERT (VALUES, INSERT ... SELECT, ON CONFLICT), ALTER TABLE, CREATE VIEW and DROP VIEW.
  `Executor.execute` hands these statements to `dml.execute(storage, stmt, select, subquery)`; views live in
  `storage.views` (name -> query tree) and follow snapshots and persistence like tables.
- `plan.py`: tree walking/rewriting and `Scope` (column -> position in the combined join row, ambiguity checks).
  Every column in a statement is qualified with its table alias before evaluation.
- `executor.py` (select side, v2): `_query` runs a Select or Compound, first materialising its `WITH` CTEs (a CTE hides
  a table or view of the same name; later CTEs see earlier ones). `_source` turns a FROM item into a table-like object:
  derived table, CTE or view (a `plan.Relation` of precomputed rows) or a real table. Set operations
  (UNION [ALL]/INTERSECT/EXCEPT, NULLs equal) combine whole results; ORDER BY/LIMIT/OFFSET apply to the combined rows.
  Joins: INNER, LEFT, RIGHT (unmatched right rows come last) and CROSS. Correlated subqueries: `Scope` has a
  `parent`; a column not found in the own tables resolves in the enclosing scopes as `Column(name, '#level:alias')`,
  read at run time from the frame stack of the running queries. Uncorrelated subquery results are cached for one
  statement; correlated ones are re-run per outer row. Original text of v1 follows.
- `executor.py` (v1): `_analyze` resolves names, expands `*`, resolves ORDER BY aliases/ordinals and checks grouping;
  `_select` runs scan/index lookup, nested-loop joins, WHERE, grouping, projection, DISTINCT, ORDER BY, LIMIT.
  An index is used when a top-level AND conjunct of WHERE is `column = literal` (also for joined tables; WHERE is
  still applied afterwards, so results are the same). `EXPLAIN` uses the same `_plan`.
- `database.py`: one snapshot per mutating statement (restored on error), BEGIN/COMMIT/ROLLBACK, JSON save after each
  committed change (write to a temp file, then replace).
- `cli.py`: splits stdin at top-level semicolons, prints tables, `OK` lines and `Error:` lines.

Choices where the task left room: `||` renders booleans as `true`/`false`; AND/OR/NOT need booleans or NULL; a table
alias hides the table name; `OFFSET` is only accepted after `LIMIT`; `*` is lexed as an OP token.
