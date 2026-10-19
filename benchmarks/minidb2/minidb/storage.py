"""The stored data: Storage, Table, Index."""

from __future__ import annotations

from .ast import ColumnDef
from .errors import SQLError
from .values import is_number

TYPES = ("INTEGER", "REAL", "TEXT", "BOOLEAN")


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

    # ---- changes
    def insert_row(self, values):
        row = self._check_row(values)
        self._check_unique(row)
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

    def delete_row(self, rowid):
        row = self.rows.pop(rowid, None)
        if row is None:
            raise SQLError("no such row")
        for idx in self.indexes.values():
            idx.remove(rowid, row)

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

    def build_index(self, idx):
        for rid, row in self.rows.items():
            v = row[idx.pos]
            if idx.unique and v is not None and v in idx.entries:
                raise SQLError(f"cannot create unique index {idx.name}: duplicate value {v!r}")
            idx.add(rid, row)


class Storage:
    def __init__(self):
        self.rows_read = 0
        self.tables = {}

    def table(self, name):
        t = self.tables.get(name)
        if t is None:
            raise SQLError(f"no such table: {name}")
        return t

    def create_table(self, stmt):
        if stmt.name in self.tables:
            if stmt.if_not_exists:
                return
            raise SQLError(f"table {stmt.name} already exists")
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
        self.tables[stmt.name] = Table(stmt.name, stmt.columns, self)

    def drop_table(self, name, if_exists=False):
        if name not in self.tables:
            if if_exists:
                return
            raise SQLError(f"no such table: {name}")
        del self.tables[name]

    def create_index(self, stmt):
        t = self.table(stmt.table)
        pos = t.column_position(stmt.column)
        if any(stmt.name in tb.indexes for tb in self.tables.values()):
            raise SQLError(f"index {stmt.name} already exists")
        idx = Index(stmt.name, stmt.column, stmt.unique, pos)
        t.build_index(idx)
        t.indexes[stmt.name] = idx

    def snapshot(self):
        return {n: t.clone(self) for n, t in self.tables.items()}

    def restore(self, snap):
        self.tables = {n: t.clone(self) for n, t in snap.items()}

    def dump(self):
        tables = []
        for t in self.tables.values():
            tables.append({
                "name": t.name,
                "columns": [{"name": c.name, "type": c.type, "primary_key": c.primary_key, "not_null": c.not_null,
                             "unique": c.unique, "default": c.default} for c in t.columns],
                "next_rowid": t.next_rowid,
                "rows": [[rid, list(row)] for rid, row in t.rows.items()],
                "indexes": [{"name": i.name, "column": i.column, "unique": i.unique} for i in t.indexes.values()],
            })
        return {"version": 1, "tables": tables}

    @classmethod
    def load(cls, data):
        s = cls()
        try:
            for td in data["tables"]:
                cols = [ColumnDef(c["name"], c["type"], c["primary_key"], c["not_null"], c["unique"], c["default"])
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
        except (KeyError, TypeError, ValueError) as e:
            raise SQLError(f"corrupt database file: {e}") from None
        return s
