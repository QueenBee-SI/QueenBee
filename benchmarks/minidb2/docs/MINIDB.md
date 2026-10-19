# MiniDB: the specification of the finished engine

MiniDB is a small SQL database engine written in Python 3.11, **standard library only** (no pip installs). You build
it from this document. `minidb/ast.py` (the syntax tree), `minidb/errors.py` (`SQLError`), `minidb/result.py` (`Result`) and
`minidb/__init__.py` are finished; do not change them. Everything else is to be written.

The parts meet only through the interfaces written here, so they can be built in any order or side by side.
`python check_minidb.py` checks the whole engine through its public API (`python check_minidb.py N` runs check N,
`python check_minidb.py --list` lists them); it only passes when every part exists and the parts fit together.

## Public API (`minidb/__init__.py`)

```python
from minidb import Database, Result, SQLError

db = Database()                    # in memory;  Database("file.db") persists (section 10)
r = db.execute("SELECT 1 + 1")     # exactly one statement; a trailing ";" is allowed
r.columns   # list[str]            r.rows   # list[tuple]
r.rowcount  # rows changed by INSERT/UPDATE/DELETE, rows returned by SELECT
db.executescript("CREATE TABLE ...; INSERT ...;")   # many statements -> list[Result]
db.close()
```

`SQLError` (a subclass of `Exception`) is raised for every problem with the SQL or its data: syntax errors, unknown
tables or columns, ambiguous columns, constraint violations, type errors, division by zero, misuse of transactions.
The message says what was wrong. After an error nothing the statement did is kept.

## Module map (each is a separate file in `minidb/`)

| Module | Contents |
|---|---|
| `lexer.py` | `tokenize(sql) -> list[Token]`; `Token(kind, value, pos)` with kind in `KEYWORD IDENT NUMBER STRING OP PUNCT EOF` |
| `parser.py` | `parse(sql) -> list[Statement]` (statements split at `;`, empty ones skipped); builds `ast.py` nodes |
| `functions.py` | `call_scalar(name, args) -> value` for the scalar functions of section 4 |
| `expr.py` | `evaluate(node, resolve, subquery=None)`: evaluates an expression node. `resolve(table_or_None, column)` returns the value of a column in the current row; `subquery(select_node)` returns the list of row tuples of a nested select |
| `storage.py` | the stored data: `Storage`, `Table`, `Index` (section 12) |
| `executor.py` | `Executor(storage).execute(stmt) -> Result` runs one parsed statement (section 12) |
| `database.py` | `Database`: parses, runs, transactions, persistence |
| `cli.py`, `__main__.py` | the command line (section 11) |

Write unit tests for your own module in `tests/test_<module>.py` (`python -m unittest discover -s tests` must stay
green) and keep a short description of the architecture in `docs/ARCHITECTURE.md`.

## 1. Lexing

Keywords are case-insensitive. Identifiers are case-insensitive and are lowercased by the lexer (`Name` and `NAME`
are the same column). String literals use single quotes, `''` inside is a quote. Numbers: `12`, `3.5`; a leading
minus is the unary operator. Operators: `= <> != < <= > >= + - * / % ||`. Punctuation: `( ) , . ; *`. Comments
start with `--` and run to the end of the line. Anything else is an `SQLError` (raised by the lexer). Token values: a KEYWORD's is the uppercase word, an IDENT's the
lowercase name, a STRING's the text without quotes (`''` already turned into `'`), a NUMBER's a Python int or float,
an OP's or PUNCT's the characters, and the EOF token's is `""`. `!=` stays `!=` in the lexer.
Keywords: SELECT FROM WHERE GROUP BY HAVING ORDER ASC DESC LIMIT OFFSET DISTINCT AS JOIN INNER LEFT OUTER ON AND OR
NOT NULL IS IN BETWEEN LIKE TRUE FALSE CREATE TABLE DROP IF EXISTS INDEX UNIQUE PRIMARY KEY DEFAULT INSERT INTO VALUES
UPDATE SET DELETE BEGIN COMMIT ROLLBACK EXPLAIN.

## 2. Tables

`CREATE TABLE [IF NOT EXISTS] t (col TYPE [PRIMARY KEY] [NOT NULL] [UNIQUE] [DEFAULT literal], ...)`
Types: `INTEGER`, `REAL`, `TEXT`, `BOOLEAN`. `DROP TABLE [IF EXISTS] t`. A table that exists (or does not) without
the `IF` clause is an `SQLError`. A table has at most one `PRIMARY KEY` column; it is implicitly `NOT NULL` and
`UNIQUE`.

## 3. Inserting, with types and constraints

`INSERT INTO t [(c1, c2)] VALUES (...), (...)`. Without a column list the values are in table order; a wrong number of values is an `SQLError`. Columns not
mentioned get their `DEFAULT`, or NULL. Values are checked and converted:
INTEGER takes an int (a float with a fraction, or text, is an `SQLError`); REAL takes an int or float (stored as a
float); TEXT takes only strings; BOOLEAN takes TRUE/FALSE only. NULL is accepted by every type. Violations of
`NOT NULL`, `PRIMARY KEY` or `UNIQUE` are an `SQLError`. A multi-row INSERT in which one row fails inserts nothing.
`rowcount` is the number of rows inserted.

## 4. Expressions

Precedence, lowest first: `OR`; `AND`; `NOT`; comparisons (`= <> < <= > >=`), `IS [NOT] NULL`, `[NOT] IN`,
`[NOT] BETWEEN a AND b`, `[NOT] LIKE`; `||`; `+ -`; `* / %`; unary `-`. Parentheses group.

- NULL: any arithmetic or comparison involving NULL is NULL; `AND`/`OR`/`NOT` use three-valued logic
  (`NULL AND FALSE` is FALSE, `NULL OR TRUE` is TRUE); `WHERE` and `HAVING` keep only rows where the condition is
  TRUE. `x IN (...)` with no match but a NULL in the list is NULL.
- `/` on two integers is integer division truncating toward zero; if either side is a float it is a float division.
  `%` is the remainder with the sign of the left operand. Division or remainder by zero is an `SQLError`.
- `||` joins the text of both sides (NULL if either is NULL). `LIKE`: `%` any run, `_` one character, case-sensitive.
- Comparing a number with text, or doing arithmetic with text, is an `SQLError`. TRUE/FALSE are the booleans (`TRUE = 1` is not allowed: error).
- Scalar functions (`functions.py`): `UPPER(s)`, `LOWER(s)`, `LENGTH(s)`, `ABS(x)`, `ROUND(x)` and `ROUND(x, n)` (round half away from zero; the result is always a float: `ROUND(2.5)` is `3.0`, `ROUND(-2.5)` is `-3.0`, `ROUND(2.25, 1)` is `2.3`), `COALESCE(a, b, ...)`, `NULLIF(a, b)`, `SUBSTR(s, start)` and `SUBSTR(s, start, length)` (1-based).
  A function that gets NULL returns NULL (except `COALESCE`, `NULLIF`). Wrong argument count or unknown function:
  `SQLError`.

## 5. SELECT

`SELECT [DISTINCT] items [FROM t [alias]] [WHERE e] [GROUP BY ...] [HAVING e] [ORDER BY ...] [LIMIT n [OFFSET m]]`

- items: `*`, `t.*`, an expression with optional `[AS] alias`. Without FROM there is one row (`SELECT 1 + 1`).
- Column names of the result: the alias, else the column's own name for a plain column, else the expression text as
  written, lowercased and with single spaces (`SELECT salary * 2` has the column `salary * 2`).
- `ORDER BY`: expressions, a result alias, or a 1-based ordinal; `ASC` (default) / `DESC`; several keys; NULLs sort
  first in ascending order and last in descending order. The sort is stable. `LIMIT`/`OFFSET` apply after ordering.
- Without `ORDER BY` rows come in insertion order.
- `DISTINCT` removes duplicate result rows (keeping the first).
- Subqueries (not correlated): `x IN (SELECT ...)` (one column) and a scalar subquery `(SELECT max(x) FROM t)`,
  which must return one column and at most one row (no rows: NULL; more: `SQLError`).

## 6. Aggregates and grouping

`COUNT(*)`, `COUNT(x)`, `COUNT(DISTINCT x)`, `SUM`, `AVG`, `MIN`, `MAX`. They ignore NULLs. `COUNT` of nothing is 0;
`SUM`/`AVG`/`MIN`/`MAX` of nothing are NULL. `SUM` of integers is an integer, of floats a float; `AVG` is always a
float. With `GROUP BY` one row per group, in order of first appearance of the group; without it, an aggregate in the
select list makes the whole table one group (one row, even on an empty table). `HAVING` filters groups and may use
aggregates. A selected column that is neither grouped nor inside an aggregate is an `SQLError`. Aggregates are not
allowed in `WHERE`.

## 7. Joins

`FROM a [alias] [INNER] JOIN b [alias] ON cond [LEFT [OUTER] JOIN c ON cond ...]`. Any number of joins, applied left to
right. `LEFT JOIN` keeps every row of the left side and fills the right columns with NULL when nothing matches.
Qualified names (`e.name`) work everywhere; an unqualified name that exists in two joined tables is an `SQLError`
(ambiguous). `*` lists the columns of all tables in order; `t.*` those of one table (or alias).

## 8. UPDATE and DELETE

`UPDATE t SET c = expr [, ...] [WHERE e]` (the expressions see the old row); `DELETE FROM t [WHERE e]`.
`rowcount` is the number of rows changed. Types and constraints are enforced as for INSERT, and the statement is
atomic: if it fails for any row, no row is changed.

## 9. Transactions

`BEGIN`, `COMMIT`, `ROLLBACK`. Everything between BEGIN and ROLLBACK (data, tables created or dropped, indexes) is
undone by ROLLBACK. `BEGIN` inside a transaction, or `COMMIT`/`ROLLBACK` outside one, is an `SQLError`. Outside a
transaction every statement commits at once. An error inside a transaction does not end it.

## 10. Indexes, EXPLAIN and persistence

- `CREATE [UNIQUE] INDEX name ON t (col)`: one column. A UNIQUE index (and a PRIMARY KEY or UNIQUE column) rejects
  duplicate non-NULL values. Creating a unique index on data that already has duplicates is an `SQLError`. Index
  names are unique in the database.
- `EXPLAIN SELECT ...` returns one column `plan` with one row per table access: `SCAN t` normally, and
  `INDEX SCAN t USING name` when the table has an index on column `c` and the WHERE clause has, as a top-level
  `AND` conjunct, `c = <literal>` (either order, qualified or not). A PRIMARY KEY column has the automatic index
  `pk_<table>`; a UNIQUE column `uq_<table>_<column>`. For a join, the plan has one row per table in join order.
  Whether the executor really uses the index is shown by `check_minidb.py` through the number of rows it reads
  (`db.rows_read`, a counter that grows by one for each stored row the executor examines).
- `Database("file.db")` saves everything (tables, rows, indexes) as JSON after every committed change, and loads the
  file when it exists. A rolled-back change is never saved. `Database()` never touches the disk.

## 11. Command line

`python -m minidb [file.db]` reads SQL from standard input, runs it statement by statement and prints for each:

- A SELECT or EXPLAIN: a table, then a count line.
  ```
  id | name
  ---+-----
  1  | ann
  2  | bob
  (2 rows)
  ```
  Each column is as wide as its longest cell (header included); cells are left-aligned and padded with spaces, the
  columns joined by ` | ` (the last column is not padded); the line under the header is the same widths in `-`,
  joined by `-+-`. NULL is printed as `NULL`, booleans as `true` / `false`, floats as Python prints them. The count
  line is `(1 row)` or `(N rows)`.
- Any other statement: `OK, N rows affected` (`OK` when N is not meaningful: CREATE, DROP, BEGIN, COMMIT, ROLLBACK).
- An error: `Error: <message>`, and the program goes on with the next statement. The exit code is 0.

## 12. Interfaces between the parts

So that the parts can be built side by side, these are fixed.

**`storage.py`**

```python
class Index:                                   # one column of one table
    name: str; column: str; unique: bool

class Table:
    name: str
    columns: list[ColumnDef]                   # from ast.py, in table order
    indexes: dict[str, Index]                  # by index name; includes the automatic pk_/uq_ ones
    def column_position(self, name) -> int     # SQLError if there is no such column
    def index_on(self, column) -> Index | None
    def insert_row(self, values: tuple) -> int         # full row in table order (defaults already applied by the
                                                       # caller); coerces types (section 3), enforces NOT NULL and
                                                       # uniqueness; SQLError changes nothing; returns the row id
    def update_row(self, rowid, values: tuple) -> None # same checks, same atomicity
    def delete_row(self, rowid) -> None
    def scan(self) -> Iterator[tuple[int, tuple]]      # (rowid, values) in insertion order
    def lookup(self, index_name, value) -> list[tuple[int, tuple]]   # rows whose indexed column equals value

class Storage:
    rows_read: int                             # +1 for every row scan() or lookup() hands out
    tables: dict[str, Table]
    def create_table(self, stmt: CreateTable) -> None  # SQLError if it exists (unless if_not_exists)
    def drop_table(self, name, if_exists=False) -> None
    def table(self, name) -> Table                     # SQLError if unknown
    def create_index(self, stmt: CreateIndex) -> None  # builds it from the rows, SQLError on duplicates / bad names
    def snapshot(self)                                 # an opaque copy of everything (used for transactions and to
                                                       # make every statement atomic)
    def restore(self, snap) -> None
    def dump(self) -> dict                             # JSON-able
    @classmethod
    def load(cls, data: dict) -> "Storage"
```

**`executor.py`**: `Executor(storage)`, `execute(stmt) -> Result` for CreateTable, DropTable, CreateIndex, Insert,
Select, Explain, Update, Delete. It uses `expr.evaluate` for expressions and `Storage` for data; it handles the
aggregate functions itself (they are not in `functions.py`), the joins, grouping, ordering and subqueries, and chooses
index lookups (section 10). It does not handle Begin/Commit/Rollback.

**`database.py`**: `Database(path=None)`. For each statement: parse, snapshot, execute; on `SQLError` restore the
snapshot and re-raise (so every statement is atomic); Begin/Commit/Rollback are handled here; after a committed change
the storage is saved to `path` as JSON. `rows_read` is a property returning `storage.rows_read`. `Result` and
`SQLError` come from `result.py` and `errors.py`.
