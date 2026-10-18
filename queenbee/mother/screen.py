"""The screen an agent's terminal shows now, rebuilt from its output stream.

Agent TUIs (Claude Code, Codex) draw by moving the cursor, not by printing
lines: Codex places each word of a wrapped command with its own cursor move.
Reading their output as text lines -- escape codes stripped -- glued words
together or split them one per line, and Mother missed questions it was meant
to answer. This replays the stream onto a grid the size of the terminal and
returns its rows, which is exactly what a person would read.

Only what those TUIs use is implemented: printing, CR/LF/BS/TAB, cursor
movement (CUU/CUD/CUF/CUB/CNL/CPL/CHA/CUP/HVP/VPA), erase in line/display,
insert/delete lines, scroll up/down, scroll regions, save/restore cursor and
the alternate screen. Colours and other attributes are ignored.
"""

from __future__ import annotations

import re

_TOKEN = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"          # OSC (window title, ...)
    r"|\x1bP.*?\x1b\\"                             # DCS
    r"|\x1b\[([0-?]*)([ -/]*)([@-~])"             # CSI
    r"|\x1b[()][0-9A-Za-z]"                       # charset selection
    r"|\x1b([78=>DEMc])"                          # short escapes
    r"|([\x00-\x1f])"                             # control characters
    r"|([^\x00-\x1f\x1b]+)",                      # printable text
    re.S)


class Screen:
    def __init__(self, cols: int = 120, rows: int = 32) -> None:
        self.cols, self.rows = max(20, cols), max(5, rows)
        self.grid = [[" "] * self.cols for _ in range(self.rows)]
        self.x = self.y = 0
        self.top, self.bottom = 0, self.rows - 1
        self.saved = (0, 0)
        self._main = None

    # -- primitives ------------------------------------------------------

    def _blank(self):
        return [" "] * self.cols

    def _scroll_up(self, n=1):
        for _ in range(n):
            del self.grid[self.top]
            self.grid.insert(self.bottom, self._blank())

    def _scroll_down(self, n=1):
        for _ in range(n):
            del self.grid[self.bottom]
            self.grid.insert(self.top, self._blank())

    def _linefeed(self):
        if self.y == self.bottom:
            self._scroll_up()
        elif self.y < self.rows - 1:
            self.y += 1

    def _put(self, text: str):
        for ch in text:
            if self.x >= self.cols:
                self.x = 0
                self._linefeed()
            self.grid[self.y][self.x] = ch
            self.x += 1

    def _clamp(self):
        self.x = min(max(self.x, 0), self.cols - 1)
        self.y = min(max(self.y, 0), self.rows - 1)

    # -- stream ----------------------------------------------------------

    def feed(self, data: str) -> "Screen":
        for m in _TOKEN.finditer(data):
            params, inter, final, short, ctrl, text = m.group(1, 2, 3, 4, 5, 6)
            if text:
                self._put(text)
            elif ctrl:
                if ctrl == "\n" or ctrl == "\x0b" or ctrl == "\x0c":
                    self._linefeed()
                elif ctrl == "\r":
                    self.x = 0
                elif ctrl == "\b":
                    self.x = max(0, self.x - 1)
                elif ctrl == "\t":
                    self.x = min(self.cols - 1, (self.x // 8 + 1) * 8)
            elif short:
                if short == "7":
                    self.saved = (self.x, self.y)
                elif short == "8":
                    self.x, self.y = self.saved
                elif short == "D":
                    self._linefeed()
                elif short == "E":
                    self.x = 0
                    self._linefeed()
                elif short == "M":
                    if self.y == self.top:
                        self._scroll_down()
                    else:
                        self.y = max(0, self.y - 1)
            elif final:
                self._csi(params or "", inter or "", final)
        return self

    def _csi(self, params: str, inter: str, final: str):
        private = params.startswith(("?", ">", "<", "="))
        nums = [int(p) if p.isdigit() else 0 for p in params.lstrip("?><=").split(";")] if params else []
        n = (nums[0] if nums and nums[0] else 1)
        if private:
            if final in "hl" and nums and nums[0] in (47, 1047, 1049):
                self._alternate(final == "h")
            return
        if inter:
            return
        if final == "A":
            self.y -= n
        elif final == "B" or final == "e":
            self.y += n
        elif final == "C" or final == "a":
            self.x += n
        elif final == "D":
            self.x -= n
        elif final == "E":
            self.y += n
            self.x = 0
        elif final == "F":
            self.y -= n
            self.x = 0
        elif final == "G" or final == "`":
            self.x = n - 1
        elif final == "d":
            self.y = n - 1
        elif final in "Hf":
            self.y = (nums[0] if len(nums) > 0 and nums[0] else 1) - 1
            self.x = (nums[1] if len(nums) > 1 and nums[1] else 1) - 1
        elif final == "K":
            mode = nums[0] if nums else 0
            row = self.grid[self.y]
            lo, hi = {0: (self.x, self.cols), 1: (0, self.x + 1), 2: (0, self.cols)}.get(mode, (0, 0))
            for i in range(lo, min(hi, self.cols)):
                row[i] = " "
        elif final == "J":
            mode = nums[0] if nums else 0
            if mode == 0:
                self._csi("0", "", "K")
                for r in range(self.y + 1, self.rows):
                    self.grid[r] = self._blank()
            elif mode == 1:
                for r in range(0, self.y):
                    self.grid[r] = self._blank()
                self._csi("1", "", "K")
            elif mode in (2, 3):
                self.grid = [self._blank() for _ in range(self.rows)]
        elif final == "L":
            for _ in range(n):
                if self.top <= self.y <= self.bottom:
                    del self.grid[self.bottom]
                    self.grid.insert(self.y, self._blank())
        elif final == "M":
            for _ in range(n):
                if self.top <= self.y <= self.bottom:
                    del self.grid[self.y]
                    self.grid.insert(self.bottom, self._blank())
        elif final == "P":
            row = self.grid[self.y]
            del row[self.x:self.x + n]
            row.extend([" "] * (self.cols - len(row)))
        elif final == "X":
            row = self.grid[self.y]
            for i in range(self.x, min(self.cols, self.x + n)):
                row[i] = " "
        elif final == "@":
            row = self.grid[self.y]
            for _ in range(n):
                row.insert(self.x, " ")
            del row[self.cols:]
        elif final == "S":
            self._scroll_up(n)
        elif final == "T":
            self._scroll_down(n)
        elif final == "r":
            top = (nums[0] if len(nums) > 0 and nums[0] else 1) - 1
            bottom = (nums[1] if len(nums) > 1 and nums[1] else self.rows) - 1
            if 0 <= top < bottom < self.rows:
                self.top, self.bottom = top, bottom
            else:
                self.top, self.bottom = 0, self.rows - 1
            self.x = self.y = 0
        elif final == "s":
            self.saved = (self.x, self.y)
        elif final == "u":
            self.x, self.y = self.saved
        self._clamp()

    def _alternate(self, on: bool):
        if on and self._main is None:
            self._main = (self.grid, self.x, self.y)
            self.grid = [self._blank() for _ in range(self.rows)]
        elif not on and self._main is not None:
            self.grid, self.x, self.y = self._main
            self._main = None

    def lines(self) -> list[str]:
        return ["".join(row).rstrip() for row in self.grid]


def render(data: str, cols: int, rows: int) -> list[str]:
    """The rows of the screen after replaying `data`."""
    return Screen(cols, rows).feed(data).lines()
