"""Runs parsed statements against the storage."""

from __future__ import annotations

from . import ast as A
from .errors import SQLError
from .expr import evaluate
from .plan import Relation, Scope, conjuncts, has_agg, has_window, is_agg, is_window, literal_of, rewrite, walk
from .result import Result
from .values import compare, hash_key, is_number, type_name


class _Env:
    """The resolver handed to expr.evaluate: looks a qualified column up in the current combined row."""
    __slots__ = ("index", "row", "ex")

    def __init__(self, index, ex):
        self.index = index
        self.row = ()
        self.ex = ex

    def __call__(self, table, column):
        if table is not None and table.startswith("#"):          # a column of an enclosing query: '#level:alias'
            level, alias = table[1:].split(":", 1)
            ex, level = self.ex, int(level)
            ex._min_outer = min(ex._min_outer, level)
            return ex._frames[level][1](alias, column)
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


_WINDOW_FUNCS = frozenset({"ROW_NUMBER", "RANK", "DENSE_RANK", "LAG", "LEAD", "SUM", "COUNT", "AVG", "MIN", "MAX"})


def _sort(items, keys, descending):
    """Sorts items (stable, NULLs first when ascending) by the key lists in `keys` (parallel to items)."""
    order = list(range(len(items)))
    for i in range(len(descending) - 1, -1, -1):
        try:
            order.sort(key=lambda j, i=i: (keys[j][i] is not None, keys[j][i]), reverse=descending[i])
        except TypeError:
            raise SQLError("ORDER BY values cannot be compared") from None
    return [items[j] for j in order]


def _limit(rows, limit, offset):
    start = offset or 0
    if limit is not None:
        return rows[start:start + max(limit, 0)]
    return rows[start:] if start else rows


def _distinct(rows):
    seen, uniq = set(), []
    for r in rows:
        k = tuple(hash_key(v) for v in r)
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq


class Executor:
    def __init__(self, storage):
        self.storage = storage
        self._reset()

    def _reset(self):
        self._sub_cache = {}        # (id(select), level) -> (select, rows) for uncorrelated subqueries
        self._analyses = {}         # (id(select), level) -> (select, analysis)
        self._cte_cache = {}        # id(cte) -> (cte, Relation)
        self._ctes = {}             # visible CTE name -> Relation
        self._frames = []           # (scope, env) of the queries being run, outermost first
        self._min_outer = 1 << 30   # lowest enclosing level read since last reset (to spot correlation)

    def execute(self, stmt):
        self._reset()
        if isinstance(stmt, (A.Select, A.Compound)):
            names, rows = self._query(stmt)
            return Result(names, rows, len(rows))
        if isinstance(stmt, A.Explain):
            return self._explain(stmt.select)
        if isinstance(stmt, (A.Insert, A.AlterTable, A.CreateView, A.DropView)):
            from . import dml
            return dml.execute(self.storage, stmt, self.run_query, self._subquery)
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

    def run_query(self, query):
        """(names, rows) of a Select or Compound; used by dml for INSERT ... SELECT and to check views."""
        if not self._frames:
            self._reset()
        return self._query(query)

    # ---- helpers
    def _subquery(self, sel):
        """Rows of a subquery evaluated inside an expression (correlated ones are re-run for every outer row)."""
        level = len(self._frames)
        key = (id(sel), level)
        hit = self._sub_cache.get(key)
        if hit is not None:
            return hit[1]
        saved, self._min_outer = self._min_outer, 1 << 30
        try:
            _, rows = self._query(sel, self._frames[-1][0] if self._frames else None)
            correlated = self._min_outer < level
        finally:
            self._min_outer = min(saved, self._min_outer)
        if not correlated:
            self._sub_cache[key] = (sel, rows)
        return rows

    def _ev(self, node, env):
        return evaluate(node, env, self._subquery)

    def _plan(self, table, alias, conj):
        """(index name, value) when a top-level conjunct is  column = literal  on an indexed column."""
        if isinstance(table, Relation):
            return None
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
        if isinstance(table, Relation):
            return list(enumerate(table.rows))
        plan = self._plan(table, alias, conj)
        if plan:
            return table.lookup(*plan)
        return list(table.scan())

    # ---- UPDATE / DELETE
    def _matching(self, t, where, scope, env):
        """[(rowid, row)] of the table that satisfy the (unqualified) WHERE expression."""
        w = scope.qualify(where) if where is not None else None
        if w is not None and has_agg(w):
            raise SQLError("aggregate functions are not allowed in WHERE")
        if w is not None and has_window(w):
            raise SQLError("window functions are not allowed in WHERE")
        found = []
        for rid, row in self._access(t, t.name, conjuncts(w)):
            env.row = row
            if w is None or _truthy(self._ev(w, env)):
                found.append((rid, row))
        return found

    def _push_table(self, t):
        scope = Scope(None, len(self._frames))
        scope.add(t.name, t)
        env = _Env(scope.index, self)
        self._frames.append((scope, env))
        return scope, env

    def _update(self, stmt):
        t = self.storage.table(stmt.table)
        scope, env = self._push_table(t)
        try:
            found = self._matching(t, stmt.where, scope, env)
            sets = []
            for col, e in stmt.assignments:
                q = scope.qualify(e)
                if has_agg(q):
                    raise SQLError("aggregate functions are not allowed in UPDATE")
                if has_window(q):
                    raise SQLError("window functions are not allowed in UPDATE")
                sets.append((t.column_position(col), q))
            changes = []
            for rid, row in found:
                env.row = row
                new = list(row)
                for pos, e in sets:
                    new[pos] = self._ev(e, env)
                changes.append((rid, tuple(new)))
        finally:
            self._frames.pop()
        t.update_rows(changes)
        return Result(message=f"OK, {len(changes)} rows affected", rowcount=len(changes))

    def _delete(self, stmt):
        t = self.storage.table(stmt.table)
        scope, env = self._push_table(t)
        try:
            found = self._matching(t, stmt.where, scope, env)
        finally:
            self._frames.pop()
        for rid, _ in found:
            t.delete_row(rid)
        return Result(message=f"OK, {len(found)} rows affected", rowcount=len(found))

    # ---- queries: WITH, set operations
    def _query(self, q, parent=None):
        """(names, rows) of a Select or Compound; `parent` is the scope of the enclosing query (correlation)."""
        if q.ctes:
            saved, visible = self._ctes, dict(self._ctes)
            try:
                for c in q.ctes:
                    hit = self._cte_cache.get(id(c))
                    if hit is None:
                        self._ctes = dict(visible)
                        names, rows = self._query(c.select)
                        hit = self._cte_cache[id(c)] = (c, Relation(c.name, names, rows))
                    visible[c.name] = hit[1]
                self._ctes = visible
                return self._query_body(q, parent)
            finally:
                self._ctes = saved
        return self._query_body(q, parent)

    def _query_body(self, q, parent):
        if isinstance(q, A.Select):
            return self._select(q, parent)
        names, left = self._query(q.left, parent)
        rnames, right = self._query(q.right, parent)
        if len(names) != len(rnames):
            raise SQLError(f"{q.op} needs the same number of columns on both sides ({len(names)} and {len(rnames)})")
        if q.op == "UNION ALL":
            rows = left + right
        elif q.op == "UNION":
            rows = _distinct(left + right)
        else:
            other = {tuple(hash_key(v) for v in r) for r in right}
            keep = q.op == "INTERSECT"
            rows = _distinct([r for r in left if (tuple(hash_key(v) for v in r) in other) == keep])
        if q.order_by:
            idx, desc = [], []
            for o in q.order_by:
                e = o.expr
                if isinstance(e, A.Literal) and isinstance(e.value, int) and not isinstance(e.value, bool):
                    if not 1 <= e.value <= len(names):
                        raise SQLError(f"ORDER BY position {e.value} is out of range")
                    idx.append(e.value - 1)
                elif isinstance(e, A.Column) and e.name in names:
                    idx.append(names.index(e.name))
                else:
                    raise SQLError("ORDER BY of a set operation must name a result column or position")
                desc.append(o.descending)
            rows = _sort(rows, [[r[i] for i in idx] for r in rows], desc)
        return names, _limit(rows, q.limit, q.offset)

    def _source(self, ref):
        """The table-like object a FROM item stands for: derived table, CTE, view or table."""
        if ref.select is not None:
            names, rows = self._query(ref.select)
            return Relation(ref.alias or "subquery", names, rows)
        if ref.name in self._ctes:
            return self._ctes[ref.name]
        view = getattr(self.storage, "views", {}).get(ref.name)
        if view is not None:
            saved, self._ctes = self._ctes, {}
            try:
                names, rows = self._query(view)
            finally:
                self._ctes = saved
            return Relation(ref.name, names, rows)
        return self.storage.table(ref.name)

    # ---- SELECT: analysis
    def _analyze(self, sel, parent, level):
        a = _Query()
        a.sel = sel
        scope = a.scope = Scope(parent, level)
        a.ons = []
        if sel.table is not None:
            t = self._source(sel.table)
            scope.add(sel.table.alias or t.name, t)
            for j in sel.joins:
                jt = self._source(j.table)
                scope.add(j.table.alias or jt.name, jt)
                on = scope.qualify(j.on) if j.on is not None else None
                if on is not None and has_agg(on):
                    raise SQLError("aggregate functions are not allowed in ON")
                if on is not None and has_window(on):
                    raise SQLError("window functions are not allowed in ON")
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
        if a.where is not None and has_window(a.where):
            raise SQLError("window functions are not allowed in WHERE")
        a.group_by = [scope.qualify(g) for g in sel.group_by]
        if any(has_agg(g) for g in a.group_by):
            raise SQLError("aggregate functions are not allowed in GROUP BY")
        if any(has_window(g) for g in a.group_by):
            raise SQLError("window functions are not allowed in GROUP BY")
        a.having = scope.qualify(sel.having) if sel.having is not None else None
        if a.having is not None and has_window(a.having):
            raise SQLError("window functions are not allowed in HAVING")
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
        a.windows, seen = [], set()
        for e in every:
            for n in walk(e):
                if is_agg(n):
                    self._check_agg(n)
                elif is_window(n) and id(n) not in seen:
                    seen.add(id(n))
                    self._check_window(n)
                    a.windows.append(n)
        a.agg_mode = bool(a.group_by) or a.having is not None or any(has_agg(e) for e in every)
        if a.agg_mode:
            for e in a.exprs + [q for q, _ in a.order] + ([a.having] if a.having is not None else []):
                self._check_grouped(e, a.group_by)
        return a

    @staticmethod
    def _check_window(f):
        if f.name not in _WINDOW_FUNCS:
            raise SQLError(f"{f.name} is not a window function")
        parts = list(f.args) + list(f.over.partition_by) + [o.expr for o in f.over.order_by]
        for p in parts:
            if has_window(p):
                raise SQLError("window functions cannot be nested")

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
            if e.table is not None and e.table.startswith("#"):      # a constant for this query
                return
            raise SQLError(f"column {e.name} must appear in GROUP BY or inside an aggregate function")
        from .plan import children
        for c in children(e):
            self._check_grouped(c, group_by)

    def _explain(self, sel):
        a = self._analyze(sel, None, 0)
        conj = conjuncts(a.where)
        lines = []
        for alias, t, _ in a.scope.tables:
            plan = self._plan(t, alias, conj)
            lines.append(f"INDEX SCAN {t.name} USING {plan[0]}" if plan else f"SCAN {t.name}")
        return Result(["plan"], [(ln,) for ln in lines], len(lines))

    # ---- SELECT: running
    def _source_rows(self, a, env):
        scope = a.scope
        if not scope.tables:
            return [()]
        conj = conjuncts(a.where)
        alias, t, _ = scope.tables[0]
        rows = [r for _, r in self._access(t, alias, conj)]
        for j, on, (alias, t, offset) in zip(a.sel.joins, a.ons, scope.tables[1:]):
            right = [r for _, r in self._access(t, alias, conj)]
            pad = (None,) * len(t.columns)
            out = []
            hit = [False] * len(right)
            for left in rows:
                matched = False
                for k, r in enumerate(right):
                    env.row = left + r
                    if on is None or _truthy(self._ev(on, env)):
                        out.append(env.row)
                        matched = hit[k] = True
                if not matched and j.kind == "LEFT":
                    out.append(left + pad)
            if j.kind == "RIGHT":
                lpad = (None,) * offset
                out.extend(lpad + r for k, r in enumerate(right) if not hit[k])
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

    def _ev_ctx(self, e, ctx, env, wvals=None):
        """Evaluates e for one output context (row, group rows or None): aggregates and windows become literals."""
        row, grows = ctx
        if grows is not None or wvals:
            def sub(n):
                if is_agg(n):
                    return A.Literal(self._aggregate(n, grows, env))
                if wvals and is_window(n):
                    return A.Literal(wvals[id(n)])
                return None
            e = rewrite(e, sub)
        env.row = row
        return self._ev(e, env)

    def _select(self, sel, parent=None):
        level = len(self._frames)
        hit = self._analyses.get((id(sel), level))
        if hit is None:
            hit = self._analyses[(id(sel), level)] = (sel, self._analyze(sel, parent, level))
        a = hit[1]
        env = _Env(a.scope.index, self)
        self._frames.append((a.scope, env))
        try:
            return a.names, self._run_select(a, env)
        finally:
            self._frames.pop()

    def _run_select(self, a, env):
        sel = a.sel
        rows = self._source_rows(a, env)
        if a.where is not None:
            kept = []
            for r in rows:
                env.row = r
                if _truthy(self._ev(a.where, env)):
                    kept.append(r)
            rows = kept
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
            ctxs = [(g[0] if g else (), g) for g in group_rows]
            if a.having is not None:
                ctxs = [c for c in ctxs if _truthy(self._ev_ctx(a.having, c, env))]
        else:
            ctxs = [(r, None) for r in rows]
        wvals = [None] * len(ctxs)
        if a.windows:
            from .windows import compute_window
            results = [compute_window(w, ctxs, lambda n, c: self._ev_ctx(n, c, env)) for w in a.windows]
            wvals = [{id(w): res[i] for w, res in zip(a.windows, results)} for i in range(len(ctxs))]
        order_exprs = [q for q, _ in a.order]
        produced = []                                  # (output row, sort keys)
        for ctx, wv in zip(ctxs, wvals):
            out = tuple(self._ev_ctx(e, ctx, env, wv) for e in a.exprs)
            keys = [self._ev_ctx(e, ctx, env, wv) for e in order_exprs]
            produced.append((out, keys))
        if sel.distinct:
            seen, uniq = set(), []
            for out, keys in produced:
                k = tuple(hash_key(v) for v in out)
                if k not in seen:
                    seen.add(k)
                    uniq.append((out, keys))
            produced = uniq
        result = _sort([out for out, _ in produced], [k for _, k in produced], [d for _, d in a.order])
        return _limit(result, sel.limit, sel.offset)
