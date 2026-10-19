import unittest

from minidb import SQLError, ast as A
from minidb.parser import parse


class ParserTest(unittest.TestCase):
    def test_precedence(self):
        e = parse("SELECT 1 + 2 * 3")[0].items[0].expr
        self.assertEqual(e, A.Binary("+", A.Literal(1), A.Binary("*", A.Literal(2), A.Literal(3))))

    def test_concat_below_additive(self):
        e = parse("SELECT 1 + 2 || 3")[0].items[0].expr
        self.assertEqual(e.op, "||")

    def test_not_equal_and_logic(self):
        w = parse("SELECT a FROM t WHERE x = 1 OR y != 2 AND z = 3")[0].where
        self.assertEqual(w.op, "OR")
        self.assertEqual(w.right.left.op, "<>")

    def test_item_text(self):
        item = parse("SELECT Salary  *  2 FROM t")[0].items[0]
        self.assertEqual(item.text, "salary * 2")

    def test_statements_and_empty(self):
        kinds = [type(s).__name__ for s in parse(";; BEGIN; COMMIT;; ROLLBACK")]
        self.assertEqual(kinds, ["Begin", "Commit", "Rollback"])

    def test_create_table(self):
        s = parse("CREATE TABLE IF NOT EXISTS t (a INTEGER PRIMARY KEY, b TEXT NOT NULL DEFAULT 'x', c REAL UNIQUE)")[0]
        self.assertTrue(s.if_not_exists)
        self.assertTrue(s.columns[0].primary_key)
        self.assertEqual(s.columns[1].default, "x")
        self.assertTrue(s.columns[2].unique)

    def test_errors(self):
        for bad in ["SELECT FROM t", "SELECT 1 1", "CREATE TABLE t ()", "SELECT (1", "UPDATE t SET",
                    "CREATE TABLE t (a BLOB)", "SELECT a FROM t LIMIT 1.5"]:
            with self.assertRaises(SQLError, msg=bad):
                parse(bad)


if __name__ == "__main__":
    unittest.main()
