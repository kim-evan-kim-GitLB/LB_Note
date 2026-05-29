from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np

from src.types import Segment


class STTBackend(ABC):
    name: str

    @abstractmethod
    def load(self) -> None: ...

    @abstractmethod
    def unload(self) -> None: ...

    @abstractmethod
    def transcribe(self, audio: Path, language: str = "Korean") -> list[Segment]: ...

    @abstractmethod
    def vram_peak_mb(self) -> int | None: ...

    def transcribe_array(
        self,
        audio: np.ndarray,
        sr: int = 16000,
        start_offset: float = 0.0,
        language: str = "Korean",
    ) -> list[Segment]:
        """raw float32 ndarray 입력. 미구현 backend 는 NotImplementedError."""
        raise NotImplementedError(
            f"{self.__class__.__name__} 는 transcribe_array 미구현"
        )
