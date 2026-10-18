"""The command line. Already written: it calls the three parts and prints what they return."""

import argparse
import sys

from . import export, search, store


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="notes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add")
    add.add_argument("text")
    add.add_argument("--tag", action="append", default=[])
    sub.add_parser("list")
    find = sub.add_parser("find")
    find.add_argument("query", nargs="?")
    find.add_argument("--tag")
    exp = sub.add_parser("export")
    exp.add_argument("path")
    a = ap.parse_args(argv)
    if a.cmd == "add":
        print(f"added {store.add(a.text, a.tag)}")
    elif a.cmd == "list":
        for note in store.all():
            print(store.line(note))
    elif a.cmd == "find":
        found = search.find(store.all(), a.query, a.tag)
        if not found:
            print("no notes found")
            return 1
        for note in found:
            print(store.line(note))
    else:
        notes = store.all()
        with open(a.path, "w", encoding="utf-8") as fh:
            fh.write(export.to_markdown(notes))
        print(f"wrote {len(notes)} notes to {a.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
