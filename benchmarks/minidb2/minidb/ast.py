"""The syntax tree. Finished (version 2): do not change it. The parser builds these, the executor reads them.

Version 2 adds nodes and fields; every new field has a default, so the version 1 code keeps working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Union


# ---- expressions -----------------------------------------------------------------------------------------------

@dataclass
class Literal:
    value: Any                      # int, float, str, bool or None (NULL)


@dataclass
class Column:
    name: str                       # lowercase
    table: Optional[str] = None     # the qualifier ("e" in e.name), lowercase


@dataclass
class Unary:
    op: str                         # "-", "NOT"
    operand: "Expr"


@dataclass
class Binary:
    op: str                         # + - * / % || = <> < <= > >= AND OR   (the parser turns != into <>)
    left: "Expr"
    right: "Expr"


@dataclass
class IsNull:
    operand: "Expr"
    negated: bool = False           # IS NOT NULL


@dataclass
class InList:
    operand: "Expr"
    items: list["Expr"]
    negated: bool = False


@dataclass
class InSelect:
    operand: "Expr"
    select: "Query"
    negated: bool = False


@dataclass
class Between:
    operand: "Expr"
    low: "Expr"
    high: "Expr"
    negated: bool = False


@dataclass
class Like:
    operand: "Expr"
    pattern: "Expr"
    negated: bool = False


@dataclass
class OrderItem:
    expr: "Expr"                    # an Expr, or Literal(int) for ORDER BY 2
    descending: bool = False


@dataclass
class WindowSpec:
    partition_by: list["Expr"] = field(default_factory=list)
    order_by: list[OrderItem] = field(default_factory=list)


@dataclass
class Func:
    name: str                       # UPPERCASE: UPPER, COUNT, SUM, ...
    args: list["Expr"]
    star: bool = False              # COUNT(*)
    distinct: bool = False          # COUNT(DISTINCT x)
    over: Optional[WindowSpec] = None   # set for a window function: SUM(x) OVER (...), ROW_NUMBER() OVER (...)


@dataclass
class ScalarSelect:
    select: "Query"                 # (SELECT max(x) FROM t) used as a value


@dataclass
class Case:
    operand: Optional["Expr"]                       # CASE x WHEN ...: the x; None for CASE WHEN cond ...
    whens: list[tuple["Expr", "Expr"]]              # (when, then) in order
    else_: Optional["Expr"] = None


@dataclass
class Cast:
    operand: "Expr"
    type: str                                       # INTEGER, REAL, TEXT, BOOLEAN


@dataclass
class Exists:
    select: "Query"
    negated: bool = False


Expr = Union[Literal, Column, Unary, Binary, IsNull, InList, InSelect, Between, Like, Func, ScalarSelect, Case, Cast,
             Exists]


# ---- statements ------------------------------------------------------------------------------------------------

@dataclass
class ColumnDef:
    name: str
    type: str                       # INTEGER, REAL, TEXT, BOOLEAN
    primary_key: bool = False
    not_null: bool = False
    unique: bool = False
    default: Any = None             # a literal value, or None
    references: Optional[tuple[str, str]] = None    # REFERENCES parent(column) -> ("parent", "column")
    on_delete: str = "RESTRICT"                     # RESTRICT, CASCADE or SET NULL


@dataclass
class CreateTable:
    name: str
    columns: list[ColumnDef]
    if_not_exists: bool = False


@dataclass
class DropTable:
    name: str
    if_exists: bool = False


@dataclass
class CreateIndex:
    name: str
    table: str
    column: str
    unique: bool = False


@dataclass
class OnConflict:
    target: Optional[str]           # ON CONFLICT (col); None when not written
    action: str                     # "NOTHING" or "UPDATE"
    assignments: list[tuple[str, "Expr"]] = field(default_factory=list)   # DO UPDATE SET c = expr (excluded.c allowed)


@dataclass
class Insert:
    table: str
    columns: Optional[list[str]]    # None when not written
    rows: list[list[Expr]]          # empty when `select` is used
    select: Optional["Query"] = None            # INSERT INTO t [(cols)] SELECT ...
    on_conflict: Optional[OnConflict] = None


@dataclass
class SelectItem:
    expr: Optional[Expr]            # None for * and t.*
    alias: Optional[str] = None
    star: bool = False
    star_table: Optional[str] = None


@dataclass
class TableRef:
    name: str                       # "" for a derived table
    alias: Optional[str] = None
    select: Optional["Query"] = None    # FROM (SELECT ...) alias


@dataclass
class Join:
    kind: str                       # "INNER", "LEFT", "RIGHT" or "CROSS"
    table: TableRef
    on: Optional[Expr] = None       # None for CROSS


@dataclass
class CTE:
    name: str
    select: "Query"


@dataclass
class Select:
    items: list[SelectItem]
    table: Optional[TableRef] = None
    joins: list[Join] = field(default_factory=list)
    where: Optional[Expr] = None
    group_by: list[Expr] = field(default_factory=list)
    having: Optional[Expr] = None
    order_by: list[OrderItem] = field(default_factory=list)
    limit: Optional[int] = None
    offset: Optional[int] = None
    distinct: bool = False
    ctes: list[CTE] = field(default_factory=list)   # WITH name AS (...), ...


@dataclass
class Compound:
    """left UNION [ALL] / INTERSECT / EXCEPT right. Left-associative: a UNION b EXCEPT c is (a UNION b) EXCEPT c.
    ORDER BY / LIMIT / OFFSET written after the last select belong to the whole result."""
    op: str                         # "UNION", "UNION ALL", "INTERSECT", "EXCEPT"
    left: "Query"
    right: "Query"
    order_by: list[OrderItem] = field(default_factory=list)
    limit: Optional[int] = None
    offset: Optional[int] = None
    ctes: list[CTE] = field(default_factory=list)


Query = Union[Select, Compound]


@dataclass
class Explain:
    select: Select


@dataclass
class CreateView:
    name: str
    select: "Query"


@dataclass
class DropView:
    name: str
    if_exists: bool = False


@dataclass
class AlterTable:
    table: str
    action: str                     # "ADD COLUMN", "DROP COLUMN", "RENAME COLUMN", "RENAME TO"
    column: Optional[ColumnDef] = None      # ADD COLUMN
    name: Optional[str] = None              # DROP COLUMN: the column; RENAME COLUMN: the old name
    new_name: Optional[str] = None          # RENAME COLUMN / RENAME TO


@dataclass
class Update:
    table: str
    assignments: list[tuple[str, Expr]]
    where: Optional[Expr] = None


@dataclass
class Delete:
    table: str
    where: Optional[Expr] = None


@dataclass
class Begin:
    pass


@dataclass
class Commit:
    pass


@dataclass
class Rollback:
    pass


Statement = Union[CreateTable, DropTable, CreateIndex, Insert, Select, Compound, Explain, Update, Delete, Begin, Commit,
                  Rollback, CreateView, DropView, AlterTable]
