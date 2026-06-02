"""입력 음성 데이터의 품질 진단 (무참조/no-reference, 전체 파일).

STT 정답 없이 '음원 자체가 얼마나 깨끗/온전한가'를 정량화한다. 핵심은 Silero VAD 로
발화/무음 구간을 갈라 '발화 RMS vs 무음(노이즈플로어) RMS = SNR' 을 추정하는 것.

지표:
  - clipping(%)      : |x|≥0.99 비율 (녹음 과입력 손상)
  - DC offset        : 평균 편이 (마이크/ADC 결함)
  - RMS / peak / crest: 전체 음량·여유
  - SNR(dB)          : 발화 RMS − 무음 RMS (VAD 기반 segmental SNR 근사)  ← 헤드라인
  - speech ratio(%)  : 발화 시간 비율
  - 대역 cutoff(Hz)  : 피크−40dB 고역 한계 (대역제한/압축 손실)
  - dynamic spread   : 1초 RMS p90−p10 (원/근거리 화자 음량차)

전체 파일을 블록 단위로 처리(메모리 안전). 모델 불필요(VAD 만, CPU).
출력: output/audio_quality-<stem>.md, output/audio_quality-<stem>.json
사용: sudo .venv/bin/python tools/audio_quality_report.py "samples/....m4a"
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import librosa  # noqa: E402
import numpy as np  # noqa: E402

from src.stt import get_vad  # noqa: E402

SR = 16000
N_FFT = 1024
HOP = 256


def speech_mask(regions, n_samples, sr):
    """VAD 발화 구간 → 샘플 단위 boolean mask."""
    mask = np.zeros(n_samples, dtype=bool)
    for s, e in regions:
        a, b = int(s * sr), min(int(e * sr), n_samples)
        if b > a:
            mask[a:b] = True
    return mask


def avg_psd_db(y, sr):
    """블록 평균 파워 스펙트럼(dB) → 고역 cutoff 계산용."""
    psd = np.zeros(N_FFT // 2 + 1, dtype=np.float64)
    n_frames = 0
    block = sr * 60  # 60s 블록
    for start in range(0, len(y), block):
        seg = y[start:start + block]
        if len(seg) < N_FFT:
            continue
        S = np.abs(librosa.stft(seg, n_fft=N_FFT, hop_length=HOP)) ** 2
        psd += S.sum(axis=1)
        n_frames += S.shape[1]
    if n_frames == 0:
        return None, None
    psd /= n_frames
    psd_db = 10 * np.log10(psd + 1e-12)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=N_FFT)
    cutoff = float(freqs[psd_db >= psd_db.max() - 40].max())
    bands = {"low(0-500)": (0, 500), "mid(500-2k)": (500, 2000),
             "high(2k-4k)": (2000, 4000), "vhigh(4k-8k)": (4000, 8000)}
    tot = psd.sum()
    band_pct = {k: float(psd[(freqs >= lo) & (freqs < hi)].sum() / tot * 100)
                for k, (lo, hi) in bands.items()}
    return cutoff, band_pct


def db(x):
    return float(20 * np.log10(x + 1e-12))


def grade_snr(snr):
    if snr >= 30:
        return "매우 좋음(클린)"
    if snr >= 20:
        return "좋음"
    if snr >= 12:
        return "보통(회의실 전형)"
    if snr >= 6:
        return "나쁨(노이즈 큼)"
    return "매우 나쁨"


def main() -> int:
    if len(sys.argv) < 2:
        print("사용: audio_quality_report.py <audio>", file=sys.stderr)
        return 2
    audio = Path(sys.argv[1])
    if not audio.exists():
        print(f"입력 없음: {audio}", file=sys.stderr)
        return 2

    y, sr = librosa.load(str(audio), sr=SR, mono=True)
    dur = len(y) / sr
    print(f"[quality] {audio.name}  dur={dur:.1f}s ({dur/60:.1f}분) sr={sr}")

    # --- 진폭/클리핑/DC ---
    peak = float(np.abs(y).max())
    clip_pct = float(np.mean(np.abs(y) >= 0.99) * 100)
    nearclip_pct = float(np.mean(np.abs(y) >= 0.95) * 100)
    dc_offset = float(y.mean())
    rms_all = float(np.sqrt((y ** 2).mean()))
    crest_db = db(peak) - db(rms_all)

    # --- VAD 기반 SNR ---
    vad = get_vad("silero")
    vad.load()
    try:
        regions = vad.detect(y, sr=sr)
    finally:
        vad.unload()
    sp = speech_mask(regions, len(y), sr)
    speech_ratio = float(sp.mean() * 100)
    rms_speech = float(np.sqrt((y[sp] ** 2).mean())) if sp.any() else 0.0
    rms_noise = float(np.sqrt((y[~sp] ** 2).mean())) if (~sp).any() else 1e-12
    snr = db(rms_speech) - db(rms_noise)

    # --- 1초 RMS 동적범위 ---
    win = sr
    rms_1s = np.array([np.sqrt((y[i:i + win] ** 2).mean())
                       for i in range(0, len(y) - win, win)])
    rms_db_1s = 20 * np.log10(rms_1s + 1e-12)
    p10, p90 = float(np.percentile(rms_db_1s, 10)), float(np.percentile(rms_db_1s, 90))
    dyn_spread = p90 - p10

    # --- 스펙트럼 ---
    cutoff, band_pct = avg_psd_db(y, sr)

    report = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "audio": str(audio.relative_to(ROOT)) if str(audio).startswith(str(ROOT)) else str(audio),
        "duration_sec": round(dur, 1),
        "sample_rate": sr,
        "amplitude": {
            "peak": round(peak, 4), "peak_dbfs": round(db(peak), 2),
            "rms_dbfs": round(db(rms_all), 2), "crest_factor_db": round(crest_db, 2),
            "clipping_pct": round(clip_pct, 4), "near_clip_pct": round(nearclip_pct, 4),
            "dc_offset": round(dc_offset, 6),
        },
        "snr": {
            "snr_db": round(snr, 2), "grade": grade_snr(snr),
            "speech_rms_dbfs": round(db(rms_speech), 2),
            "noise_rms_dbfs": round(db(rms_noise), 2),
            "speech_ratio_pct": round(speech_ratio, 1),
            "vad_regions": len(regions),
        },
        "dynamics": {"rms1s_p10_db": round(p10, 2), "rms1s_p90_db": round(p90, 2),
                     "dynamic_spread_db": round(dyn_spread, 2)},
        "spectrum": {"highfreq_cutoff_hz": round(cutoff) if cutoff else None,
                     "band_energy_pct": {k: round(v, 1) for k, v in (band_pct or {}).items()}},
    }
    out_json = ROOT / "output" / f"audio_quality-{audio.stem}.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    a, s, d, sp_ = report["amplitude"], report["snr"], report["dynamics"], report["spectrum"]
    lines = [
        f"# 음성 데이터 품질 리포트 — {audio.name}",
        "",
        f"- generated_at: {dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- duration: {dur:.1f}s ({dur/60:.1f}분), sr: {sr} Hz",
        "- 무참조(no-reference) 진단 — STT 정답 불필요, 음원 자체 품질만 평가",
        "",
        "## 헤드라인",
        "",
        "| 지표 | 값 | 판정 |",
        "|---|---|---|",
        f"| **SNR (발화 vs 무음)** | **{s['snr_db']} dB** | {s['grade']} |",
        f"| clipping (|x|≥0.99) | {a['clipping_pct']}% | {'손상 있음' if a['clipping_pct']>0.1 else '양호'} |",
        f"| 고역 cutoff | {sp_['highfreq_cutoff_hz']} Hz | "
        f"{'대역제한/압축 손실' if (sp_['highfreq_cutoff_hz'] or 0) < 7000 else '광대역'} |",
        f"| 음량 동적범위 (1s p90−p10) | {d['dynamic_spread_db']} dB | "
        f"{'화자 음량차 큼' if d['dynamic_spread_db']>12 else '보통'} |",
        "",
        "## 상세",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        f"| peak | {a['peak']} ({a['peak_dbfs']} dBFS) |",
        f"| RMS | {a['rms_dbfs']} dBFS |",
        f"| crest factor | {a['crest_factor_db']} dB |",
        f"| near-clip (|x|≥0.95) | {a['near_clip_pct']}% |",
        f"| DC offset | {a['dc_offset']} |",
        f"| 발화 RMS | {s['speech_rms_dbfs']} dBFS |",
        f"| 무음(노이즈) RMS | {s['noise_rms_dbfs']} dBFS |",
        f"| 발화 비율 | {s['speech_ratio_pct']}% (VAD 구간 {s['vad_regions']}개) |",
        "",
        "## 대역 에너지 분포",
        "",
        "| 대역 | 에너지 % |",
        "|---|---|",
    ]
    for k, v in sp_["band_energy_pct"].items():
        lines.append(f"| {k} | {v} |")
    lines += [
        "",
        "## 해석",
        "",
        f"- SNR {s['snr_db']}dB → **{s['grade']}**. 12~20dB 면 회의실 원거리 마이크의 전형적 수준.",
        f"- 고역 {sp_['highfreq_cutoff_hz']}Hz 절벽 + 저역(0-500Hz)에 에너지 집중 → "
        "원거리/압축 음원 특성(자음 변별 정보가 담긴 고역 부족 = 고유명사 오인식과 연결).",
        "- clipping·DC offset 으로 녹음 단계 손상 유무 확인.",
        "",
    ]
    out_md = ROOT / "output" / f"audio_quality-{audio.stem}.md"
    out_md.write_text("\n".join(lines), encoding="utf-8")

    print(f"[quality] SNR={s['snr_db']}dB({s['grade']}) clip={a['clipping_pct']}% "
          f"cutoff={sp_['highfreq_cutoff_hz']}Hz dyn={d['dynamic_spread_db']}dB "
          f"speech={s['speech_ratio_pct']}%")
    print(f"[quality] saved → {out_json.relative_to(ROOT)}, {out_md.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
