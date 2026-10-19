"""Window functions: compute_window(func, rows, eval_expr) -> one value per row, aligned with rows."""

from __future__ import annotations

import functools

from . import ast as A
from .errors import SQLError
from .values import compare, hash_key, is_number, type_name

RANKING = frozenset({"ROW_NUMBER", "RANK", "DENSE_RANK"})
OFFSETTING = frozenset({"LAG", "LEAD"})
AGGREGATES = frozenset({"COUNT", "SUM", "AVG", "MIN", "MAX"})


def _cmp_keys(keys_a, keys_b, order):
    """Compares two lists of order-by values: NULLs first ascending, last descending."""
    for a, b, item in zip(keys_a, keys_b, order):
        if a is None and b is None:
            continue
        if a is None:
            c = -1
        elif b is None:
            c = 1
        else:
            c = compare(a, b)
        if c:
            return -c if item.descending else c
    return 0


def _partitions(spec, rows, eval_expr):
    """Lists of row indexes per partition, in order of first appearance, each in row order."""
    groups: dict = {}
    for i, row in enumerate(rows):
        key = tuple(hash_key(eval_expr(e, row)) for e in spec.partition_by)
        groups.setdefault(key, []).append(i)
    return list(groups.values())


def _need_count(name, v, what):
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise SQLError(f"{name} {what} must be a non-negative integer, got {v!r}" if is_number(v)
                       else f"{name} {what} must be an integer, got {type_name(v)}")
    return v


def _aggregate(func, values):
    """The aggregate over the values (already collected for the frame); NULLs are skipped except for COUNT(*)."""
    name = func.name
    if func.star:
        return len(values)
    vals = [v for v in values if v is not None]
    if func.distinct:
        seen, uniq = set(), []
        for v in vals:
            k = hash_key(v)
            if k not in seen:
                seen.add(k)
                uniq.append(v)
        vals = uniq
    if name == "COUNT":
        return len(vals)
    if not vals:
        return None
    if name in ("SUM", "AVG"):
        for v in vals:
            if not is_number(v):
                raise SQLError(f"{name} needs numbers, got {type_name(v)}")
        total = sum(vals)
        return total if name == "SUM" else total / len(vals)
    best = vals[0]
    for v in vals[1:]:
        c = compare(v, best)
        if (c < 0) if name == "MIN" else (c > 0):
            best = v
    return best


def compute_window(func: A.Func, rows: list, eval_expr) -> list:
    spec = func.over
    if spec is None:
        raise SQLError(f"{func.name} is not a window function call")
    name = func.name
    if name not in RANKING and name not in OFFSETTING and name not in AGGREGATES:
        raise SQLError(f"{name} cannot be used as a window function")
    if name in RANKING and (func.args or func.star or func.distinct):
        raise SQLError(f"{name} takes no arguments")
    if name in OFFSETTING and not 1 <= len(func.args) <= 3:
        raise SQLError(f"wrong number of arguments for {name}: got {len(func.args)}")
    if name in AGGREGATES and not func.star and len(func.args) != 1:
        raise SQLError(f"wrong number of arguments for {name}: got {len(func.args)}")
    if func.star and name != "COUNT":
        raise SQLError(f"{name}(*) is not allowed")

    out: list = [None] * len(rows)
    for part in _partitions(spec, rows, eval_expr):
        if spec.order_by:
            keys = {i: [eval_expr(o.expr, rows[i]) for o in spec.order_by] for i in part}
            part = sorted(part, key=functools.cmp_to_key(lambda a, b: _cmp_keys(keys[a], keys[b], spec.order_by)))
            # peer groups: runs of equal order keys
            groups: list[list[int]] = []
            for i in part:
                if groups and _cmp_keys(keys[groups[-1][0]], keys[i], spec.order_by) == 0:
                    groups[-1].append(i)
                else:
                    groups.append([i])
        else:
            groups = [part]

        if name == "ROW_NUMBER":
            for n, i in enumerate(part, 1):
                out[i] = n
        elif name == "RANK":
            n = 1
            for g in groups:
                for i in g:
                    out[i] = n
                n += len(g)
        elif name == "DENSE_RANK":
            for n, g in enumerate(groups, 1):
                for i in g:
                    out[i] = n
        elif name in OFFSETTING:
            sign = -1 if name == "LAG" else 1
            for pos, i in enumerate(part):
                off = 1
                if len(func.args) > 1:
                    off = eval_expr(func.args[1], rows[i])
                    if off is None:
                        raise SQLError(f"{name} offset must not be NULL")
                    off = _need_count(name, off, "offset")
                j = pos + sign * off
                if 0 <= j < len(part):
                    out[i] = eval_expr(func.args[0], rows[part[j]])
                elif len(func.args) > 2:
                    out[i] = eval_expr(func.args[2], rows[i])
        else:
            if func.star:
                values = lambda i: 1
            else:
                values = lambda i: eval_expr(func.args[0], rows[i])
            if not spec.order_by:
                result = _aggregate(func, [values(i) for i in part])
                for i in part:
                    out[i] = result
            else:
                frame: list = []                       # running frame: first row up to the last peer
                for g in groups:
                    frame.extend(values(i) for i in g)
                    result = _aggregate(func, frame)
                    for i in g:
                        out[i] = result
    return out
