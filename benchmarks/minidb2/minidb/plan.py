"""Tree helpers and name resolution used by the executor."""

from __future__ import annotations

import dataclasses

from . import ast as A
from .errors import SQLError
from .expr import AGGREGATES


def children(n):
    if isinstance(n, A.Unary):
        return [n.operand]
    if isinstance(n, A.Binary):
        return [n.left, n.right]
    if isinstance(n, (A.IsNull, A.InSelect)):
        return [n.operand]
    if isinstance(n, A.Like):
        return [n.operand, n.pattern]
    if isinstance(n, A.InList):
        return [n.operand] + list(n.items)
    if isinstance(n, A.Between):
        return [n.operand, n.low, n.high]
    if isinstance(n, A.Func):
        return list(n.args)
    return []


def walk(n):
    yield n
    for c in children(n):
        yield from walk(c)


def map_children(n, f):
    if isinstance(n, A.Unary):
        return dataclasses.replace(n, operand=f(n.operand))
    if isinstance(n, A.Binary):
        return dataclasses.replace(n, left=f(n.left), right=f(n.right))
    if isinstance(n, (A.IsNull, A.InSelect)):
        return dataclasses.replace(n, operand=f(n.operand))
    if isinstance(n, A.Like):
        return dataclasses.replace(n, operand=f(n.operand), pattern=f(n.pattern))
    if isinstance(n, A.InList):
        return dataclasses.replace(n, operand=f(n.operand), items=[f(i) for i in n.items])
    if isinstance(n, A.Between):
        return dataclasses.replace(n, operand=f(n.operand), low=f(n.low), high=f(n.high))
    if isinstance(n, A.Func):
        return dataclasses.replace(n, args=[f(a) for a in n.args])
    return n


def rewrite(n, f):
    """f(node) returns a replacement, or None to descend into the node."""
    r = f(n)
    if r is not None:
        return r
    return map_children(n, lambda c: rewrite(c, f))


def is_agg(n):
    return isinstance(n, A.Func) and n.name in AGGREGATES


def has_agg(n):
    return any(is_agg(x) for x in walk(n))


def conjuncts(n):
    if n is None:
        return []
    if isinstance(n, A.Binary) and n.op == "AND":
        return conjuncts(n.left) + conjuncts(n.right)
    return [n]


def literal_of(n):
    """(True, value) when n is a non-NULL literal (a negated number counts)."""
    if isinstance(n, A.Literal) and n.value is not None:
        return True, n.value
    if isinstance(n, A.Unary) and n.op == "-" and isinstance(n.operand, A.Literal):
        v = n.operand.value
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return True, -v
    return False, None


class Scope:
    """The tables of a FROM clause, in order, with the position of each column in the combined row."""

    def __init__(self):
        self.tables = []                   # (alias, Table, offset)
        self.width = 0
        self.index = {}                    # (alias, column) -> position in the combined row

    def add(self, alias, table):
        if any(a == alias for a, _, _ in self.tables):
            raise SQLError(f"table name {alias} used twice; give one of them an alias")
        self.tables.append((alias, table, self.width))
        for i, c in enumerate(table.columns):
            self.index[(alias, c.name)] = self.width + i
        self.width += len(table.columns)

    def find(self, qual, name):
        """The alias that owns the column; SQLError when unknown or ambiguous."""
        if qual is not None:
            for alias, t, _ in self.tables:
                if alias == qual:
                    if name not in t._pos:
                        raise SQLError(f"no such column: {qual}.{name}")
                    return alias
            raise SQLError(f"no such table: {qual}")
        found = [alias for alias, t, _ in self.tables if name in t._pos]
        if not found:
            raise SQLError(f"no such column: {name}")
        if len(found) > 1:
            raise SQLError(f"ambiguous column name: {name}")
        return found[0]

    def qualify(self, node):
        """A copy of the expression in which every column carries the alias of its table."""
        def f(n):
            if isinstance(n, A.Column):
                return A.Column(n.name, self.find(n.table, n.name))
            return None
        return rewrite(node, f)
