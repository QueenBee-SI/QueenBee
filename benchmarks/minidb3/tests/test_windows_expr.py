import unittest

from minidb import SQLError
from minidb.expr import evaluate
from minidb.functions import call_scalar
from minidb.parser import parse
from minidb.windows import compute_window


def window(sql_func, rows):
    """Computes `sql_func` (e.g. "RANK() OVER (ORDER BY g)") over dict rows."""
    func = parse("SELECT " + sql_func)[0].items[0].expr
    return compute_window(func, rows, lambda node, row: evaluate(node, lambda t, n: row[n]))


def ev(sql, row=None, subquery=None):
    node = parse("SELECT " + sql)[0].items[0].expr
    return evaluate(node, lambda t, n: (row or {})[n], subquery)


R = [{"g": "a", "v": 3}, {"g": "b", "v": 1}, {"g": "a", "v": 3}, {"g": "a", "v": 5}, {"g": "b", "v": None}]


class WindowTest(unittest.TestCase):
    def test_row_number(self):
        self.assertEqual(window("ROW_NUMBER() OVER ()", R), [1, 2, 3, 4, 5])
        self.assertEqual(window("ROW_NUMBER() OVER (PARTITION BY g)", R), [1, 1, 2, 3, 2])
        self.assertEqual(window("ROW_NUMBER() OVER (ORDER BY v DESC)", R), [2, 4, 3, 1, 5])

    def test_rank_and_dense_rank(self):
        self.assertEqual(window("RANK() OVER (ORDER BY v)", R), [3, 2, 3, 5, 1])
        self.assertEqual(window("DENSE_RANK() OVER (ORDER BY v)", R), [3, 2, 3, 4, 1])
        self.assertEqual(window("RANK() OVER (PARTITION BY g ORDER BY v DESC)", R), [2, 1, 2, 1, 2])
        self.assertEqual(window("RANK() OVER ()", R), [1] * 5)

    def test_lag_lead(self):
        rows = [{"v": x} for x in (10, 20, 30)]
        self.assertEqual(window("LAG(v) OVER (ORDER BY v)", rows), [None, 10, 20])
        self.assertEqual(window("LEAD(v, 2) OVER (ORDER BY v)", rows), [30, None, None])
        self.assertEqual(window("LAG(v, 1, -1) OVER (ORDER BY v)", rows), [-1, 10, 20])
        self.assertEqual(window("LEAD(v, 1, 0) OVER (ORDER BY v DESC)", rows), [0, 10, 20])

    def test_lag_over_partitions(self):
        self.assertEqual(window("LAG(v) OVER (PARTITION BY g ORDER BY v)", R), [None, None, 3, 3, None])

    def test_running_aggregates_and_peers(self):
        rows = [{"v": 1}, {"v": 2}, {"v": 2}, {"v": 3}]
        self.assertEqual(window("SUM(v) OVER (ORDER BY v)", rows), [1, 5, 5, 8])
        self.assertEqual(window("COUNT(*) OVER (ORDER BY v)", rows), [1, 3, 3, 4])
        self.assertEqual(window("SUM(v) OVER ()", rows), [8] * 4)
        self.assertEqual(window("AVG(v) OVER (ORDER BY v)", rows), [1.0, 5 / 3, 5 / 3, 2.0])
        self.assertEqual(window("MAX(v) OVER (ORDER BY v DESC)", rows), [3, 3, 3, 3])
        self.assertEqual(window("MIN(v) OVER (ORDER BY v DESC)", rows), [1, 2, 2, 3])

    def test_nulls(self):
        self.assertEqual(window("COUNT(v) OVER (PARTITION BY g)", R), [3, 1, 3, 3, 1])
        self.assertEqual(window("SUM(v) OVER (PARTITION BY g)", R), [11, 1, 11, 11, 1])
        rows = [{"g": None, "v": 1}, {"g": None, "v": 2}, {"g": 1, "v": 5}]
        self.assertEqual(window("SUM(v) OVER (PARTITION BY g)", rows), [3, 3, 5])
        self.assertEqual(window("ROW_NUMBER() OVER (ORDER BY g)", rows), [1, 2, 3])
        self.assertEqual(window("SUM(v) OVER (PARTITION BY g)", [{"g": 1, "v": None}]), [None])

    def test_errors(self):
        with self.assertRaises(SQLError):
            window("UPPER(g) OVER ()", R)
        with self.assertRaises(SQLError):
            window("SUM(v) OVER (PARTITION BY g ORDER BY g)", [{"g": 1, "v": "x"}])
        with self.assertRaises(SQLError):
            window("LAG(v, -1) OVER ()", R)


class CaseCastTest(unittest.TestCase):
    def test_case(self):
        self.assertEqual(ev("CASE WHEN a > 1 THEN 'big' ELSE 'small' END", {"a": 2}), "big")
        self.assertIsNone(ev("CASE WHEN a > 1 THEN 1 END", {"a": 0}))
        self.assertEqual(ev("CASE WHEN a > 1 THEN 1 ELSE 2 END", {"a": None}), 2)
        self.assertEqual(ev("CASE a WHEN 1 THEN 'one' WHEN 2 THEN 'two' END", {"a": 2}), "two")
        self.assertIsNone(ev("CASE a WHEN NULL THEN 1 END", {"a": None}))

    def test_cast_integer(self):
        self.assertEqual(ev("CAST(-2.7 AS INTEGER)"), -2)
        self.assertEqual(ev("CAST(2.7 AS INTEGER)"), 2)
        self.assertEqual(ev("CAST('12' AS INTEGER)"), 12)
        self.assertEqual(ev("CAST('2.5' AS INTEGER)"), 2)
        self.assertIsNone(ev("CAST(NULL AS INTEGER)"))
        with self.assertRaises(SQLError):
            ev("CAST('abc' AS INTEGER)")

    def test_cast_real_text_boolean(self):
        self.assertEqual(ev("CAST('2.5' AS REAL)"), 2.5)
        self.assertIsInstance(ev("CAST(3 AS REAL)"), float)
        self.assertEqual(ev("CAST(5 AS TEXT)"), "5")
        self.assertEqual(ev("CAST(2.0 AS TEXT)"), "2.0")
        self.assertIs(ev("CAST(0 AS BOOLEAN)"), False)
        self.assertIs(ev("CAST(7 AS BOOLEAN)"), True)
        with self.assertRaises(SQLError):
            ev("CAST(1.5 AS BOOLEAN)")
        with self.assertRaises(SQLError):
            ev("CAST('maybe' AS BOOLEAN)")

    def test_exists(self):
        self.assertIs(ev("EXISTS (SELECT 1)", subquery=lambda q: [(1,)]), True)
        self.assertIs(ev("NOT EXISTS (SELECT 1)", subquery=lambda q: [(1,)]), False)
        self.assertIs(ev("EXISTS (SELECT 1)", subquery=lambda q: []), False)

    def test_window_function_in_plain_expression_is_an_error(self):
        with self.assertRaises(SQLError):
            ev("ROW_NUMBER() OVER ()")


class ScalarFunctionTest(unittest.TestCase):
    def test_functions(self):
        self.assertEqual(call_scalar("REPLACE", ["banana", "an", "AN"]), "bANANa")
        self.assertEqual(call_scalar("TRIM", ["  a b  "]), "a b")
        self.assertEqual(call_scalar("LTRIM", ["  a "]), "a ")
        self.assertEqual(call_scalar("RTRIM", [" a  "]), " a")
        self.assertEqual(call_scalar("INSTR", ["hello", "ll"]), 3)
        self.assertEqual(call_scalar("INSTR", ["hello", "z"]), 0)
        self.assertEqual(call_scalar("IIF", [True, 1, 2]), 1)
        self.assertEqual(call_scalar("IIF", [None, 1, 2]), 2)
        self.assertIsNone(call_scalar("IIF", [False, 1, None]))

    def test_null_propagation_and_errors(self):
        self.assertIsNone(call_scalar("REPLACE", [None, "a", "b"]))
        self.assertIsNone(call_scalar("TRIM", [None]))
        self.assertIsNone(call_scalar("INSTR", ["a", None]))
        with self.assertRaises(SQLError):
            call_scalar("TRIM", [5])
        with self.assertRaises(SQLError):
            call_scalar("REPLACE", ["a", "b"])

    def test_iif_is_lazy(self):
        self.assertEqual(ev("IIF(1 = 1, 'ok', 1 / 0)"), "ok")


if __name__ == "__main__":
    unittest.main()
