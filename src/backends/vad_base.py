"""VAD 공통 인터페이스. 발화 구간을 (start_sec, end_sec) 리스트로 반환(비파괴).

무음 압축/타임라인 remap 은 src/preprocess.py 가 이 결과를 받아 수행.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class VADBackend(ABC):
    name: str

    @abstractmethod
    def load(self) -> None: ...

    @abstractmethod
    def unload(self) -> None: ...

    @abstractmethod
    def detect(self, samples: np.ndarray, sr: int = 16000) -> list[tuple[float, float]]:
        """16k mono float32 → 발화 구간 [(start_sec, end_sec), ...] (원본 타임라인)."""
        raise NotImplementedError
