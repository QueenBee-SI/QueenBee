"""Entry point of the packaged app (QueenBee.exe).

The code starts its helper processes the way Python does -- `<interpreter> -m queenbee.host ...`.
In the packaged app the "interpreter" is this very program, so it understands the same form:

    QueenBee.exe                       open the QueenBee window
    QueenBee.exe -m queenbee.host ...     run that module (what the app spawns for itself)
    QueenBee.exe --shortcuts           desktop and Start menu shortcuts
    QueenBee.exe --no-window           serve the page only (open it in any browser)
    QueenBee.exe --stop-all            end every worker, the host and the window
"""

from __future__ import annotations

import multiprocessing
import os
import runpy
import sys
from pathlib import Path


def _unblock_bundle() -> None:
    """Drop the "downloaded from the internet" mark from the bundle's DLLs.

    A zip saved by a browser and extracted with Explorer passes that mark (the Zone.Identifier stream) to every file in
    it, and .NET refuses to load a marked assembly: pythonnet and WebView2 fail, and the window never opens. This is what
    Unblock-File does, limited to our own folder. Files an installer writes carry no mark."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    for dll in Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).rglob("*.dll"):
        try:
            os.remove(f"{dll}:Zone.Identifier")
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    _unblock_bundle()
    if args[:1] == ["--stop-all"]:
        from queenbee.host import main as host

        return host(["--stop"])                      # ends the host and every terminal in it; the window closes with it
    if len(args) >= 2 and args[0] == "-m":
        module = args[1]
        sys.argv = [module] + args[2:]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        return 0
    from queenbee.mother.__main__ import main as queen

    return queen(args)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
