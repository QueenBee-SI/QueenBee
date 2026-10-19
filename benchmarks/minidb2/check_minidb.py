"""Checks MiniDB (versions 1 and 2) through its public API: `python check_minidb.py` runs all, `python check_minidb.py N` one,
`python check_minidb.py --list` lists them. Prints one line per check and a last line "N of M checks pass".
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SETUP = """
CREATE TABLE emp (id INTEGER PRIMARY KEY, name TEXT NOT NULL, dept TEXT, salary REAL, boss INTEGER);
INSERT INTO emp VALUES (1,'ann','eng',100.0,NULL),(2,'bob','eng',80.0,1),(3,'cat','ops',70.0,1),
  (4,'dan','ops',NULL,3),(5,'eve','hr',60.0,1),(6,'fay','eng',80.0,2);
CREATE TABLE dept (code TEXT PRIMARY KEY, title TEXT);
INSERT INTO dept VALUES ('eng','Engineering'),('ops','Operations'),('fin','Finance');
"""

# (sql, expected rows). With ORDER BY the order must match exactly; without it, rows are compared as a multiset,
# except for the group-order check below.
QUERIES = [
    ("SELECT 1 + 2 * 3", [(7,)]),
    ("SELECT 7 / 2, -7 / 2, 7 % 3, -7 % 3", [(3, -3, 1, -1)]),
    ("SELECT 7 / 2.0", [(3.5,)]),
    ("SELECT 'a' || 'b' || 'c'", [("abc",)]),
    ("SELECT NULL + 1, NULL = NULL", [(None, None)]),
    ("SELECT NULL AND 1 = 2, NULL OR 1 = 1", [(False, True)]),
    ("SELECT name FROM emp WHERE dept = 'eng' ORDER BY name DESC", [("fay",), ("bob",), ("ann",)]),
    ("SELECT * FROM emp WHERE id = 1", [(1, "ann", "eng", 100.0, None)]),
    ("SELECT name FROM emp ORDER BY salary, name", [("dan",), ("eve",), ("cat",), ("bob",), ("fay",), ("ann",)]),
    ("SELECT name FROM emp ORDER BY salary DESC, name", [("ann",), ("bob",), ("fay",), ("cat",), ("eve",), ("dan",)]),
    ("SELECT id FROM emp ORDER BY id LIMIT 2 OFFSET 1", [(2,), (3,)]),
    ("SELECT DISTINCT dept FROM emp ORDER BY dept", [("eng",), ("hr",), ("ops",)]),
    ("SELECT name, salary FROM emp WHERE salary IS NOT NULL ORDER BY 2 DESC, 1",
     [("ann", 100.0), ("bob", 80.0), ("fay", 80.0), ("cat", 70.0), ("eve", 60.0)]),
    ("SELECT salary * 2 AS dbl FROM emp WHERE salary > 70 ORDER BY dbl", [(160.0,), (160.0,), (200.0,)]),
    ("SELECT name FROM emp WHERE salary BETWEEN 70 AND 80 ORDER BY id", [("bob",), ("cat",), ("fay",)]),
    ("SELECT name FROM emp WHERE dept IN ('hr', 'ops') ORDER BY id", [("cat",), ("dan",), ("eve",)]),
    ("SELECT name FROM emp WHERE name LIKE '_a%' ORDER BY id", [("cat",), ("dan",), ("fay",)]),
    ("SELECT name FROM emp WHERE dept NOT IN ('eng', NULL) ORDER BY id", []),
    ("SELECT name FROM emp WHERE boss IS NULL", [("ann",)]),
    ("SELECT name FROM emp WHERE NOT dept = 'eng' ORDER BY id", [("cat",), ("dan",), ("eve",)]),
    ("SELECT -salary, ABS(-3), ROUND(2.5), ROUND(-2.5), ROUND(2.25, 1) FROM emp WHERE id = 1",
     [(-100.0, 3, 3.0, -3.0, 2.3)]),
    ("SELECT UPPER('ab'), LOWER('AB'), LENGTH('hello'), SUBSTR('hello', 2, 3), SUBSTR('hello', 4)",
     [("AB", "ab", 5, "ell", "lo")]),
    ("SELECT COALESCE(NULL, NULL, 3), NULLIF(1, 1), NULLIF(1, 2)", [(3, None, 1)]),
    ("SELECT UPPER(NULL), LENGTH(NULL)", [(None, None)]),
    ("SELECT COUNT(*), COUNT(salary), SUM(salary), AVG(salary), MIN(salary), MAX(salary) FROM emp",
     [(6, 5, 390.0, 78.0, 60.0, 100.0)]),
    ("SELECT COUNT(*), SUM(salary), MAX(salary) FROM emp WHERE id > 100", [(0, None, None)]),
    ("SELECT dept, COUNT(*), SUM(salary) FROM emp GROUP BY dept ORDER BY dept",
     [("eng", 3, 260.0), ("hr", 1, 60.0), ("ops", 2, 70.0)]),
    ("SELECT dept FROM emp GROUP BY dept HAVING COUNT(*) > 1 ORDER BY dept", [("eng",), ("ops",)]),
    ("SELECT COUNT(DISTINCT dept), COUNT(DISTINCT boss) FROM emp", [(3, 3)]),
    ("SELECT dept, COUNT(*) AS n FROM emp WHERE salary IS NOT NULL GROUP BY dept ORDER BY n DESC, dept",
     [("eng", 3), ("hr", 1), ("ops", 1)]),
    ("SELECT SUM(id), AVG(id) FROM emp", [(21, 3.5)]),
    ("SELECT e.name, d.title FROM emp e JOIN dept d ON e.dept = d.code ORDER BY e.id",
     [("ann", "Engineering"), ("bob", "Engineering"), ("cat", "Operations"), ("dan", "Operations"),
      ("fay", "Engineering")]),
    ("SELECT e.name, d.title FROM emp e LEFT JOIN dept d ON e.dept = d.code WHERE e.id >= 4 ORDER BY e.id",
     [("dan", "Operations"), ("eve", None), ("fay", "Engineering")]),
    ("SELECT d.code, COUNT(e.id) FROM dept d LEFT JOIN emp e ON e.dept = d.code GROUP BY d.code ORDER BY d.code",
     [("eng", 3), ("fin", 0), ("ops", 2)]),
    ("SELECT e.name, b.name FROM emp e JOIN emp b ON e.boss = b.id ORDER BY e.id",
     [("bob", "ann"), ("cat", "ann"), ("dan", "cat"), ("eve", "ann"), ("fay", "bob")]),
    ("SELECT e.name, b.name, d.title FROM emp e JOIN emp b ON e.boss = b.id JOIN dept d ON b.dept = d.code "
     "ORDER BY e.id",
     [("bob", "ann", "Engineering"), ("cat", "ann", "Engineering"), ("dan", "cat", "Operations"),
      ("eve", "ann", "Engineering"), ("fay", "bob", "Engineering")]),
    ("SELECT d.* FROM dept d WHERE d.code = 'fin'", [("fin", "Finance")]),
    ("SELECT name FROM emp WHERE dept IN (SELECT code FROM dept WHERE title LIKE 'O%') ORDER BY id",
     [("cat",), ("dan",)]),
    ("SELECT name FROM emp WHERE salary = (SELECT MAX(salary) FROM emp)", [("ann",)]),
    ("SELECT (SELECT COUNT(*) FROM dept) + 1", [(4,)]),
    ("SELECT TRUE, FALSE, NOT TRUE", [(True, False, False)]),
    ("SELECT 1 -- one\n + 1", [(2,)]),
    ("SELECT 'it''s'", [("it's",)]),
    ("SELECT 2 = 2.0, 1 < 1.5", [(True, True)]),
    ("SELECT id FROM emp WHERE salary * 2 > 150 ORDER BY id", [(1,), (2,), (6,)]),
    ("SELECT name, salary FROM emp ORDER BY dept DESC, salary DESC, name",
     [("cat", 70.0), ("dan", None), ("eve", 60.0), ("ann", 100.0), ("bob", 80.0), ("fay", 80.0)]),
    ("SELECT dept, MAX(salary) - MIN(salary) FROM emp GROUP BY dept HAVING MAX(salary) IS NOT NULL ORDER BY dept",
     [("eng", 20.0), ("hr", 0.0), ("ops", 0.0)]),
    ("SELECT SUM(salary) / COUNT(*) FROM emp", [(65.0,)]),
    ("SELECT e.dept, d.title FROM emp e JOIN dept d ON e.dept = d.code GROUP BY e.dept, d.title ORDER BY e.dept",
     [("eng", "Engineering"), ("ops", "Operations")]),
]


def seeded():
    from minidb import Database
    db = Database()
    db.executescript(SETUP)
    return db


def norm(rows):
    return sorted(repr(tuple(r)) for r in rows)


def exact(rows, want):
    got = [tuple(r) for r in rows]
    assert got == [tuple(w) for w in want], f"got {got!r}, want {want!r}"
    for g, w in zip(got, want):          # 2 must not come back where 2.0 is expected, and the other way round
        for a, b in zip(g, w):
            assert type(a) is type(b), f"got {g!r}, want {w!r} (types differ)"


def make_query_check(sql, want):
    def run():
        rows = seeded().execute(sql).rows
        if "ORDER BY" in sql.upper():
            exact(rows, want)
        else:
            assert norm(rows) == norm(want), f"got {rows!r}, want {want!r}"
            exact(sorted(rows, key=repr), sorted(want, key=repr))
    return run


CHECKS: list[tuple[str, object]] = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


for _sql, _want in QUERIES:
    CHECKS.append(("query: " + _sql.replace("\n", " ")[:70], make_query_check(_sql, _want)))


def raises(db, sql):
    from minidb import SQLError
    try:
        db.execute(sql)
    except SQLError:
        return
    except Exception as e:                      # noqa: BLE001
        raise AssertionError(f"{sql!r} raised {type(e).__name__}, not SQLError") from e
    raise AssertionError(f"{sql!r} should be an SQLError")


@check("groups come in order of first appearance")
def _():
    rows = seeded().execute("SELECT dept, COUNT(*) FROM emp GROUP BY dept").rows
    assert [tuple(r) for r in rows] == [("eng", 3), ("ops", 2), ("hr", 1)], rows


@check("rows come back in insertion order without ORDER BY")
def _():
    assert [r[0] for r in seeded().execute("SELECT id FROM emp").rows] == [1, 2, 3, 4, 5, 6]


@check("result column names")
def _():
    r = seeded().execute("SELECT name, Salary * 2 AS dbl, id  +  1, e.boss FROM emp e WHERE id = 1")
    assert list(r.columns) == ["name", "dbl", "id + 1", "boss"], r.columns


@check("star columns follow the tables in join order")
def _():
    r = seeded().execute("SELECT * FROM dept d JOIN emp e ON e.dept = d.code WHERE e.id = 1")
    assert list(r.columns) == ["code", "title", "id", "name", "dept", "salary", "boss"], r.columns


@check("a plain select reports rowcount = rows returned")
def _():
    assert seeded().execute("SELECT * FROM emp").rowcount == 6


@check("syntax and name errors are SQLError")
def _():
    db = seeded()
    for sql in ["SELEC 1", "SELECT FROM", "SELECT 1 +", "SELECT nope FROM emp", "SELECT * FROM nope",
                "SELECT 'unterminated", "SELECT 1 # 2", "SELECT FOO(1)", "SELECT UPPER()", "SELECT UPPER(1, 2)",
                "DROP TABLE nope", "CREATE TABLE emp (x INTEGER)", "SELECT (1", "SELECT 1 FROM emp WHERE"]:
        raises(db, sql)


@check("ambiguous columns are an error, qualified ones are not")
def _():
    db = seeded()
    raises(db, "SELECT id FROM emp e JOIN emp b ON e.boss = b.id")
    assert db.execute("SELECT e.id FROM emp e JOIN emp b ON e.boss = b.id WHERE b.id = 1").rowcount == 3


@check("arithmetic and comparison type errors")
def _():
    db = seeded()
    for sql in ["SELECT 1 / 0", "SELECT 5 % 0", "SELECT 1.5 / 0", "SELECT 'a' + 1", "SELECT 1 < 'a'",
                "SELECT TRUE = 1", "SELECT -'a'"]:
        raises(db, sql)


@check("aggregate misuse is an error")
def _():
    db = seeded()
    for sql in ["SELECT name FROM emp WHERE COUNT(*) > 1", "SELECT name, COUNT(*) FROM emp",
                "SELECT dept, name FROM emp GROUP BY dept", "SELECT (SELECT id FROM emp)",
                "SELECT (SELECT id, name FROM emp WHERE id = 1)",
                "SELECT name FROM emp WHERE id IN (SELECT id, name FROM emp)"]:
        raises(db, sql)


@check("a scalar subquery with no rows is NULL")
def _():
    assert seeded().execute("SELECT (SELECT id FROM emp WHERE id > 100)").rows == [(None,)]


@check("insert: constraint and type errors")
def _():
    db = seeded()
    for sql in ["INSERT INTO emp VALUES (1,'zed','eng',1.0,NULL)", "INSERT INTO emp (id) VALUES (9)",
                "INSERT INTO emp VALUES (9,'zed','eng','high',NULL)", "INSERT INTO emp VALUES (9.5,'z',NULL,NULL,NULL)",
                "INSERT INTO emp VALUES (9,5,NULL,NULL,NULL)", "INSERT INTO emp VALUES (9,'z')",
                "INSERT INTO emp VALUES (NULL,'z',NULL,NULL,NULL)", "INSERT INTO nope VALUES (1)",
                "INSERT INTO emp (id, nope) VALUES (9, 1)"]:
        raises(db, sql)
    assert db.execute("SELECT COUNT(*) FROM emp").rows == [(6,)]


@check("a multi-row insert with one bad row inserts nothing")
def _():
    db = seeded()
    raises(db, "INSERT INTO emp VALUES (10,'a','x',1.0,NULL),(1,'dup','x',1.0,NULL)")
    assert db.execute("SELECT COUNT(*) FROM emp").rows == [(6,)]


@check("REAL columns store floats, BOOLEAN takes only booleans, defaults apply")
def _():
    db = seeded()
    db.execute("INSERT INTO emp VALUES (7,'gus','hr',50,NULL)")
    v = db.execute("SELECT salary FROM emp WHERE id = 7").rows[0][0]
    assert v == 50.0 and type(v) is float, v
    db.execute("CREATE TABLE b (x BOOLEAN)")
    db.execute("INSERT INTO b VALUES (TRUE), (FALSE), (NULL)")
    assert db.execute("SELECT x FROM b").rows == [(True,), (False,), (None,)]
    raises(db, "INSERT INTO b VALUES (1)")
    db.execute("CREATE TABLE d (a INTEGER, b TEXT DEFAULT 'x', c INTEGER DEFAULT 5)")
    db.execute("INSERT INTO d (a) VALUES (1)")
    assert db.execute("SELECT * FROM d").rows == [(1, "x", 5)]


@check("update and delete: rowcount and values")
def _():
    db = seeded()
    assert db.execute("UPDATE emp SET salary = salary + 1 WHERE dept = 'eng'").rowcount == 3
    assert db.execute("SELECT salary FROM emp WHERE id IN (1, 2, 6) ORDER BY id").rows == [(101.0,), (81.0,), (81.0,)]
    assert db.execute("DELETE FROM emp WHERE dept = 'hr'").rowcount == 1
    assert db.execute("SELECT COUNT(*) FROM emp").rows == [(5,)]
    assert db.execute("DELETE FROM emp").rowcount == 5
    assert db.execute("SELECT COUNT(*) FROM emp").rows == [(0,)]


@check("update expressions see the old row")
def _():
    db = seeded()
    db.execute("CREATE TABLE sw (a INTEGER, b INTEGER)")
    db.execute("INSERT INTO sw VALUES (1, 2)")
    db.execute("UPDATE sw SET a = b, b = a")
    assert db.execute("SELECT a, b FROM sw").rows == [(2, 1)]


@check("a failing update changes nothing")
def _():
    db = seeded()
    raises(db, "UPDATE emp SET id = 1 WHERE id >= 2")
    raises(db, "UPDATE emp SET name = NULL")
    raises(db, "UPDATE emp SET salary = 'x' WHERE id = 1")
    raises(db, "UPDATE emp SET salary = salary / 0")
    assert db.execute("SELECT id, name, salary FROM emp ORDER BY id").rows[:2] == [(1, "ann", 100.0), (2, "bob", 80.0)]
    assert db.execute("SELECT COUNT(*) FROM emp WHERE name IS NULL").rows == [(0,)]


@check("IF NOT EXISTS and IF EXISTS")
def _():
    db = seeded()
    db.execute("CREATE TABLE IF NOT EXISTS emp (x INTEGER)")
    assert db.execute("SELECT COUNT(*) FROM emp").rows == [(6,)]
    db.execute("DROP TABLE IF EXISTS nope")
    db.execute("DROP TABLE dept")
    raises(db, "SELECT * FROM dept")


@check("executescript, trailing semicolons, one statement per execute")
def _():
    from minidb import Database
    db = Database()
    rs = db.executescript("CREATE TABLE t (a INTEGER); INSERT INTO t VALUES (1); ; SELECT a FROM t;")
    assert len(rs) == 3 and rs[2].rows == [(1,)], rs
    db.execute("SELECT 1;")
    raises(db, "SELECT 1; SELECT 2")


@check("transactions: rollback undoes data, tables and drops")
def _():
    db = seeded()
    db.execute("BEGIN")
    db.execute("INSERT INTO emp VALUES (8,'hal','hr',1.0,NULL)")
    db.execute("DELETE FROM dept")
    db.execute("CREATE TABLE tmp (a INTEGER)")
    db.execute("DROP TABLE emp")
    db.execute("ROLLBACK")
    assert db.execute("SELECT COUNT(*) FROM emp").rows == [(6,)]
    assert db.execute("SELECT COUNT(*) FROM dept").rows == [(3,)]
    raises(db, "SELECT * FROM tmp")


@check("transactions: commit keeps, misuse is an error, errors do not end it")
def _():
    db = seeded()
    db.execute("BEGIN")
    raises(db, "BEGIN")
    raises(db, "INSERT INTO emp VALUES (1,'dup','x',1.0,NULL)")
    db.execute("INSERT INTO emp VALUES (9,'ivy','hr',1.0,NULL)")
    db.execute("COMMIT")
    assert db.execute("SELECT COUNT(*) FROM emp").rows == [(7,)]
    raises(db, "COMMIT")
    raises(db, "ROLLBACK")
    db.execute("BEGIN")
    db.execute("ROLLBACK")


@check("rollback also undoes index changes and constraints stay right afterwards")
def _():
    db = seeded()
    db.execute("BEGIN")
    db.execute("CREATE INDEX i_name ON emp (name)")
    db.execute("INSERT INTO emp VALUES (20,'zed','hr',1.0,NULL)")
    db.execute("ROLLBACK")
    assert db.execute("EXPLAIN SELECT * FROM emp WHERE name = 'ann'").rows == [("SCAN emp",)]
    db.execute("INSERT INTO emp VALUES (20,'zed','hr',1.0,NULL)")
    raises(db, "INSERT INTO emp VALUES (20,'zed2','hr',1.0,NULL)")


@check("unique indexes")
def _():
    db = seeded()
    db.execute("CREATE UNIQUE INDEX u_title ON dept (title)")
    raises(db, "INSERT INTO dept VALUES ('x', 'Finance')")
    db.execute("INSERT INTO dept VALUES ('y', NULL), ('z', NULL)")      # NULLs do not collide
    raises(db, "CREATE UNIQUE INDEX u_dept ON emp (dept)")              # existing duplicates
    raises(db, "CREATE INDEX u_title ON emp (name)")                    # name taken
    raises(db, "CREATE INDEX i_x ON emp (nope)")
    raises(db, "CREATE INDEX i_y ON nope (x)")


@check("UNIQUE columns are enforced")
def _():
    from minidb import Database
    db = Database()
    db.execute("CREATE TABLE u (a INTEGER UNIQUE, b TEXT)")
    db.execute("INSERT INTO u VALUES (1, 'x'), (NULL, 'y'), (NULL, 'z')")
    raises(db, "INSERT INTO u VALUES (1, 'w')")
    raises(db, "UPDATE u SET a = 1 WHERE b = 'y'")


@check("EXPLAIN: primary key, either order, AND but not OR")
def _():
    db = seeded()
    r = db.execute("EXPLAIN SELECT * FROM emp WHERE id = 3")
    assert list(r.columns) == ["plan"] and r.rows == [("INDEX SCAN emp USING pk_emp",)], (r.columns, r.rows)
    assert db.execute("EXPLAIN SELECT * FROM emp WHERE 3 = id").rows == [("INDEX SCAN emp USING pk_emp",)]
    assert db.execute("EXPLAIN SELECT * FROM emp e WHERE e.id = 3 AND dept = 'ops'").rows == [
        ("INDEX SCAN emp USING pk_emp",)]
    assert db.execute("EXPLAIN SELECT * FROM emp WHERE id = 3 OR dept = 'x'").rows == [("SCAN emp",)]
    assert db.execute("EXPLAIN SELECT * FROM emp WHERE id > 3").rows == [("SCAN emp",)]
    assert db.execute("EXPLAIN SELECT * FROM emp").rows == [("SCAN emp",)]


@check("EXPLAIN: created and UNIQUE-column indexes, joins")
def _():
    db = seeded()
    assert db.execute("EXPLAIN SELECT * FROM emp WHERE name = 'ann'").rows == [("SCAN emp",)]
    db.execute("CREATE INDEX i_name ON emp (name)")
    assert db.execute("EXPLAIN SELECT * FROM emp WHERE name = 'ann'").rows == [("INDEX SCAN emp USING i_name",)]
    db.execute("CREATE TABLE u (a INTEGER UNIQUE)")
    assert db.execute("EXPLAIN SELECT * FROM u WHERE a = 1").rows == [("INDEX SCAN u USING uq_u_a",)]
    r = db.execute("EXPLAIN SELECT * FROM emp e JOIN dept d ON e.dept = d.code WHERE d.code = 'eng'")
    assert r.rows == [("SCAN emp",), ("INDEX SCAN dept USING pk_dept",)], r.rows


def _big(db, n=1000):
    db.execute("CREATE TABLE big (id INTEGER PRIMARY KEY, v INTEGER, s TEXT)")
    db.execute("INSERT INTO big VALUES " + ", ".join(f"({i}, {i * 2}, 's{i}')" for i in range(n)))


@check("the executor uses the primary-key index (rows read)")
def _():
    from minidb import Database
    db = Database()
    _big(db)
    before = db.rows_read
    assert db.execute("SELECT s FROM big WHERE id = 500").rows == [("s500",)]
    assert db.rows_read - before <= 2, db.rows_read - before
    assert db.execute("SELECT s FROM big WHERE id = 5000").rows == []


@check("a full scan reads every row, an index created later is used")
def _():
    from minidb import Database
    db = Database()
    _big(db)
    before = db.rows_read
    assert db.execute("SELECT s FROM big WHERE v = 1000").rows == [("s500",)]
    assert db.rows_read - before >= 1000, db.rows_read - before
    db.execute("CREATE INDEX i_v ON big (v)")
    before = db.rows_read
    assert db.execute("SELECT s FROM big WHERE v = 1000").rows == [("s500",)]
    assert db.rows_read - before <= 2, db.rows_read - before
    db.execute("UPDATE big SET v = -1 WHERE id = 500")                  # the index follows updates and deletes
    assert db.execute("SELECT s FROM big WHERE v = 1000").rows == []
    assert db.execute("SELECT s FROM big WHERE v = -1").rows == [("s500",)]
    db.execute("DELETE FROM big WHERE id = 500")
    assert db.execute("SELECT s FROM big WHERE v = -1").rows == []


@check("an index lookup agrees with a scan when the WHERE has more conditions")
def _():
    from minidb import Database
    db = Database()
    _big(db, 200)
    db.execute("CREATE INDEX i_v ON big (v)")
    assert db.execute("SELECT s FROM big WHERE v = 100 AND s = 'nope'").rows == []
    assert db.execute("SELECT s FROM big WHERE v = 100 AND s = 's50'").rows == [("s50",)]
    assert db.execute("SELECT s FROM big WHERE 100 = v").rows == [("s50",)]


@check("persistence: tables, rows and indexes survive a reopen")
def _():
    from minidb import Database
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "x.db")
        db = Database(path)
        db.executescript(SETUP)
        db.execute("CREATE UNIQUE INDEX u_title ON dept (title)")
        db.execute("UPDATE emp SET salary = 1.5 WHERE id = 1")
        db.close()
        db2 = Database(path)
        assert db2.execute("SELECT salary FROM emp WHERE id = 1").rows == [(1.5,)]
        assert db2.execute("SELECT COUNT(*) FROM emp").rows == [(6,)]
        assert db2.execute("EXPLAIN SELECT * FROM dept WHERE title = 'Finance'").rows == [
            ("INDEX SCAN dept USING u_title",)]
        raises(db2, "INSERT INTO dept VALUES ('q', 'Finance')")
        raises(db2, "INSERT INTO emp VALUES (1,'dup','x',1.0,NULL)")
        db2.close()


@check("persistence: a rolled-back or uncommitted change is never saved")
def _():
    from minidb import Database
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "x.db")
        db = Database(path)
        db.executescript(SETUP)
        db.execute("BEGIN")
        db.execute("INSERT INTO emp VALUES (30,'rolled','hr',1.0,NULL)")
        db.execute("ROLLBACK")
        db.execute("BEGIN")
        db.execute("INSERT INTO emp VALUES (31,'open','hr',1.0,NULL)")
        other = Database(path)                                           # sees only what was committed
        assert other.execute("SELECT COUNT(*) FROM emp").rows == [(6,)]
        db.execute("COMMIT")
        again = Database(path)
        assert again.execute("SELECT name FROM emp WHERE id >= 30").rows == [("open",)]


@check("an in-memory database leaves no file behind")
def _():
    from minidb import Database
    before = set(os.listdir(os.getcwd()))
    db = Database()
    db.executescript(SETUP)
    db.close()
    assert set(os.listdir(os.getcwd())) == before


@check("lexer: kinds and values")
def _():
    from minidb.lexer import tokenize
    toks = [(t.kind, t.value) for t in tokenize("SELECT Name, 'it''s', 12, 3.5 FROM t WHERE a != 2 -- x")]
    assert toks == [("KEYWORD", "SELECT"), ("IDENT", "name"), ("PUNCT", ","), ("STRING", "it's"), ("PUNCT", ","),
                    ("NUMBER", 12), ("PUNCT", ","), ("NUMBER", 3.5), ("KEYWORD", "FROM"), ("IDENT", "t"),
                    ("KEYWORD", "WHERE"), ("IDENT", "a"), ("OP", "!="), ("NUMBER", 2), ("EOF", "")], toks


@check("lexer: errors")
def _():
    from minidb import SQLError
    from minidb.lexer import tokenize
    for bad in ["SELECT 'abc", "SELECT 1 # 2", "SELECT @x"]:
        try:
            tokenize(bad)
        except SQLError:
            continue
        raise AssertionError(f"{bad!r} should be an SQLError")


@check("parser: a select builds the right tree")
def _():
    from minidb import ast as A
    from minidb.parser import parse
    got = parse("SELECT a + 1 AS x FROM t WHERE b = 2 ORDER BY x DESC LIMIT 3")
    want = [A.Select(items=[A.SelectItem(A.Binary("+", A.Column("a"), A.Literal(1)), "x")], table=A.TableRef("t"),
                     where=A.Binary("=", A.Column("b"), A.Literal(2)),
                     order_by=[A.OrderItem(A.Column("x"), True)], limit=3)]
    assert got == want, got


@check("parser: precedence and !=")
def _():
    from minidb import ast as A
    from minidb.parser import parse
    e = parse("SELECT 1 + 2 * 3")[0].items[0].expr
    assert e == A.Binary("+", A.Literal(1), A.Binary("*", A.Literal(2), A.Literal(3))), e
    w = parse("SELECT a FROM t WHERE x = 1 OR y != 2 AND z = 3")[0].where
    assert w == A.Binary("OR", A.Binary("=", A.Column("x"), A.Literal(1)),
                         A.Binary("AND", A.Binary("<>", A.Column("y"), A.Literal(2)),
                                  A.Binary("=", A.Column("z"), A.Literal(3)))), w
    n = parse("SELECT NOT a = 1 FROM t")[0].items[0].expr
    assert n == A.Unary("NOT", A.Binary("=", A.Column("a"), A.Literal(1))), n


@check("parser: joins, subqueries, other statements")
def _():
    from minidb import ast as A
    from minidb.parser import parse
    s = parse("SELECT e.name FROM emp e LEFT OUTER JOIN dept d ON e.dept = d.code WHERE e.id IN (SELECT 1)")[0]
    assert s.joins == [A.Join("LEFT", A.TableRef("dept", "d"),
                              A.Binary("=", A.Column("dept", "e"), A.Column("code", "d")))], s.joins
    assert isinstance(s.where, A.InSelect)
    kinds = [type(x).__name__ for x in parse(
        "CREATE TABLE t (a INTEGER PRIMARY KEY); DROP TABLE t; INSERT INTO t VALUES (1); UPDATE t SET a = 2; "
        "DELETE FROM t; BEGIN; COMMIT; ROLLBACK; CREATE UNIQUE INDEX i ON t (a); EXPLAIN SELECT 1")]
    assert kinds == ["CreateTable", "DropTable", "Insert", "Update", "Delete", "Begin", "Commit", "Rollback",
                     "CreateIndex", "Explain"], kinds


@check("parser: syntax errors")
def _():
    from minidb import SQLError
    from minidb.parser import parse
    for bad in ["SELECT FROM t", "SELECT a FROM", "INSERT t VALUES (1)", "CREATE TABLE t ()", "SELECT (1",
                "SELECT a FROM t WHERE", "UPDATE t SET", "SELECT 1 1"]:
        try:
            parse(bad)
        except SQLError:
            continue
        raise AssertionError(f"{bad!r} should be an SQLError")


@check("functions and expr evaluate on their own")
def _():
    from minidb import ast as A
    from minidb.expr import evaluate
    from minidb.functions import call_scalar
    assert call_scalar("UPPER", ["a"]) == "A" and call_scalar("COALESCE", [None, 2]) == 2
    assert call_scalar("SUBSTR", ["hello", 2, 3]) == "ell" and call_scalar("ROUND", [2.5]) == 3.0
    assert evaluate(A.Binary("+", A.Literal(1), A.Literal(2)), lambda t, c: None) == 3
    assert evaluate(A.Column("a"), lambda t, c: {"a": 5}[c]) == 5
    assert evaluate(A.Binary("AND", A.Literal(None), A.Literal(False)), lambda t, c: None) is False
    assert evaluate(A.InSelect(A.Literal(2), A.Select(items=[])), lambda t, c: None, lambda s: [(1,), (2,)]) is True


def _cli(sql, *args):
    r = subprocess.run([sys.executable, "-m", "minidb", *args], input=sql, capture_output=True, text=True, cwd=HERE,
                       timeout=60)
    return r


def _table(headers, rows):
    cells = [[str(c) for c in headers]] + [[str(c) for c in r] for r in rows]
    w = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    line = lambda r: " | ".join(c.ljust(w[i]) if i < len(w) - 1 else c for i, c in enumerate(r))   # noqa: E731
    out = [line(cells[0]), "-+-".join("-" * x for x in w)] + [line(r) for r in cells[1:]]
    return out + [f"({len(rows)} row{'s' if len(rows) != 1 else ''})"]


@check("command line: tables, counts, OK lines and errors")
def _():
    sql = ("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT);\n"
           "INSERT INTO t VALUES (1,'ann'),(2,'bob');\n"
           "SELECT * FROM t;\nSELECT nope FROM t;\nSELECT * FROM t WHERE id = 9;\nDELETE FROM t WHERE id = 1;\n")
    r = _cli(sql)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    want = ["OK", "OK, 2 rows affected"] + _table(["id", "name"], [(1, "ann"), (2, "bob")])
    assert lines[:len(want)] == want, lines
    rest = lines[len(want):]
    assert rest[0].startswith("Error: ") and len(rest[0]) > 8, rest
    assert rest[1:4] == _table(["id", "name"], [])[:2] + ["(0 rows)"], rest
    assert rest[4] == "OK, 1 rows affected" or rest[4] == "OK, 1 row affected", rest


@check("command line: NULL, booleans, floats and wide cells")
def _():
    r = _cli("SELECT NULL AS a, TRUE AS b, 2.5 AS c, 'long text here' AS d;\n")
    assert r.stdout.splitlines() == _table(["a", "b", "c", "d"], [("NULL", "true", 2.5, "long text here")]), r.stdout


@check("command line: a database file persists between runs")
def _():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "cli.db")
        _cli("CREATE TABLE t (a INTEGER); INSERT INTO t VALUES (7);", path)
        r = _cli("SELECT a FROM t;", path)
        assert r.stdout.splitlines() == _table(["a"], [(7,)]), r.stdout


# ---- version 2 (TASK.md) -------------------------------------------------------------------------------------------

SETUP2 = """
CREATE TABLE cust (id INTEGER PRIMARY KEY, name TEXT, city TEXT);
INSERT INTO cust VALUES (1,'ann co','oslo'),(2,'bob ltd','rome'),(3,'cat inc','oslo'),(4,'dan gmbh',NULL);
CREATE TABLE ord (id INTEGER PRIMARY KEY, cust INTEGER, amount REAL, day INTEGER);
INSERT INTO ord VALUES (1,1,50.0,1),(2,1,70.0,2),(3,2,20.0,2),(4,3,90.0,3),(5,3,10.0,5),(6,1,30.0,6),(7,NULL,5.0,7);
"""

QUERIES2 = [
    # section 13
    ("SELECT name, CASE WHEN salary >= 100 THEN 'high' WHEN salary >= 70 THEN 'mid' ELSE 'low' END FROM emp ORDER BY id",
     [("ann", "high"), ("bob", "mid"), ("cat", "mid"), ("dan", "low"), ("eve", "low"), ("fay", "mid")]),
    ("SELECT id, CASE dept WHEN 'eng' THEN 1 WHEN 'ops' THEN 2 END FROM emp ORDER BY id",
     [(1, 1), (2, 1), (3, 2), (4, 2), (5, None), (6, 1)]),
    ("SELECT CASE WHEN 1 = 2 THEN 'a' END, CASE WHEN NULL THEN 'a' ELSE 'b' END, CASE 2 WHEN 1 THEN 'x' WHEN 2 THEN 'y' END",
     [(None, "b", "y")]),
    ("SELECT CAST(3.7 AS INTEGER), CAST(-3.7 AS INTEGER), CAST('12' AS INTEGER), CAST(5 AS TEXT), CAST(2 AS REAL), "
     "CAST('2.5' AS REAL), CAST(NULL AS INTEGER)", [(3, -3, 12, "5", 2.0, 2.5, None)]),
    ("SELECT REPLACE('banana', 'an', 'AN'), TRIM('  x  '), LTRIM('  x'), RTRIM('x  '), INSTR('hello', 'll'), "
     "INSTR('hello', 'z'), IIF(1 < 2, 'y', 'n'), IIF(NULL, 'y', 'n')", [("bANANa", "x", "x", "x", 3, 0, "y", "n")]),
    ("SELECT REPLACE(NULL, 'a', 'b'), TRIM(NULL), INSTR(NULL, 'a')", [(None, None, None)]),
    # section 14
    ("SELECT name FROM cust c WHERE EXISTS (SELECT 1 FROM ord o WHERE o.cust = c.id) ORDER BY name",
     [("ann co",), ("bob ltd",), ("cat inc",)]),
    ("SELECT name FROM cust c WHERE NOT EXISTS (SELECT 1 FROM ord o WHERE o.cust = c.id)", [("dan gmbh",)]),
    ("SELECT c.name, (SELECT SUM(amount) FROM ord o WHERE o.cust = c.id) FROM cust c ORDER BY c.id",
     [("ann co", 150.0), ("bob ltd", 20.0), ("cat inc", 100.0), ("dan gmbh", None)]),
    ("SELECT id FROM ord o WHERE amount > (SELECT AVG(amount) FROM ord x WHERE x.cust = o.cust) ORDER BY id",
     [(2,), (4,)]),
    ("SELECT c.id FROM cust c WHERE EXISTS (SELECT 1 FROM ord WHERE ord.cust = c.id AND amount > 80)", [(3,)]),
    ("SELECT c.name FROM cust c WHERE c.id IN (SELECT o.cust FROM ord o WHERE o.amount < c.id * 30) ORDER BY c.id",
     [("bob ltd",), ("cat inc",)]),
    ("SELECT c.city, COUNT(*) FROM cust c GROUP BY c.city HAVING COUNT(*) > (SELECT COUNT(*) FROM ord WHERE cust = 2) "
     "ORDER BY c.city", [("oslo", 2)]),
    ("SELECT t.cust, t.total FROM (SELECT cust, SUM(amount) AS total FROM ord GROUP BY cust) t WHERE t.total > 50 "
     "ORDER BY t.cust", [(1, 150.0), (3, 100.0)]),
    ("SELECT c.name, t.n FROM cust c JOIN (SELECT cust, COUNT(*) AS n FROM ord GROUP BY cust) t ON t.cust = c.id "
     "ORDER BY c.id", [("ann co", 3), ("bob ltd", 1), ("cat inc", 2)]),
    ("WITH big AS (SELECT * FROM ord WHERE amount >= 50), n AS (SELECT COUNT(*) AS c FROM big) SELECT c FROM n", [(3,)]),
    ("WITH t AS (SELECT cust, SUM(amount) AS s FROM ord GROUP BY cust) SELECT c.name, t.s FROM cust c "
     "JOIN t ON t.cust = c.id ORDER BY t.s DESC", [("ann co", 150.0), ("cat inc", 100.0), ("bob ltd", 20.0)]),
    ("WITH emp AS (SELECT 1 AS id) SELECT * FROM emp", [(1,)]),
    ("SELECT x FROM (WITH a AS (SELECT 5 AS x) SELECT x FROM a)", [(5,)]),
    # section 15
    ("SELECT dept FROM emp UNION SELECT code FROM dept ORDER BY 1", [("eng",), ("fin",), ("hr",), ("ops",)]),
    ("SELECT COUNT(*) FROM (SELECT dept FROM emp UNION ALL SELECT code FROM dept) t", [(9,)]),
    ("SELECT dept FROM emp INTERSECT SELECT code FROM dept ORDER BY 1", [("eng",), ("ops",)]),
    ("SELECT code FROM dept EXCEPT SELECT dept FROM emp", [("fin",)]),
    ("SELECT 1 UNION SELECT 2 UNION SELECT 1 ORDER BY 1", [(1,), (2,)]),
    ("SELECT id FROM emp UNION SELECT id + 100 FROM emp ORDER BY 1 DESC LIMIT 2", [(106,), (105,)]),
    ("SELECT 1 UNION SELECT 2 EXCEPT SELECT 2", [(1,)]),
    ("SELECT NULL UNION SELECT NULL", [(None,)]),
    ("SELECT id, name FROM emp WHERE id < 3 UNION ALL SELECT id, name FROM emp WHERE id > 5 ORDER BY id",
     [(1, "ann"), (2, "bob"), (6, "fay")]),
    ("WITH a AS (SELECT 1 AS x) SELECT x FROM a UNION SELECT 2 ORDER BY 1", [(1,), (2,)]),
    # section 16
    ("SELECT c.name, o.id FROM ord o RIGHT JOIN cust c ON o.cust = c.id ORDER BY c.id, o.id",
     [("ann co", 1), ("ann co", 2), ("ann co", 6), ("bob ltd", 3), ("cat inc", 4), ("cat inc", 5), ("dan gmbh", None)]),
    ("SELECT COUNT(*) FROM dept CROSS JOIN cust", [(12,)]),
    ("SELECT d.code, c.id FROM dept d CROSS JOIN cust c WHERE c.id <= 2 ORDER BY 1, 2",
     [("eng", 1), ("eng", 2), ("fin", 1), ("fin", 2), ("ops", 1), ("ops", 2)]),
    ("SELECT c.name, o.id FROM cust c LEFT JOIN ord o ON o.cust = c.id AND o.amount > 60 ORDER BY c.id, o.id",
     [("ann co", 2), ("bob ltd", None), ("cat inc", 4), ("dan gmbh", None)]),
    # section 17
    ("SELECT name, ROW_NUMBER() OVER (ORDER BY id) FROM emp ORDER BY id",
     [("ann", 1), ("bob", 2), ("cat", 3), ("dan", 4), ("eve", 5), ("fay", 6)]),
    ("SELECT name, dept, RANK() OVER (PARTITION BY dept ORDER BY salary DESC) AS r FROM emp ORDER BY id",
     [("ann", "eng", 1), ("bob", "eng", 2), ("cat", "ops", 1), ("dan", "ops", 2), ("eve", "hr", 1), ("fay", "eng", 2)]),
    ("SELECT name, DENSE_RANK() OVER (ORDER BY salary DESC) FROM emp ORDER BY id",
     [("ann", 1), ("bob", 2), ("cat", 3), ("dan", 5), ("eve", 4), ("fay", 2)]),
    ("SELECT id, SUM(amount) OVER (ORDER BY day) FROM ord ORDER BY id",
     [(1, 50.0), (2, 140.0), (3, 140.0), (4, 230.0), (5, 240.0), (6, 270.0), (7, 275.0)]),
    ("SELECT id, SUM(amount) OVER (PARTITION BY cust) FROM ord ORDER BY id",
     [(1, 150.0), (2, 150.0), (3, 20.0), (4, 100.0), (5, 100.0), (6, 150.0), (7, 5.0)]),
    ("SELECT id, COUNT(*) OVER (PARTITION BY cust), MAX(amount) OVER (PARTITION BY cust ORDER BY id) FROM ord ORDER BY id",
     [(1, 3, 50.0), (2, 3, 70.0), (3, 1, 20.0), (4, 2, 90.0), (5, 2, 90.0), (6, 3, 70.0), (7, 1, 5.0)]),
    ("SELECT id, LAG(amount) OVER (ORDER BY id), LEAD(amount, 2, -1.0) OVER (ORDER BY id) FROM ord ORDER BY id",
     [(1, None, 20.0), (2, 50.0, 90.0), (3, 70.0, 10.0), (4, 20.0, 30.0), (5, 90.0, 5.0), (6, 10.0, -1.0), (7, 30.0, -1.0)]),
    ("SELECT cust, SUM(amount) AS s, RANK() OVER (ORDER BY SUM(amount) DESC) FROM ord WHERE cust IS NOT NULL "
     "GROUP BY cust ORDER BY cust", [(1, 150.0, 1), (2, 20.0, 3), (3, 100.0, 2)]),
    ("SELECT id FROM ord ORDER BY ROW_NUMBER() OVER (ORDER BY amount DESC)", [(4,), (2,), (1,), (6,), (3,), (5,), (7,)]),
    ("SELECT id, AVG(amount) OVER (PARTITION BY cust ORDER BY day) FROM ord WHERE cust = 1 ORDER BY id",
     [(1, 50.0), (2, 60.0), (6, 50.0)]),
    ("SELECT DISTINCT cust, SUM(amount) OVER (PARTITION BY cust) FROM ord ORDER BY 1", [(None, 5.0), (1, 150.0), (2, 20.0), (3, 100.0)]),
    ("SELECT id, ROW_NUMBER() OVER (PARTITION BY cust ORDER BY amount DESC) AS rn FROM ord ORDER BY id LIMIT 4",
     [(1, 2), (2, 1), (3, 1), (4, 1)]),
]

for _sql, _want in QUERIES2:
    CHECKS.append(("v2 query: " + _sql.replace("\n", " ")[:68], (lambda s, w: lambda: make_query_check2(s, w)())(_sql, _want)))


def seeded2():
    db = seeded()
    db.executescript(SETUP2)
    return db


def make_query_check2(sql, want):
    def run():
        rows = seeded2().execute(sql).rows
        if "ORDER BY" in sql.upper():
            exact(rows, want)
        else:
            assert norm(rows) == norm(want), f"got {rows!r}, want {want!r}"
    return run


@check("v2: set operations without ORDER BY keep the row order of the spec")
def _():
    db = seeded2()
    assert db.execute("SELECT id FROM cust UNION ALL SELECT id FROM cust WHERE id < 3").rows == [(1,), (2,), (3,), (4,), (1,), (2,)]
    assert db.execute("SELECT dept FROM emp UNION SELECT code FROM dept").rows == [("eng",), ("ops",), ("hr",), ("fin",)]
    assert db.execute("SELECT dept FROM emp INTERSECT SELECT code FROM dept").rows == [("eng",), ("ops",)]
    assert db.execute("SELECT code FROM dept EXCEPT SELECT dept FROM emp").rows == [("fin",)]


@check("v2: set operation column names and mismatches")
def _():
    db = seeded2()
    r = db.execute("SELECT id AS a, name AS b FROM emp WHERE id = 1 UNION ALL SELECT id, name FROM cust WHERE id = 1")
    assert list(r.columns) == ["a", "b"], r.columns
    raises(db, "SELECT id, name FROM emp UNION SELECT id FROM cust")
    raises(db, "SELECT id FROM emp EXCEPT SELECT id, name FROM cust")


@check("v2: window functions are only allowed in the select list and ORDER BY")
def _():
    db = seeded2()
    for sql in ["SELECT id FROM ord WHERE ROW_NUMBER() OVER (ORDER BY id) = 1",
                "SELECT cust FROM ord GROUP BY cust HAVING RANK() OVER (ORDER BY cust) = 1",
                "SELECT RANK(1) OVER (ORDER BY id) FROM ord", "SELECT ROW_NUMBER() FROM ord",
                "UPDATE ord SET amount = ROW_NUMBER() OVER (ORDER BY id)"]:
        raises(db, sql)


@check("v2: CASE, CAST and function errors")
def _():
    db = seeded2()
    for sql in ["SELECT CAST('abc' AS INTEGER)", "SELECT CAST('x' AS REAL)", "SELECT CAST(1 AS BLOB)", "SELECT CAST(2.5 AS BOOLEAN)",
                "SELECT CASE END", "SELECT CASE WHEN 1 THEN END", "SELECT REPLACE('a', 'b')", "SELECT INSTR('a')",
                "SELECT IIF(1, 2)", "SELECT TRIM()"]:
        raises(db, sql)
    assert db.execute("SELECT CAST(0 AS BOOLEAN), CAST(7 AS BOOLEAN)").rows == [(False, True)]


@check("v2: subquery name resolution (inner names win, outer ones are found, unknown ones fail)")
def _():
    db = seeded2()
    assert db.execute("SELECT name FROM cust c WHERE (SELECT COUNT(*) FROM ord WHERE ord.cust = c.id) = 3").rows == [("ann co",)]
    assert db.execute("SELECT id FROM cust WHERE EXISTS (SELECT 1 FROM ord WHERE id = 7)").rows == [(1,), (2,), (3,), (4,)]
    assert db.execute("SELECT id FROM cust WHERE NOT EXISTS (SELECT 1 FROM ord WHERE id = 99)").rows == [(1,), (2,), (3,), (4,)]
    raises(db, "SELECT id FROM cust WHERE EXISTS (SELECT 1 FROM ord WHERE nope = id)")
    raises(db, "SELECT (SELECT c.nope FROM ord) FROM cust c")


@check("v2: views")
def _():
    db = seeded2()
    db.execute("CREATE VIEW oslo AS SELECT id, name FROM cust WHERE city = 'oslo'")
    assert db.execute("SELECT name FROM oslo ORDER BY id").rows == [("ann co",), ("cat inc",)]
    db.execute("CREATE VIEW oslo_orders AS SELECT o.id AS oid, o.amount FROM ord o JOIN oslo ON o.cust = oslo.id")
    assert db.execute("SELECT SUM(amount) FROM oslo_orders").rows == [(250.0,)]
    db.execute("INSERT INTO cust VALUES (5, 'eve sa', 'oslo')")
    assert db.execute("SELECT COUNT(*) FROM oslo").rows == [(3,)]
    for sql in ["INSERT INTO oslo VALUES (9, 'x')", "UPDATE oslo SET name = 'x'", "DELETE FROM oslo",
                "CREATE VIEW oslo AS SELECT 1", "CREATE VIEW cust AS SELECT 1", "CREATE TABLE oslo (a INTEGER)",
                "DROP TABLE oslo", "DROP VIEW nope", "CREATE VIEW bad AS SELECT nope FROM cust"]:
        raises(db, sql)
    db.execute("DROP VIEW IF EXISTS nope")
    db.execute("DROP VIEW oslo_orders")
    raises(db, "SELECT * FROM oslo_orders")


@check("v2: views and rollback")
def _():
    db = seeded2()
    db.execute("BEGIN")
    db.execute("CREATE VIEW v AS SELECT 1 AS one")
    db.execute("ROLLBACK")
    raises(db, "SELECT * FROM v")
    db.execute("CREATE VIEW v AS SELECT 1 AS one")
    db.execute("BEGIN")
    db.execute("DROP VIEW v")
    db.execute("ROLLBACK")
    assert db.execute("SELECT * FROM v").rows == [(1,)]


@check("v2: INSERT ... SELECT")
def _():
    db = seeded2()
    db.execute("CREATE TABLE big (id INTEGER PRIMARY KEY, total REAL)")
    r = db.execute("INSERT INTO big SELECT cust, SUM(amount) FROM ord WHERE cust IS NOT NULL GROUP BY cust")
    assert r.rowcount == 3
    assert db.execute("SELECT * FROM big ORDER BY id").rows == [(1, 150.0), (2, 20.0), (3, 100.0)]
    db.execute("INSERT INTO big (id) SELECT id + 10 FROM cust")
    assert db.execute("SELECT COUNT(*) FROM big").rows == [(7,)]
    raises(db, "INSERT INTO big SELECT id, 1 FROM cust")             # duplicate keys: nothing is inserted
    assert db.execute("SELECT COUNT(*) FROM big").rows == [(7,)]
    raises(db, "INSERT INTO big SELECT id FROM cust UNION SELECT id, 1 FROM cust")


@check("v2: INSERT ... ON CONFLICT")
def _():
    db = seeded2()
    db.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v INTEGER, n INTEGER DEFAULT 0)")
    db.execute("INSERT INTO kv (k, v) VALUES ('a', 1)")
    assert db.execute("INSERT INTO kv (k, v) VALUES ('a', 5) ON CONFLICT (k) DO NOTHING").rowcount == 0
    assert db.execute("INSERT INTO kv (k, v) VALUES ('a', 5) ON CONFLICT DO NOTHING").rowcount == 0
    assert db.execute("SELECT v FROM kv").rows == [(1,)]
    r = db.execute("INSERT INTO kv (k, v) VALUES ('a', 5), ('b', 2) ON CONFLICT (k) DO UPDATE SET v = v + excluded.v, n = n + 1")
    assert r.rowcount == 2
    assert db.execute("SELECT k, v, n FROM kv ORDER BY k").rows == [("a", 6, 1), ("b", 2, 0)]
    raises(db, "INSERT INTO kv (k, v) VALUES ('a', 5) ON CONFLICT DO UPDATE SET v = 0")
    raises(db, "INSERT INTO kv (k, v) VALUES ('a', 5) ON CONFLICT (k) DO UPDATE SET nope = 0")
    raises(db, "INSERT INTO kv (k, v) VALUES (NULL, 5) ON CONFLICT (k) DO NOTHING")      # NOT NULL is not a conflict
    db.execute("CREATE TABLE u (a INTEGER, b TEXT UNIQUE)")
    db.execute("INSERT INTO u VALUES (1, 'x')")
    assert db.execute("INSERT INTO u VALUES (2, 'x') ON CONFLICT (b) DO UPDATE SET a = excluded.a").rowcount == 1
    assert db.execute("SELECT a FROM u").rows == [(2,)]


@check("v2: ALTER TABLE ADD COLUMN")
def _():
    db = seeded2()
    db.execute("ALTER TABLE dept ADD COLUMN size INTEGER DEFAULT 10")
    db.execute("ALTER TABLE dept ADD COLUMN note TEXT")
    assert db.execute("SELECT * FROM dept WHERE code = 'eng'").rows == [("eng", "Engineering", 10, None)]
    db.execute("INSERT INTO dept (code, title) VALUES ('zzz', 'Z')")
    assert db.execute("SELECT size FROM dept WHERE code = 'zzz'").rows == [(10,)]
    for sql in ["ALTER TABLE dept ADD COLUMN x INTEGER NOT NULL", "ALTER TABLE dept ADD COLUMN x INTEGER PRIMARY KEY",
                "ALTER TABLE dept ADD COLUMN x INTEGER UNIQUE", "ALTER TABLE dept ADD COLUMN size INTEGER",
                "ALTER TABLE nope ADD COLUMN x INTEGER"]:
        raises(db, sql)
    db.execute("CREATE TABLE empty (a INTEGER)")
    db.execute("ALTER TABLE empty ADD COLUMN b INTEGER NOT NULL")                 # no rows: fine


@check("v2: ALTER TABLE rename and drop")
def _():
    db = seeded2()
    db.execute("CREATE INDEX i_city ON cust (city)")
    db.execute("ALTER TABLE cust RENAME COLUMN city TO town")
    assert db.execute("SELECT town FROM cust WHERE id = 1").rows == [("oslo",)]
    assert db.execute("EXPLAIN SELECT * FROM cust WHERE town = 'oslo'").rows == [("INDEX SCAN cust USING i_city",)]
    raises(db, "SELECT city FROM cust")
    db.execute("ALTER TABLE cust RENAME TO client")
    assert db.execute("SELECT COUNT(*) FROM client").rows == [(4,)]
    assert db.execute("EXPLAIN SELECT * FROM client WHERE id = 1").rows[0][0].startswith("INDEX SCAN client USING")
    raises(db, "SELECT * FROM cust")
    raises(db, "ALTER TABLE client RENAME TO ord")
    raises(db, "ALTER TABLE client RENAME COLUMN town TO name")
    db.execute("ALTER TABLE ord DROP COLUMN day")
    assert list(db.execute("SELECT * FROM ord WHERE id = 1").columns) == ["id", "cust", "amount"]
    assert db.execute("SELECT * FROM ord WHERE id = 1").rows == [(1, 1, 50.0)]
    for sql in ["ALTER TABLE ord DROP COLUMN id", "ALTER TABLE client DROP COLUMN town", "ALTER TABLE ord DROP COLUMN nope",
                "ALTER TABLE dept DROP COLUMN code"]:
        raises(db, sql)
    db.execute("CREATE TABLE one (a INTEGER)")
    raises(db, "ALTER TABLE one DROP COLUMN a")


@check("v2: ALTER TABLE and views, rollback")
def _():
    db = seeded2()
    db.execute("CREATE VIEW names AS SELECT name FROM cust")
    db.execute("ALTER TABLE cust RENAME COLUMN name TO label")
    raises(db, "SELECT * FROM names")
    db.execute("BEGIN")
    db.execute("ALTER TABLE ord ADD COLUMN extra INTEGER")
    db.execute("ALTER TABLE ord RENAME TO ord2")
    db.execute("ROLLBACK")
    assert list(db.execute("SELECT * FROM ord WHERE id = 1").columns) == ["id", "cust", "amount", "day"]


def _fk_db():
    from minidb import Database
    db = Database()
    db.executescript("""
        CREATE TABLE parent (id INTEGER PRIMARY KEY, code TEXT UNIQUE, name TEXT);
        INSERT INTO parent VALUES (1, 'a', 'one'), (2, 'b', 'two'), (3, 'c', 'three');
        CREATE TABLE kid (id INTEGER PRIMARY KEY, pid INTEGER REFERENCES parent(id));
        CREATE TABLE kid_c (id INTEGER PRIMARY KEY, pid INTEGER REFERENCES parent(id) ON DELETE CASCADE);
        CREATE TABLE kid_n (id INTEGER PRIMARY KEY, pid INTEGER REFERENCES parent(id) ON DELETE SET NULL);
        CREATE TABLE kid_code (id INTEGER PRIMARY KEY, pc TEXT REFERENCES parent(code));
        INSERT INTO kid VALUES (1, 1), (2, NULL);
        INSERT INTO kid_c VALUES (1, 2), (2, 2), (3, 3);
        INSERT INTO kid_n VALUES (1, 3), (2, 3);
        INSERT INTO kid_code VALUES (1, 'a');
    """)
    return db


@check("v2: foreign keys: inserts and updates of children")
def _():
    db = _fk_db()
    for sql in ["INSERT INTO kid VALUES (3, 99)", "UPDATE kid SET pid = 99 WHERE id = 1", "INSERT INTO kid_code VALUES (2, 'zz')"]:
        raises(db, sql)
    db.execute("INSERT INTO kid VALUES (3, NULL), (4, 2)")
    db.execute("UPDATE kid SET pid = 3 WHERE id = 1")
    assert db.execute("SELECT pid FROM kid ORDER BY id").rows == [(3,), (None,), (None,), (2,)]
    raises(db, "INSERT INTO kid VALUES (5, 3), (6, 98)")                      # all or nothing
    assert db.execute("SELECT COUNT(*) FROM kid").rows == [(4,)]


@check("v2: foreign keys: deleting and changing parents")
def _():
    db = _fk_db()
    raises(db, "DELETE FROM parent WHERE id = 1")                             # kid and kid_code use it: RESTRICT
    raises(db, "UPDATE parent SET id = 10 WHERE id = 1")
    raises(db, "UPDATE parent SET code = 'zz' WHERE id = 1")
    db.execute("UPDATE parent SET name = 'uno' WHERE id = 1")                 # not a referenced value: fine
    db.execute("DELETE FROM kid WHERE id = 1")
    db.execute("DELETE FROM kid_code")
    assert db.execute("DELETE FROM parent WHERE id = 1").rowcount == 1
    assert db.execute("DELETE FROM parent WHERE id = 2").rowcount == 1       # cascades to kid_c ids 1, 2
    assert db.execute("SELECT id FROM kid_c").rows == [(3,)]
    assert db.execute("DELETE FROM parent WHERE id = 3").rowcount == 1       # cascades kid_c 3, nulls kid_n
    assert db.execute("SELECT COUNT(*) FROM kid_c").rows == [(0,)]
    assert db.execute("SELECT pid FROM kid_n ORDER BY id").rows == [(None,), (None,)]


@check("v2: foreign keys: a failed delete changes nothing, cascades are recursive")
def _():
    from minidb import Database
    db = Database()
    db.executescript("""
        CREATE TABLE a (id INTEGER PRIMARY KEY);
        CREATE TABLE b (id INTEGER PRIMARY KEY, a_id INTEGER REFERENCES a(id) ON DELETE CASCADE);
        CREATE TABLE c (id INTEGER PRIMARY KEY, b_id INTEGER REFERENCES b(id) ON DELETE CASCADE);
        CREATE TABLE d (id INTEGER PRIMARY KEY, a_id INTEGER REFERENCES a(id));
        INSERT INTO a VALUES (1), (2);
        INSERT INTO b VALUES (1, 1), (2, 1), (3, 2);
        INSERT INTO c VALUES (1, 1), (2, 3);
        INSERT INTO d VALUES (1, 2);
    """)
    raises(db, "DELETE FROM a")                                               # d blocks id 2; id 1 must stay too
    assert db.execute("SELECT COUNT(*) FROM a").rows == [(2,)]
    assert db.execute("SELECT COUNT(*) FROM b").rows == [(3,)]
    assert db.execute("DELETE FROM a WHERE id = 1").rowcount == 1
    assert db.execute("SELECT id FROM b").rows == [(3,)]
    assert db.execute("SELECT id FROM c").rows == [(2,)]


@check("v2: foreign keys: definition errors and DROP TABLE")
def _():
    from minidb import Database
    db = Database()
    db.execute("CREATE TABLE p (id INTEGER PRIMARY KEY, x INTEGER, u INTEGER UNIQUE)")
    for sql in ["CREATE TABLE k (a INTEGER REFERENCES nope(id))", "CREATE TABLE k (a INTEGER REFERENCES p(x))",
                "CREATE TABLE k (a INTEGER REFERENCES p(zzz))"]:
        raises(db, sql)
    db.execute("CREATE TABLE k (a INTEGER REFERENCES p(u))")
    raises(db, "DROP TABLE p")
    db.execute("DROP TABLE k")
    db.execute("DROP TABLE p")


@check("v2: foreign keys and transactions")
def _():
    db = _fk_db()
    db.execute("BEGIN")
    db.execute("DELETE FROM parent WHERE id = 2")
    assert db.execute("SELECT COUNT(*) FROM kid_c").rows == [(1,)]
    db.execute("ROLLBACK")
    assert db.execute("SELECT COUNT(*) FROM kid_c").rows == [(3,)]
    assert db.execute("SELECT COUNT(*) FROM parent").rows == [(3,)]


@check("v2: views, foreign keys, altered tables and window queries persist")
def _():
    from minidb import Database
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "v2.db")
        db = Database(path)
        db.executescript(SETUP + SETUP2)
        db.executescript("""
            CREATE VIEW oslo AS SELECT id, name FROM cust WHERE city = 'oslo';
            CREATE TABLE kid (id INTEGER PRIMARY KEY, cid INTEGER REFERENCES cust(id) ON DELETE CASCADE);
            INSERT INTO kid VALUES (1, 1), (2, 2);
            ALTER TABLE dept ADD COLUMN size INTEGER DEFAULT 7;
            ALTER TABLE dept RENAME COLUMN title TO label;
        """)
        db.close()
        db2 = Database(path)
        assert db2.execute("SELECT name FROM oslo ORDER BY id").rows == [("ann co",), ("cat inc",)]
        assert db2.execute("SELECT code, label, size FROM dept WHERE code = 'fin'").rows == [("fin", "Finance", 7)]
        raises(db2, "INSERT INTO kid VALUES (3, 99)")
        db2.execute("DELETE FROM cust WHERE id = 1")
        assert db2.execute("SELECT id FROM kid").rows == [(2,)]
        assert db2.execute("SELECT id, SUM(amount) OVER (ORDER BY id) FROM ord WHERE id < 3 ORDER BY id").rows == [(1, 50.0), (2, 120.0)]
        db2.close()


@check("v2: the command line prints the new statements")
def _():
    r = _cli("CREATE TABLE t (a INTEGER);\nINSERT INTO t VALUES (1), (2);\n"
             "SELECT a, ROW_NUMBER() OVER (ORDER BY a DESC) AS n FROM t UNION ALL SELECT 9, 9 ORDER BY 1;\n"
             "ALTER TABLE t ADD COLUMN b INTEGER DEFAULT 4;\nSELECT * FROM t WHERE EXISTS (SELECT 1 FROM t WHERE a = 2);\n"
             "SELECT CASE WHEN 1 = 1 THEN 'x' END AS c;\n")
    assert r.returncode == 0, r.stderr
    want = ["OK", "OK, 2 rows affected"] + _table(["a", "n"], [(1, 2), (2, 1), (9, 9)]) + ["OK"] + \
        _table(["a", "b"], [(1, 4), (2, 4)]) + _table(["c"], [("x",)])
    assert r.stdout.splitlines() == want, r.stdout


def main(argv):
    if argv and argv[0] == "--list":
        for i, (name, _) in enumerate(CHECKS, 1):
            print(f"{i:3d} {name}")
        return 0
    only = {int(a) for a in argv if a.isdigit()}
    ok = 0
    ran = 0
    for i, (name, fn) in enumerate(CHECKS, 1):
        if only and i not in only:
            continue
        ran += 1
        try:
            fn()
        except Exception as e:                                  # noqa: BLE001
            msg = (str(e) or type(e).__name__).splitlines()[0][:150]
            print(f"NOT DONE {i:3d} {name}: {type(e).__name__}: {msg}")
        else:
            ok += 1
            print(f"done     {i:3d} {name}")
    print(f"{ok} of {ran} checks pass")
    return 0 if ok == ran else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
