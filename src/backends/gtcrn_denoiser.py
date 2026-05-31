"""GTCRN 단일채널 denoiser (16k native, CPU). vendored 모델 사용.

STFT 계약은 upstream infer.py 그대로: n_fft=512, hop=256, win=hann(512).pow(0.5).
긴 신호는 메모리 안전을 위해 블록 단위로 처리(STFT 프레임 경계에 맞춰).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import torch

from src.backends._vendor.gtcrn import GTCRN
from src.backends.enhancer_base import AudioEnhancer

_NFFT = 512
_HOP = 256
_BLOCK_SEC = 60.0  # 블록 처리 단위(STFT 프레임 정렬). CPU·메모리 안전.


class GTCRNDenoiser(AudioEnhancer):
    name = "gtcrn"

    def __init__(self, ckpt_path: Path):
        self.ckpt_path = Path(ckpt_path)
        self._model = None
        self._window = None

    def load(self) -> None:
        if not self.ckpt_path.exists():
            raise FileNotFoundError(
                f"GTCRN 체크포인트 없음: {self.ckpt_path} "
                "(models/gtcrn/ 는 gitignore — worktree 에서 fetch 필요)"
            )
        model = GTCRN().eval()
        ckpt = torch.load(str(self.ckpt_path), map_location="cpu")
        model.load_state_dict(ckpt["model"])
        self._model = model
        self._window = torch.hann_window(_NFFT).pow(0.5)

    def unload(self) -> None:
        self._model = None
        self._window = None

    def _process_block(self, block: np.ndarray) -> np.ndarray:
        x = torch.from_numpy(block.astype(np.float32))
        spec = torch.stft(
            x, _NFFT, _HOP, _NFFT, self._window, return_complex=False
        )
        with torch.inference_mode():
            out = self._model(spec[None])[0]
        # 모델 출력은 real-format (freq, frames, 2). torch>=2 istft 는 complex 입력 요구.
        enh = torch.istft(
            torch.view_as_complex(out.contiguous()),
            _NFFT, _HOP, _NFFT, self._window,
        )
        return enh.detach().cpu().numpy().astype(np.float32)

    def process(self, samples: np.ndarray, sr: int = 16000) -> np.ndarray:
        assert self._model is not None, "load() 먼저 호출"
        assert samples.ndim == 1, f"1D mono 만 지원. shape={samples.shape}"
        block_n = int(_BLOCK_SEC * sr)
        # 블록 경계는 hop 의 배수로 맞춰 STFT 프레임 정렬 유지
        block_n = (block_n // _HOP) * _HOP
        if len(samples) <= block_n:
            out = self._process_block(samples)
            return _match_len(out, len(samples))

        out = np.zeros(len(samples), dtype=np.float32)
        for start in range(0, len(samples), block_n):
            seg = samples[start:start + block_n]
            if len(seg) < _NFFT:  # 잔여 짧은 꼬리는 그대로 둠
                out[start:start + len(seg)] = seg
                continue
            enh = self._process_block(seg)
            n = min(len(seg), len(enh))
            out[start:start + n] = enh[:n]
        return out


def _match_len(out: np.ndarray, target: int) -> np.ndarray:
    """istft 길이가 입력과 ±몇 샘플 어긋날 수 있어 입력 길이에 맞춤."""
    if len(out) == target:
        return out
    if len(out) > target:
        return out[:target]
    return np.pad(out, (0, target - len(out)))
