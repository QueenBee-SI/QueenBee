def find(notes, query=None, tag=None):
    return [n for n in notes
            if (query is None or query.lower() in n["text"].lower()) and (tag is None or tag in n["tags"])]
