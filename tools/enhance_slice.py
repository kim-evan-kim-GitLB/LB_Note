"""음성 구간 슬라이스 + WPE dereverb 보정 도구 (정성 청취 평가용).

원본 오디오의 특정 구간을 잘라 raw / WPE 보정 두 버전을 생성한다.
WPE 는 nara_wpe (numpy/scipy 순수 연산, CPU) — 현재 환경 그대로 동작.

사용 예:
    uv run python tools/enhance_slice.py \\
        --input "samples/ax과제회의(클로바노트)_음성파일.m4a" \\
        --start 20 --end 80

출력:
    samples/enhanced/raw/<stem>_<start>-<end>s.wav
    samples/enhanced/wpe/<stem>_<start>-<end>s_taps<N>.wav
"""
from __future__ import annotations

import argparse
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
from nara_wpe.utils import istft, stft
from nara_wpe.wpe import wpe

STFT_SIZE = 512
STFT_SHIFT = 128


def apply_wpe(y: np.ndarray, taps: int, delay: int, iterations: int) -> np.ndarray:
    """단일 채널 mono 신호에 WPE dereverberation 적용.

    nara_wpe 규약: wpe 입력/출력은 (freq, channels, frames) complex.
    """
    Y = stft(y[None, :], size=STFT_SIZE, shift=STFT_SHIFT)  # (1, T, F)
    Y = Y.transpose(2, 0, 1)  # (F, 1, T)
    Z = wpe(Y, taps=taps, delay=delay, iterations=iterations)  # (F, 1, T)
    Z = Z.transpose(1, 2, 0)  # (1, T, F)
    z = istft(Z, size=STFT_SIZE, shift=STFT_SHIFT)  # (1, samples)
    return z[0]


def match_peak(reference: np.ndarray, target: np.ndarray) -> np.ndarray:
    """청취 비교 공정성을 위해 target 의 peak 를 reference 에 맞춤."""
    peak_ref = float(np.abs(reference).max())
    peak_tgt = float(np.abs(target).max())
    if peak_tgt > 0:
        return target * (peak_ref / peak_tgt)
    return target


def main() -> None:
    ap = argparse.ArgumentParser(description="구간 슬라이스 + WPE 보정")
    ap.add_argument("--input", required=True, help="원본 오디오 경로 (m4a/wav 등)")
    ap.add_argument("--start", type=float, required=True, help="시작 초")
    ap.add_argument("--end", type=float, required=True, help="끝 초")
    ap.add_argument("--out-dir", default="samples/enhanced")
    ap.add_argument("--sr", type=int, default=16000)
    ap.add_argument("--taps", type=int, default=10, help="WPE filter taps (회의실=10, 강당=15~20)")
    ap.add_argument("--delay", type=int, default=3)
    ap.add_argument("--iterations", type=int, default=3)
    args = ap.parse_args()

    if args.end <= args.start:
        raise SystemExit(f"end({args.end}) must be > start({args.start})")

    duration = args.end - args.start
    y, sr = librosa.load(
        args.input, sr=args.sr, mono=True, offset=args.start, duration=duration
    )
    print(f"loaded: {len(y) / sr:.1f}s @ {sr}Hz  (peak={np.abs(y).max():.3f})")

    stem = Path(args.input).stem
    tag = f"{int(args.start)}-{int(args.end)}s"
    out_dir = Path(args.out_dir)
    (out_dir / "raw").mkdir(parents=True, exist_ok=True)
    (out_dir / "wpe").mkdir(parents=True, exist_ok=True)

    raw_path = out_dir / "raw" / f"{stem}_{tag}.wav"
    sf.write(raw_path, y, sr)
    print(f"raw -> {raw_path}")

    y_wpe = apply_wpe(y, args.taps, args.delay, args.iterations)
    n = min(len(y), len(y_wpe))
    y_wpe = match_peak(y[:n], y_wpe[:n])
    wpe_path = out_dir / "wpe" / f"{stem}_{tag}_taps{args.taps}.wav"
    sf.write(wpe_path, y_wpe, sr)
    print(f"wpe -> {wpe_path}  (taps={args.taps}, delay={args.delay}, iters={args.iterations})")


if __name__ == "__main__":
    main()
