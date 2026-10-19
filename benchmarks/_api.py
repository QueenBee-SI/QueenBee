"""The benchmark drivers' way into the queen's local API: the token the server wrote at its start (auth.py), from
QB_TOKEN or the state folder (QUEENBEE_STATE_DIR) the server was started with."""
import os
import sys
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def token(base: str = "http://127.0.0.1:7990") -> str:
    t = os.environ.get("QB_TOKEN")
    if t:
        return t
    try:
        from queenbee.mother.auth import read_token
        return read_token(urlsplit(base).port or 7990) or ""
    except Exception:  # noqa: BLE001
        return ""


def auth_headers(base: str = "http://127.0.0.1:7990") -> dict:
    t = token(base)
    return {"X-QueenBee-Token": t} if t else {}


def page_url(base: str) -> str:
    """The page with the token in its URL, the way the app opens it."""
    t = token(base)
    return base + "/" + (f"?k={t}" if t else "")
