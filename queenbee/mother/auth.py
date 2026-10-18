"""The token that guards the local API.

Host and Origin checks keep other *websites* out of the queen's page server; they do not keep out another *program* on
this machine. So every start makes a token, writes it next to the lock file (``mother-<port>.token``, readable by this
user only where the OS allows), and every ``/api/`` request must carry it: the page gets it once in the URL the app
opens (``/?k=<token>``) and keeps it for the session; the hook reads the file. ``/api/ping`` is
the one open route, so a second start can tell a live server from a dead one.
"""
from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path
from typing import Any, Optional

from ..config import _state_dir

HEADER = "X-QueenBee-Token"


def token_path(port: int) -> Path:
    return _state_dir() / f"mother-{int(port)}.token"


def make_token(port: int) -> str:
    """A fresh token for this start, written for the hook and the drivers to read."""
    token = secrets.token_urlsafe(24)
    path = token_path(port)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass
    return token


def read_token(port: int) -> Optional[str]:
    try:
        return token_path(port).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def given(headers: Any, query: Optional[dict[str, list[str]]] = None) -> str:
    """The token a request carries: a bearer Authorization, the X-QueenBee-Token header, or ?token= in the query."""
    auth = str(headers.get("Authorization") or "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    t = str(headers.get(HEADER) or "").strip()
    if t:
        return t
    if query:
        return str((query.get("token") or [""])[0]).strip()
    return ""


def allowed(token: Optional[str], headers: Any, query: Optional[dict[str, list[str]]] = None) -> bool:
    """True when the request carries the server's token. A handler built without a token (a test that wires the class by
    hand) checks nothing; `serve()` always sets one."""
    if not token:
        return True
    return hmac.compare_digest(given(headers, query), token)
