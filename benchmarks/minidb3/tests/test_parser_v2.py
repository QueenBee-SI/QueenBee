import unittest

from minidb import SQLError, ast as A
from minidb.lexer import tokenize
from minidb.parser import parse


def one(sql):
    return parse(sql)[0]


class ExpressionSyntaxTest(unittest.TestCase):
    def test_searched_case(self):
        e = one("SELECT CASE WHEN a > 1 THEN 'x' WHEN a < 0 THEN 'y' ELSE 'z' END").items[0].expr
        self.assertIsNone(e.operand)
        self.assertEqual(len(e.whens), 2)
        self.assertEqual(e.else_, A.Literal("z"))

    def test_simple_case_without_else(self):
        e = one("SELECT CASE a WHEN 1 THEN 2 END").items[0].expr
        self.assertEqual(e, A.Case(A.Column("a"), [(A.Literal(1), A.Literal(2))], None))

    def test_cast(self):
        self.assertEqual(one("SELECT CAST(a AS text)").items[0].expr, A.Cast(A.Column("a"), "TEXT"))
        with self.assertRaises(SQLError):
            one("SELECT CAST(a AS blob)")

    def test_exists(self):
        w = one("SELECT 1 WHERE NOT EXISTS (SELECT 1) AND EXISTS (SELECT 2)").where
        self.assertTrue(w.left.negated)
        self.assertIsInstance(w.right, A.Exists)
        self.assertFalse(w.right.negated)

    def test_window_spec(self):
        f = one("SELECT RANK() OVER (PARTITION BY a, b ORDER BY c DESC) FROM t").items[0].expr
        self.assertEqual(f.name, "RANK")
        self.assertEqual(f.over.partition_by, [A.Column("a"), A.Column("b")])
        self.assertTrue(f.over.order_by[0].descending)
        f = one("SELECT SUM(x) OVER () FROM t").items[0].expr
        self.assertEqual(f.over, A.WindowSpec())
        self.assertIsNone(one("SELECT SUM(x) FROM t").items[0].expr.over)

    def test_contextual_words_stay_identifiers(self):
        s = one("SELECT over, cast, partition, add, column, rename, to, do FROM t")
        self.assertEqual(len(s.items), 8)
        self.assertEqual([t.kind for t in tokenize("over cast")][:2], ["IDENT", "IDENT"])


class QuerySyntaxTest(unittest.TestCase):
    def test_derived_table(self):
        s = one("SELECT x FROM (SELECT 1 AS x) d JOIN t ON d.x = t.a")
        self.assertEqual(s.table.name, "")
        self.assertEqual(s.table.alias, "d")
        self.assertIsInstance(s.table.select, A.Select)

    def test_derived_table_needs_alias(self):
        with self.assertRaises(SQLError):
            parse("SELECT x FROM (SELECT 1 AS x)")
        s = one("SELECT x FROM (WITH a AS (SELECT 1 AS x) SELECT x FROM a)")
        self.assertIsNone(s.table.alias)

    def test_with(self):
        s = one("WITH a AS (SELECT 1), b AS (SELECT * FROM a) SELECT * FROM b")
        self.assertEqual([c.name for c in s.ctes], ["a", "b"])

    def test_with_in_subquery(self):
        w = one("SELECT 1 WHERE a IN (WITH q AS (SELECT 1) SELECT * FROM q)").where
        self.assertEqual(w.select.ctes[0].name, "q")

    def test_compound(self):
        q = one("SELECT a FROM t UNION SELECT b FROM u INTERSECT SELECT c FROM v ORDER BY 1 DESC LIMIT 3 OFFSET 1")
        self.assertEqual(q.op, "INTERSECT")
        self.assertEqual(q.left.op, "UNION")
        self.assertEqual(q.limit, 3)
        self.assertEqual(q.offset, 1)
        self.assertEqual(q.order_by, [A.OrderItem(A.Literal(1), True)])
        self.assertEqual(q.right.order_by, [])
        self.assertIsNone(q.right.limit)

    def test_union_all_with_cte(self):
        q = one("WITH a AS (SELECT 1) SELECT 1 UNION ALL SELECT 2 EXCEPT SELECT 3")
        self.assertEqual(q.op, "EXCEPT")
        self.assertEqual(q.left.op, "UNION ALL")
        self.assertEqual(len(q.ctes), 1)

    def test_order_before_set_operator_is_an_error(self):
        with self.assertRaises(SQLError):
            parse("SELECT 1 ORDER BY 1 UNION SELECT 2")

    def test_joins(self):
        s = one("SELECT * FROM a RIGHT OUTER JOIN b ON a.x = b.x CROSS JOIN c RIGHT JOIN d ON 1 = 1")
        self.assertEqual([j.kind for j in s.joins], ["RIGHT", "CROSS", "RIGHT"])
        self.assertIsNone(s.joins[1].on)
        with self.assertRaises(SQLError):
            parse("SELECT * FROM a CROSS JOIN b ON 1 = 1")

    def test_views(self):
        v = one("CREATE VIEW v AS SELECT a FROM t UNION SELECT b FROM u")
        self.assertIsInstance(v, A.CreateView)
        self.assertIsInstance(v.select, A.Compound)
        self.assertEqual(one("DROP VIEW IF EXISTS v"), A.DropView("v", True))
        self.assertEqual(one("DROP TABLE t"), A.DropTable("t", False))


class StatementSyntaxTest(unittest.TestCase):
    def test_insert_select(self):
        i = one("INSERT INTO t (a, b) SELECT x, y FROM u")
        self.assertEqual(i.columns, ["a", "b"])
        self.assertEqual(i.rows, [])
        self.assertIsInstance(i.select, A.Select)
        self.assertIsInstance(one("INSERT INTO t SELECT 1 UNION SELECT 2").select, A.Compound)

    def test_on_conflict(self):
        i = one("INSERT INTO t VALUES (1) ON CONFLICT (a) DO NOTHING")
        self.assertEqual(i.on_conflict, A.OnConflict("a", "NOTHING"))
        i = one("INSERT INTO t VALUES (1) ON CONFLICT DO NOTHING")
        self.assertIsNone(i.on_conflict.target)
        i = one("INSERT INTO t VALUES (1) ON CONFLICT (a) DO UPDATE SET v = v + excluded.v, n = 1")
        self.assertEqual(i.on_conflict.action, "UPDATE")
        self.assertEqual(i.on_conflict.assignments[0][1].right, A.Column("v", "excluded"))
        self.assertEqual(len(i.on_conflict.assignments), 2)
        self.assertIsNone(one("INSERT INTO t VALUES (1)").on_conflict)

    def test_alter(self):
        a = one("ALTER TABLE t ADD COLUMN c INTEGER NOT NULL DEFAULT 0")
        self.assertEqual(a.action, "ADD COLUMN")
        self.assertEqual((a.column.name, a.column.default, a.column.not_null), ("c", 0, True))
        self.assertEqual(one("ALTER TABLE t ADD c TEXT").column.type, "TEXT")
        self.assertEqual(one("ALTER TABLE t DROP COLUMN c"), A.AlterTable("t", "DROP COLUMN", name="c"))
        self.assertEqual(one("ALTER TABLE t RENAME COLUMN a TO b"),
                         A.AlterTable("t", "RENAME COLUMN", name="a", new_name="b"))
        self.assertEqual(one("ALTER TABLE t RENAME TO u"), A.AlterTable("t", "RENAME TO", new_name="u"))
        with self.assertRaises(SQLError):
            parse("ALTER TABLE t")

    def test_references(self):
        s = one("CREATE TABLE c (id INTEGER PRIMARY KEY, p INTEGER REFERENCES parent(id) ON DELETE CASCADE, "
                "q INTEGER REFERENCES parent(id) ON DELETE SET NULL, r INTEGER REFERENCES parent(id) NOT NULL)")
        self.assertEqual(s.columns[1].references, ("parent", "id"))
        self.assertEqual([c.on_delete for c in s.columns[1:]], ["CASCADE", "SET NULL", "RESTRICT"])
        self.assertTrue(s.columns[3].not_null)
        self.assertIsNone(s.columns[0].references)

    def test_errors(self):
        for bad in ["SELECT CASE END", "SELECT CASE WHEN 1 THEN 2", "SELECT 1 UNION", "SELECT 1 WHERE EXISTS (1)",
                    "SELECT SUM(x) OVER (PARTITION a) FROM t", "INSERT INTO t VALUES (1) ON CONFLICT DO",
                    "CREATE TABLE t (a INTEGER REFERENCES p)", "CREATE TABLE t (a INTEGER REFERENCES p(x) ON DELETE)",
                    "CREATE VIEW v SELECT 1", "ALTER TABLE t RENAME a TO b"]:
            with self.assertRaises(SQLError, msg=bad):
                parse(bad)


if __name__ == "__main__":
    unittest.main()
