"""전체 음성에 WPE dereverb 적용 (블록 overlap-add, 메모리 안전).

전체를 한 번에 STFT 하면 수 GB~수십 GB 메모리가 필요하므로,
블록 단위로 나눠 WPE 적용 후 overlap-add 로 이어붙인다.

이후 ffmpeg 로 EQ + dynaudnorm 을 얹어 combo 를 완성하는 건 별도 호출.

사용 예:
    uv run python tools/enhance_full.py \\
        --input "samples/ax과제회의(클로바노트)_음성파일.m4a" \\
        --out samples/enhanced/full/wpe25_full.wav --taps 25 --iterations 5
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
from nara_wpe.utils import istft, stft
from nara_wpe.wpe import wpe

SR = 16000
SIZE = 512
SHIFT = 128


def apply_wpe(y: np.ndarray, taps: int, delay: int, iterations: int) -> np.ndarray:
    Y = stft(y[None, :], size=SIZE, shift=SHIFT).transpose(2, 0, 1)
    Z = wpe(Y, taps=taps, delay=delay, iterations=iterations).transpose(1, 2, 0)
    return istft(Z, size=SIZE, shift=SHIFT)[0]


def enhance_full(
    y: np.ndarray, sr: int, block_sec: float, overlap_sec: float,
    taps: int, delay: int, iterations: int,
) -> np.ndarray:
    block = int(block_sec * sr)
    ov = int(overlap_sec * sr)
    hop = block - ov
    out = np.zeros(len(y) + block, dtype=np.float64)
    wsum = np.zeros(len(y) + block, dtype=np.float64)

    n_blocks = max(1, (len(y) - ov) // hop + 1)
    for bi, start in enumerate(range(0, len(y), hop)):
        seg = y[start:start + block]
        if len(seg) < SIZE * 2:
            break
        z = apply_wpe(seg, taps, delay, iterations)
        n = min(len(seg), len(z))
        # overlap 영역 crossfade (선형 taper)
        w = np.ones(n)
        if start > 0 and n > ov:
            w[:ov] = np.linspace(0.0, 1.0, ov)
        if n > ov:
            w[-ov:] = np.linspace(1.0, 0.0, ov)
        out[start:start + n] += z[:n] * w
        wsum[start:start + n] += w
        print(f"  block {bi + 1}/{n_blocks}  [{start / sr:6.1f}s ~ {(start + n) / sr:6.1f}s]")

    wsum[wsum == 0] = 1.0
    return (out / wsum)[:len(y)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--block-sec", type=float, default=300.0)
    ap.add_argument("--overlap-sec", type=float, default=5.0)
    ap.add_argument("--taps", type=int, default=25)
    ap.add_argument("--delay", type=int, default=3)
    ap.add_argument("--iterations", type=int, default=5)
    args = ap.parse_args()

    t0 = time.time()
    y, sr = librosa.load(args.input, sr=SR, mono=True)
    print(f"loaded {len(y) / sr:.1f}s @ {sr}Hz, WPE 블록 처리 시작 "
          f"(block={args.block_sec}s overlap={args.overlap_sec}s taps={args.taps})")

    z = enhance_full(y, sr, args.block_sec, args.overlap_sec,
                     args.taps, args.delay, args.iterations)
    # peak match (청취/STT 음량 공정)
    peak_raw, peak_z = np.abs(y).max(), np.abs(z).max()
    if peak_z > 0:
        z = z * (peak_raw / peak_z)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(out, z.astype(np.float32), sr)
    print(f"done in {time.time() - t0:.1f}s -> {out}")


if __name__ == "__main__":
    main()
