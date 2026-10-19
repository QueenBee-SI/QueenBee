import unittest

from minidb import SQLError, ast as A
from minidb.expr import evaluate
from minidb.functions import call_scalar


def ev(node):
    return evaluate(node, lambda t, c: None)


def lit(v):
    return A.Literal(v)


def b(op, x, y):
    return ev(A.Binary(op, lit(x), lit(y)))


class FunctionsTest(unittest.TestCase):
    def test_round_half_away(self):
        self.assertEqual(call_scalar("ROUND", [2.5]), 3.0)
        self.assertEqual(call_scalar("ROUND", [-2.5]), -3.0)
        self.assertEqual(call_scalar("ROUND", [2.25, 1]), 2.3)
        self.assertIsInstance(call_scalar("ROUND", [4]), float)

    def test_substr_and_null(self):
        self.assertEqual(call_scalar("SUBSTR", ["hello", 2, 3]), "ell")
        self.assertEqual(call_scalar("SUBSTR", ["hello", 4]), "lo")
        self.assertIsNone(call_scalar("UPPER", [None]))
        self.assertEqual(call_scalar("COALESCE", [None, None, 3]), 3)
        self.assertIsNone(call_scalar("NULLIF", [1, 1]))

    def test_errors(self):
        for name, args in [("NOPE", [1]), ("UPPER", []), ("UPPER", [1]), ("LENGTH", [1, 2])]:
            with self.assertRaises(SQLError):
                call_scalar(name, args)


class ExprTest(unittest.TestCase):
    def test_division(self):
        self.assertEqual(b("/", -7, 2), -3)
        self.assertEqual(b("%", -7, 3), -1)
        self.assertEqual(b("/", 7, 2.0), 3.5)
        with self.assertRaises(SQLError):
            b("/", 1, 0)

    def test_three_valued_logic(self):
        self.assertIs(b("AND", None, False), False)
        self.assertIs(b("OR", None, True), True)
        self.assertIsNone(b("=", None, 1))
        self.assertIsNone(ev(A.InList(lit(1), [lit(2), lit(None)])))

    def test_type_errors(self):
        with self.assertRaises(SQLError):
            b("=", True, 1)
        with self.assertRaises(SQLError):
            b("+", "a", 1)

    def test_like_and_concat(self):
        self.assertTrue(ev(A.Like(lit("cat"), lit("_a%"))))
        self.assertFalse(ev(A.Like(lit("Cat"), lit("c%"))))
        self.assertEqual(b("||", "a", 1), "a1")


if __name__ == "__main__":
    unittest.main()
