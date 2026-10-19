"""Token handling shared by the parser."""

from __future__ import annotations

import re

from .errors import SQLError
from .lexer import tokenize

_Q = chr(39)
_TEXT_NOISE = re.compile(_Q + "(?:[^" + _Q + "]|" + _Q + _Q + ")*" + _Q + "|--[^\n]*|\s+")


def clean_text(s):
    """Source text of an expression: comments dropped, whitespace collapsed to single spaces, lowercased."""
    def sub(m):
        t = m.group(0)
        return t if t.startswith(_Q) else " "
    return _TEXT_NOISE.sub(sub, s).strip().lower()


class TokenStream:
    def __init__(self, sql):
        self.sql = sql
        self.toks = tokenize(sql)
        self.i = 0

    def peek(self, k=0):
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def next(self):
        t = self.toks[self.i]
        if t.kind != "EOF":
            self.i += 1
        return t

    def error(self, msg=None):
        t = self.peek()
        shown = "end of input" if t.kind == "EOF" else repr(t.value)
        raise SQLError(f"syntax error at position {t.pos} near {shown}" + (f": {msg}" if msg else ""))

    def at(self, kind, *values, k=0):
        t = self.peek(k)
        return t.kind == kind and (not values or t.value in values)

    def accept(self, kind, *values):
        if self.at(kind, *values):
            return self.next()
        return None

    def expect(self, kind, *values):
        t = self.accept(kind, *values)
        if t is None:
            self.error("expected " + (" or ".join(values) if values else kind))
        return t

    def kw(self, *words):
        return self.accept("KEYWORD", *words)

    def expect_kw(self, word):
        return self.expect("KEYWORD", word)

    def ident(self):
        return self.expect("IDENT").value
