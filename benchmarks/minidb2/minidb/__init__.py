"""MiniDB: a small SQL database engine."""

from .errors import SQLError
from .result import Result


def __getattr__(name):                       # Database is imported on first use, so the parts can be built one by one
    if name == "Database":
        from .database import Database
        return Database
    raise AttributeError(name)


__all__ = ["Database", "Result", "SQLError"]
