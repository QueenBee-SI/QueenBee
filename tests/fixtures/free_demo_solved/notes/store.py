"""The store: notes live in a JSON list of {"id", "text", "tags"} in the file NOTES_FILE."""

import json
import os


def _path():
    return os.environ.get("NOTES_FILE", "notes.json")


def all():
    try:
        with open(_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return []


def add(text, tags):
    notes = all()
    note_id = max((n["id"] for n in notes), default=0) + 1
    notes.append({"id": note_id, "text": text, "tags": list(tags or [])})
    with open(_path(), "w", encoding="utf-8") as fh:
        json.dump(notes, fh)
    return note_id


def line(note):
    tags = f"  [{', '.join(note['tags'])}]" if note["tags"] else ""
    return f"{note['id']}  {note['text']}{tags}"
