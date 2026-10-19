import unittest

from minidb import SQLError
from minidb.lexer import tokenize


class LexerTest(unittest.TestCase):
    def test_kinds_and_values(self):
        toks = [(t.kind, t.value) for t in tokenize("select Name, 'ab', 12, 3.5 -- c\n")]
        self.assertEqual(toks, [("KEYWORD", "SELECT"), ("IDENT", "name"), ("PUNCT", ","), ("STRING", "ab"),
                                ("PUNCT", ","), ("NUMBER", 12), ("PUNCT", ","), ("NUMBER", 3.5), ("EOF", "")])

    def test_quote_escape(self):
        self.assertEqual(tokenize("'a" + chr(39) * 2 + "b'")[0].value, "a" + chr(39) + "b")

    def test_operators(self):
        ops = [t.value for t in tokenize("<> != <= >= || + - * / % =") if t.kind == "OP"]
        self.assertEqual(ops, ["<>", "!=", "<=", ">=", "||", "+", "-", "*", "/", "%", "="])

    def test_errors(self):
        for bad in ["'abc", "1 # 2", "@x", "1abc", "a | b"]:
            with self.assertRaises(SQLError):
                tokenize(bad)


if __name__ == "__main__":
    unittest.main()
