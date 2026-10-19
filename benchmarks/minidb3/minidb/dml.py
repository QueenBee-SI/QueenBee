"""INSERT (VALUES / SELECT / ON CONFLICT), ALTER TABLE, CREATE / DROP VIEW. executor.execute hands these over."""

from __future__ import annotations

from . import ast as A
from .errors import SQLError
from .expr import evaluate
from .result import Result


def execute(storage, stmt, select=None, subquery=None):
    """Run one Insert / AlterTable / CreateView / DropView.

    select(query) -> (names, rows) runs a Select or Compound; subquery(sel) is the scalar-subquery callback for
    expr.evaluate. Both are optional for statements that do not need them."""
    if isinstance(stmt, A.Insert):
        return _insert(storage, stmt, select, subquery)
    if isinstance(stmt, A.AlterTable):
        return _alter(storage, stmt)
    if isinstance(stmt, A.CreateView):
        return _create_view(storage, stmt, select)
    if isinstance(stmt, A.DropView):
        storage.drop_view(stmt.name, stmt.if_exists)
        return Result(message="OK")
    raise SQLError(f"cannot execute {type(stmt).__name__} here")


def execute_dml(target, stmt):
    """Same, given a Storage, an Executor or a Database; the select side is taken from the executor."""
    from .executor import Executor
    from .storage import Storage
    if isinstance(target, Storage):
        ex = Executor(target)
    else:
        ex = getattr(target, "executor", target)
    storage = ex.storage
    run = getattr(ex, "run_query", None) or ex._select
    return execute(storage, stmt, run, getattr(ex, "_subquery", None))


# ---- INSERT
def _insert(storage, stmt, select, subquery):
    t = storage.table(stmt.table)
    if stmt.columns is None:
        positions = list(range(len(t.columns)))
    else:
        positions = [t.column_position(c) for c in stmt.columns]
        if len(set(positions)) != len(positions):
            raise SQLError("a column is listed twice")

    if stmt.select is not None:
        if select is None:
            raise SQLError("INSERT ... SELECT is not available here")
        _, source = select(stmt.select)
        source = [list(r) for r in source]
        for r in source:
            if len(r) != len(positions):
                raise SQLError(f"expected {len(positions)} values, got {len(r)}")
    else:
        def no_columns(table, column):
            raise SQLError(f"no such column: {column} (VALUES cannot use columns)")

        source = []
        for exprs in stmt.rows:
            if len(exprs) != len(positions):
                raise SQLError(f"expected {len(positions)} values, got {len(exprs)}")
            source.append([evaluate(e, no_columns, subquery) for e in exprs])

    oc = stmt.on_conflict
    unique_idx = None
    if oc is not None:
        if oc.action == "UPDATE" and oc.target is None:
            raise SQLError("ON CONFLICT DO UPDATE needs a conflict target: ON CONFLICT (column)")
        if oc.target is not None:
            t.column_position(oc.target)
            unique_idx = next((i for i in t.indexes.values() if i.unique and i.column == oc.target), None)
            if unique_idx is None:
                raise SQLError(f"ON CONFLICT target {oc.target} is not a PRIMARY KEY or UNIQUE column")
        for col, _ in oc.assignments:
            t.column_position(col)

    built = []
    for vals in source:
        row = [c.default for c in t.columns]
        for pos, v in zip(positions, vals):
            row[pos] = v
        built.append(tuple(row))

    snap = storage.snapshot() if len(built) > 1 or oc is not None else None
    count = 0
    try:
        for row in built:
            if oc is None:
                t.insert_row(row)
                count += 1
                continue
            row = t._check_row(row)
            rid = _conflict(t, row, unique_idx)
            if rid is None:
                t.insert_row(row)
                count += 1
            elif oc.action == "UPDATE":
                _upsert(t, rid, row, oc, subquery)
                count += 1
    except BaseException:
        if snap is not None:
            storage.restore(snap)
        raise
    return Result(message=f"OK, {count} rows affected", rowcount=count)


def _conflict(t, row, target):
    """Row id of the stored row that stands in the way of row, or None."""
    for idx in ([target] if target is not None else t.indexes.values()):
        if idx.unique and row[idx.pos] is not None:
            ids = idx.entries.get(row[idx.pos])
            if ids:
                return ids[0]
    return None


def _upsert(t, rid, new, oc, subquery):
    old = t.rows[rid]

    def resolve(table, column):
        if table == "excluded":
            return new[t.column_position(column)]
        if table is not None and table != t.name:
            raise SQLError(f"no such table: {table}")
        return old[t.column_position(column)]

    values = list(old)
    for col, expr in oc.assignments:
        values[t.column_position(col)] = evaluate(expr, resolve, subquery)
    t.update_rows([(rid, tuple(values))])


# ---- ALTER TABLE
def _alter(storage, stmt):
    act = stmt.action.upper()
    if act == "ADD COLUMN":
        storage.add_column(stmt.table, stmt.column)
    elif act == "DROP COLUMN":
        storage.drop_column(stmt.table, stmt.name)
    elif act == "RENAME COLUMN":
        storage.rename_column(stmt.table, stmt.name, stmt.new_name)
    elif act == "RENAME TO":
        storage.rename_table(stmt.table, stmt.new_name)
    else:
        raise SQLError(f"unknown ALTER TABLE action {stmt.action}")
    return Result(message="OK")


# ---- views
def _create_view(storage, stmt, select):
    if stmt.name in storage.tables or stmt.name in storage.views:
        raise SQLError(f"{stmt.name} already exists")
    if select is not None:
        select(stmt.select)                 # an invalid query is rejected now, not when the view is first read
    storage.create_view(stmt.name, stmt.select)
    return Result(message="OK")
