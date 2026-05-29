"""오디오 원인 진단 — 노이즈 제거 도구 선택을 데이터로 결정하기 위한 분석.

스펙트로그램 + 정량지표를 생성한다 (모델 설치 불필요, 메인 환경에서 동작).

진단 항목:
  1. 노이즈 플로어   — 무음 구간 에너지 (denoise 필요성)
  2. 고역 cutoff     — 몇 kHz부터 에너지가 죽었나 (super-resolution 필요성)
  3. 대역별 에너지   — 저/중/고역 분포
  4. transient(충격음) — onset strength 피크 = 책상 치는 소리 위치
  5. 구간별 RMS 편차 — 원거리 화자 음량 편차

사용 예:
    uv run python tools/diagnose_audio.py \\
        --input "samples/ax과제회의(클로바노트)_음성파일.m4a" --start 20 --end 80
"""
from __future__ import annotations

import argparse
from pathlib import Path

import librosa
import librosa.display
import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

SR = 16000
N_FFT = 1024
HOP = 256


def db_floor_and_cutoff(y: np.ndarray, sr: int) -> tuple[float, float, dict]:
    S = np.abs(librosa.stft(y, n_fft=N_FFT, hop_length=HOP)) ** 2
    freqs = librosa.fft_frequencies(sr=sr, n_fft=N_FFT)
    # 시간평균 파워 스펙트럼 (dB)
    psd = S.mean(axis=1)
    psd_db = 10 * np.log10(psd + 1e-12)
    peak_db = psd_db.max()
    # 고역 cutoff: 피크 대비 -40dB 아래로 내려가는 가장 낮은 고주파
    thresh = peak_db - 40
    above = freqs[psd_db >= thresh]
    cutoff = float(above.max()) if len(above) else 0.0
    # 대역별 에너지
    bands = {
        "low(0-500)": (0, 500),
        "mid(500-2k)": (500, 2000),
        "high(2k-4k)": (2000, 4000),
        "vhigh(4k-8k)": (4000, 8000),
    }
    band_energy = {}
    total = S.sum()
    for name, (lo, hi) in bands.items():
        m = (freqs >= lo) & (freqs < hi)
        band_energy[name] = float(S[m].sum() / total * 100)
    return peak_db, cutoff, band_energy


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--out-dir", default="output/diagnosis")
    args = ap.parse_args()

    dur = args.end - args.start
    y, sr = librosa.load(args.input, sr=SR, mono=True, offset=args.start, duration=dur)
    t = np.arange(len(y)) / sr

    # --- 정량 지표 ---
    peak_db, cutoff, band_energy = db_floor_and_cutoff(y, sr)

    # 구간별 RMS (1초 단위) — 음량 편차 = 원거리 화자
    win = sr
    rms_1s = np.array([
        np.sqrt((y[i:i + win] ** 2).mean()) for i in range(0, len(y) - win, win)
    ])
    rms_db = 20 * np.log10(rms_1s + 1e-12)
    quiet_floor = float(np.percentile(rms_db, 10))  # 하위10% = 노이즈플로어 근사
    loud = float(np.percentile(rms_db, 90))
    dynamic_spread = loud - quiet_floor

    # transient(충격음) 검출: onset strength
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    onset_times = librosa.times_like(onset_env, sr=sr, hop_length=HOP)
    # 강한 피크 = 책상소리 후보
    thr = onset_env.mean() + 3 * onset_env.std()
    transient_idx = np.where(onset_env > thr)[0]
    transient_times = onset_times[transient_idx]

    print("=" * 60)
    print(f"구간 {args.start}-{args.end}s  ({dur:.0f}s @ {sr}Hz)")
    print("-" * 60)
    print(f"[고역] cutoff(피크-40dB) = {cutoff:.0f} Hz   (낮을수록 고역 손실 심함)")
    print(f"[대역 에너지 %] " + "  ".join(f"{k}={v:.1f}" for k, v in band_energy.items()))
    print(f"[음량] 노이즈플로어~={quiet_floor:.1f}dB  큰소리~={loud:.1f}dB  편차={dynamic_spread:.1f}dB")
    print(f"       (편차 클수록 원거리/근거리 화자 음량차 큼)")
    print(f"[충격음] onset 강피크 {len(transient_times)}개 검출 "
          f"(책상소리 후보): {np.round(transient_times[:15], 1).tolist()}")
    print("=" * 60)

    # --- 시각화 ---
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(args.input).stem
    tag = f"{int(args.start)}-{int(args.end)}s"

    fig, axes = plt.subplots(3, 1, figsize=(14, 10))

    # 1) waveform + transient 마커
    axes[0].plot(t, y, linewidth=0.4, color="steelblue")
    for tt in transient_times:
        axes[0].axvline(tt, color="red", alpha=0.5, linewidth=0.8)
    axes[0].set_title(f"Waveform + transient markers (red = 충격음 후보, {len(transient_times)})")
    axes[0].set_xlabel("time (s)"); axes[0].set_ylabel("amp"); axes[0].set_xlim(0, dur)

    # 2) log-freq 스펙트로그램
    D = librosa.amplitude_to_db(np.abs(librosa.stft(y, n_fft=N_FFT, hop_length=HOP)), ref=np.max)
    img = librosa.display.specshow(D, sr=sr, hop_length=HOP, x_axis="time",
                                    y_axis="linear", ax=axes[1], cmap="magma")
    axes[1].axhline(cutoff, color="cyan", linestyle="--", linewidth=1.2,
                    label=f"고역 cutoff ~{cutoff:.0f}Hz")
    axes[1].set_title("Spectrogram (linear freq) — 고역 절벽/잔향 꼬리 확인")
    axes[1].legend(loc="upper right")
    fig.colorbar(img, ax=axes[1], format="%+2.0f dB")

    # 3) 구간별 RMS
    axes[2].plot(np.arange(len(rms_db)), rms_db, marker="o", color="darkgreen")
    axes[2].axhline(quiet_floor, color="gray", linestyle="--", label=f"noise floor ~{quiet_floor:.0f}dB")
    axes[2].set_title(f"1초 단위 RMS (음량 편차={dynamic_spread:.1f}dB) — 원거리 화자 진단")
    axes[2].set_xlabel("time (s)"); axes[2].set_ylabel("RMS (dB)"); axes[2].legend()

    plt.tight_layout()
    png = out_dir / f"diag_{stem}_{tag}.png"
    plt.savefig(png, dpi=110)
    print(f"saved -> {png}")


if __name__ == "__main__":
    main()
