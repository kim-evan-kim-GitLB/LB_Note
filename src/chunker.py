"""고정 길이 오디오 청크 분할 + 인접 청크 텍스트 dedupe.

청크 = 60초 길이 + 10초 overlap. 마지막 청크는 짧을 수 있음.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

from src.types import Segment

_PUNCT_RE = re.compile(r"[\.,!?…\"'‘’“”]+")


def _norm_token(t: str) -> str:
    return _PUNCT_RE.sub("", t).strip()

DEFAULT_CHUNK_SEC = 60.0
DEFAULT_OVERLAP_SEC = 10.0


@dataclass
class AudioChunk:
    index: int
    start_sec: float
    end_sec: float
    samples: np.ndarray


def chunk_audio(
    audio: np.ndarray,
    sr: int = 16000,
    chunk_sec: float = DEFAULT_CHUNK_SEC,
    overlap_sec: float = DEFAULT_OVERLAP_SEC,
) -> list[AudioChunk]:
    assert audio.ndim == 1, f"1D 배열만 지원. shape={audio.shape}"
    if overlap_sec >= chunk_sec:
        raise ValueError("overlap_sec 은 chunk_sec 미만이어야 합니다")

    total = len(audio)
    chunk_n = int(round(chunk_sec * sr))
    step_n = int(round((chunk_sec - overlap_sec) * sr))
    if total <= chunk_n:
        return [AudioChunk(0, 0.0, total / sr, audio)]

    chunks: list[AudioChunk] = []
    idx = 0
    start = 0
    while start < total:
        end = min(start + chunk_n, total)
        chunks.append(AudioChunk(
            index=idx,
            start_sec=start / sr,
            end_sec=end / sr,
            samples=audio[start:end],
        ))
        if end >= total:
            break
        start += step_n
        idx += 1
    return chunks


def _strip_overlap(prev_text: str, next_text: str, max_tokens: int = 50) -> str:
    """next 의 첫 k 토큰이 prev 의 마지막 max_tokens 윈도우 어딘가에 등장하면 그 부분 제거.

    문장부호는 정규화 후 비교 → hallucination 토큰이 끼어있어도 dedupe 동작.
    """
    prev_t = prev_text.split()
    next_t = next_text.split()
    if not prev_t or not next_t:
        return next_text
    prev_norm = [_norm_token(t) for t in prev_t]
    next_norm = [_norm_token(t) for t in next_t]
    window = prev_norm[-max_tokens:] if len(prev_norm) > max_tokens else prev_norm
    upper = min(max_tokens, len(window), len(next_norm))
    for k in range(upper, 2, -1):
        target = next_norm[:k]
        if not all(target):
            continue
        for i in range(len(window) - k + 1):
            if window[i:i + k] == target:
                return " ".join(next_t[k:])
    return next_text


def merge_segments(segments: list[Segment]) -> list[Segment]:
    """청크별 Segment 리스트를 받아 인접 텍스트 dedupe 후 반환.

    각 Segment 의 start/end 는 그대로 유지(청크 경계 시각).
    """
    if not segments:
        return []
    merged: list[Segment] = [segments[0]]
    for nxt in segments[1:]:
        prev = merged[-1]
        cleaned = _strip_overlap(prev.text, nxt.text)
        merged.append(Segment(
            start=nxt.start,
            end=nxt.end,
            text=cleaned,
            confidence=nxt.confidence,
            speaker=nxt.speaker,
            meta=nxt.meta,
        ))
    return merged
