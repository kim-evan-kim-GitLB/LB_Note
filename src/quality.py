"""음원 품질 측정 + 증거기반 향상 라우팅 (P5).

`tools/audio_quality_report.py` 의 무참조 지표 계산을 라이브러리로 추출해 파이프라인이
in-memory 로 재사용한다(이미 로드된 16k samples 대상, 추가 디코딩 없음).

핵심 설계(메모리 검증 결과 반영): **향상(WPE/GTCRN)은 net-negative** — 병목이 노이즈가
아니라 6kHz 대역제한이기 때문. 따라서 "품질 낮음→향상"이 아니라:
  - 대역제한(cutoff 낮음)  → 향상 건너뛰고 VAD 직행 (denoise 해도 손실 못 복구, 역효과)
  - 노이즈우세 + 대역양호 → denoise(gtcrn)만 적용
  - 그 외(클린)           → 향상 없음
결정과 측정값을 모두 노출해 감사 가능하게 한다.
"""
from __future__ import annotations

import numpy as np

_N_FFT = 1024
_HOP = 256


def _db(x: float) -> float:
    return float(20 * np.log10(x + 1e-12))


def _grade_snr(snr: float) -> str:
    if snr >= 30:
        return "매우 좋음(클린)"
    if snr >= 20:
        return "좋음"
    if snr >= 12:
        return "보통(회의실 전형)"
    if snr >= 6:
        return "나쁨(노이즈 큼)"
    return "매우 나쁨"


def _avg_psd(y: np.ndarray, sr: int) -> tuple[float | None, dict | None]:
    """블록 평균 파워 스펙트럼 → (고역 cutoff Hz, 대역별 에너지 %)."""
    import librosa

    psd = np.zeros(_N_FFT // 2 + 1, dtype=np.float64)
    n_frames = 0
    block = sr * 60
    for start in range(0, len(y), block):
        seg = y[start:start + block]
        if len(seg) < _N_FFT:
            continue
        S = np.abs(librosa.stft(seg, n_fft=_N_FFT, hop_length=_HOP)) ** 2
        psd += S.sum(axis=1)
        n_frames += S.shape[1]
    if n_frames == 0:
        return None, None
    psd /= n_frames
    psd_db = 10 * np.log10(psd + 1e-12)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=_N_FFT)
    cutoff = float(freqs[psd_db >= psd_db.max() - 40].max())
    bands = {"low(0-500)": (0, 500), "mid(500-2k)": (500, 2000),
             "high(2k-4k)": (2000, 4000), "vhigh(4k-8k)": (4000, 8000)}
    tot = psd.sum()
    band_pct = {k: float(psd[(freqs >= lo) & (freqs < hi)].sum() / tot * 100)
                for k, (lo, hi) in bands.items()}
    return cutoff, band_pct


def compute_quality_metrics(y: np.ndarray, sr: int = 16000) -> dict:
    """이미 로드된 16k mono float32 음원의 무참조 품질 지표.

    tools/audio_quality_report.py 와 동일 정의(SNR=발화RMS−무음RMS, 고역 cutoff,
    clipping, 동적범위). Silero VAD 로 발화/무음 분리(CPU). 반환 dict 는 라우팅 입력.
    """
    from src.stt import get_vad

    y = np.asarray(y, dtype=np.float32)
    peak = float(np.abs(y).max()) if y.size else 0.0
    clip_pct = float(np.mean(np.abs(y) >= 0.99) * 100) if y.size else 0.0

    vad = get_vad("silero")
    vad.load()
    try:
        regions = vad.detect(y, sr=sr)
    finally:
        vad.unload()

    mask = np.zeros(len(y), dtype=bool)
    for s, e in regions:
        a, b = int(s * sr), min(int(e * sr), len(y))
        if b > a:
            mask[a:b] = True
    speech_ratio = float(mask.mean() * 100) if y.size else 0.0
    rms_speech = float(np.sqrt((y[mask] ** 2).mean())) if mask.any() else 0.0
    rms_noise = float(np.sqrt((y[~mask] ** 2).mean())) if (~mask).any() else 1e-12
    snr = _db(rms_speech) - _db(rms_noise)

    cutoff, band_pct = _avg_psd(y, sr)

    return {
        "duration_sec": round(len(y) / float(sr), 1) if y.size else 0.0,
        "amplitude": {"peak": round(peak, 4), "clipping_pct": round(clip_pct, 4)},
        "snr": {"snr_db": round(snr, 2), "grade": _grade_snr(snr),
                "speech_ratio_pct": round(speech_ratio, 1), "vad_regions": len(regions)},
        "spectrum": {"highfreq_cutoff_hz": round(cutoff) if cutoff else None,
                     "band_energy_pct": {k: round(v, 1) for k, v in (band_pct or {}).items()}},
    }


def decide_enhancers(
    metrics: dict,
    *,
    snr_lo: float = 12.0,
    cutoff_ok_hz: float = 7000.0,
) -> tuple[list[str], str]:
    """품질 지표 → 적용할 enhancer 리스트 + 사유(증거기반).

    - 대역제한(cutoff < cutoff_ok_hz): 향상 net-negative([[ax-stt-enhancement-net-negative]])
      → [] (VAD 직행). denoise 는 노이즈만 줄일 뿐 잘려나간 고역을 복구 못 함.
    - 노이즈우세(snr < snr_lo) + 대역양호: ["gtcrn"] (denoise 만; WPE 는 검증상 역효과라 제외).
    - 그 외: [] (클린 — 손대지 않음).
    """
    snr = metrics.get("snr", {}).get("snr_db")
    cutoff = metrics.get("spectrum", {}).get("highfreq_cutoff_hz") or 0

    if snr is None:
        return [], "지표없음(측정실패)→향상생략"
    if cutoff < cutoff_ok_hz:
        return [], f"대역제한(cutoff={cutoff}Hz<{cutoff_ok_hz:.0f})→향상생략(net-negative)"
    if snr < snr_lo:
        return ["gtcrn"], f"노이즈우세(SNR={snr}dB<{snr_lo})+대역양호→denoise"
    return [], f"클린(SNR={snr}dB,cutoff={cutoff}Hz)→향상불필요"
