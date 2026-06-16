"""에너지 기반 VAD (numpy 벡터화, 모델·의존성 없음).

Silero VAD 가 32ms 윈도를 파이썬 루프로 ~15만 번 호출(83분 기준)하느라 ~27s 걸리는 데 비해,
프레임 RMS 를 벡터 연산으로 한 번에 계산해 ~1.8s 로 끝난다(약 15x). 발화/무음 경계만
필요한 '청킹' 용도에 적합. 정밀한 발화 판정(예: SNR 계산)이 필요하면 Silero 를 쓸 것.

VADBackend 계약 그대로: 16k mono float32 → 발화 구간 [(start_sec, end_sec), ...] (원본 타임라인).
"""
from __future__ import annotations

import numpy as np

from src.backends.vad_base import VADBackend


class EnergyVAD(VADBackend):
    name = "energy"

    def __init__(
        self,
        min_speech_sec: float = 0.2,
        min_silence_sec: float = 0.3,
        margin_db: float = 8.0,
        frame_ms: float = 32.0,
        hop_ms: float = 16.0,
        floor_percentile: float = 10.0,
    ):
        self.min_speech_sec = min_speech_sec
        self.min_silence_sec = min_silence_sec
        self.margin_db = margin_db          # 노이즈플로어 위 몇 dB 부터 발화로 볼지
        self.frame_ms = frame_ms
        self.hop_ms = hop_ms
        self.floor_percentile = floor_percentile

    def load(self) -> None:   # 모델 없음
        pass

    def unload(self) -> None:
        pass

    def detect(self, samples: np.ndarray, sr: int = 16000) -> list[tuple[float, float]]:
        assert samples.ndim == 1, f"1D mono 만 지원. shape={samples.shape}"
        y = samples.astype(np.float32)
        fl = max(1, int(sr * self.frame_ms / 1000))
        hop = max(1, int(sr * self.hop_ms / 1000))
        if len(y) < fl:
            return []
        n = 1 + (len(y) - fl) // hop
        # 프레임 인덱스 행렬 → RMS(dB) 벡터화
        idx = np.arange(n)[:, None] * hop + np.arange(fl)[None, :]
        rms = np.sqrt((y[idx] ** 2).mean(axis=1) + 1e-12)
        db = 20 * np.log10(rms + 1e-12)
        thr = np.percentile(db, self.floor_percentile) + self.margin_db
        voiced = db > thr
        times = np.arange(n) * hop / sr

        # 발화 런 추출
        regions: list[list[float]] = []
        i = 0
        while i < n:
            if voiced[i]:
                j = i
                while j < n and voiced[j]:
                    j += 1
                regions.append([float(times[i]), float(times[min(j, n - 1)])])
                i = j
            else:
                i += 1
        # 짧은 무음 병합(min_silence 이하 gap)
        merged: list[list[float]] = []
        for s, e in regions:
            if merged and s - merged[-1][1] <= self.min_silence_sec:
                merged[-1][1] = e
            else:
                merged.append([s, e])
        # 짧은 발화 제거(min_speech 미만)
        return [(s, e) for s, e in merged if e - s >= self.min_speech_sec]
