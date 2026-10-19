# Task: MiniDB 3, nine more features

MiniDB (see `docs/MINIDB.md` for version 1 and `docs/TASK2.md` for the nine features of version 2; `docs/ARCHITECTURE.md`
says how the modules fit) is finished for version 2: `python check_minidb.py` passes all 154 checks. It is to get the
features below. **The 154 existing checks must keep passing**, and `python check_minidb3.py` runs them together with the
checks of this work package (`python check_minidb3.py N` runs one, `--list` lists them).

Unlike version 2, the syntax tree is *not* prepared: `minidb/ast.py` needs new nodes and fields for this work, and the
parser's owner adds them (every new field with a default, so the existing code keeps working). `minidb/errors.py` and
`minidb/result.py` stay as they are.

Where this document says nothing, behave as **SQLite** does (the query checks were validated against it), except where
`docs/MINIDB.md` differs from SQLite (case-sensitive LIKE, strict types, SQLError instead of silent conversions).

## 21. NULLS FIRST and NULLS LAST

`ORDER BY e [ASC | DESC] [NULLS FIRST | NULLS LAST]` on every key of a query's ORDER BY, of a set operation's ORDER BY,
and of a window's ORDER BY. Without the clause the order of NULLs stays as it is now (section 5 of the spec: NULLs
sort first, which `DESC` reverses). `NULLS FIRST` puts the NULL rows before the others and `NULLS LAST` after them,
whatever the direction; among themselves the NULL rows keep their relative order.

## 22. More aggregates

- `GROUP_CONCAT(x)` and `GROUP_CONCAT(x, sep)`: the non-NULL values of `x`, each as `CAST(x AS TEXT)`, joined by `sep`
  (default `','`), in the order the rows are seen; NULL when there is no non-NULL value. `GROUP_CONCAT(DISTINCT x)`
  joins each distinct value once (then no `sep` may be given: `SQLError`). Also as a window function.
- `agg(...) FILTER (WHERE cond)` after any aggregate (`COUNT`, `SUM`, `AVG`, `MIN`, `MAX`, `GROUP_CONCAT`, in a plain,
  grouped or windowed query): only the rows for which `cond` is true are counted. `FILTER` on a scalar function is an
  `SQLError`.

## 23. New scalar functions

`MOD(a, b)` (like `%`), `POWER(a, b)` (always REAL; `POWER(2, 3)` is `8.0`), `SIGN(x)` (`-1`, `0` or `1` as INTEGER),
`CEIL(x)` and `FLOOR(x)` (INTEGER results; `CEIL(2)` is `2`), `REPEAT(s, n)` (`n` times `s`; `n` below 1 gives `''`),
`REVERSE(s)`, `LPAD(s, n, p)` and `RPAD(s, n, p)` (pad `s` with `p` to length `n` on the left or right; a string
already `n` or longer is cut to `n`), `CONCAT_WS(sep, a, b, ...)` (joins the non-NULL arguments, each as TEXT, with
`sep`; NULL when `sep` is NULL; `''` when every other argument is NULL). For the others: NULL in, NULL out. Wrong argument
types (`REPEAT(1, 'x')`, `CEIL('a')`) are an `SQLError`.

## 24. CHECK constraints and composite UNIQUE

- `col TYPE ... CHECK (expr)` in a column definition and `CHECK (expr)` as a table constraint (after the columns, any
  number, comma-separated, also together with foreign keys and the other constraints). `expr` sees the row being
  inserted or updated by plain column names; it may use functions and CASE but no subqueries. The row is refused with an
  `SQLError` when `expr` is false; NULL counts as passing (as in SQLite). Checked on INSERT (also INSERT ... SELECT and
  upserts) and on UPDATE, atomically with the other constraints.
- `UNIQUE (a, b [, ...])` as a table constraint: no two rows may have the same combination (rows with a NULL in any of
  the columns never conflict). It persists, follows RENAME COLUMN, and `DROP COLUMN` of one of its columns is an
  `SQLError`. `ON CONFLICT DO NOTHING` skips a row that would break it (DO UPDATE with a composite target is not
  required).
- Both survive `dump`/`load` and transactions; `ALTER TABLE ADD COLUMN` accepts a column CHECK, which the existing rows
  must pass (else `SQLError` and nothing changes).

## 25. RETURNING

`INSERT ... RETURNING items`, `UPDATE ... RETURNING items`, `DELETE ... RETURNING items`, where `items` is a select
list (`*`, expressions over the row's columns, aliases). The `Result` has those columns and one row per row inserted,
updated (its new values) or deleted (its old values), in the order the rows were touched; `rowcount` stays the number
of rows changed. With `ON CONFLICT DO NOTHING` the skipped rows do not appear; with `DO UPDATE` the updated row does.
Without RETURNING nothing changes: the result has no columns and no rows, as today.

## 26. Savepoints

`SAVEPOINT name`, `RELEASE [SAVEPOINT] name`, `ROLLBACK TO [SAVEPOINT] name`, inside a transaction (any of them
outside BEGIN ... COMMIT is an `SQLError`). Savepoints nest: `ROLLBACK TO s` undoes everything done since `SAVEPOINT s`
(data and schema), keeps the transaction open and keeps `s` itself (so it can be rolled back to again); `RELEASE s`
forgets `s` and every savepoint set after it, keeping the changes. A name that is not set is an `SQLError`; the same
name set twice refers to the newest. `COMMIT` keeps everything and forgets all savepoints; `ROLLBACK` undoes everything.

## 27. CREATE TABLE ... AS

`CREATE TABLE [IF NOT EXISTS] t AS select` (any query, with a WITH, set operations, ORDER BY and LIMIT allowed): the
table gets the query's column names, a type per column from its first non-NULL value (INTEGER, REAL, TEXT or BOOLEAN;
TEXT when every value is NULL), no primary key and no constraints, and the query's rows in its order. A name already
used by a table or view is an `SQLError` (nothing when `IF NOT EXISTS`); duplicate column names in the query are an
`SQLError`. The table persists and takes part in transactions like any other.

## 28. Recursive CTEs

`WITH RECURSIVE name [(cols)] AS (base UNION [ALL] step) select`: `base` is run once; `step` refers to `name` (once, in
its FROM) and is run again and again on the rows the previous round produced, until a round produces no row. `UNION ALL`
keeps every row; `UNION` drops a row already produced (and a round that produces only such rows ends it). The rows come
in the order they were produced. `(cols)` names the columns; without it the base query's names are used. The step may
have WHERE, joins and expressions; it may not use aggregates or ORDER BY (an `SQLError`). More than 10000 rows in all
is an `SQLError` ("recursion too deep"). Ordinary (non-recursive) CTEs may be written with `RECURSIVE` too.

## 29. UPDATE ... FROM

`UPDATE t SET col = expr [, ...] FROM source [JOIN ...] [WHERE cond]`: `source` is a table (with alias), a derived
table or a join; the assignments and `cond` may use the columns of `t` and of the source (qualified when ambiguous). A
row of `t` is updated once, with the first matching source row in the source's scan order; rows of `t` that match no
source row are not touched. `RETURNING` works here too and sees the new values of `t`. The target table may not appear
again in `source` (an `SQLError`).

## 30. Who does what

Three parts of about the same size:

1. **Frontend:** `lexer.py` (new keywords), `parser.py`, `parser_base.py` (all the new syntax), **`ast.py`** (the new
   nodes and fields; post their shapes on the hive board before the others need them), `expr.py` and `functions.py`
   (section 23; the FILTER and GROUP_CONCAT parsing of 22), `windows.py` (NULLS FIRST/LAST and GROUP_CONCAT in windows).
2. **Storage and DML:** `storage.py` (section 24, persistence, the savepoint snapshots), `dml.py` (CHECK and composite
   UNIQUE on insert, upsert and INSERT ... SELECT; section 25 for INSERT; section 27), `database.py` (section 26).
3. **Executor, select side:** `executor.py`, `plan.py` (sections 21, 22 in queries, 28, 29; section 25 for UPDATE and
   DELETE), and `docs/ARCHITECTURE.md`.

Each worker edits only its own files and writes its own `tests/test_<part>_v3.py`. The interfaces between the parts
are what `docs/ARCHITECTURE.md` and `.queenbee/FINDINGS.md` say they are now; if one must change, post it on the hive
board naming who it concerns, before changing code.
