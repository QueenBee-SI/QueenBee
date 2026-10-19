"""Runs parsed statements against the storage."""

from __future__ import annotations

from . import ast as A
from .errors import SQLError
from .expr import evaluate
from .plan import Scope, conjuncts, has_agg, is_agg, literal_of, rewrite, walk
from .result import Result
from .values import compare, hash_key, is_number, type_name


class _Env:
    """The resolver handed to expr.evaluate: looks a qualified column up in the current combined row."""
    __slots__ = ("index", "row")

    def __init__(self, index):
        self.index = index
        self.row = ()

    def __call__(self, table, column):
        try:
            return self.row[self.index[(table, column)]]
        except KeyError:
            raise SQLError(f"no such column: {column}") from None


class _Query:
    pass


def _truthy(v):
    if v is None or v is False:
        return False
    if v is True:
        return True
    raise SQLError(f"a condition must be boolean, got {type_name(v)}")


def _literal_fits(col, value):
    if col.type in ("INTEGER", "REAL"):
        return is_number(value)
    if col.type == "TEXT":
        return isinstance(value, str)
    return isinstance(value, bool)


class Executor:
    def __init__(self, storage):
        self.storage = storage
        self._sub_cache = {}

    def execute(self, stmt):
        self._sub_cache = {}
        if isinstance(stmt, A.Select):
            names, rows = self._select(stmt)
            return Result(names, rows, len(rows))
        if isinstance(stmt, A.Explain):
            return self._explain(stmt.select)
        if isinstance(stmt, A.Insert):
            return self._insert(stmt)
        if isinstance(stmt, A.Update):
            return self._update(stmt)
        if isinstance(stmt, A.Delete):
            return self._delete(stmt)
        if isinstance(stmt, A.CreateTable):
            self.storage.create_table(stmt)
        elif isinstance(stmt, A.DropTable):
            self.storage.drop_table(stmt.name, stmt.if_exists)
        elif isinstance(stmt, A.CreateIndex):
            self.storage.create_index(stmt)
        else:
            raise SQLError(f"cannot execute {type(stmt).__name__} here")
        return Result(message="OK")

    # ---- helpers
    def _subquery(self, sel):
        key = id(sel)
        if key not in self._sub_cache:
            names, rows = self._select(sel)
            if len(names) != 1:
                raise SQLError("subquery must return exactly one column")
            self._sub_cache[key] = (sel, rows)
        return self._sub_cache[key][1]

    def _ev(self, node, env):
        return evaluate(node, env, self._subquery)

    def _plan(self, table, alias, conj):
        """(index name, value) when a top-level conjunct is  column = literal  on an indexed column."""
        for c in conj:
            if isinstance(c, A.Binary) and c.op == "=":
                for col, lit in ((c.left, c.right), (c.right, c.left)):
                    ok, value = literal_of(lit)
                    if ok and isinstance(col, A.Column) and col.table == alias:
                        idx = table.index_on(col.name)
                        if idx is not None and _literal_fits(table.columns[table.column_position(col.name)], value):
                            return idx.name, value
        return None

    def _access(self, table, alias, conj):
        plan = self._plan(table, alias, conj)
        if plan:
            return table.lookup(*plan)
        return list(table.scan())

    def _single_scope(self, name):
        t = self.storage.table(name)
        scope = Scope()
        scope.add(t.name, t)
        return t, scope, _Env(scope.index)

    # ---- INSERT / UPDATE / DELETE
    def _insert(self, stmt):
        t = self.storage.table(stmt.table)
        if stmt.columns is None:
            positions = list(range(len(t.columns)))
        else:
            positions = [t.column_position(c) for c in stmt.columns]
            if len(set(positions)) != len(positions):
                raise SQLError("a column is listed twice")

        def no_columns(table, column):
            raise SQLError(f"no such column: {column} (VALUES cannot use columns)")

        built = []
        for exprs in stmt.rows:
            if len(exprs) != len(positions):
                raise SQLError(f"expected {len(positions)} values, got {len(exprs)}")
            row = [c.default for c in t.columns]
            for pos, e in zip(positions, exprs):
                row[pos] = evaluate(e, no_columns, self._subquery)
            built.append(tuple(row))
        for row in built:
            t.insert_row(row)
        return Result(message=f"OK, {len(built)} rows affected", rowcount=len(built))

    def _matching(self, t, where):
        """[(rowid, row)] of the table that satisfy the (unqualified) WHERE expression."""
        scope = Scope()
        scope.add(t.name, t)
        env = _Env(scope.index)
        w = scope.qualify(where) if where is not None else None
        if w is not None and has_agg(w):
            raise SQLError("aggregate functions are not allowed in WHERE")
        found = []
        for rid, row in self._access(t, t.name, conjuncts(w)):
            env.row = row
            if w is None or _truthy(self._ev(w, env)):
                found.append((rid, row))
        return found, scope, env

    def _update(self, stmt):
        t = self.storage.table(stmt.table)
        found, scope, env = self._matching(t, stmt.where)
        sets = []
        for col, e in stmt.assignments:
            q = scope.qualify(e)
            if has_agg(q):
                raise SQLError("aggregate functions are not allowed in UPDATE")
            sets.append((t.column_position(col), q))
        changes = []
        for rid, row in found:
            env.row = row
            new = list(row)
            for pos, e in sets:
                new[pos] = self._ev(e, env)
            changes.append((rid, tuple(new)))
        t.update_rows(changes)
        return Result(message=f"OK, {len(changes)} rows affected", rowcount=len(changes))

    def _delete(self, stmt):
        t = self.storage.table(stmt.table)
        found, _, _ = self._matching(t, stmt.where)
        for rid, _ in found:
            t.delete_row(rid)
        return Result(message=f"OK, {len(found)} rows affected", rowcount=len(found))

    # ---- SELECT: analysis
    def _analyze(self, sel):
        a = _Query()
        a.sel = sel
        scope = a.scope = Scope()
        a.ons = []
        if sel.table is not None:
            t = self.storage.table(sel.table.name)
            scope.add(sel.table.alias or t.name, t)
            for j in sel.joins:
                jt = self.storage.table(j.table.name)
                scope.add(j.table.alias or jt.name, jt)
                on = scope.qualify(j.on)
                if has_agg(on):
                    raise SQLError("aggregate functions are not allowed in ON")
                a.ons.append(on)
        a.names, a.exprs = [], []
        for it in sel.items:
            if it.star:
                if not scope.tables:
                    raise SQLError("SELECT * needs a FROM clause")
                matched = False
                for alias, t, _ in scope.tables:
                    if it.star_table is None or it.star_table == alias:
                        matched = True
                        for c in t.columns:
                            a.names.append(c.name)
                            a.exprs.append(A.Column(c.name, alias))
                if not matched:
                    raise SQLError(f"no such table: {it.star_table}")
            else:
                a.exprs.append(scope.qualify(it.expr))
                if it.alias:
                    a.names.append(it.alias)
                elif isinstance(it.expr, A.Column):
                    a.names.append(it.expr.name)
                else:
                    a.names.append(getattr(it, "text", "expr"))
        a.where = scope.qualify(sel.where) if sel.where is not None else None
        if a.where is not None and has_agg(a.where):
            raise SQLError("aggregate functions are not allowed in WHERE")
        a.group_by = [scope.qualify(g) for g in sel.group_by]
        if any(has_agg(g) for g in a.group_by):
            raise SQLError("aggregate functions are not allowed in GROUP BY")
        a.having = scope.qualify(sel.having) if sel.having is not None else None
        a.order = []
        for o in sel.order_by:
            e = o.expr
            if isinstance(e, A.Literal) and isinstance(e.value, int) and not isinstance(e.value, bool):
                if not 1 <= e.value <= len(a.exprs):
                    raise SQLError(f"ORDER BY position {e.value} is out of range")
                q = a.exprs[e.value - 1]
            elif isinstance(e, A.Column) and e.table is None and e.name in a.names:
                q = a.exprs[a.names.index(e.name)]
            else:
                q = scope.qualify(e)
            a.order.append((q, o.descending))
        every = a.exprs + a.group_by + [q for q, _ in a.order] + ([a.having] if a.having is not None else [])
        for e in every:
            for n in walk(e):
                if is_agg(n):
                    self._check_agg(n)
        a.agg_mode = bool(a.group_by) or a.having is not None or any(has_agg(e) for e in every)
        if a.agg_mode:
            for e in a.exprs + [q for q, _ in a.order] + ([a.having] if a.having is not None else []):
                self._check_grouped(e, a.group_by)
        return a

    @staticmethod
    def _check_agg(f):
        if f.star:
            if f.name != "COUNT":
                raise SQLError(f"{f.name}(*) is not allowed")
            return
        if len(f.args) != 1:
            raise SQLError(f"{f.name} takes exactly one argument")
        if has_agg(f.args[0]):
            raise SQLError("aggregate functions cannot be nested")

    def _check_grouped(self, e, group_by):
        if is_agg(e) or any(e == g for g in group_by):
            return
        if isinstance(e, A.Column):
            raise SQLError(f"column {e.name} must appear in GROUP BY or inside an aggregate function")
        from .plan import children
        for c in children(e):
            self._check_grouped(c, group_by)

    def _explain(self, sel):
        a = self._analyze(sel)
        conj = conjuncts(a.where)
        lines = []
        for alias, t, _ in a.scope.tables:
            plan = self._plan(t, alias, conj)
            lines.append(f"INDEX SCAN {t.name} USING {plan[0]}" if plan else f"SCAN {t.name}")
        return Result(["plan"], [(ln,) for ln in lines], len(lines))

    # ---- SELECT: running
    def _source_rows(self, a):
        scope = a.scope
        if not scope.tables:
            return [()]
        conj = conjuncts(a.where)
        alias, t, _ = scope.tables[0]
        rows = [r for _, r in self._access(t, alias, conj)]
        env = _Env(scope.index)
        for j, on, (alias, t, _) in zip(a.sel.joins, a.ons, scope.tables[1:]):
            right = [r for _, r in self._access(t, alias, conj)]
            pad = (None,) * len(t.columns)
            out = []
            for left in rows:
                matched = False
                for r in right:
                    env.row = left + r
                    if _truthy(self._ev(on, env)):
                        out.append(env.row)
                        matched = True
                if not matched and j.kind == "LEFT":
                    out.append(left + pad)
            rows = out
        return rows

    def _aggregate(self, f, rows, env):
        if f.star:
            return len(rows)
        vals = []
        for r in rows:
            env.row = r
            v = self._ev(f.args[0], env)
            if v is not None:
                vals.append(v)
        if f.distinct:
            seen, uniq = set(), []
            for v in vals:
                k = hash_key(v)
                if k not in seen:
                    seen.add(k)
                    uniq.append(v)
            vals = uniq
        if f.name == "COUNT":
            return len(vals)
        if not vals:
            return None
        if f.name in ("SUM", "AVG"):
            for v in vals:
                if not is_number(v):
                    raise SQLError(f"{f.name} needs numbers, got {type_name(v)}")
            total = sum(vals)
            return total / len(vals) if f.name == "AVG" else total
        best = vals[0]
        for v in vals[1:]:
            c = compare(v, best)
            if (c < 0) if f.name == "MIN" else (c > 0):
                best = v
        return best

    def _select(self, sel):
        a = self._analyze(sel)
        env = _Env(a.scope.index)
        rows = self._source_rows(a)
        if a.where is not None:
            kept = []
            for r in rows:
                env.row = r
                if _truthy(self._ev(a.where, env)):
                    kept.append(r)
            rows = kept
        produced = []                                  # (output row, sort keys)
        order_exprs = [q for q, _ in a.order]
        if a.agg_mode:
            if a.group_by:
                groups = {}
                for r in rows:
                    env.row = r
                    k = tuple(hash_key(self._ev(g, env)) for g in a.group_by)
                    groups.setdefault(k, []).append(r)
                group_rows = list(groups.values())
            else:
                group_rows = [rows]
            for grows in group_rows:
                def sub(n, grows=grows):
                    return A.Literal(self._aggregate(n, grows, env)) if is_agg(n) else None
                genv = _Env(a.scope.index)
                genv.row = grows[0] if grows else ()
                if a.having is not None and not _truthy(self._ev(rewrite(a.having, sub), genv)):
                    continue
                out = tuple(self._ev(rewrite(e, sub), genv) for e in a.exprs)
                keys = [self._ev(rewrite(e, sub), genv) for e in order_exprs]
                produced.append((out, keys))
        else:
            for r in rows:
                env.row = r
                out = tuple(self._ev(e, env) for e in a.exprs)
                keys = [self._ev(e, env) for e in order_exprs]
                produced.append((out, keys))
        if sel.distinct:
            seen, uniq = set(), []
            for out, keys in produced:
                k = tuple(hash_key(v) for v in out)
                if k not in seen:
                    seen.add(k)
                    uniq.append((out, keys))
            produced = uniq
        for i in range(len(a.order) - 1, -1, -1):
            try:
                produced.sort(key=lambda p, i=i: (p[1][i] is not None, p[1][i]), reverse=a.order[i][1])
            except TypeError:
                raise SQLError("ORDER BY values cannot be compared") from None
        result = [out for out, _ in produced]
        start = sel.offset or 0
        if sel.limit is not None:
            result = result[start:start + max(sel.limit, 0)]
        elif start:
            result = result[start:]
        return a.names, result
