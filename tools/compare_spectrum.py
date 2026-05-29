"""두 오디오의 평균 파워 스펙트럼 + 대역별 에너지 비교 (전체 길이용).

전체 83분은 스펙트로그램 1장이 무의미하므로, 시간평균 PSD 로 비교한다.
raw 원본 vs combo 처리본의 고역 부스트/노이즈 변화를 정량화.

사용 예:
    uv run python tools/compare_spectrum.py \\
        --a "samples/ax과제회의(클로바노트)_음성파일.m4a" --a-label raw \\
        --b "samples/enhanced/full/combo_full.wav" --b-label combo \\
        --out output/diagnosis/spectrum_full_raw_vs_combo.png
"""
from __future__ import annotations

import argparse
from pathlib import Path

import librosa
import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

SR = 16000
N_FFT = 1024
HOP = 512


def mean_psd(y: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    S = np.abs(librosa.stft(y, n_fft=N_FFT, hop_length=HOP)) ** 2
    psd = S.mean(axis=1)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=N_FFT)
    return freqs, 10 * np.log10(psd + 1e-12)


def band_table(y: np.ndarray, sr: int) -> dict:
    S = np.abs(librosa.stft(y, n_fft=N_FFT, hop_length=HOP)) ** 2
    freqs = librosa.fft_frequencies(sr=sr, n_fft=N_FFT)
    total = S.sum()
    bands = {"low(0-500)": (0, 500), "mid(500-2k)": (500, 2000),
             "high(2k-4k)": (2000, 4000), "vhigh(4k-8k)": (4000, 8000)}
    return {k: float(S[(freqs >= lo) & (freqs < hi)].sum() / total * 100)
            for k, (lo, hi) in bands.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True); ap.add_argument("--a-label", default="A")
    ap.add_argument("--b", required=True); ap.add_argument("--b-label", default="B")
    ap.add_argument("--out", default="output/diagnosis/spectrum_compare.png")
    args = ap.parse_args()

    ya, _ = librosa.load(args.a, sr=SR, mono=True)
    yb, _ = librosa.load(args.b, sr=SR, mono=True)
    fa, pa = mean_psd(ya, SR)
    fb, pb = mean_psd(yb, SR)
    ba, bb = band_table(ya, SR), band_table(yb, SR)

    print("=" * 64)
    print(f"{'band':<14}{args.a_label:>12}{args.b_label:>12}{'delta':>10}")
    print("-" * 64)
    for k in ba:
        print(f"{k:<14}{ba[k]:>11.2f}%{bb[k]:>11.2f}%{bb[k]-ba[k]:>+9.2f}%")
    print("=" * 64)

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(fa, pa, label=f"{args.a_label} (raw)", color="gray", linewidth=1.2)
    ax.plot(fb, pb, label=f"{args.b_label}", color="crimson", linewidth=1.2)
    ax.set_xlabel("frequency (Hz)"); ax.set_ylabel("power (dB)")
    ax.set_title("평균 파워 스펙트럼 비교 — 고역 부스트/노이즈 변화")
    ax.set_xlim(0, SR // 2); ax.legend(); ax.grid(alpha=0.3)

    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout(); plt.savefig(out, dpi=120)
    print(f"saved -> {out}")


if __name__ == "__main__":
    main()
