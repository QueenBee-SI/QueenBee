"""Checks MiniDB 3 through its public API: the 154 checks of versions 1 and 2 (from check_minidb.py) and the checks of the
nine features of TASK.md. `python check_minidb3.py` runs all, `python check_minidb3.py N` one, `--list` lists them.
Prints one line per check and a last line "N of M checks pass".
"""

from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import check_minidb as v2  # noqa: E402  (the 154 earlier checks, and the helpers)

seeded, norm, exact, raises, check = v2.seeded, v2.norm, v2.exact, v2.raises, v2.check
CHECKS = v2.CHECKS

# (sql, expected rows) on the seeded emp/dept tables of check_minidb.py. With ORDER BY the order must match exactly;
# without it rows are compared as a multiset. Validated against SQLite 3.45 where SQLite has the feature.
QUERIES3 = [
    # 21. NULLS FIRST / NULLS LAST
    ("SELECT name FROM emp ORDER BY salary NULLS LAST, name", [("eve",), ("cat",), ("bob",), ("fay",), ("ann",), ("dan",)]),
    ("SELECT name FROM emp ORDER BY salary DESC NULLS FIRST, name", [("dan",), ("ann",), ("bob",), ("fay",), ("cat",), ("eve",)]),
    ("SELECT name FROM emp ORDER BY salary DESC NULLS LAST, name", [("ann",), ("bob",), ("fay",), ("cat",), ("eve",), ("dan",)]),
    ("SELECT name FROM emp ORDER BY boss NULLS LAST, id", [("bob",), ("cat",), ("eve",), ("fay",), ("dan",), ("ann",)]),
    ("SELECT salary FROM emp WHERE dept = 'eng' UNION SELECT salary FROM emp WHERE dept = 'ops' ORDER BY 1 NULLS LAST",
     [(70.0,), (80.0,), (100.0,), (None,)]),
    ("SELECT name, ROW_NUMBER() OVER (ORDER BY salary DESC NULLS LAST) FROM emp ORDER BY 2",
     [("ann", 1), ("bob", 2), ("fay", 3), ("cat", 4), ("eve", 5), ("dan", 6)]),
    # 22. GROUP_CONCAT and FILTER
    ("SELECT GROUP_CONCAT(name) FROM emp", [("ann,bob,cat,dan,eve,fay",)]),
    ("SELECT GROUP_CONCAT(name, ' | ') FROM emp WHERE dept = 'eng'", [("ann | bob | fay",)]),
    ("SELECT dept, GROUP_CONCAT(salary) FROM emp GROUP BY dept ORDER BY dept",
     [("eng", "100.0,80.0,80.0"), ("hr", "60.0"), ("ops", "70.0")]),
    ("SELECT GROUP_CONCAT(DISTINCT dept) FROM emp", [("eng,ops,hr",)]),
    ("SELECT GROUP_CONCAT(salary) FROM emp WHERE id = 4", [(None,)]),
    ("SELECT COUNT(*) FILTER (WHERE salary > 70), SUM(salary) FILTER (WHERE dept = 'eng') FROM emp", [(3, 260.0)]),
    ("SELECT dept, COUNT(*), COUNT(*) FILTER (WHERE salary IS NULL) FROM emp GROUP BY dept ORDER BY dept",
     [("eng", 3, 0), ("hr", 1, 0), ("ops", 2, 1)]),
    ("SELECT name, SUM(salary) FILTER (WHERE dept = 'eng') OVER (ORDER BY id) FROM emp ORDER BY id",
     [("ann", 100.0), ("bob", 180.0), ("cat", 180.0), ("dan", 180.0), ("eve", 180.0), ("fay", 260.0)]),
    ("SELECT MAX(salary) FILTER (WHERE dept = 'fin') FROM emp", [(None,)]),
    ("SELECT dept, GROUP_CONCAT(name) OVER (PARTITION BY dept ORDER BY id) FROM emp ORDER BY id",
     [("eng", "ann"), ("eng", "ann,bob"), ("ops", "cat"), ("ops", "cat,dan"), ("hr", "eve"), ("eng", "ann,bob,fay")]),
    # 23. New scalar functions
    ("SELECT MOD(7, 3), MOD(-7, 3), POWER(2, 3), POWER(2, 0.5)", [(1, -1, 8.0, 1.4142135623730951)]),
    ("SELECT SIGN(-5), SIGN(0), SIGN(2.5), CEIL(2.1), CEIL(-2.1), FLOOR(2.9), FLOOR(-2.1), CEIL(2)", [(-1, 0, 1, 3, -2, 2, -3, 2)]),
    ("SELECT REPEAT('ab', 3), REPEAT('ab', 0), REVERSE('abc'), LPAD('7', 3, '0'), RPAD('ab', 5, 'xy'), LPAD('abcdef', 3, '0')",
     [("ababab", "", "cba", "007", "abxyx", "abc")]),
    ("SELECT CONCAT_WS('-', 'a', NULL, 'b', 1), CONCAT_WS(NULL, 'a'), CONCAT_WS('-', NULL, NULL)", [("a-b-1", None, "")]),
    ("SELECT MOD(NULL, 2), REVERSE(NULL), LPAD('a', NULL, 'x'), SIGN(NULL)", [(None, None, None, None)]),
    ("SELECT name, POWER(salary, 2) FROM emp WHERE id = 2", [("bob", 6400.0)]),
    ("SELECT REVERSE(name) FROM emp WHERE id = 3", [("tac",)]),
    ("SELECT name FROM emp WHERE MOD(id, 2) = 0 ORDER BY id", [("bob",), ("dan",), ("fay",)]),
    # 28. Recursive CTEs
    ("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 5) SELECT x FROM n", [(1,), (2,), (3,), (4,), (5,)]),
    ("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 5) SELECT SUM(x), COUNT(*) FROM n", [(15, 5)]),
    ("WITH RECURSIVE chain(id, name, lvl) AS (SELECT id, name, 0 FROM emp WHERE boss IS NULL UNION ALL "
     "SELECT e.id, e.name, c.lvl + 1 FROM emp e JOIN chain c ON e.boss = c.id) SELECT name, lvl FROM chain ORDER BY lvl, name",
     [("ann", 0), ("bob", 1), ("cat", 1), ("eve", 1), ("dan", 2), ("fay", 2)]),
    ("WITH RECURSIVE t(x) AS (SELECT 1 UNION SELECT (x % 3) + 1 FROM t) SELECT x FROM t ORDER BY x", [(1,), (2,), (3,)]),
    ("WITH RECURSIVE n AS (SELECT 1 AS v UNION ALL SELECT v * 2 FROM n WHERE v < 20) SELECT v FROM n", [(1,), (2,), (4,), (8,), (16,), (32,)]),
    ("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 3), m AS (SELECT x * 10 AS y FROM n) "
     "SELECT y FROM m ORDER BY y", [(10,), (20,), (30,)]),
    ("WITH RECURSIVE under(id) AS (SELECT id FROM emp WHERE name = 'ann' UNION ALL SELECT e.id FROM emp e JOIN under u ON e.boss = u.id) "
     "SELECT COUNT(*) FROM under", [(6,)]),
    ("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 4) SELECT x FROM n ORDER BY x DESC LIMIT 2", [(4,), (3,)]),
]

for _sql, _want in QUERIES3:
    CHECKS.append(("v3 query: " + _sql.replace("\n", " ")[:70], v2.make_query_check(_sql, _want)))


@check("v3: a recursive CTE names its columns")
def _():
    r = seeded().execute("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 2) SELECT * FROM n")
    assert r.columns == ["x"], r.columns
    assert norm(r.rows) == norm([(1,), (2,)])


@check("v3: a recursive CTE that never ends is an SQLError, not a hang")
def _():
    raises(seeded(), "WITH RECURSIVE t(x) AS (SELECT 1 UNION ALL SELECT x FROM t) SELECT COUNT(*) FROM t")


@check("v3: the recursive step may not order or aggregate")
def _():
    db = seeded()
    raises(db, "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 3 ORDER BY x) SELECT x FROM n")
    raises(db, "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT MAX(x) + 1 FROM n WHERE x < 3) SELECT x FROM n")


@check("v3: FILTER on a scalar function, and DISTINCT with a separator, are SQLErrors")
def _():
    db = seeded()
    raises(db, "SELECT UPPER(name) FILTER (WHERE id = 1) FROM emp")
    raises(db, "SELECT GROUP_CONCAT(DISTINCT name, '-') FROM emp")


@check("v3: wrong types and division by zero in the new functions are SQLErrors")
def _():
    db = seeded()
    raises(db, "SELECT REPEAT(1, 'x')")
    raises(db, "SELECT CEIL('a')")
    raises(db, "SELECT MOD(1, 0)")


# ---- 24. CHECK constraints and composite UNIQUE -----------------------------------------------------------------

ITEMS = """
CREATE TABLE item (id INTEGER PRIMARY KEY, sku TEXT NOT NULL, qty INTEGER CHECK (qty > 0), price REAL,
  CHECK (price >= 0), UNIQUE (sku, qty));
INSERT INTO item VALUES (1, 'a', 2, 1.5), (2, 'a', 3, 2.5), (3, 'b', NULL, NULL);
"""


def items():
    db = seeded()
    db.executescript(ITEMS)
    return db


@check("v3: a row that breaks a CHECK is refused and nothing changes")
def _():
    db = items()
    raises(db, "INSERT INTO item VALUES (4, 'c', 0, 1.0)")
    raises(db, "INSERT INTO item VALUES (4, 'c', 1, -1.0)")
    assert db.execute("SELECT COUNT(*) FROM item").rows == [(3,)]
    db.execute("INSERT INTO item VALUES (4, 'c', NULL, NULL)")        # NULL passes a CHECK
    assert db.execute("SELECT COUNT(*) FROM item").rows == [(4,)]


@check("v3: UPDATE is checked too, and keeps the old value on failure")
def _():
    db = items()
    raises(db, "UPDATE item SET qty = -1 WHERE id = 1")
    raises(db, "UPDATE item SET price = price - 10 WHERE id = 2")
    exact(db.execute("SELECT qty, price FROM item WHERE id IN (1, 2) ORDER BY id").rows, [(2, 1.5), (3, 2.5)])
    db.execute("UPDATE item SET qty = 9 WHERE id = 1")
    assert db.execute("SELECT qty FROM item WHERE id = 1").rows == [(9,)]


@check("v3: a composite UNIQUE refuses the same pair, allows NULLs, and DO NOTHING skips")
def _():
    db = items()
    raises(db, "INSERT INTO item VALUES (5, 'a', 2, 9.0)")
    db.execute("INSERT INTO item VALUES (5, 'a', 4, 9.0)")
    db.execute("INSERT INTO item VALUES (6, 'b', NULL, 1.0)")           # NULL in the pair never conflicts
    r = db.execute("INSERT INTO item VALUES (7, 'a', 2, 1.0) ON CONFLICT DO NOTHING")
    assert r.rowcount == 0, r.rowcount
    assert db.execute("SELECT COUNT(*) FROM item").rows == [(5,)]
    raises(db, "UPDATE item SET qty = 2 WHERE id = 5")


@check("v3: INSERT ... SELECT that breaks a CHECK inserts nothing")
def _():
    db = items()
    db.executescript("CREATE TABLE src (id INTEGER, sku TEXT, qty INTEGER, price REAL); "
                     "INSERT INTO src VALUES (10, 'x', 1, 1.0), (11, 'y', 0, 1.0);")
    raises(db, "INSERT INTO item SELECT * FROM src")
    assert db.execute("SELECT COUNT(*) FROM item").rows == [(3,)]


@check("v3: CHECK and composite UNIQUE survive a transaction and persist")
def _():
    db = items()
    db.execute("BEGIN")
    raises(db, "INSERT INTO item VALUES (8, 'z', 0, 1.0)")
    db.execute("INSERT INTO item VALUES (8, 'z', 1, 1.0)")
    db.execute("ROLLBACK")
    assert db.execute("SELECT COUNT(*) FROM item").rows == [(3,)]
    from minidb import Database
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "items.db")
        f = Database(path)
        f.executescript(v2.SETUP + ITEMS)
        f.close()
        g = Database(path)
        raises(g, "INSERT INTO item VALUES (9, 'q', 0, 1.0)")
        raises(g, "INSERT INTO item VALUES (9, 'a', 2, 1.0)")
        g.execute("INSERT INTO item VALUES (9, 'q', 1, 1.0)")
        assert g.execute("SELECT COUNT(*) FROM item").rows == [(4,)]
        g.close()


@check("v3: ALTER TABLE ADD COLUMN with a CHECK, and the composite UNIQUE under RENAME and DROP COLUMN")
def _():
    db = items()
    db.execute("ALTER TABLE item ADD COLUMN weight REAL CHECK (weight > 0)")      # NULL for the existing rows: passes
    raises(db, "ALTER TABLE item ADD COLUMN d INTEGER DEFAULT 0 CHECK (d > 0)")   # the existing rows would fail it
    assert db.execute("SELECT COUNT(*) FROM item").rows == [(3,)]
    raises(db, "UPDATE item SET weight = -1 WHERE id = 1")
    db.execute("ALTER TABLE item RENAME COLUMN sku TO code")
    raises(db, "INSERT INTO item (id, code, qty, price) VALUES (5, 'a', 2, 1.0)")
    raises(db, "ALTER TABLE item DROP COLUMN qty")


# ---- 25. RETURNING -------------------------------------------------------------------------------------------------

@check("v3: INSERT ... RETURNING gives the inserted rows")
def _():
    db = seeded()
    r = db.execute("INSERT INTO dept VALUES ('qa', 'Quality'), ('it', 'IT') RETURNING code, title")
    assert r.columns == ["code", "title"], r.columns
    exact(r.rows, [("qa", "Quality"), ("it", "IT")])
    assert r.rowcount == 2, r.rowcount


@check("v3: UPDATE and DELETE ... RETURNING give the new and the old rows")
def _():
    db = seeded()
    r = db.execute("UPDATE emp SET salary = salary + 10 WHERE dept = 'ops' RETURNING id, salary")
    exact(r.rows, [(3, 80.0), (4, None)])
    assert r.rowcount == 2
    r = db.execute("DELETE FROM emp WHERE dept = 'hr' RETURNING *")
    exact(r.rows, [(5, "eve", "hr", 60.0, 1)])
    assert r.rowcount == 1 and db.execute("SELECT COUNT(*) FROM emp").rows == [(5,)]


@check("v3: RETURNING with upserts and INSERT ... SELECT")
def _():
    db = seeded()
    r = db.execute("INSERT INTO dept VALUES ('eng', 'X'), ('qa', 'Quality') ON CONFLICT DO NOTHING RETURNING code")
    exact(r.rows, [("qa",)])
    assert r.rowcount == 1
    r = db.execute("INSERT INTO dept VALUES ('eng', 'Eng2') ON CONFLICT (code) DO UPDATE SET title = excluded.title RETURNING title")
    exact(r.rows, [("Eng2",)])
    r = db.execute("INSERT INTO dept SELECT dept, dept FROM emp WHERE dept = 'hr' RETURNING code")
    exact(r.rows, [("hr",)])


@check("v3: RETURNING takes aliases and expressions; without it the result is empty as before")
def _():
    db = seeded()
    r = db.execute("UPDATE dept SET title = 'T' WHERE code = 'fin' RETURNING title AS t, UPPER(code)")
    assert r.columns[0] == "t", r.columns
    exact(r.rows, [("T", "FIN")])
    r = db.execute("UPDATE dept SET title = 'U' WHERE code = 'fin'")
    assert r.rows == [] and r.columns == [] and r.rowcount == 1


@check("v3: RETURNING a row that breaks a CHECK returns nothing and changes nothing")
def _():
    db = items()
    raises(db, "INSERT INTO item VALUES (4, 'c', 0, 1.0) RETURNING id")
    raises(db, "UPDATE item SET qty = 0 WHERE id = 1 RETURNING qty")
    assert db.execute("SELECT qty FROM item WHERE id = 1").rows == [(2,)]


# ---- 26. Savepoints -------------------------------------------------------------------------------------------------

@check("v3: ROLLBACK TO a savepoint undoes what came after it and keeps the transaction open")
def _():
    db = seeded()
    db.execute("BEGIN")
    db.execute("INSERT INTO dept VALUES ('a', 'A')")
    db.execute("SAVEPOINT s1")
    db.execute("INSERT INTO dept VALUES ('b', 'B')")
    db.execute("ROLLBACK TO s1")
    assert db.execute("SELECT COUNT(*) FROM dept").rows == [(4,)]
    db.execute("INSERT INTO dept VALUES ('c', 'C')")
    db.execute("RELEASE s1")
    db.execute("COMMIT")
    exact(db.execute("SELECT code FROM dept WHERE code IN ('a', 'b', 'c') ORDER BY code").rows, [("a",), ("c",)])


@check("v3: savepoints nest; RELEASE forgets the later ones")
def _():
    db = seeded()
    db.execute("BEGIN")
    db.execute("SAVEPOINT s1")
    db.execute("INSERT INTO dept VALUES ('a', 'A')")
    db.execute("SAVEPOINT s2")
    db.execute("INSERT INTO dept VALUES ('b', 'B')")
    db.execute("ROLLBACK TO s1")
    assert db.execute("SELECT COUNT(*) FROM dept").rows == [(3,)]
    raises(db, "RELEASE s2")                                   # gone with the rollback to s1
    db.execute("ROLLBACK TO s1")                               # s1 itself is still there
    db.execute("SAVEPOINT s3")
    db.execute("RELEASE SAVEPOINT s1")
    raises(db, "ROLLBACK TO SAVEPOINT s3")                     # released together with s1
    db.execute("COMMIT")


@check("v3: savepoints outside a transaction, and unknown names, are SQLErrors")
def _():
    db = seeded()
    raises(db, "SAVEPOINT s1")
    raises(db, "RELEASE s1")
    db.execute("BEGIN")
    raises(db, "ROLLBACK TO nothere")
    db.execute("COMMIT")
    raises(db, "SAVEPOINT s1")


@check("v3: ROLLBACK TO undoes schema changes too; the same name twice means the newest")
def _():
    db = seeded()
    db.execute("BEGIN")
    db.execute("SAVEPOINT s")
    db.execute("CREATE TABLE tmp (x INTEGER)")
    db.execute("INSERT INTO tmp VALUES (1)")
    db.execute("SAVEPOINT s")
    db.execute("INSERT INTO tmp VALUES (2)")
    db.execute("ROLLBACK TO s")
    assert db.execute("SELECT COUNT(*) FROM tmp").rows == [(1,)]
    db.execute("ROLLBACK TO s")
    db.execute("RELEASE s")
    db.execute("ROLLBACK TO s")                                # the outer s
    raises(db, "SELECT * FROM tmp")
    db.execute("ROLLBACK")


@check("v3: a plain ROLLBACK after savepoints undoes everything; COMMIT keeps it")
def _():
    db = seeded()
    db.execute("BEGIN")
    db.execute("INSERT INTO dept VALUES ('a', 'A')")
    db.execute("SAVEPOINT s1")
    db.execute("INSERT INTO dept VALUES ('b', 'B')")
    db.execute("ROLLBACK")
    assert db.execute("SELECT COUNT(*) FROM dept").rows == [(3,)]
    db.execute("BEGIN")
    db.execute("SAVEPOINT s1")
    db.execute("INSERT INTO dept VALUES ('b', 'B')")
    db.execute("COMMIT")
    assert db.execute("SELECT COUNT(*) FROM dept").rows == [(4,)]
    raises(db, "ROLLBACK TO s1")


@check("v3: a failing statement inside a savepoint leaves the savepoint usable")
def _():
    db = seeded()
    db.execute("BEGIN")
    db.execute("SAVEPOINT s1")
    db.execute("INSERT INTO dept VALUES ('a', 'A')")
    raises(db, "INSERT INTO dept VALUES ('a', 'again')")
    assert db.execute("SELECT COUNT(*) FROM dept").rows == [(4,)]
    db.execute("ROLLBACK TO s1")
    assert db.execute("SELECT COUNT(*) FROM dept").rows == [(3,)]
    db.execute("COMMIT")


# ---- 27. CREATE TABLE ... AS -----------------------------------------------------------------------------------------

@check("v3: CREATE TABLE AS copies the rows in the query's order")
def _():
    db = seeded()
    db.execute("CREATE TABLE eng AS SELECT id, name, salary FROM emp WHERE dept = 'eng' ORDER BY id DESC")
    r = db.execute("SELECT * FROM eng")
    assert r.columns == ["id", "name", "salary"], r.columns
    exact(r.rows, [(6, "fay", 80.0), (2, "bob", 80.0), (1, "ann", 100.0)])


@check("v3: CREATE TABLE AS takes the names and the types from the query")
def _():
    db = seeded()
    db.execute("CREATE TABLE t2 AS SELECT name AS n, salary * 2 AS d, boss FROM emp")
    assert db.execute("SELECT n, d FROM t2 WHERE n = 'bob'").rows == [("bob", 160.0)]
    raises(db, "INSERT INTO t2 VALUES ('x', 'not a number', NULL)")
    db.execute("INSERT INTO t2 VALUES ('x', 1.5, NULL)")
    assert db.execute("SELECT COUNT(*) FROM t2").rows == [(7,)]
    db.execute("CREATE TABLE t3 AS SELECT boss FROM emp WHERE id = 1")      # only NULL: TEXT
    db.execute("INSERT INTO t3 VALUES ('a')")
    assert db.execute("SELECT COUNT(*) FROM t3").rows == [(2,)]


@check("v3: CREATE TABLE AS with WITH and a set operation")
def _():
    db = seeded()
    db.execute("CREATE TABLE codes AS WITH c AS (SELECT code FROM dept) SELECT code FROM c UNION SELECT dept FROM emp")
    assert db.execute("SELECT COUNT(*) FROM codes").rows == [(4,)]
    exact(db.execute("SELECT code FROM codes ORDER BY code").rows, [("eng",), ("fin",), ("hr",), ("ops",)])


@check("v3: CREATE TABLE AS refuses a used name and duplicate columns; IF NOT EXISTS keeps the old table")
def _():
    db = seeded()
    raises(db, "CREATE TABLE emp AS SELECT 1 AS x")
    db.execute("CREATE VIEW v AS SELECT 1 AS x")
    raises(db, "CREATE TABLE v AS SELECT 1 AS x")
    raises(db, "CREATE TABLE dup AS SELECT id, id FROM emp")
    db.execute("CREATE TABLE IF NOT EXISTS dept AS SELECT 1 AS x")
    assert db.execute("SELECT COUNT(*) FROM dept").rows == [(3,)]


@check("v3: a table made with CREATE TABLE AS persists and rolls back")
def _():
    from minidb import Database
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "ctas.db")
        f = Database(path)
        f.executescript(v2.SETUP)
        f.execute("CREATE TABLE eng AS SELECT id, name FROM emp WHERE dept = 'eng'")
        f.close()
        g = Database(path)
        assert g.execute("SELECT COUNT(*) FROM eng").rows == [(3,)]
        g.execute("BEGIN")
        g.execute("CREATE TABLE ops AS SELECT id FROM emp WHERE dept = 'ops'")
        assert g.execute("SELECT COUNT(*) FROM ops").rows == [(2,)]
        g.execute("ROLLBACK")
        raises(g, "SELECT * FROM ops")
        g.close()


# ---- 29. UPDATE ... FROM ------------------------------------------------------------------------------------------

@check("v3: UPDATE ... FROM takes values from the matching source row")
def _():
    db = seeded()
    r = db.execute("UPDATE emp SET dept = d.title FROM dept d WHERE emp.dept = d.code")
    assert r.rowcount == 5, r.rowcount
    exact(db.execute("SELECT dept FROM emp ORDER BY id").rows,
          [("Engineering",), ("Engineering",), ("Operations",), ("Operations",), ("hr",), ("Engineering",)])


@check("v3: UPDATE ... FROM a derived table")
def _():
    db = seeded()
    db.execute("UPDATE dept SET title = CAST(s.n AS TEXT) FROM (SELECT dept AS code, COUNT(*) AS n FROM emp GROUP BY dept) s "
               "WHERE dept.code = s.code")
    exact(db.execute("SELECT code, title FROM dept ORDER BY code").rows, [("eng", "3"), ("fin", "Finance"), ("ops", "2")])


@check("v3: UPDATE ... FROM uses the first matching source row, once per target row")
def _():
    db = seeded()
    r = db.execute("UPDATE dept SET title = e.name FROM emp e WHERE e.dept = dept.code")
    assert r.rowcount == 2, r.rowcount
    exact(db.execute("SELECT code, title FROM dept ORDER BY code").rows, [("eng", "ann"), ("fin", "Finance"), ("ops", "cat")])


@check("v3: UPDATE ... FROM with RETURNING, with no match, and with the target in the source")
def _():
    db = seeded()
    r = db.execute("UPDATE dept SET title = e.name FROM emp e WHERE e.dept = dept.code AND e.id = 6 RETURNING code, title")
    exact(r.rows, [("eng", "fay")])
    r = db.execute("UPDATE dept SET title = e.name FROM emp e WHERE e.dept = 'nowhere'")
    assert r.rowcount == 0
    raises(db, "UPDATE emp SET salary = 1 FROM emp e2 WHERE e2.id = emp.id")


@check("v3: UPDATE ... FROM is checked like any update")
def _():
    db = items()
    db.executescript("CREATE TABLE fix (id INTEGER, q INTEGER); INSERT INTO fix VALUES (1, 0), (2, 7);")
    raises(db, "UPDATE item SET qty = fix.q FROM fix WHERE fix.id = item.id")
    exact(db.execute("SELECT qty FROM item WHERE id IN (1, 2) ORDER BY id").rows, [(2,), (3,)])
    db.execute("UPDATE item SET qty = fix.q FROM fix WHERE fix.id = item.id AND fix.q > 0")
    exact(db.execute("SELECT qty FROM item WHERE id IN (1, 2) ORDER BY id").rows, [(2,), (7,)])


@check("v3: the command line runs the new statements")
def _():
    r = v2._cli("CREATE TABLE n AS WITH RECURSIVE s(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM s WHERE x < 3) SELECT x FROM s;\n"
                "SELECT GROUP_CONCAT(x, '+') FROM n;")
    assert "1+2+3" in r.stdout, r.stdout + r.stderr


def main(argv):
    return v2.main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
