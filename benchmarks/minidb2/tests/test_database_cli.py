import os
import subprocess
import sys
import tempfile
import unittest

from minidb import Database, SQLError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, g TEXT, v INTEGER);"
                              "INSERT INTO t VALUES (1,'a',10),(2,'a',20),(3,'b',NULL)")

    def q(self, sql):
        return self.db.execute(sql).rows

    def test_group_and_order(self):
        self.assertEqual(self.q("SELECT g, SUM(v), COUNT(*) FROM t GROUP BY g ORDER BY g DESC"),
                         [("b", None, 1), ("a", 30, 2)])
        self.assertEqual(self.q("SELECT v FROM t ORDER BY v"), [(None,), (10,), (20,)])

    def test_atomic_statement(self):
        with self.assertRaises(SQLError):
            self.db.execute("UPDATE t SET v = v / 0")
        with self.assertRaises(SQLError):
            self.db.execute("INSERT INTO t VALUES (9,'z',1),(1,'dup',1)")
        self.assertEqual(self.q("SELECT COUNT(*) FROM t"), [(3,)])

    def test_pk_shift_update(self):
        self.db.execute("UPDATE t SET id = id + 1")
        self.assertEqual(self.q("SELECT id FROM t"), [(2,), (3,), (4,)])

    def test_transaction(self):
        self.db.execute("BEGIN")
        self.db.execute("DELETE FROM t")
        self.db.execute("ROLLBACK")
        self.assertEqual(self.q("SELECT COUNT(*) FROM t"), [(3,)])
        with self.assertRaises(SQLError):
            self.db.execute("COMMIT")

    def test_index_plan_and_rows_read(self):
        self.assertEqual(self.q("EXPLAIN SELECT * FROM t WHERE id = 2"), [("INDEX SCAN t USING pk_t",)])
        before = self.db.rows_read
        self.q("SELECT * FROM t WHERE id = 2")
        self.assertEqual(self.db.rows_read - before, 1)

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.db")
            db = Database(path)
            db.execute("CREATE TABLE a (x INTEGER UNIQUE)")
            db.execute("INSERT INTO a VALUES (1)")
            db.close()
            db2 = Database(path)
            self.assertEqual(db2.execute("SELECT x FROM a").rows, [(1,)])
            with self.assertRaises(SQLError):
                db2.execute("INSERT INTO a VALUES (1)")


class CliTest(unittest.TestCase):
    def test_cli_output(self):
        sql = "CREATE TABLE t (a INTEGER, b TEXT);\nINSERT INTO t VALUES (1,'x'),(2,NULL);\nSELECT * FROM t;\nSELEC 1;\n"
        r = subprocess.run([sys.executable, "-m", "minidb"], input=sql, capture_output=True, text=True, cwd=ROOT)
        lines = r.stdout.splitlines()
        self.assertEqual(lines[:2], ["OK", "OK, 2 rows affected"])
        self.assertEqual(lines[2:7], ["a | b", "--+-----", "1 | x", "2 | NULL", "(2 rows)"])
        self.assertTrue(lines[7].startswith("Error: "))
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
