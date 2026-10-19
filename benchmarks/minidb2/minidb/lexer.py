"""Turns SQL text into tokens."""

from __future__ import annotations

from typing import Any, NamedTuple

from .errors import SQLError

KEYWORDS = frozenset("""SELECT FROM WHERE GROUP BY HAVING ORDER ASC DESC LIMIT OFFSET DISTINCT AS JOIN INNER LEFT OUTER ON
AND OR NOT NULL IS IN BETWEEN LIKE TRUE FALSE CREATE TABLE DROP IF EXISTS INDEX UNIQUE PRIMARY KEY DEFAULT INSERT INTO
VALUES UPDATE SET DELETE BEGIN COMMIT ROLLBACK EXPLAIN""".split())

_TWO_CHAR_OPS = ("<>", "!=", "<=", ">=", "||")
_ONE_CHAR_OPS = "=<>+-*/%"
_PUNCT = "(),.;"
_DIGITS = "0123456789"


class Token(NamedTuple):
    kind: str          # KEYWORD IDENT NUMBER STRING OP PUNCT EOF
    value: Any
    pos: int


def tokenize(sql: str) -> list[Token]:
    out: list[Token] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
        elif c == "'":
            j, parts = i + 1, []
            while True:
                k = sql.find("'", j)
                if k < 0:
                    raise SQLError(f"unterminated string literal at position {i}")
                parts.append(sql[j:k])
                if sql.startswith("''", k):
                    parts.append("'")
                    j = k + 2
                else:
                    j = k + 1
                    break
            out.append(Token("STRING", "".join(parts), i))
            i = j
        elif c in _DIGITS:
            j = i
            while j < n and sql[j] in _DIGITS:
                j += 1
            if j + 1 < n and sql[j] == "." and sql[j + 1] in _DIGITS:
                j += 1
                while j < n and sql[j] in _DIGITS:
                    j += 1
                value: Any = float(sql[i:j])
            else:
                value = int(sql[i:j])
            if j < n and (sql[j].isalpha() or sql[j] == "_"):
                raise SQLError(f"malformed number at position {i}")
            out.append(Token("NUMBER", value, i))
            i = j
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            word = sql[i:j]
            if word.upper() in KEYWORDS:
                out.append(Token("KEYWORD", word.upper(), i))
            else:
                out.append(Token("IDENT", word.lower(), i))
            i = j
        elif sql[i:i + 2] in _TWO_CHAR_OPS:
            out.append(Token("OP", sql[i:i + 2], i))
            i += 2
        elif c in _ONE_CHAR_OPS:
            out.append(Token("OP", c, i))
            i += 1
        elif c in _PUNCT:
            out.append(Token("PUNCT", c, i))
            i += 1
        else:
            raise SQLError(f"unexpected character {c!r} at position {i}")
    out.append(Token("EOF", "", n))
    return out
