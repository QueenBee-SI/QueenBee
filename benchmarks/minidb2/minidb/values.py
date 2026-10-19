"""Helpers shared by the expression evaluator, the scalar functions and the executor: typing and comparison."""

from __future__ import annotations

from .errors import SQLError


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def type_name(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "real"
    return "text"


def compare(a, b):
    """-1, 0 or 1 for two non-NULL values; SQLError when their types cannot be compared."""
    if isinstance(a, bool) or isinstance(b, bool):
        ok = isinstance(a, bool) and isinstance(b, bool)
    elif is_number(a) and is_number(b):
        ok = True
    else:
        ok = isinstance(a, str) and isinstance(b, str)
    if not ok:
        raise SQLError(f"cannot compare {type_name(a)} with {type_name(b)}")
    return (a > b) - (a < b)


def hash_key(v):
    """A hashable stand-in for a value (so that 1 and 1.0 group together but TRUE does not join them)."""
    return ("bool", v) if isinstance(v, bool) else v
