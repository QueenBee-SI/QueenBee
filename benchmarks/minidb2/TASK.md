# Task: MiniDB 2, nine new features

MiniDB (see `docs/MINIDB.md`, finished, 90 checks pass) is to get the features below. `minidb/ast.py` has already been
extended with the new syntax nodes (read it; do not change it): every new field has a default, so the existing code
keeps working. **The 90 existing checks must keep passing**, and `python check_minidb.py` now also runs the checks of
this work package (`python check_minidb.py N` runs one, `--list` lists them).

Where this document says nothing, behave as **SQLite** does (the new query checks were validated against it), except
where `docs/MINIDB.md` differs from SQLite (case-sensitive LIKE, strict types, SQLError instead of silent conversions).

## 13. New expressions and functions

- `CASE WHEN c1 THEN r1 [WHEN c2 THEN r2 ...] [ELSE r] END` and `CASE x WHEN v1 THEN r1 ... END`. No ELSE and no
  match: NULL. A condition that is NULL is not true.
- `CAST(x AS INTEGER | REAL | TEXT | BOOLEAN)`. NULL stays NULL. REAL to INTEGER truncates toward zero; numeric text
  (`'12'`, `'2.5'`) converts to a number; text that is not a number, or a REAL/TEXT that cannot be a BOOLEAN, is an
  `SQLError`. Number to TEXT is the number as Python prints it (`5` is `'5'`, `2.0` is `'2.0'`). INTEGER to BOOLEAN: 0
  is FALSE, anything else TRUE.
- New scalar functions: `REPLACE(s, a, b)`, `TRIM(s)`, `LTRIM(s)`, `RTRIM(s)` (spaces), `INSTR(s, t)` (1-based
  position, 0 when absent), `IIF(c, a, b)`. NULL in gives NULL out (except `IIF`, which looks at `c` being true).

## 14. Subqueries, derived tables, CTEs, views

- `EXISTS (SELECT ...)` and `NOT EXISTS (...)`.
- Subqueries may be **correlated**: inside a subquery a column name that is not found among the subquery's own tables is
  looked up in the enclosing query (any depth); names found in the subquery win. This works in WHERE, in the select list
  and in HAVING, for `EXISTS`, `IN (SELECT ...)` and scalar subqueries.
- A derived table: `FROM (SELECT ...) alias` (the alias is required), usable in joins like any table.
- `WITH name AS (select) [, name2 AS (select) ...] select`: non-recursive CTEs; a later CTE may use an earlier one; a CTE
  hides a table or view of the same name. Also allowed in front of a set operation (below) and inside subqueries.
- `CREATE VIEW v AS select` and `DROP VIEW [IF EXISTS] v`. A view is read-only (INSERT/UPDATE/DELETE on it is an
  `SQLError`), shows the current data, may use other views, and a name already used by a table or view is an `SQLError`.
  Views belong to the transactions and to persistence like tables do.

## 15. Set operations

`select UNION [ALL] select`, `INTERSECT`, `EXCEPT`, any number, left to right. All sides need the same number of columns
(`SQLError` otherwise); the column names are those of the first select. `UNION`, `INTERSECT`, `EXCEPT` remove duplicate
rows. Without `ORDER BY` the rows come: `UNION ALL`: left then right; `UNION`: the same with later duplicates dropped;
`INTERSECT`: the left rows that also occur on the right, once each; `EXCEPT`: the left rows that do not occur on the
right, once each. `ORDER BY` (a column name of the first select, or an ordinal), `LIMIT` and `OFFSET` written after the
last select apply to the whole result. NULLs are equal to each other here.

## 16. More joins

`RIGHT [OUTER] JOIN` (every right row kept, left columns NULL when nothing matches) and `CROSS JOIN` (no ON).

## 17. Window functions

`func(...) OVER ([PARTITION BY e1, e2 ...] [ORDER BY ...])` in the select list and in ORDER BY (anywhere else:
`SQLError`). Functions: `ROW_NUMBER()`, `RANK()`, `DENSE_RANK()`, `LAG(x [, offset [, default]])`,
`LEAD(x [, offset [, default]])` (offset defaults to 1, default to NULL; these four need an ORDER BY in the window or
the result order of the partition is the row order), and `SUM`, `COUNT`, `AVG`, `MIN`, `MAX` over a window.
Windows are computed after WHERE, GROUP BY and HAVING (so `RANK() OVER (ORDER BY SUM(x) DESC)` works in a grouped
query); the query's own ORDER BY, DISTINCT and LIMIT apply afterwards. Rows with equal values in the window's ORDER BY
are peers: `RANK` gives peers the same rank (gaps follow), `DENSE_RANK` the same rank (no gaps), `ROW_NUMBER` numbers
them in row order. An aggregate over a window with ORDER BY covers the partition from its first row up to the last
peer of the current row (a running total); without ORDER BY it covers the whole partition. NULLs form one partition and
sort as in section 5.

## 18. More INSERT

- `INSERT INTO t [(cols)] SELECT ...` (or a set operation): inserts the rows; `rowcount` is how many.
- `INSERT ... VALUES ... ON CONFLICT [(col)] DO NOTHING` skips a row that would break a UNIQUE or PRIMARY KEY rule;
  `... ON CONFLICT (col) DO UPDATE SET c = expr [, ...]` updates the row that is in the way instead (the expressions see
  the stored row by plain column names and the row that was not inserted as `excluded.c`). `DO UPDATE` needs the target
  column (`SQLError` without it). `rowcount` counts rows inserted or updated, not skipped ones.

## 19. Schema changes and foreign keys

- `ALTER TABLE t ADD COLUMN coldef`: existing rows get the column's DEFAULT or NULL; a NOT NULL column without a
  DEFAULT on a table that has rows, and a PRIMARY KEY or UNIQUE column, are an `SQLError`.
  `ALTER TABLE t RENAME TO u`, `ALTER TABLE t RENAME COLUMN a TO b`, `ALTER TABLE t DROP COLUMN c` (an `SQLError` for the
  primary key, a column with an index, a column that a foreign key uses, or the last column). Indexes follow a renamed
  table or column; views are not rewritten (a view that used the old name fails when it is read, with an `SQLError`).
- Foreign keys: `col TYPE REFERENCES parent(pcol) [ON DELETE CASCADE | RESTRICT | SET NULL]` in a column definition.
  `pcol` must be the parent's PRIMARY KEY or a UNIQUE column, else `CREATE TABLE` is an `SQLError`. Inserting or
  updating a child row whose (non-NULL) value is not in the parent is an `SQLError`. Deleting a parent row, or changing
  its referenced value, while children exist: RESTRICT (the default) is an `SQLError`; CASCADE deletes the children
  (and their children, recursively); SET NULL sets the child column to NULL. `DROP TABLE` of a table that other tables
  reference is an `SQLError`. All of it is atomic, rolls back with transactions, and persists.

## 20. Who does what

`parser.py` (all the new syntax), `lexer.py` (new keywords), `expr.py`/`functions.py` (sections 13 and the scalar parts of
14), a new `windows.py` with `compute_window(func: ast.Func, rows: list, eval_expr) -> list`, which returns the window
function's value for every element of `rows` (aligned with it; `eval_expr(node, row)` evaluates an expression for one
row; the rows arrive in the order the query would return them), `storage.py` (sections 19 and persistence), `executor.py`
(the select side: sections 14 to 17, including calling `compute_window`) and a new `dml.py` (section 18 and the
DDL of 19 and the views, which `executor.execute` hands over to). Update `docs/ARCHITECTURE.md` and add tests to `tests/`.
