"""공유 전처리 모듈 — chunk-size 무관. pipeline(60s)·slice(600s) 양쪽이 호출.

순서(각 opt-in): dereverb(WPE) → denoise(GTCRN) → VAD(무음압축).
- enhancer: 전체 16k 신호에 적용(길이 보존).
- VAD: 발화 구간 검출 후 긴 무음을 제거해 압축 신호 생성. offset_map 으로
  압축시간→원본시간 복원 가능(비파괴). speech_regions(원본 타임라인)는
  향후 diarization 정렬용으로 보존.

전 스테이지 비활성이면 no-op(samples 불변, offset_map 항등) → 기존 출력 보존.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from src.backends.enhancer_base import AudioEnhancer
from src.backends.vad_base import VADBackend


@dataclass
class PreprocessResult:
    samples: np.ndarray                      # 전처리된 신호 (VAD 시 길이 단축 가능)
    speech_regions: list[tuple[float, float]]  # 원본 타임라인 발화 구간
    offset_map: list[tuple[float, float, float]]  # (comp_start, orig_start, length) sec
    applied: list[str] = field(default_factory=list)
    original_sec: float = 0.0
    compressed_sec: float = 0.0


def _identity_map(dur: float) -> list[tuple[float, float, float]]:
    return [(0.0, 0.0, dur)]


def remap_time(offset_map: list[tuple[float, float, float]], t: float) -> float:
    """압축 타임라인 시각 t(sec) → 원본 타임라인 시각."""
    if not offset_map:
        return t
    for comp_start, orig_start, length in offset_map:
        if t < comp_start + length or length == 0:
            return orig_start + max(0.0, t - comp_start)
    # 마지막 조각 끝을 넘으면 마지막 원본 끝으로 클램프
    comp_start, orig_start, length = offset_map[-1]
    return orig_start + length


def _compress_silence(
    samples: np.ndarray, sr: int, regions: list[tuple[float, float]],
    pad_sec: float, max_silence_sec: float,
) -> tuple[np.ndarray, list[tuple[float, float, float]]]:
    """발화 ±pad 를 유지하고, gap>max_silence 인 무음을 제거. (압축신호, offset_map)."""
    dur = len(samples) / sr
    if not regions:
        return samples, _identity_map(dur)

    # 1) pad 확장 + 클립
    exp = [(max(0.0, s - pad_sec), min(dur, e + pad_sec)) for s, e in regions]
    # 2) gap<=max_silence 면 병합(짧은 자연 pause 는 원본 그대로 유지)
    merged: list[list[float]] = [list(exp[0])]
    for s, e in exp[1:]:
        if s - merged[-1][1] <= max_silence_sec:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    pieces: list[np.ndarray] = []
    offset_map: list[tuple[float, float, float]] = []
    comp_cursor = 0.0
    for s, e in merged:
        a, b = int(round(s * sr)), int(round(e * sr))
        if b <= a:
            continue
        pieces.append(samples[a:b])
        length = (b - a) / sr
        offset_map.append((comp_cursor, a / sr, length))
        comp_cursor += length

    if not pieces:  # 안전장치: 발화 추출 실패 시 원본 유지
        return samples, _identity_map(dur)
    return np.concatenate(pieces).astype(np.float32), offset_map


def preprocess(
    samples: np.ndarray,
    sr: int,
    enhancers: list[AudioEnhancer] | None = None,
    vad: VADBackend | None = None,
    vad_pad_sec: float = 0.25,
    vad_max_silence_sec: float = 0.5,
) -> PreprocessResult:
    enhancers = enhancers or []
    original_sec = len(samples) / sr
    out = samples
    applied: list[str] = []

    for enh in enhancers:
        enh.load()
        try:
            out = enh.process(out, sr=sr)
        finally:
            enh.unload()
        applied.append(enh.name)

    speech_regions: list[tuple[float, float]] = []
    offset_map = _identity_map(len(out) / sr)
    if vad is not None:
        vad.load()
        try:
            speech_regions = vad.detect(out, sr=sr)
        finally:
            vad.unload()
        out, offset_map = _compress_silence(
            out, sr, speech_regions, vad_pad_sec, vad_max_silence_sec
        )
        applied.append(vad.name)

    return PreprocessResult(
        samples=out,
        speech_regions=speech_regions,
        offset_map=offset_map,
        applied=applied,
        original_sec=round(original_sec, 3),
        compressed_sec=round(len(out) / sr, 3),
    )
