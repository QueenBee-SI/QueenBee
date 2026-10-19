# MiniDB 2: the brief every worker reads first

MiniDB is a small SQL engine in Python 3.11, standard library only, in `minidb/`. It is finished for the first spec
(`docs/MINIDB.md`, 90 checks). TASK.md adds nine features (sections 13 to 19; 154 checks in all). You own one part; the
parts meet through the interfaces below, which are fixed. Do not change `minidb/ast.py`, `errors.py` or `result.py`.

## Pipeline and modules

SQL text -> `lexer.tokenize` -> `parser.parse` (nodes from `ast.py`) -> `Database` -> `Executor` -> `Storage`.

| Module | What it is |
|---|---|
| `lexer.py` | `tokenize(sql) -> list[Token]`, `Token(kind, value, pos)`, kinds KEYWORD IDENT NUMBER STRING OP PUNCT EOF |
| `parser.py`, `parser_base.py` | recursive descent, `parse(sql) -> list[Statement]`; attaches `item.text` to select items |
| `values.py` | typing rules shared by all (`compare` raises SQLError on mixed types, `hash_key`) |
| `functions.py` | `call_scalar(name, args) -> value` |
| `expr.py` | `evaluate(node, resolve, subquery=None)`; three-valued logic; aggregates are the executor's, not here |
| `storage.py` | `Storage` -> `Table` (insertion-ordered rows by rowid) -> `Index`; `snapshot`/`restore`; `dump`/`load` |
| `plan.py` | tree walking and `Scope` (column -> position in the joined row); every column is qualified before evaluation |
| `executor.py` | `Executor(storage).execute(stmt) -> Result`: `_analyze` resolves names, `_select` scans, joins, groups, orders |
| `database.py` | one snapshot per mutating statement, BEGIN/COMMIT/ROLLBACK, JSON save after each commit |
| `cli.py` | splits stdin at top-level `;`, prints tables, `OK` and `Error:` lines |

## Who owns what (TASK.md section 20)

1. **Frontend:** `lexer.py` (new keywords), `parser.py`, `parser_base.py` (all new syntax), `expr.py`, `functions.py`
   (section 13 and the scalar parts of 14), and a new `windows.py` with
   `compute_window(func: ast.Func, rows: list, eval_expr) -> list`: one value per element of `rows`, aligned with it;
   `eval_expr(node, row)` evaluates an expression for one row; rows arrive in the order the query returns them.
2. **Storage and DML:** `storage.py` (section 19 and persistence) and a new `dml.py` (section 18, the DDL of 19, the
   views). `executor.execute` hands INSERT ... SELECT, upserts, ALTER TABLE, CREATE/DROP VIEW and foreign keys to `dml.py`;
   `dml.py` uses `storage.py` and calls back into the executor for any SELECT source.
3. **Executor, select side:** `executor.py`, `plan.py` (sections 14 to 17: correlated subqueries, derived tables, CTEs,
   views when read, set operations, RIGHT and CROSS JOIN, window functions by calling `compute_window`), and
   `docs/ARCHITECTURE.md`.

Each worker edits only its own files and writes its own `tests/test_<part>.py`. The interfaces above are the contract:
if yours must change, post it on the hive board naming who it concerns, before changing code.

## Facts that save you reading

- A derived table without an alias is accepted (check 109 needs it): `TableRef.alias` is None and `name` is "".
- The parser stores a WITH on the outer node (`Select.ctes` or `Compound.ctes`); a set operation's trailing ORDER BY, LIMIT
  and OFFSET sit on the `Compound` node.
- `Storage.snapshot()` returns `(tables, views)` in version 2; the JSON file is version 2 with views as SQL text and foreign
  keys in the column definitions; version 1 files still load.
- `expr.evaluate` must handle `ast.Exists` through the subquery runner (`runner(node.select)` returns the row list) and
  `ast.Case`/`ast.Cast`; ON CONFLICT DO UPDATE without a target is not rejected by the parser, `dml.py` must raise SQLError.
- Every user error is a `SQLError` from `minidb.errors`; match the surrounding code's style and comment density.

## How to check your part, and the whole

- Your part: `python -m unittest tests.test_<part>`; the whole: `python -m unittest discover -s tests` and
  `python check_minidb.py` (prints `N of 154 checks pass`). The queen runs the whole check when every worker is in.
- Read only what you must: your files, the sections of TASK.md for your part, and `docs/MINIDB.md` section 12. Do not
  read the whole project to find out what the others do; this brief says it.
