"""The scalar functions: call_scalar(name, args)."""

from __future__ import annotations

import decimal

from .errors import SQLError
from .values import compare, is_number, type_name


def _need_text(name, v):
    if not isinstance(v, str):
        raise SQLError(f"{name} needs text, got {type_name(v)}")


def _need_number(name, v):
    if not is_number(v):
        raise SQLError(f"{name} needs a number, got {type_name(v)}")


def _need_int(name, v):
    if not isinstance(v, int) or isinstance(v, bool):
        raise SQLError(f"{name} needs an integer, got {type_name(v)}")


def _round(x, n=0):
    _need_number("ROUND", x)
    _need_int("ROUND", n)
    ctx = decimal.Context(prec=400, rounding=decimal.ROUND_HALF_UP)
    d = decimal.Decimal(repr(x))
    if not d.is_finite():
        return float(x)
    return float(d.quantize(decimal.Decimal(1).scaleb(-n), rounding=decimal.ROUND_HALF_UP, context=ctx))


def _substr(s, start, length=None):
    _need_text("SUBSTR", s)
    _need_int("SUBSTR", start)
    if length is None:
        return s[max(start, 1) - 1:]
    _need_int("SUBSTR", length)
    if length < 0:
        return ""
    end = start + length                      # 1-based, exclusive
    return s[max(start, 1) - 1:max(end, 1) - 1]


def _abs(x):
    _need_number("ABS", x)
    return abs(x)


def _length(s):
    _need_text("LENGTH", s)
    return len(s)


def _upper(s):
    _need_text("UPPER", s)
    return s.upper()


def _lower(s):
    _need_text("LOWER", s)
    return s.lower()


def _coalesce(*args):
    for a in args:
        if a is not None:
            return a
    return None


def _nullif(a, b):
    if a is None or b is None:
        return a
    return None if compare(a, b) == 0 else a


def _replace(s, a, b):
    _need_text("REPLACE", s)
    _need_text("REPLACE", a)
    _need_text("REPLACE", b)
    return s.replace(a, b) if a else s


def _trim(s):
    _need_text("TRIM", s)
    return s.strip(" ")


def _ltrim(s):
    _need_text("LTRIM", s)
    return s.lstrip(" ")


def _rtrim(s):
    _need_text("RTRIM", s)
    return s.rstrip(" ")


def _instr(s, t):
    _need_text("INSTR", s)
    _need_text("INSTR", t)
    return s.find(t) + 1


def _iif(c, a, b):
    if c is not None and not isinstance(c, bool):
        raise SQLError(f"IIF needs a boolean condition, got {type_name(c)}")
    return a if c is True else b


# name -> (function, min args, max args, propagates NULL)
_FUNCTIONS = {
    "UPPER": (_upper, 1, 1, True),
    "LOWER": (_lower, 1, 1, True),
    "LENGTH": (_length, 1, 1, True),
    "ABS": (_abs, 1, 1, True),
    "ROUND": (_round, 1, 2, True),
    "SUBSTR": (_substr, 2, 3, True),
    "COALESCE": (_coalesce, 1, None, False),
    "NULLIF": (_nullif, 2, 2, False),
    "REPLACE": (_replace, 3, 3, True),
    "TRIM": (_trim, 1, 1, True),
    "LTRIM": (_ltrim, 1, 1, True),
    "RTRIM": (_rtrim, 1, 1, True),
    "INSTR": (_instr, 2, 2, True),
    "IIF": (_iif, 3, 3, False),
}


def call_scalar(name, args):
    name = name.upper()
    entry = _FUNCTIONS.get(name)
    if entry is None:
        raise SQLError(f"unknown function {name}")
    fn, lo, hi, null_propagates = entry
    if len(args) < lo or (hi is not None and len(args) > hi):
        raise SQLError(f"wrong number of arguments for {name}: got {len(args)}")
    if null_propagates and any(a is None for a in args):
        return None
    return fn(*args)
