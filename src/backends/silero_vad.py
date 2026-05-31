"""Silero VAD 백엔드 (MIT, ~2MB, CPU). 발화 구간 타임스탬프만 반환(비파괴).

silero-vad 패키지의 load_silero_vad() + get_speech_timestamps() 래핑.
"""
from __future__ import annotations

import numpy as np

from src.backends.vad_base import VADBackend


class SileroVAD(VADBackend):
    name = "silero"

    def __init__(self, threshold: float = 0.5,
                 min_speech_sec: float = 0.2, min_silence_sec: float = 0.3):
        self.threshold = threshold
        self.min_speech_sec = min_speech_sec
        self.min_silence_sec = min_silence_sec
        self._model = None
        self._get_ts = None

    def load(self) -> None:
        from silero_vad import get_speech_timestamps, load_silero_vad

        self._model = load_silero_vad()
        self._get_ts = get_speech_timestamps

    def unload(self) -> None:
        self._model = None
        self._get_ts = None

    def detect(self, samples: np.ndarray, sr: int = 16000) -> list[tuple[float, float]]:
        assert self._model is not None, "load() 먼저 호출"
        assert samples.ndim == 1, f"1D mono 만 지원. shape={samples.shape}"
        import torch

        wav = torch.from_numpy(samples.astype(np.float32))
        ts = self._get_ts(
            wav,
            self._model,
            sampling_rate=sr,
            threshold=self.threshold,
            min_speech_duration_ms=int(self.min_speech_sec * 1000),
            min_silence_duration_ms=int(self.min_silence_sec * 1000),
            return_seconds=True,
        )
        return [(float(d["start"]), float(d["end"])) for d in ts]
