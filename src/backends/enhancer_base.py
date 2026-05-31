"""오디오 인핸서(dereverb/denoise) 공통 인터페이스.

STTBackend 패턴을 미러링. 모든 인핸서는 16k mono float32 ndarray 를 받아
같은 sr 의 float32 ndarray 를 반환(shape 는 달라질 수 있으나 본 프로젝트의
WPE/GTCRN 은 길이 보존). VAD 무음압축만 길이를 바꾸며 그건 VADBackend 소관.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class AudioEnhancer(ABC):
    name: str

    @abstractmethod
    def load(self) -> None: ...

    @abstractmethod
    def unload(self) -> None: ...

    @abstractmethod
    def process(self, samples: np.ndarray, sr: int = 16000) -> np.ndarray:
        """16k mono float32 in → 같은 길이 float32 out (인핸스 적용)."""
        raise NotImplementedError
