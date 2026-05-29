from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Segment:
    start: float
    end: float
    text: str
    confidence: float | None = None
    speaker: str | None = None
    meta: dict = field(default_factory=dict)
