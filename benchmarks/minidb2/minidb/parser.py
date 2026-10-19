"""Recursive-descent parser: parse(sql) -> list of ast statements."""

from __future__ import annotations

from . import ast as A
from .parser_base import TokenStream, clean_text

TYPES = ("INTEGER", "REAL", "TEXT", "BOOLEAN")
_LITERALS = {"NULL": None, "TRUE": True, "FALSE": False}
_TXN = {"BEGIN": A.Begin, "COMMIT": A.Commit, "ROLLBACK": A.Rollback}


class Parser(TokenStream):
    # ---- statements
    def statements(self):
        out = []
        while True:
            while self.accept("PUNCT", ";"):
                pass
            if self.at("EOF"):
                return out
            out.append(self.statement())
            if not (self.at("PUNCT", ";") or self.at("EOF")):
                self.error("unexpected token after statement")

    def statement(self):
        t = self.peek()
        if t.kind != "KEYWORD":
            self.error("expected a statement")
        v = t.value
        if v == "SELECT":
            return self.select()
        if v == "EXPLAIN":
            self.next()
            if not self.at("KEYWORD", "SELECT"):
                self.error("EXPLAIN needs a SELECT")
            return A.Explain(self.select())
        if v == "CREATE":
            return self.create()
        if v == "DROP":
            self.next()
            self.expect_kw("TABLE")
            if_exists = False
            if self.kw("IF"):
                self.expect_kw("EXISTS")
                if_exists = True
            return A.DropTable(self.ident(), if_exists)
        if v == "INSERT":
            return self.insert()
        if v == "UPDATE":
            return self.update()
        if v == "DELETE":
            self.next()
            self.expect_kw("FROM")
            name = self.ident()
            return A.Delete(name, self.where())
        if v in _TXN:
            self.next()
            return _TXN[v]()
        self.error("unknown statement")

    def where(self):
        return self.expr() if self.kw("WHERE") else None

    def create(self):
        self.expect_kw("CREATE")
        if self.kw("TABLE"):
            if_not_exists = False
            if self.kw("IF"):
                self.expect_kw("NOT")
                self.expect_kw("EXISTS")
                if_not_exists = True
            name = self.ident()
            self.expect("PUNCT", "(")
            cols = [self.column_def()]
            while self.accept("PUNCT", ","):
                cols.append(self.column_def())
            self.expect("PUNCT", ")")
            return A.CreateTable(name, cols, if_not_exists)
        unique = bool(self.kw("UNIQUE"))
        self.expect_kw("INDEX")
        name = self.ident()
        self.expect_kw("ON")
        table = self.ident()
        self.expect("PUNCT", "(")
        col = self.ident()
        self.expect("PUNCT", ")")
        return A.CreateIndex(name, table, col, unique)

    def column_def(self):
        name = self.ident()
        t = self.expect("IDENT")
        if t.value.upper() not in TYPES:
            self.error("unknown column type " + t.value)
        col = A.ColumnDef(name, t.value.upper())
        while True:
            if self.kw("PRIMARY"):
                self.expect_kw("KEY")
                col.primary_key = True
            elif self.kw("NOT"):
                self.expect_kw("NULL")
                col.not_null = True
            elif self.kw("UNIQUE"):
                col.unique = True
            elif self.kw("DEFAULT"):
                col.default = self.literal_value()
            else:
                return col

    def literal_value(self):
        neg = bool(self.accept("OP", "-"))
        t = self.next()
        if t.kind == "NUMBER":
            return -t.value if neg else t.value
        if neg:
            self.i -= 1
            self.error("expected a number")
        if t.kind == "STRING":
            return t.value
        if t.kind == "KEYWORD" and t.value in _LITERALS:
            return _LITERALS[t.value]
        self.i -= 1
        self.error("expected a literal value")

    def insert(self):
        self.expect_kw("INSERT")
        self.expect_kw("INTO")
        table = self.ident()
        cols = None
        if self.accept("PUNCT", "("):
            cols = [self.ident()]
            while self.accept("PUNCT", ","):
                cols.append(self.ident())
            self.expect("PUNCT", ")")
        self.expect_kw("VALUES")
        rows = [self.value_row()]
        while self.accept("PUNCT", ","):
            rows.append(self.value_row())
        return A.Insert(table, cols, rows)

    def value_row(self):
        self.expect("PUNCT", "(")
        row = [self.expr()]
        while self.accept("PUNCT", ","):
            row.append(self.expr())
        self.expect("PUNCT", ")")
        return row

    def update(self):
        self.expect_kw("UPDATE")
        table = self.ident()
        self.expect_kw("SET")
        sets = []
        while True:
            col = self.ident()
            self.expect("OP", "=")
            sets.append((col, self.expr()))
            if not self.accept("PUNCT", ","):
                break
        return A.Update(table, sets, self.where())

    def count(self):
        t = self.expect("NUMBER")
        if not isinstance(t.value, int):
            self.i -= 1
            self.error("expected an integer")
        return t.value

    def table_ref(self):
        name = self.ident()
        alias = None
        if self.kw("AS"):
            alias = self.ident()
        elif self.at("IDENT"):
            alias = self.next().value
        return A.TableRef(name, alias)

    def select_item(self):
        if self.at("OP", "*"):
            self.next()
            return A.SelectItem(None, star=True)
        if self.at("IDENT") and self.at("PUNCT", ".", k=1) and self.at("OP", "*", k=2):
            t = self.next().value
            self.next()
            self.next()
            return A.SelectItem(None, star=True, star_table=t)
        start = self.peek().pos
        e = self.expr()
        text = clean_text(self.sql[start:self.peek().pos])
        alias = None
        if self.kw("AS"):
            alias = self.ident()
        elif self.at("IDENT"):
            alias = self.next().value
        item = A.SelectItem(e, alias)
        item.text = text
        return item

    def select(self):
        self.expect_kw("SELECT")
        sel = A.Select(items=[])
        sel.distinct = bool(self.kw("DISTINCT"))
        sel.items.append(self.select_item())
        while self.accept("PUNCT", ","):
            sel.items.append(self.select_item())
        if self.kw("FROM"):
            sel.table = self.table_ref()
            while True:
                if self.kw("INNER"):
                    self.expect_kw("JOIN")
                    kind = "INNER"
                elif self.kw("JOIN"):
                    kind = "INNER"
                elif self.kw("LEFT"):
                    self.kw("OUTER")
                    self.expect_kw("JOIN")
                    kind = "LEFT"
                else:
                    break
                ref = self.table_ref()
                self.expect_kw("ON")
                sel.joins.append(A.Join(kind, ref, self.expr()))
        sel.where = self.where()
        if self.kw("GROUP"):
            self.expect_kw("BY")
            sel.group_by = [self.expr()]
            while self.accept("PUNCT", ","):
                sel.group_by.append(self.expr())
        if self.kw("HAVING"):
            sel.having = self.expr()
        if self.kw("ORDER"):
            self.expect_kw("BY")
            while True:
                e = self.expr()
                desc = False
                if self.kw("DESC"):
                    desc = True
                else:
                    self.kw("ASC")
                sel.order_by.append(A.OrderItem(e, desc))
                if not self.accept("PUNCT", ","):
                    break
        if self.kw("LIMIT"):
            sel.limit = self.count()
            if self.kw("OFFSET"):
                sel.offset = self.count()
        return sel

    # ---- expressions
    def expr(self):
        left = self.and_expr()
        while self.kw("OR"):
            left = A.Binary("OR", left, self.and_expr())
        return left

    def and_expr(self):
        left = self.not_expr()
        while self.kw("AND"):
            left = A.Binary("AND", left, self.not_expr())
        return left

    def not_expr(self):
        if self.kw("NOT"):
            return A.Unary("NOT", self.not_expr())
        return self.comparison()

    def comparison(self):
        left = self.concat()
        t = self.peek()
        if t.kind == "OP" and t.value in ("=", "<>", "!=", "<", "<=", ">", ">="):
            self.next()
            return A.Binary("<>" if t.value == "!=" else t.value, left, self.concat())
        if self.kw("IS"):
            neg = bool(self.kw("NOT"))
            self.expect_kw("NULL")
            return A.IsNull(left, neg)
        neg = False
        if self.at("KEYWORD", "NOT") and self.at("KEYWORD", "IN", "BETWEEN", "LIKE", k=1):
            self.next()
            neg = True
        if self.kw("IN"):
            self.expect("PUNCT", "(")
            if self.at("KEYWORD", "SELECT"):
                sel = self.select()
                self.expect("PUNCT", ")")
                return A.InSelect(left, sel, neg)
            items = [self.expr()]
            while self.accept("PUNCT", ","):
                items.append(self.expr())
            self.expect("PUNCT", ")")
            return A.InList(left, items, neg)
        if self.kw("BETWEEN"):
            low = self.concat()
            self.expect_kw("AND")
            return A.Between(left, low, self.concat(), neg)
        if self.kw("LIKE"):
            return A.Like(left, self.concat(), neg)
        return left

    def concat(self):
        left = self.additive()
        while self.accept("OP", "||"):
            left = A.Binary("||", left, self.additive())
        return left

    def additive(self):
        left = self.term()
        while self.at("OP", "+", "-"):
            op = self.next().value
            left = A.Binary(op, left, self.term())
        return left

    def term(self):
        left = self.unary()
        while self.at("OP", "*", "/", "%"):
            op = self.next().value
            left = A.Binary(op, left, self.unary())
        return left

    def unary(self):
        if self.accept("OP", "-"):
            return A.Unary("-", self.unary())
        return self.primary()

    def primary(self):
        t = self.peek()
        if t.kind == "NUMBER" or t.kind == "STRING":
            self.next()
            return A.Literal(t.value)
        if t.kind == "KEYWORD" and t.value in _LITERALS:
            self.next()
            return A.Literal(_LITERALS[t.value])
        if t.kind == "PUNCT" and t.value == "(":
            self.next()
            if self.at("KEYWORD", "SELECT"):
                sel = self.select()
                self.expect("PUNCT", ")")
                return A.ScalarSelect(sel)
            e = self.expr()
            self.expect("PUNCT", ")")
            return e
        if t.kind == "IDENT":
            self.next()
            if self.accept("PUNCT", "("):
                name = t.value.upper()
                if self.accept("OP", "*"):
                    self.expect("PUNCT", ")")
                    return A.Func(name, [], star=True)
                distinct = bool(self.kw("DISTINCT"))
                args = []
                if not self.at("PUNCT", ")"):
                    args.append(self.expr())
                    while self.accept("PUNCT", ","):
                        args.append(self.expr())
                self.expect("PUNCT", ")")
                return A.Func(name, args, distinct=distinct)
            if self.accept("PUNCT", "."):
                return A.Column(self.ident(), t.value)
            return A.Column(t.value)
        self.error("expected an expression")


def parse(sql):
    return Parser(sql).statements()
