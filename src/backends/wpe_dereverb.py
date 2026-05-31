"""WPE dereverberation 인핸서 (nara_wpe, CPU, 단일채널).

tools/enhance_full.py 의 apply_wpe + 블록 overlap-add 로직을 AudioEnhancer 로 이식.
전체를 한 번에 STFT 하면 메모리 폭증 → 블록 단위 WPE 후 crossfade overlap-add.
"""
from __future__ import annotations

import numpy as np

from src.backends.enhancer_base import AudioEnhancer

_SIZE = 512
_SHIFT = 128


class WPEDereverb(AudioEnhancer):
    name = "wpe"

    def __init__(
        self,
        taps: int = 25,
        delay: int = 3,
        iterations: int = 5,
        block_sec: float = 300.0,
        overlap_sec: float = 5.0,
    ):
        self.taps = taps
        self.delay = delay
        self.iterations = iterations
        self.block_sec = block_sec
        self.overlap_sec = overlap_sec

    def load(self) -> None:  # 모델 가중치 없음 (순수 신호처리)
        pass

    def unload(self) -> None:
        pass

    def _apply_wpe(self, y: np.ndarray) -> np.ndarray:
        from nara_wpe.utils import istft, stft
        from nara_wpe.wpe import wpe

        Y = stft(y[None, :], size=_SIZE, shift=_SHIFT).transpose(2, 0, 1)
        Z = wpe(Y, taps=self.taps, delay=self.delay,
                iterations=self.iterations).transpose(1, 2, 0)
        return istft(Z, size=_SIZE, shift=_SHIFT)[0]

    def process(self, samples: np.ndarray, sr: int = 16000) -> np.ndarray:
        assert samples.ndim == 1, f"1D mono 만 지원. shape={samples.shape}"
        y = samples.astype(np.float64)
        block = int(self.block_sec * sr)
        ov = int(self.overlap_sec * sr)
        hop = block - ov
        out = np.zeros(len(y) + block, dtype=np.float64)
        wsum = np.zeros(len(y) + block, dtype=np.float64)

        for start in range(0, len(y), hop):
            seg = y[start:start + block]
            if len(seg) < _SIZE * 2:
                break
            z = self._apply_wpe(seg)
            n = min(len(seg), len(z))
            w = np.ones(n)
            if start > 0 and n > ov:
                w[:ov] = np.linspace(0.0, 1.0, ov)
            if n > ov:
                w[-ov:] = np.linspace(1.0, 0.0, ov)
            out[start:start + n] += z[:n] * w
            wsum[start:start + n] += w

        wsum[wsum == 0] = 1.0
        z = (out / wsum)[:len(y)]
        # peak match (입력 음량 보존)
        peak_raw, peak_z = np.abs(samples).max(), np.abs(z).max()
        if peak_z > 0:
            z = z * (peak_raw / peak_z)
        return z.astype(np.float32)
