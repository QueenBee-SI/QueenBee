def to_markdown(notes):
    lines = [f"- {n['text']} ({', '.join(n['tags'])})" if n["tags"] else f"- {n['text']}" for n in notes]
    return "# Notes\n\n" + "".join(l + "\n" for l in lines)
