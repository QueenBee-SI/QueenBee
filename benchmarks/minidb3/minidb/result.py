"""What a statement returns. Finished: do not change it."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Result:
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    rowcount: int = 0
    message: str = ""
