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
        return list(n.args) + _over_exprs(n)
    if isinstance(n, A.Case):
        out = [] if n.operand is None else [n.operand]
        for w, t in n.whens:
            out += [w, t]
        return out + ([] if n.else_ is None else [n.else_])
    if isinstance(n, A.Cast):
        return [n.operand]
    return []


def _over_exprs(f):
    if f.over is None:
        return []
    return list(f.over.partition_by) + [o.expr for o in f.over.order_by]


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
        over = n.over
        if over is not None:
            over = A.WindowSpec([f(e) for e in over.partition_by],
                                [dataclasses.replace(o, expr=f(o.expr)) for o in over.order_by])
        return dataclasses.replace(n, args=[f(a) for a in n.args], over=over)
    if isinstance(n, A.Case):
        return dataclasses.replace(
            n, operand=None if n.operand is None else f(n.operand),
            whens=[(f(w), f(t)) for w, t in n.whens], else_=None if n.else_ is None else f(n.else_))
    if isinstance(n, A.Cast):
        return dataclasses.replace(n, operand=f(n.operand))
    return n


def rewrite(n, f):
    """f(node) returns a replacement, or None to descend into the node."""
    r = f(n)
    if r is not None:
        return r
    return map_children(n, lambda c: rewrite(c, f))


def is_agg(n):
    return isinstance(n, A.Func) and n.name in AGGREGATES and n.over is None


def has_agg(n):
    return any(is_agg(x) for x in walk(n))


def is_window(n):
    return isinstance(n, A.Func) and n.over is not None


def has_window(n):
    return any(is_window(x) for x in walk(n))


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


class _Col:
    __slots__ = ("name", "type")

    def __init__(self, name):
        self.name = name
        self.type = None


class Relation:
    """A table-like source made of rows computed earlier (derived table, CTE, view)."""

    def __init__(self, name, names, rows):
        self.name = name
        self.columns = [_Col(n) for n in names]
        self.rows = rows
        self._pos = {}
        self.dups = set()
        for i, n in enumerate(names):
            if n in self._pos:
                self.dups.add(n)
            else:
                self._pos[n] = i


class Scope:
    """The tables of a FROM clause, in order, with the position of each column in the combined row.
    `parent` is the scope of the enclosing query (correlated subqueries); `level` numbers the nesting."""

    def __init__(self, parent=None, level=0):
        self.tables = []                   # (alias, Table, offset)
        self.width = 0
        self.index = {}                    # (alias, column) -> position in the combined row
        self.parent = parent
        self.level = level
        self.uses_outer = False

    def add(self, alias, table):
        if any(a == alias for a, _, _ in self.tables):
            raise SQLError(f"table name {alias} used twice; give one of them an alias")
        self.tables.append((alias, table, self.width))
        for i, c in enumerate(table.columns):
            self.index.setdefault((alias, c.name), self.width + i)
        self.width += len(table.columns)

    def lookup(self, qual, name):
        """The alias that owns the column, None when this scope has no such table/column; SQLError when ambiguous."""
        if qual is not None:
            for alias, t, _ in self.tables:
                if alias == qual:
                    if name not in t._pos:
                        raise SQLError(f"no such column: {qual}.{name}")
                    if name in getattr(t, "dups", ()):
                        raise SQLError(f"ambiguous column name: {qual}.{name}")
                    return alias
            return None
        found = [alias for alias, t, _ in self.tables if name in t._pos]
        if len(found) > 1 or any(name in getattr(t, "dups", ()) for _, t, _ in self.tables):
            raise SQLError(f"ambiguous column name: {name}")
        return found[0] if found else None

    def find(self, qual, name):
        """The alias that owns the column; SQLError when unknown or ambiguous."""
        alias = self.lookup(qual, name)
        if alias is None:
            raise SQLError(f"no such table: {qual}" if qual is not None else f"no such column: {name}")
        return alias

    def resolve(self, col):
        """A Column that names its table by alias; columns of enclosing queries get the table '#level:alias'."""
        alias = self.lookup(col.table, col.name)
        if alias is not None:
            return A.Column(col.name, alias)
        scope = self.parent
        while scope is not None:
            alias = scope.lookup(col.table, col.name)
            if alias is not None:
                self.uses_outer = True
                return A.Column(col.name, f"#{scope.level}:{alias}")
            scope = scope.parent
        return A.Column(col.name, self.find(col.table, col.name))

    def qualify(self, node):
        """A copy of the expression in which every column carries the alias of its table."""
        def f(n):
            if isinstance(n, A.Column):
                return self.resolve(n)
            return None
        return rewrite(node, f)
