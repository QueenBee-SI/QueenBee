"""The stored data: Storage, Table, Index."""

from __future__ import annotations

import dataclasses

from . import ast as A
from .ast import ColumnDef
from .errors import SQLError
from .values import is_number

TYPES = ("INTEGER", "REAL", "TEXT", "BOOLEAN")
ACTIONS = ("RESTRICT", "CASCADE", "SET NULL")


def coerce(col, v):
    """Check a value against the type of a column and convert it (section 3 of the task)."""
    if v is None:
        return None
    t = col.type
    ok = False
    if t == "INTEGER":
        if is_number(v):
            if isinstance(v, float):
                if v != v or v in (float("inf"), float("-inf")) or v != int(v):
                    raise SQLError(f"column {col.name}: {v!r} is not an integer")
                return int(v)
            return v
    elif t == "REAL":
        if is_number(v):
            return float(v)
    elif t == "TEXT":
        ok = isinstance(v, str)
    elif t == "BOOLEAN":
        ok = isinstance(v, bool)
    if not ok:
        raise SQLError(f"column {col.name} is {t}, cannot store {v!r}")
    return v


class Index:
    def __init__(self, name, column, unique, pos):
        self.name = name
        self.column = column
        self.unique = unique
        self.pos = pos                     # position of the column in the row
        self.entries = {}                  # value -> [rowid, ...]   (NULLs are not indexed)

    def add(self, rowid, row):
        v = row[self.pos]
        if v is not None:
            self.entries.setdefault(v, []).append(rowid)

    def remove(self, rowid, row):
        v = row[self.pos]
        if v is not None:
            ids = self.entries.get(v)
            if ids is not None:
                ids.remove(rowid)
                if not ids:
                    del self.entries[v]

    def clone(self):
        c = Index(self.name, self.column, self.unique, self.pos)
        c.entries = {k: list(v) for k, v in self.entries.items()}
        return c


class Table:
    def __init__(self, name, columns, storage, auto_indexes=True):
        self.name = name
        self.columns = list(columns)
        self.storage = storage
        self.rows = {}                     # rowid -> tuple, in insertion order
        self.next_rowid = 1
        self.indexes = {}
        self._pos = {c.name: i for i, c in enumerate(self.columns)}
        if auto_indexes:
            for i, c in enumerate(self.columns):
                if c.primary_key:
                    self.indexes["pk_" + name] = Index("pk_" + name, c.name, True, i)
                elif c.unique:
                    n = f"uq_{name}_{c.name}"
                    self.indexes[n] = Index(n, c.name, True, i)

    def column_position(self, name):
        try:
            return self._pos[name]
        except KeyError:
            raise SQLError(f"table {self.name} has no column {name}") from None

    def index_on(self, column):
        for idx in self.indexes.values():
            if idx.column == column:
                return idx
        return None

    def clone(self, storage):
        t = Table(self.name, self.columns, storage, auto_indexes=False)
        t.rows = dict(self.rows)
        t.next_rowid = self.next_rowid
        t.indexes = {n: i.clone() for n, i in self.indexes.items()}
        return t

    # ---- checks
    def _check_row(self, values):
        if len(values) != len(self.columns):
            raise SQLError(f"table {self.name} has {len(self.columns)} columns, got {len(values)} values")
        row = tuple(coerce(c, v) for c, v in zip(self.columns, values))
        for c, v in zip(self.columns, row):
            if v is None and (c.not_null or c.primary_key):
                raise SQLError(f"column {c.name} must not be NULL")
        return row

    def _check_unique(self, row, ignore=()):
        for idx in self.indexes.values():
            if idx.unique:
                v = row[idx.pos]
                if v is not None and any(r not in ignore for r in idx.entries.get(v, ())):
                    raise SQLError(f"duplicate value {v!r} for unique column {idx.column} of {self.name}")

    def _parent_has(self, parent, pcol, v):
        """Is v stored in the (unique) column pcol of the parent table?"""
        idx = next((i for i in parent.indexes.values() if i.unique and i.column == pcol), None)
        if idx is not None:
            return v in idx.entries
        pos = parent.column_position(pcol)
        return any(r[pos] == v for r in parent.rows.values())

    def _check_parents(self, row, old=None, extra=None):
        """Every non-NULL foreign key value of row (that changed against old) must exist in its parent table."""
        for i, c in enumerate(self.columns):
            v = row[i]
            if c.references is None or v is None or (old is not None and old[i] == v):
                continue
            pname, pcol = c.references
            parent = self if pname == self.name else self.storage.tables.get(pname)
            if parent is None:
                raise SQLError(f"foreign key {self.name}.{c.name}: no such table {pname}")
            if parent is self and (row[self.column_position(pcol)] == v or (extra and v in extra.get(pcol, ()))):
                continue
            if not self._parent_has(parent, pcol, v):
                raise SQLError(f"foreign key violation: {c.name} = {v!r} is not in {pname}({pcol})")

    # ---- changes
    def insert_row(self, values):
        row = self._check_row(values)
        self._check_unique(row)
        self._check_parents(row)
        rowid = self.next_rowid
        self.next_rowid += 1
        self.rows[rowid] = row
        for idx in self.indexes.values():
            idx.add(rowid, row)
        return rowid

    def update_row(self, rowid, values):
        self.update_rows([(rowid, values)])

    def update_rows(self, changes):
        """Several updates as one step: uniqueness is judged on the final state; on error nothing changes."""
        new = [(rid, self._check_row(vals)) for rid, vals in changes]
        for rid, _ in new:
            if rid not in self.rows:
                raise SQLError("no such row")
        old = {rid: self.rows[rid] for rid, _ in new}
        extra = {c.name: {row[i] for _, row in new} for i, c in enumerate(self.columns)}
        for rid, row in new:
            self._check_parents(row, old[rid], extra)
        plan = self.storage._plan_parent_update(self, old, dict(new))
        for rid, row in old.items():
            for idx in self.indexes.values():
                idx.remove(rid, row)
        done = []
        try:
            for rid, row in new:
                self._check_unique(row)
                self.rows[rid] = row
                for idx in self.indexes.values():
                    idx.add(rid, row)
                done.append(rid)
        except SQLError:
            for rid in done:
                for idx in self.indexes.values():
                    idx.remove(rid, self.rows[rid])
            for rid, row in old.items():
                self.rows[rid] = row
                for idx in self.indexes.values():
                    idx.add(rid, row)
            raise
        self.storage._apply_plan(plan)

    def delete_row(self, rowid):
        if rowid not in self.rows:
            raise SQLError("no such row")
        self.storage._apply_plan(self.storage._plan_delete(self, [rowid]))

    def _remove(self, rowid):
        row = self.rows.pop(rowid, None)
        if row is not None:
            for idx in self.indexes.values():
                idx.remove(rowid, row)

    def _set_null(self, rowid, pos):
        row = self.rows.get(rowid)
        if row is None or row[pos] is None:
            return
        new = row[:pos] + (None,) + row[pos + 1:]
        for idx in self.indexes.values():
            idx.remove(rowid, row)
        self.rows[rowid] = new
        for idx in self.indexes.values():
            idx.add(rowid, new)

    # ---- reading
    def scan(self):
        for rowid, row in list(self.rows.items()):
            self.storage.rows_read += 1
            yield rowid, row

    def lookup(self, index_name, value):
        idx = self.indexes.get(index_name)
        if idx is None:
            raise SQLError(f"no such index {index_name}")
        try:
            ids = sorted(idx.entries.get(value, ()))
        except TypeError:
            ids = []
        self.storage.rows_read += len(ids)
        return [(rid, self.rows[rid]) for rid in ids]

    def rows_with(self, pos, value):
        """Row ids whose column at position pos equals value (NULL never matches)."""
        if value is None:
            return []
        idx = self.index_on(self.columns[pos].name)
        if idx is not None:
            return list(idx.entries.get(value, ()))
        return [rid for rid, r in self.rows.items() if r[pos] == value]

    def _renumber_columns(self):
        self._pos = {c.name: i for i, c in enumerate(self.columns)}

    def build_index(self, idx):
        for rid, row in self.rows.items():
            v = row[idx.pos]
            if idx.unique and v is not None and v in idx.entries:
                raise SQLError(f"cannot create unique index {idx.name}: duplicate value {v!r}")
            idx.add(rid, row)


def _encode(x):
    """AST node / value -> JSON-able structure (the views are stored as their parsed query)."""
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        d = {k: _encode(v) for k, v in vars(x).items()}
        d["__ast__"] = type(x).__name__
        return d
    if isinstance(x, tuple):
        return {"__tuple__": [_encode(v) for v in x]}
    if isinstance(x, list):
        return [_encode(v) for v in x]
    return x


def _decode(x):
    if isinstance(x, list):
        return [_decode(v) for v in x]
    if isinstance(x, dict):
        if "__tuple__" in x:
            return tuple(_decode(v) for v in x["__tuple__"])
        cls = getattr(A, x["__ast__"])
        fields = {f.name for f in dataclasses.fields(cls)}
        data = {k: _decode(v) for k, v in x.items() if k != "__ast__"}
        node = cls(**{k: v for k, v in data.items() if k in fields})
        for k, v in data.items():
            if k not in fields:
                setattr(node, k, v)
        return node
    return x


class Storage:
    def __init__(self):
        self.rows_read = 0
        self.tables = {}
        self.views = {}                    # lowercase name -> parsed query (Select or Compound)

    def table(self, name):
        t = self.tables.get(name)
        if t is None:
            if name in self.views:
                raise SQLError(f"{name} is a view, views are read-only")
            raise SQLError(f"no such table: {name}")
        return t

    # ---- tables
    def _check_references(self, name, col, columns):
        pname, pcol = col.references
        if col.on_delete not in ACTIONS:
            raise SQLError(f"unknown ON DELETE action {col.on_delete}")
        if pname == name:
            pcols = columns
        elif pname in self.tables:
            pcols = self.tables[pname].columns
        else:
            raise SQLError(f"foreign key {col.name}: no such table {pname}")
        target = next((c for c in pcols if c.name == pcol), None)
        if target is None:
            raise SQLError(f"foreign key {col.name}: table {pname} has no column {pcol}")
        if not (target.primary_key or target.unique):
            raise SQLError(f"foreign key {col.name}: {pname}({pcol}) is not a PRIMARY KEY or UNIQUE column")

    def create_table(self, stmt):
        if stmt.name in self.tables or stmt.name in self.views:
            if stmt.if_not_exists and stmt.name in self.tables:
                return
            raise SQLError(f"table {stmt.name} already exists" if stmt.name in self.tables
                           else f"{stmt.name} is already a view")
        if not stmt.columns:
            raise SQLError("a table needs at least one column")
        names = [c.name for c in stmt.columns]
        if len(set(names)) != len(names):
            raise SQLError(f"duplicate column name in table {stmt.name}")
        if sum(1 for c in stmt.columns if c.primary_key) > 1:
            raise SQLError("a table can have only one PRIMARY KEY column")
        for c in stmt.columns:
            if c.type not in TYPES:
                raise SQLError(f"unknown column type {c.type}")
            coerce(c, c.default)
            if c.references is not None:
                self._check_references(stmt.name, c, stmt.columns)
        self.tables[stmt.name] = Table(stmt.name, stmt.columns, self)

    def referencing(self, name):
        """[(child table, column position, ColumnDef)] of the foreign keys that point at table name."""
        return [(t, i, c) for t in self.tables.values() for i, c in enumerate(t.columns)
                if c.references is not None and c.references[0] == name]

    def drop_table(self, name, if_exists=False):
        if name not in self.tables:
            if name in self.views:
                raise SQLError(f"{name} is a view, use DROP VIEW")
            if if_exists:
                return
            raise SQLError(f"no such table: {name}")
        for t, _, c in self.referencing(name):
            if t.name != name:
                raise SQLError(f"cannot drop table {name}: {t.name}.{c.name} references it")
        del self.tables[name]

    def create_index(self, stmt):
        t = self.table(stmt.table)
        pos = t.column_position(stmt.column)
        if any(stmt.name in tb.indexes for tb in self.tables.values()):
            raise SQLError(f"index {stmt.name} already exists")
        idx = Index(stmt.name, stmt.column, stmt.unique, pos)
        t.build_index(idx)
        t.indexes[stmt.name] = idx

    # ---- views
    def create_view(self, name, query):
        if name in self.tables:
            raise SQLError(f"{name} is already a table")
        if name in self.views:
            raise SQLError(f"view {name} already exists")
        self.views[name] = query

    def drop_view(self, name, if_exists=False):
        if name not in self.views:
            if name in self.tables:
                raise SQLError(f"{name} is a table, use DROP TABLE")
            if if_exists:
                return
            raise SQLError(f"no such view: {name}")
        del self.views[name]

    # ---- ALTER TABLE
    def add_column(self, tname, col):
        t = self.table(tname)
        if col.name in t._pos:
            raise SQLError(f"table {tname} already has a column {col.name}")
        if col.type not in TYPES:
            raise SQLError(f"unknown column type {col.type}")
        if col.primary_key or col.unique:
            raise SQLError("ADD COLUMN cannot add a PRIMARY KEY or UNIQUE column")
        default = coerce(col, col.default)
        if col.not_null and default is None and t.rows:
            raise SQLError(f"cannot add NOT NULL column {col.name} without a DEFAULT to a table with rows")
        if col.references is not None:
            self._check_references(tname, col, t.columns + [col])
            if default is not None and t.rows:
                pname, pcol = col.references
                if not t._parent_has(self.tables[pname], pcol, default):
                    raise SQLError(f"foreign key violation: {col.name} = {default!r} is not in {pname}({pcol})")
        t.columns.append(col)
        t._renumber_columns()
        for rid, row in t.rows.items():
            t.rows[rid] = row + (default,)

    def rename_table(self, old, new):
        t = self.table(old)
        if new in self.tables or new in self.views:
            raise SQLError(f"{new} already exists")
        for tb in self.tables.values():
            for i, c in enumerate(tb.columns):
                if c.references is not None and c.references[0] == old:
                    tb.columns[i] = dataclasses.replace(c, references=(new, c.references[1]))
        t.name = new
        for n, idx in list(t.indexes.items()):
            if n == "pk_" + old:
                idx.name = "pk_" + new
            elif n == f"uq_{old}_{idx.column}":
                idx.name = f"uq_{new}_{idx.column}"
            else:
                continue
            del t.indexes[n]
            t.indexes[idx.name] = idx
        self.tables = {(new if n == old else n): tb for n, tb in self.tables.items()}

    def rename_column(self, tname, old, new):
        t = self.table(tname)
        pos = t.column_position(old)
        if new in t._pos:
            raise SQLError(f"table {tname} already has a column {new}")
        for tb in self.tables.values():
            for i, c in enumerate(tb.columns):
                if c.references == (tname, old):
                    tb.columns[i] = dataclasses.replace(c, references=(tname, new))
        t.columns[pos] = dataclasses.replace(t.columns[pos], name=new)
        t._renumber_columns()
        for n, idx in list(t.indexes.items()):
            if idx.column == old:
                idx.column = new
                if n == f"uq_{tname}_{old}":
                    del t.indexes[n]
                    idx.name = f"uq_{tname}_{new}"
                    t.indexes[idx.name] = idx

    def drop_column(self, tname, name):
        t = self.table(tname)
        pos = t.column_position(name)
        col = t.columns[pos]
        if col.primary_key:
            raise SQLError(f"cannot drop PRIMARY KEY column {name}")
        if len(t.columns) == 1:
            raise SQLError("cannot drop the last column of a table")
        if any(i.column == name for i in t.indexes.values()):
            raise SQLError(f"cannot drop column {name}: it has an index")
        if col.references is not None or any(c.references[1] == name for _, _, c in self.referencing(tname)):
            raise SQLError(f"cannot drop column {name}: a foreign key uses it")
        del t.columns[pos]
        t._renumber_columns()
        for rid, row in t.rows.items():
            t.rows[rid] = row[:pos] + row[pos + 1:]
        for idx in t.indexes.values():
            if idx.pos > pos:
                idx.pos -= 1

    # ---- foreign key actions: planned first (so that an error changes nothing), then applied
    def _plan(self, items):
        """items: [(table, rowid, only, keep)]. A row with only=None is removed; otherwise it stays and only the
        foreign keys on the columns in only are looked at (keep(col, value) says the value still exists)."""
        dels, nulls, restricts = {}, {}, []
        stack = list(items)
        while stack:
            t, rid, only, keep = stack.pop()
            row = t.rows.get(rid)
            if row is None:
                continue
            if only is None:
                d = dels.setdefault(t.name, {})
                if rid in d:
                    continue
                d[rid] = None
            for child, i, c in self.referencing(t.name):
                pcol = c.references[1]
                if only is not None and pcol not in only:
                    continue
                v = row[t.column_position(pcol)]
                if v is None or (keep is not None and keep(pcol, v)):
                    continue
                for crid in child.rows_with(i, v):
                    if c.on_delete == "CASCADE":
                        stack.append((child, crid, None, None))
                    elif c.on_delete == "SET NULL":
                        if c.not_null or c.primary_key:
                            raise SQLError(f"cannot set {child.name}.{c.name} to NULL: the column is NOT NULL")
                        nulls.setdefault(child.name, {}).setdefault(crid, set()).add(i)
                    else:
                        restricts.append((t.name, child, c, crid))
        for pname, child, c, crid in restricts:
            if crid not in dels.get(child.name, {}):
                raise SQLError(f"foreign key violation: {child.name}.{c.name} still references {pname}")
        return dels, nulls

    def _plan_delete(self, table, rowids):
        return self._plan([(table, rid, None, None) for rid in rowids])

    def _plan_parent_update(self, table, old, new):
        if not self.referencing(table.name):
            return {}, {}
        items = []
        for rid, orow in old.items():
            changed = {c.name for i, c in enumerate(table.columns) if orow[i] != new[rid][i]}
            if changed:
                def keep(col, v):
                    pos = table.column_position(col)
                    if any(r[pos] == v for r in new.values()):
                        return True
                    return any(r not in new for r in table.rows_with(pos, v))
                items.append((table, rid, changed, keep))
        return self._plan(items)

    def _apply_plan(self, plan):
        dels, nulls = plan
        for tname, d in dels.items():
            t = self.tables[tname]
            for rid in d:
                t._remove(rid)
        for tname, m in nulls.items():
            t = self.tables[tname]
            for rid, poss in m.items():
                for pos in poss:
                    t._set_null(rid, pos)

    # ---- transactions and persistence
    def snapshot(self):
        return ({n: t.clone(self) for n, t in self.tables.items()}, dict(self.views))

    def restore(self, snap):
        tables, views = snap
        self.tables = {n: t.clone(self) for n, t in tables.items()}
        self.views = dict(views)

    def dump(self):
        tables = []
        for t in self.tables.values():
            tables.append({
                "name": t.name,
                "columns": [{"name": c.name, "type": c.type, "primary_key": c.primary_key, "not_null": c.not_null,
                             "unique": c.unique, "default": c.default,
                             "references": list(c.references) if c.references else None,
                             "on_delete": c.on_delete} for c in t.columns],
                "next_rowid": t.next_rowid,
                "rows": [[rid, list(row)] for rid, row in t.rows.items()],
                "indexes": [{"name": i.name, "column": i.column, "unique": i.unique} for i in t.indexes.values()],
            })
        return {"version": 2, "tables": tables, "views": {n: _encode(q) for n, q in self.views.items()}}

    @classmethod
    def load(cls, data):
        s = cls()
        try:
            for td in data["tables"]:
                cols = [ColumnDef(c["name"], c["type"], c["primary_key"], c["not_null"], c["unique"], c["default"],
                                  tuple(c["references"]) if c.get("references") else None,
                                  c.get("on_delete", "RESTRICT"))
                        for c in td["columns"]]
                t = Table(td["name"], cols, s, auto_indexes=False)
                t.next_rowid = td["next_rowid"]
                for rid, row in td["rows"]:
                    t.rows[rid] = tuple(row)
                for i in td["indexes"]:
                    idx = Index(i["name"], i["column"], i["unique"], t.column_position(i["column"]))
                    t.build_index(idx)
                    t.indexes[idx.name] = idx
                s.tables[t.name] = t
            for n, q in data.get("views", {}).items():
                s.views[n] = _decode(q)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise SQLError(f"corrupt database file: {e}") from None
        return s
