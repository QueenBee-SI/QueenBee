"""Database: parses and runs statements, transactions, persistence."""

from __future__ import annotations

import json
import os

from . import ast as A
from .errors import SQLError
from .executor import Executor
from .parser import parse
from .result import Result
from .storage import Storage

_READ_ONLY = (A.Select, A.Explain)


class Database:
    def __init__(self, path=None):
        self.path = path
        self._txn = None
        self.storage = Storage()
        if path is not None and os.path.exists(path) and os.path.getsize(path) > 0:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except ValueError as e:
                raise SQLError(f"cannot read database file {path}: {e}") from None
            self.storage = Storage.load(data)
        self.executor = Executor(self.storage)

    @property
    def rows_read(self):
        return self.storage.rows_read

    def execute(self, sql):
        stmts = parse(sql)
        if len(stmts) != 1:
            raise SQLError(f"expected exactly one statement, got {len(stmts)}")
        return self._run(stmts[0])

    def executescript(self, sql):
        return [self._run(s) for s in parse(sql)]

    def close(self):
        self._txn = None                    # an open transaction is abandoned, never saved

    def _run(self, stmt):
        if isinstance(stmt, A.Begin):
            if self._txn is not None:
                raise SQLError("a transaction is already open")
            self._txn = self.storage.snapshot()
            return Result(message="OK")
        if isinstance(stmt, A.Commit):
            if self._txn is None:
                raise SQLError("no transaction is open")
            self._txn = None
            self._save()
            return Result(message="OK")
        if isinstance(stmt, A.Rollback):
            if self._txn is None:
                raise SQLError("no transaction is open")
            self.storage.restore(self._txn)
            self._txn = None
            return Result(message="OK")
        if isinstance(stmt, _READ_ONLY):
            return self.executor.execute(stmt)
        snap = self.storage.snapshot()
        try:
            result = self.executor.execute(stmt)
        except BaseException:
            self.storage.restore(snap)
            raise
        if self._txn is None:
            self._save()
        return result

    def _save(self):
        if self.path is None:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.storage.dump(), f)
        os.replace(tmp, self.path)
