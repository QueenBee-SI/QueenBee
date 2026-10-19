import unittest

from minidb.database import Database
from minidb.errors import SQLError


class ExecutorV2Test(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.executescript("""
            CREATE TABLE emp (id INTEGER PRIMARY KEY, name TEXT, dept INTEGER, sal INTEGER);
            CREATE TABLE dept (id INTEGER PRIMARY KEY, dname TEXT);
            INSERT INTO emp VALUES (1,'a',1,10),(2,'b',1,20),(3,'c',2,30),(4,'d',NULL,40);
            INSERT INTO dept VALUES (1,'x'),(2,'y'),(3,'z');
        """)

    def q(self, sql):
        return self.db.execute(sql).rows

    def test_correlated(self):
        self.assertEqual(self.q("SELECT name FROM emp e WHERE EXISTS (SELECT 1 FROM dept d WHERE d.id = e.dept) ORDER BY id"),
                         [("a",), ("b",), ("c",)])
        self.assertEqual(self.q("SELECT name FROM emp e WHERE NOT EXISTS (SELECT 1 FROM dept d WHERE d.id = e.dept)"),
                         [("d",)])
        self.assertEqual(self.q("SELECT id, (SELECT dname FROM dept WHERE dept.id = emp.dept) FROM emp ORDER BY id"),
                         [(1, "x"), (2, "x"), (3, "y"), (4, None)])
        self.assertEqual(self.q("SELECT name FROM emp e WHERE sal > (SELECT AVG(sal) FROM emp WHERE dept = e.dept)"),
                         [("b",)])
        self.assertEqual(self.q("SELECT name FROM emp e WHERE id IN (SELECT id FROM emp WHERE sal > e.sal - 5 AND dept = e.dept)"
                                " ORDER BY id"), [("a",), ("b",), ("c",)])

    def test_correlated_depth_and_having(self):
        rows = self.q("SELECT dname FROM dept d WHERE EXISTS (SELECT 1 FROM emp e WHERE e.dept = d.id AND "
                      "EXISTS (SELECT 1 FROM emp f WHERE f.dept = d.id AND f.sal > e.sal)) ORDER BY dname")
        self.assertEqual(rows, [("x",)])
        rows = self.q("SELECT dept FROM emp e GROUP BY dept HAVING SUM(sal) > (SELECT sal FROM emp WHERE id = e.dept) ")
        self.assertEqual(rows, [(1,), (2,)])

    def test_derived_cte_view(self):
        self.assertEqual(self.q("SELECT s FROM (SELECT SUM(sal) AS s FROM emp) t"), [(100,)])
        self.assertEqual(self.q("WITH a AS (SELECT id FROM emp WHERE sal > 15), b AS (SELECT id FROM a WHERE id > 2) "
                                "SELECT id FROM b ORDER BY id"), [(3,), (4,)])
        self.assertEqual(self.q("WITH emp AS (SELECT 7 AS id) SELECT id FROM emp"), [(7,)])
        self.db.execute("CREATE VIEW rich AS SELECT id, name FROM emp WHERE sal >= 30")
        self.assertEqual(self.q("SELECT name FROM rich ORDER BY id"), [("c",), ("d",)])
        self.db.execute("INSERT INTO emp VALUES (5,'e',2,50)")
        self.assertEqual(len(self.q("SELECT * FROM rich")), 3)
        self.assertEqual(self.q("WITH rich AS (SELECT 1 AS id, 'q' AS name) SELECT name FROM rich"), [("q",)])
        with self.assertRaises(SQLError):
            self.db.execute("INSERT INTO rich VALUES (9,'z')")

    def test_set_operations(self):
        self.assertEqual(self.q("SELECT dept FROM emp UNION SELECT id FROM dept ORDER BY 1"),
                         [(None,), (1,), (2,), (3,)])
        self.assertEqual(len(self.q("SELECT dept FROM emp UNION ALL SELECT id FROM dept")), 7)
        self.assertEqual(self.q("SELECT id FROM dept INTERSECT SELECT dept FROM emp"), [(1,), (2,)])
        self.assertEqual(self.q("SELECT id FROM dept EXCEPT SELECT dept FROM emp"), [(3,)])
        self.assertEqual(self.q("SELECT id FROM dept UNION ALL SELECT id FROM dept ORDER BY id DESC LIMIT 2 OFFSET 1"),
                         [(3,), (2,)])
        self.assertEqual(self.q("WITH t AS (SELECT 1 AS v) SELECT v FROM t UNION SELECT 2"), [(1,), (2,)])
        with self.assertRaises(SQLError):
            self.q("SELECT id, dname FROM dept UNION SELECT id FROM dept")

    def test_joins(self):
        self.assertEqual(self.q("SELECT e.id, d.dname FROM emp e RIGHT JOIN dept d ON e.dept = d.id ORDER BY d.id, e.id"),
                         [(1, "x"), (2, "x"), (3, "y"), (None, "z")])
        self.assertEqual(len(self.q("SELECT * FROM emp CROSS JOIN dept")), 12)

    def test_windows(self):
        rows = self.q("SELECT id, ROW_NUMBER() OVER (PARTITION BY dept ORDER BY sal DESC) FROM emp ORDER BY id")
        self.assertEqual(rows, [(1, 2), (2, 1), (3, 1), (4, 1)])
        rows = self.q("SELECT dept, RANK() OVER (ORDER BY AVG(sal) DESC) FROM emp WHERE dept IS NOT NULL "
                      "GROUP BY dept ORDER BY dept")
        self.assertEqual(rows, [(1, 2), (2, 1)])
        rows = self.q("SELECT id, SUM(sal) OVER (ORDER BY id) FROM emp ORDER BY id DESC LIMIT 2")
        self.assertEqual(rows, [(4, 100), (3, 60)])
        for bad in ("SELECT id FROM emp WHERE ROW_NUMBER() OVER () = 1",
                    "SELECT id FROM emp GROUP BY ROW_NUMBER() OVER ()",
                    "SELECT dept FROM emp GROUP BY dept HAVING RANK() OVER () = 1"):
            with self.assertRaises(SQLError):
                self.q(bad)

    def test_dml_routing(self):
        self.db.execute("CREATE TABLE t (a INTEGER)")
        self.db.execute("INSERT INTO t SELECT id FROM dept")
        self.assertEqual(self.q("SELECT COUNT(*) FROM t"), [(3,)])
        self.db.execute("UPDATE emp SET sal = (SELECT COUNT(*) FROM dept WHERE id <= emp.id)")
        self.assertEqual(self.q("SELECT sal FROM emp ORDER BY id"), [(1,), (2,), (3,), (3,)])


if __name__ == "__main__":
    unittest.main()
