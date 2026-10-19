"""Evaluates expression nodes: evaluate(node, resolve, subquery=None)."""

from __future__ import annotations

import functools
import math
import re

from . import ast as A
from .errors import SQLError
from .functions import call_scalar
from .values import compare, is_number, type_name

AGGREGATES = frozenset({"COUNT", "SUM", "AVG", "MIN", "MAX"})


def _need_bool(v, what):
    if v is not None and not isinstance(v, bool):
        raise SQLError(f"{what} needs a boolean, got {type_name(v)}")


def _and(a, b):
    if a is False or b is False:
        return False
    return None if a is None or b is None else True


def _arith(op, a, b):
    if a is None or b is None:
        return None
    if not (is_number(a) and is_number(b)):
        raise SQLError(f"operator {op} needs numbers, got {type_name(a)} and {type_name(b)}")
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if b == 0:
        raise SQLError("division by zero")
    both_int = isinstance(a, int) and isinstance(b, int)
    if op == "/":
        if both_int:
            q = abs(a) // abs(b)
            return q if (a < 0) == (b < 0) else -q
        return a / b
    if both_int:                                           # "%": sign of the left operand
        r = abs(a) % abs(b)
        return -r if a < 0 else r
    return math.fmod(a, b)


def _text(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


@functools.lru_cache(maxsize=256)
def _like_regex(pattern):
    out = []
    for ch in pattern:
        out.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
    return re.compile("".join(out), re.DOTALL)


def _like(v, pattern):
    if v is None or pattern is None:
        return None
    if not isinstance(v, str) or not isinstance(pattern, str):
        raise SQLError("LIKE needs text on both sides")
    return _like_regex(pattern).fullmatch(v) is not None


def _in(v, values):
    """SQL IN over an iterable of candidate values (three-valued)."""
    if v is None:
        return None
    seen_null = False
    for c in values:
        if c is None:
            seen_null = True
        elif compare(v, c) == 0:
            return True
    return None if seen_null else False


def _not(v):
    return None if v is None else not v


def _one_column(rows):
    for r in rows:
        if len(r) != 1:
            raise SQLError("subquery must return exactly one column")


def _numeric_text(s):
    s = s.strip()
    try:
        return int(s)
    except ValueError:
        pass
    try:
        f = float(s)
    except ValueError:
        raise SQLError(f"cannot cast {s!r} to a number") from None
    if not math.isfinite(f):
        raise SQLError(f"cannot cast {s!r} to a number")
    return f


def cast_value(v, ty):
    if v is None:
        return None
    if ty == "TEXT":
        return _text(v) if isinstance(v, bool) else str(v)
    if ty == "BOOLEAN":
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            return v != 0
        if isinstance(v, str) and v.strip().lower() in ("true", "false"):
            return v.strip().lower() == "true"
        raise SQLError(f"cannot cast {type_name(v)} {v!r} to BOOLEAN")
    if isinstance(v, str):
        v = _numeric_text(v)
    elif isinstance(v, float) and not math.isfinite(v) and ty == "INTEGER":
        raise SQLError("cannot cast a non-finite number to INTEGER")
    if ty == "INTEGER":
        return int(v)                                      # truncates toward zero; bool gives 0/1
    if ty == "REAL":
        return float(v)
    raise SQLError(f"unknown type {ty}")


def evaluate(node, resolve, subquery=None):
    def ev(n):
        return evaluate(n, resolve, subquery)

    if isinstance(node, A.Literal):
        return node.value
    if isinstance(node, A.Column):
        return resolve(node.table, node.name)
    if isinstance(node, A.Unary):
        v = ev(node.operand)
        if node.op == "NOT":
            _need_bool(v, "NOT")
            return _not(v)
        if v is None:
            return None
        if not is_number(v):
            raise SQLError(f"unary minus needs a number, got {type_name(v)}")
        return -v
    if isinstance(node, A.Binary):
        op = node.op
        if op == "AND":
            a = ev(node.left)
            _need_bool(a, "AND")
            if a is False:
                return False
            b = ev(node.right)
            _need_bool(b, "AND")
            return _and(a, b)
        if op == "OR":
            a = ev(node.left)
            _need_bool(a, "OR")
            if a is True:
                return True
            b = ev(node.right)
            _need_bool(b, "OR")
            if b is True:
                return True
            return None if a is None or b is None else False
        a, b = ev(node.left), ev(node.right)
        if op in ("+", "-", "*", "/", "%"):
            return _arith(op, a, b)
        if op == "||":
            return None if a is None or b is None else _text(a) + _text(b)
        if a is None or b is None:
            return None
        c = compare(a, b)
        if op == "=":
            return c == 0
        if op == "<>":
            return c != 0
        if op == "<":
            return c < 0
        if op == "<=":
            return c <= 0
        if op == ">":
            return c > 0
        if op == ">=":
            return c >= 0
        raise SQLError(f"unknown operator {op}")
    if isinstance(node, A.IsNull):
        return (ev(node.operand) is None) != node.negated
    if isinstance(node, A.InList):
        v = ev(node.operand)
        if v is None:
            return None
        r = _in(v, (ev(i) for i in node.items))
        return _not(r) if node.negated else r
    if isinstance(node, A.InSelect):
        if subquery is None:
            raise SQLError("subqueries are not allowed here")
        v = ev(node.operand)
        rows = subquery(node.select)
        _one_column(rows)
        r = _in(v, (row[0] for row in rows))
        return _not(r) if node.negated else r
    if isinstance(node, A.Between):
        v, lo, hi = ev(node.operand), ev(node.low), ev(node.high)
        ge = None if v is None or lo is None else compare(v, lo) >= 0
        le = None if v is None or hi is None else compare(v, hi) <= 0
        r = _and(ge, le)
        return _not(r) if node.negated else r
    if isinstance(node, A.Like):
        r = _like(ev(node.operand), ev(node.pattern))
        return _not(r) if node.negated else r
    if isinstance(node, A.Func):
        if node.name in AGGREGATES:
            raise SQLError(f"aggregate function {node.name} is not allowed here")
        if node.over is not None:
            raise SQLError(f"window function {node.name} is not allowed here")
        if node.name == "IIF" and len(node.args) == 3:      # lazy: only the chosen branch is evaluated
            c = ev(node.args[0])
            _need_bool(c, "IIF")
            return ev(node.args[1] if c is True else node.args[2])
        return call_scalar(node.name, [ev(a) for a in node.args])
    if isinstance(node, A.Case):
        if node.operand is not None:
            base = ev(node.operand)
            for w, r in node.whens:
                wv = ev(w)
                if base is not None and wv is not None and compare(base, wv) == 0:
                    return ev(r)
        else:
            for w, r in node.whens:
                wv = ev(w)
                _need_bool(wv, "CASE WHEN")
                if wv is True:
                    return ev(r)
        return None if node.else_ is None else ev(node.else_)
    if isinstance(node, A.Cast):
        return cast_value(ev(node.operand), node.type)
    if isinstance(node, A.Exists):
        if subquery is None:
            raise SQLError("subqueries are not allowed here")
        return (len(subquery(node.select)) > 0) != node.negated
    if isinstance(node, A.ScalarSelect):
        if subquery is None:
            raise SQLError("subqueries are not allowed here")
        rows = subquery(node.select)
        _one_column(rows)
        if len(rows) > 1:
            raise SQLError("scalar subquery returned more than one row")
        return rows[0][0] if rows else None
    raise SQLError(f"cannot evaluate {type(node).__name__}")
