import json
import unittest

from minidb import SQLError, ast as A
from minidb import dml
from minidb.storage import Storage


def lit(v):
    return A.Literal(v)


def col(name, table=None):
    return A.Column(name, table)


def cd(name, type_="INTEGER", **kw):
    return A.ColumnDef(name, type_, **kw)


def make(*tables):
    s = Storage()
    for name, cols in tables:
        s.create_table(A.CreateTable(name, cols))
    return s


def ins(s, table, rows, columns=None, **kw):
    return dml.execute(s, A.Insert(table, columns, [[lit(v) for v in r] for r in rows], **kw))


def rows(s, name):
    return list(s.table(name).rows.values())


def fake_select(result_rows):
    return lambda q: (["x"], list(result_rows))


class InsertTest(unittest.TestCase):
    def test_insert_select(self):
        s = make(("t", [cd("id", primary_key=True), cd("n", "TEXT")]))
        r = dml.execute(s, A.Insert("t", None, [], select=A.Select([])), fake_select([(1, "a"), (2, "b")]))
        self.assertEqual(r.rowcount, 2)
        self.assertEqual(rows(s, "t"), [(1, "a"), (2, "b")])
        with self.assertRaises(SQLError):            # wrong width
            dml.execute(s, A.Insert("t", None, [], select=A.Select([])), fake_select([(3,)]))

    def test_insert_select_is_atomic(self):
        s = make(("t", [cd("id", primary_key=True)]))
        with self.assertRaises(SQLError):
            dml.execute(s, A.Insert("t", None, [], select=A.Select([])), fake_select([(1,), (2,), (1,)]))
        self.assertEqual(rows(s, "t"), [])

    def test_do_nothing(self):
        s = make(("t", [cd("id", primary_key=True), cd("n", "TEXT")]))
        ins(s, "t", [(1, "a")])
        oc = A.OnConflict(None, "NOTHING")
        r = ins(s, "t", [(1, "x"), (2, "b")], on_conflict=oc)
        self.assertEqual(r.rowcount, 1)
        self.assertEqual(rows(s, "t"), [(1, "a"), (2, "b")])
        r = ins(s, "t", [(1, "x")], on_conflict=A.OnConflict("id", "NOTHING"))
        self.assertEqual(r.rowcount, 0)

    def test_do_update(self):
        s = make(("t", [cd("id", primary_key=True), cd("n", "TEXT"), cd("c")]))
        ins(s, "t", [(1, "a", 1)])
        oc = A.OnConflict("id", "UPDATE", [("n", col("n", "excluded")), ("c", A.Binary("+", col("c"), lit(1)))])
        r = ins(s, "t", [(1, "z", 0), (2, "y", 0)], on_conflict=oc)
        self.assertEqual(r.rowcount, 2)
        self.assertEqual(rows(s, "t"), [(1, "z", 2), (2, "y", 0)])

    def test_do_update_needs_target(self):
        s = make(("t", [cd("id", primary_key=True)]))
        with self.assertRaises(SQLError):
            ins(s, "t", [(1,)], on_conflict=A.OnConflict(None, "UPDATE", [("id", lit(2))]))
        with self.assertRaises(SQLError):            # target is not unique
            s2 = make(("u", [cd("id", primary_key=True), cd("v")]))
            ins(s2, "u", [(1, 1)], on_conflict=A.OnConflict("v", "NOTHING"))

    def test_other_unique_violation_still_errors(self):
        s = make(("t", [cd("id", primary_key=True), cd("u", unique=True)]))
        ins(s, "t", [(1, 1)])
        with self.assertRaises(SQLError):
            ins(s, "t", [(2, 1)], on_conflict=A.OnConflict("id", "NOTHING"))
        self.assertEqual(rows(s, "t"), [(1, 1)])

    def test_do_update_failure_rolls_back_statement(self):
        s = make(("t", [cd("id", primary_key=True), cd("u", unique=True)]))
        ins(s, "t", [(1, 1), (2, 2)])
        oc = A.OnConflict("id", "UPDATE", [("u", lit(2))])
        with self.assertRaises(SQLError):
            ins(s, "t", [(3, 3), (1, 9)], on_conflict=oc)
        self.assertEqual(rows(s, "t"), [(1, 1), (2, 2)])


class AlterTest(unittest.TestCase):
    def alter(self, s, table, action, **kw):
        return dml.execute(s, A.AlterTable(table, action, **kw))

    def test_add_column(self):
        s = make(("t", [cd("id", primary_key=True)]))
        ins(s, "t", [(1,), (2,)])
        self.alter(s, "t", "ADD COLUMN", column=cd("d", default=5))
        self.alter(s, "t", "ADD COLUMN", column=cd("e", "TEXT"))
        self.assertEqual(rows(s, "t"), [(1, 5, None), (2, 5, None)])
        ins(s, "t", [(3, 1, "x")])
        for bad in (cd("f", not_null=True), cd("g", unique=True), cd("id"), cd("h", primary_key=True)):
            with self.assertRaises(SQLError):
                self.alter(s, "t", "ADD COLUMN", column=bad)
        self.alter(s, "t", "ADD COLUMN", column=cd("f", not_null=True, default=0))
        e = make(("e", [cd("id")]))
        self.alter(e, "e", "ADD COLUMN", column=cd("f", not_null=True))   # empty table is fine

    def test_rename_table(self):
        s = make(("t", [cd("id", primary_key=True), cd("u", unique=True)]), ("o", [cd("x")]))
        ins(s, "t", [(1, 1)])
        self.alter(s, "t", "RENAME TO", new_name="t2")
        self.assertEqual(list(s.tables), ["t2", "o"])
        self.assertEqual(s.table("t2").lookup("pk_t2", 1)[0][1], (1, 1))
        with self.assertRaises(SQLError):
            ins(s, "t2", [(1, 5)])
        with self.assertRaises(SQLError):
            s.table("t")
        with self.assertRaises(SQLError):
            self.alter(s, "t2", "RENAME TO", new_name="o")
        s.create_table(A.CreateTable("t", [cd("id", primary_key=True)]))   # old name is free again

    def test_rename_column(self):
        s = make(("t", [cd("id", primary_key=True), cd("u", unique=True), cd("v")]))
        ins(s, "t", [(1, 1, 1)])
        self.alter(s, "t", "RENAME COLUMN", name="u", new_name="w")
        t = s.table("t")
        self.assertEqual(t.column_position("w"), 1)
        self.assertEqual(t.index_on("w").column, "w")
        with self.assertRaises(SQLError):
            ins(s, "t", [(2, 1, 1)])
        for old, new in (("zz", "q"), ("v", "id")):
            with self.assertRaises(SQLError):
                self.alter(s, "t", "RENAME COLUMN", name=old, new_name=new)

    def test_drop_column(self):
        s = make(("t", [cd("id", primary_key=True), cd("u", unique=True), cd("v"), cd("w", "TEXT")]))
        ins(s, "t", [(1, 1, 7, "a")])
        s.create_index(A.CreateIndex("iw", "t", "w"))
        for name in ("id", "u", "w", "nope"):
            with self.assertRaises(SQLError):
                self.alter(s, "t", "DROP COLUMN", name=name)
        self.alter(s, "t", "DROP COLUMN", name="v")
        self.assertEqual(rows(s, "t"), [(1, 1, "a")])
        self.assertEqual(s.table("t").indexes["iw"].pos, 2)
        self.assertEqual(s.table("t").lookup("iw", "a")[0][1], (1, 1, "a"))
        one = make(("o", [cd("a")]))
        with self.assertRaises(SQLError):
            self.alter(one, "o", "DROP COLUMN", name="a")

    def test_alter_with_foreign_keys(self):
        s = make(("p", [cd("id", primary_key=True), cd("k", unique=True)]),
                 ("c", [cd("pid", references=("p", "id")), cd("x")]))
        with self.assertRaises(SQLError):
            self.alter(s, "c", "DROP COLUMN", name="pid")
        with self.assertRaises(SQLError):
            self.alter(s, "p", "DROP COLUMN", name="id")
        self.alter(s, "p", "RENAME TO", new_name="parent")
        self.assertEqual(s.table("c").columns[0].references, ("parent", "id"))
        self.alter(s, "parent", "RENAME COLUMN", name="id", new_name="pk")
        self.assertEqual(s.table("c").columns[0].references, ("parent", "pk"))
        ins(s, "parent", [(1, 1)])
        ins(s, "c", [(1, 0)])
        with self.assertRaises(SQLError):
            ins(s, "c", [(2, 0)])


class ForeignKeyTest(unittest.TestCase):
    def tables(self, action):
        return make(("p", [cd("id", primary_key=True), cd("code", "TEXT", unique=True)]),
                    ("c", [cd("id", primary_key=True), cd("pid", references=("p", "id"), on_delete=action)]))

    def test_create_rules(self):
        s = make(("p", [cd("id", primary_key=True), cd("plain")]))
        for ref in (("p", "plain"), ("p", "nope"), ("zz", "id")):
            with self.assertRaises(SQLError):
                s.create_table(A.CreateTable("c", [cd("x", references=ref)]))
        s.create_table(A.CreateTable("c", [cd("x", references=("p", "id"))]))

    def test_child_checks(self):
        s = self.tables("RESTRICT")
        ins(s, "p", [(1, "a")])
        ins(s, "c", [(1, 1), (2, None)])
        with self.assertRaises(SQLError):
            ins(s, "c", [(3, 9)])
        t = s.table("c")
        with self.assertRaises(SQLError):
            t.update_rows([(1, (1, 9))])
        t.update_rows([(2, (2, 1))])
        self.assertEqual(rows(s, "c"), [(1, 1), (2, 1)])

    def test_restrict(self):
        s = self.tables("RESTRICT")
        ins(s, "p", [(1, "a"), (2, "b")])
        ins(s, "c", [(1, 1)])
        p = s.table("p")
        with self.assertRaises(SQLError):
            p.delete_row(1)
        with self.assertRaises(SQLError):
            p.update_rows([(1, (5, "a"))])
        p.update_rows([(1, (1, "z"))])                  # the referenced value is unchanged
        p.delete_row(2)
        self.assertEqual(rows(s, "p"), [(1, "z")])

    def test_cascade_recursive(self):
        s = make(("a", [cd("id", primary_key=True)]),
                 ("b", [cd("id", primary_key=True), cd("a", references=("a", "id"), on_delete="CASCADE")]),
                 ("c", [cd("b", references=("b", "id"), on_delete="CASCADE")]))
        ins(s, "a", [(1,), (2,)])
        ins(s, "b", [(10, 1), (20, 2)])
        ins(s, "c", [(10,), (10,), (20,)])
        s.table("a").delete_row(1)
        self.assertEqual(rows(s, "b"), [(20, 2)])
        self.assertEqual(rows(s, "c"), [(20,)])

    def test_set_null_and_atomic_failure(self):
        s = self.tables("SET NULL")
        ins(s, "p", [(1, "a")])
        ins(s, "c", [(1, 1)])
        s.table("p").delete_row(1)
        self.assertEqual(rows(s, "c"), [(1, None)])
        # a RESTRICT further down the cascade chain: nothing may change
        s = make(("a", [cd("id", primary_key=True)]),
                 ("b", [cd("id", primary_key=True), cd("a", references=("a", "id"), on_delete="CASCADE")]),
                 ("c", [cd("b", references=("b", "id"))]))
        ins(s, "a", [(1,)])
        ins(s, "b", [(10, 1)])
        ins(s, "c", [(10,)])
        with self.assertRaises(SQLError):
            s.table("a").delete_row(1)
        self.assertEqual((rows(s, "a"), rows(s, "b"), rows(s, "c")), ([(1,)], [(10, 1)], [(10,)]))

    def test_cascade_on_referenced_update(self):
        s = self.tables("CASCADE")
        ins(s, "p", [(1, "a")])
        ins(s, "c", [(1, 1)])
        s.table("p").update_rows([(1, (7, "a"))])
        self.assertEqual(rows(s, "c"), [])

    def test_swap_keeps_children(self):
        s = self.tables("RESTRICT")
        ins(s, "p", [(1, "a"), (2, "b")])
        ins(s, "c", [(1, 1), (2, 2)])
        s.table("p").update_rows([(1, (2, "a")), (2, (1, "b"))])
        self.assertEqual(rows(s, "c"), [(1, 1), (2, 2)])

    def test_self_reference(self):
        s = make(("e", [cd("id", primary_key=True), cd("boss", references=("e", "id"), on_delete="CASCADE")]))
        ins(s, "e", [(1, None), (2, 1), (3, 2)])
        with self.assertRaises(SQLError):
            ins(s, "e", [(4, 9)])
        s.table("e").delete_row(1)
        self.assertEqual(rows(s, "e"), [])
        s.drop_table("e")

    def test_drop_table_referenced(self):
        s = self.tables("CASCADE")
        with self.assertRaises(SQLError):
            s.drop_table("p")
        s.drop_table("c")
        s.drop_table("p")


class ViewTest(unittest.TestCase):
    def test_views(self):
        s = make(("t", [cd("id")]))
        q = A.Select([A.SelectItem(None, star=True)], A.TableRef("t"))
        calls = []
        dml.execute(s, A.CreateView("v", q), lambda query: calls.append(query) or ([], []))
        self.assertEqual(calls, [q])
        self.assertIs(s.views["v"], q)
        for stmt in (A.CreateView("v", q), A.CreateView("t", q)):
            with self.assertRaises(SQLError):
                dml.execute(s, stmt, fake_select([]))
        with self.assertRaises(SQLError):
            s.create_table(A.CreateTable("v", [cd("a")]))
        with self.assertRaises(SQLError):
            s.table("v")
        with self.assertRaises(SQLError):
            ins(s, "v", [(1,)])
        with self.assertRaises(SQLError):
            dml.execute(s, A.AlterTable("t", "RENAME TO", new_name="v"))
        with self.assertRaises(SQLError):
            dml.execute(s, A.CreateView("bad", q), lambda query: (_ for _ in ()).throw(SQLError("no such table")))
        self.assertNotIn("bad", s.views)
        dml.execute(s, A.DropView("v"))
        dml.execute(s, A.DropView("v", True))
        with self.assertRaises(SQLError):
            dml.execute(s, A.DropView("v"))


class PersistenceTest(unittest.TestCase):
    def test_transactions_and_dump(self):
        s = make(("p", [cd("id", primary_key=True)]),
                 ("c", [cd("pid", references=("p", "id"), on_delete="SET NULL")]))
        q = A.Select([A.SelectItem(col("id"))], A.TableRef("p"), where=A.Binary(">", col("id"), lit(0)),
                     order_by=[A.OrderItem(col("id"), False)])
        q.items[0].text = "id"
        dml.execute(s, A.CreateView("v", q), fake_select([]))
        snap = s.snapshot()
        dml.execute(s, A.DropView("v"))
        s.drop_table("c")
        s.restore(snap)
        self.assertIn("v", s.views)
        self.assertIn("c", s.tables)
        s2 = Storage.load(json.loads(json.dumps(s.dump())))
        self.assertEqual(s2.views["v"], q)
        self.assertEqual(s2.views["v"].items[0].text, "id")
        self.assertEqual(s2.table("c").columns[0].references, ("p", "id"))
        self.assertEqual(s2.table("c").columns[0].on_delete, "SET NULL")
        ins(s2, "p", [(1,)])
        ins(s2, "c", [(1,)])
        s2.table("p").delete_row(1)
        self.assertEqual(rows(s2, "c"), [(None,)])

    def test_load_version_1(self):
        s = Storage.load({"version": 1, "tables": [{"name": "t", "columns": [
            {"name": "a", "type": "INTEGER", "primary_key": False, "not_null": False, "unique": False,
             "default": None}], "next_rowid": 1, "rows": [], "indexes": []}]})
        self.assertEqual(s.views, {})
        self.assertIn("t", s.tables)


if __name__ == "__main__":
    unittest.main()
