"""The QueenBee window: pywebview around the page, with QueenBee's identity.

With a tray (`tray=True`, used by the QueenBee app), closing the window hides it
instead of quitting: QueenBee stays in the notification area, keeps watching the
conversations it started and raises a desktop notification when one needs the
user. Quit from the tray menu. Without a tray (`queenbee.window`), closing the
window closes it.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from typing import Any

from . import winapp

log = logging.getLogger("queenbee.shell")

POLL_S = 5.0


def _settings_path():
    from .config import _state_dir

    return _state_dir() / "app.json"


def _load_settings() -> dict[str, Any]:
    try:
        return json.loads(_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_settings(settings: dict[str, Any]) -> None:
    try:
        path = _settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings), encoding="utf-8")
    except OSError:
        pass


def run(
    url: str,
    width: int = 1460,
    height: int = 920,
    tray: bool = False,
    title: str = "QueenBee",
) -> int:
    import webview

    winapp.set_app_id()
    window = webview.create_window(
        title, url,
        width=width, height=height, min_size=(900, 600),
        # The page's own navy, so the window does not flash white while the
        # web view starts.
        background_color="#070c18",
        text_select=True,
    )
    # QueenBee's icon on the title bar and taskbar instead of Python's.
    window.events.shown += lambda: winapp.set_window_icon(winapp.window_handle(window))

    icon = _start_tray(window, url) if tray else None

    # Not private: the page keeps its selection, view and filters in
    # localStorage, and a private profile would forget them on every start.
    webview.start(private_mode=False)
    if icon is not None:
        icon.stop()
    return 0


def _start_tray(window, url: str):
    try:
        import pystray
        from PIL import Image
    except ImportError:
        log.warning("pystray/Pillow missing: no tray icon, closing the window quits")
        return None

    from .notices import Watcher

    settings = {"notify": True, **_load_settings()}
    quitting = threading.Event()
    hidden = threading.Event()

    def show(icon=None, item=None) -> None:
        hidden.clear()
        window.show()
        window.restore()
        winapp.bring_to_front(winapp.window_handle(window))

    def quit_app(icon, item) -> None:
        quitting.set()
        icon.stop()
        window.destroy()

    def toggle_notify(icon, item) -> None:
        settings["notify"] = not settings["notify"]
        _save_settings(settings)

    def on_closing():
        if quitting.is_set():
            return True
        # Close means "out of the way", not "stop watching".
        hidden.set()
        window.hide()
        if not settings.get("told_about_tray"):
            settings["told_about_tray"] = True
            _save_settings(settings)
            icon.notify("QueenBee keeps watching your agents. Quit from this icon's menu.",
                        "QueenBee is still running")
        return False

    window.events.closing += on_closing

    image = Image.open(winapp.ICON.with_suffix(".png"))
    icon = pystray.Icon(
        "queenbee", image, "QueenBee",
        menu=pystray.Menu(
            pystray.MenuItem("Open QueenBee", show, default=True),
            pystray.MenuItem("Notifications", toggle_notify,
                             checked=lambda item: settings["notify"]),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit QueenBee", quit_app),
        ),
    )
    icon.run_detached()

    def watch() -> None:
        watcher = Watcher()
        while not quitting.is_set():
            try:
                with urllib.request.urlopen(url + "api/snapshot", timeout=20) as resp:
                    snapshot = json.loads(resp.read())
                notices = watcher.feed(snapshot)
                # The page already shows these as toasts while QueenBee is the
                # window being looked at; only interrupt from elsewhere.
                looking = not hidden.is_set() and winapp.is_foreground(winapp.window_handle(window))
                if settings["notify"] and not looking:
                    for title, message in notices:
                        icon.notify(message[:250] or " ", title[:63])
                        log.info("notified: %s -- %s", title, message)
            except Exception:
                log.debug("watch poll failed", exc_info=True)
            quitting.wait(POLL_S)

    threading.Thread(target=watch, name="queenbee-notices", daemon=True).start()
    return icon
