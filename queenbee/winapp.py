"""Make a QueenBee window look like QueenBee to Windows, not like python.exe.

Two things decide what the taskbar shows. The AppUserModelID groups windows
into an application: without one, every QueenBee window is filed under Python and
shows Python's icon. And the window's own icon, set with WM_SETICON, is what
the title bar and taskbar button draw.

Both are no-ops outside Windows.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_ID = "QueenBee.Workspace"
ICON = Path(__file__).resolve().parent / "assets" / "queenbee.ico"


def set_app_id() -> None:
    """Call before the first window is created."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def set_window_icon(hwnd: int) -> bool:
    if os.name != "nt" or not hwnd or not ICON.exists():
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
        WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
        big = user32.LoadImageW(None, str(ICON), IMAGE_ICON, 256, 256, LR_LOADFROMFILE)
        small = user32.LoadImageW(None, str(ICON), IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
        if big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
        if small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
        return bool(big or small)
    except Exception:
        return False


def is_foreground(hwnd: int) -> bool:
    if os.name != "nt" or not hwnd:
        return False
    try:
        import ctypes

        return int(ctypes.windll.user32.GetForegroundWindow() or 0) == hwnd
    except Exception:
        return False


def bring_to_front(hwnd: int) -> None:
    if os.name != "nt" or not hwnd:
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def window_handle(window) -> int:
    """The HWND behind a pywebview window, once it has been shown."""
    native = getattr(window, "native", None)
    try:
        return int(native.Handle.ToInt64())
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes

            return int(ctypes.windll.user32.FindWindowW(None, window.title) or 0)
        except Exception:
            return 0
    return 0
