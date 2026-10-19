import unittest

from minidb import SQLError, ast as A
from minidb.storage import Storage


def make():
    s = Storage()
    s.create_table(A.CreateTable("t", [A.ColumnDef("id", "INTEGER", primary_key=True),
                                       A.ColumnDef("v", "REAL", unique=True), A.ColumnDef("s", "TEXT")]))
    return s, s.table("t")


class StorageTest(unittest.TestCase):
    def test_coercion(self):
        _, t = make()
        t.insert_row((1, 5, "a"))
        self.assertEqual(t.rows[1], (1, 5.0, "a"))
        self.assertIsInstance(t.rows[1][1], float)
        for bad in [(2.5, 1.0, "a"), (2, "x", "a"), (2, 1.0, 5), (None, 1.0, "a"), (2, 1.0)]:
            with self.assertRaises(SQLError):
                t.insert_row(bad)

    def test_unique_and_lookup(self):
        s, t = make()
        t.insert_row((1, 1.0, "a"))
        with self.assertRaises(SQLError):
            t.insert_row((2, 1.0, "b"))
        t.insert_row((2, None, "b"))
        t.insert_row((3, None, "c"))
        before = s.rows_read
        self.assertEqual(t.lookup("pk_t", 3), [(3, (3, None, "c"))])
        self.assertEqual(s.rows_read - before, 1)
        self.assertEqual(t.index_on("v").name, "uq_t_v")

    def test_update_rows_shift_and_atomic(self):
        _, t = make()
        for i in (1, 2, 3):
            t.insert_row((i, float(i), "x"))
        t.update_rows([(i, (i + 1, float(i), "x")) for i in (1, 2, 3)])
        self.assertEqual([r[0] for r in t.rows.values()], [2, 3, 4])
        with self.assertRaises(SQLError):
            t.update_rows([(1, (9, 9.0, "x")), (2, (9, 8.0, "x"))])
        self.assertEqual([r[0] for r in t.rows.values()], [2, 3, 4])
        self.assertEqual(t.lookup("pk_t", 2)[0][0], 1)

    def test_snapshot_restore_and_dump_load(self):
        s, t = make()
        t.insert_row((1, 1.0, "a"))
        snap = s.snapshot()
        t.insert_row((2, 2.0, "b"))
        s.drop_table("t")
        s.restore(snap)
        self.assertEqual(list(s.table("t").rows), [1])
        s2 = Storage.load(s.dump())
        self.assertEqual(s2.table("t").rows, s.table("t").rows)
        self.assertIsNotNone(s2.table("t").index_on("id"))

    def test_create_table_errors(self):
        s, _ = make()
        with self.assertRaises(SQLError):
            s.create_table(A.CreateTable("t", [A.ColumnDef("a", "INTEGER")]))
        with self.assertRaises(SQLError):
            s.create_table(A.CreateTable("u", [A.ColumnDef("a", "INTEGER", primary_key=True),
                                               A.ColumnDef("b", "INTEGER", primary_key=True)]))


if __name__ == "__main__":
    unittest.main()
