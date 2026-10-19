"""The command line: python -m minidb [file.db] reads SQL from standard input."""

from __future__ import annotations

import sys

from .database import Database
from .errors import SQLError
from .lexer import tokenize


def split_statements(text):
    """Split at top-level semicolons (not inside strings or comments)."""
    out, buf, i, n, in_str = [], [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            buf.append(c)
            if c == chr(39):
                in_str = False
        elif c == chr(39):
            in_str = True
            buf.append(c)
        elif text.startswith("--", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            buf.append(text[i:j])
            i = j
            continue
        elif c == ";":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    out.append("".join(buf))
    return out


def _blank(chunk):
    try:
        return len(tokenize(chunk)) == 1
    except SQLError:
        return False


def _cell(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def format_table(columns, rows):
    cells = [[str(c) for c in columns]] + [[_cell(v) for v in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(columns))]

    def line(r):
        return " | ".join(c.ljust(widths[i]) if i < len(widths) - 1 else c for i, c in enumerate(r))

    out = [line(cells[0]), "-+-".join("-" * w for w in widths)] + [line(r) for r in cells[1:]]
    out.append(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")
    return out


def main(argv):
    db = Database(argv[0] if argv else None)
    for chunk in split_statements(sys.stdin.read()):
        if _blank(chunk):
            continue
        try:
            r = db.execute(chunk)
        except SQLError as e:
            print(f"Error: {e}")
            continue
        if r.columns:
            print("\n".join(format_table(r.columns, r.rows)))
        else:
            print(r.message or "OK")
    db.close()
    return 0
