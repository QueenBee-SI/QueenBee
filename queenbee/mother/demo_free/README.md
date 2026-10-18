# Notes: the free demo project

A tiny command-line notebook in **three parts that must fit together**. Three worker bees build it in
the same folder at the same time, one part each:

| Part | File | Job |
|---|---|---|
| Store | `notes/store.py` | keeps the notes in a file |
| Search | `notes/search.py` | finds notes by text and tag |
| Export | `notes/export.py` | writes the notes as Markdown |

`notes/__main__.py` (already written) calls the three parts. The command line:

    python -m notes add "buy milk" --tag home --tag shop     added 1
    python -m notes list                                       1  buy milk  [home, shop]
    python -m notes find milk                                  the matching notes, same format as list
    python -m notes find --tag home                            (text and --tag together: both must match)
    python -m notes export notes.md                            wrote 2 notes to notes.md

`find` with no match prints `no notes found` and exits 1. Notes live in the file named by the
`NOTES_FILE` environment variable (default `notes.json`).

## What is fixed, and what is not

**Fixed** (the function names, arguments and results the glue and the tests rely on):

- `store.add(text, tags) -> int`: saves a note, returns its id (ids start at 1 and only grow).
- `store.all() -> list`: every note, in the order they were added.
- `store.line(note) -> str`: the line `list` and `find` print: `<id>  <text>  [<tags joined by ", ">]`
  (two spaces between the parts; a note with no tags has no brackets: `2  call Ann`).
- `search.find(notes, query=None, tag=None) -> list`: the notes whose text contains `query`
  (any case) and which carry `tag` (exactly); either may be left out.
- `export.to_markdown(notes) -> str`: `# Notes`, a blank line, then one line per note,
  `- <text> (<tags joined by ", ">)` (no parentheses when there are no tags), each line ending with a newline.

**Not fixed: what a note looks like inside.** The store decides it, and the search and export parts
have to read what the store produces. The three of you have to agree on it.

## Done

Every part has its own tests in `tests/`; `tests/test_cli.py` runs the whole thing end to end.
`python -m unittest discover -s tests` passes when the three parts fit together.
