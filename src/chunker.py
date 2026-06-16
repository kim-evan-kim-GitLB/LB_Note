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

# --- VAD 분할 청킹 기본값 (tools/vad_chunk_ax_clova.py 의 '되는 버전' 상수) ---
DEFAULT_TARGET_SEC = 30.0        # < max_audio_clip_s(35) → 모델 내부 청커 재분할 없음
DEFAULT_PAD_SEC = 0.2            # 발화 구간 앞뒤 여유
DEFAULT_SEG_OVERLAP_SEC = 2.0    # 초장발화 hard-split 시에만 사용(겹침+dedup)
SEAM_DEDUP_MAX_WORDS = 12        # overlap seam 단어 중복제거 탐색 한도


@dataclass
class AudioChunk:
    index: int
    start_sec: float
    end_sec: float
    samples: np.ndarray
    # overlap seam(초장발화 hard-split 경계) 여부 — VAD 분할에서만 사용, 병합 시 dedup 대상.
    is_overlap_seam: bool = False


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


def vad_segment_chunks(
    audio: np.ndarray,
    sr: int = 16000,
    regions: list[tuple[float, float]] | None = None,
    target_sec: float = DEFAULT_TARGET_SEC,
    pad_sec: float = DEFAULT_PAD_SEC,
    overlap_sec: float = DEFAULT_SEG_OVERLAP_SEC,
) -> list[AudioChunk]:
    """VAD 발화 구간(regions) → ≤target_sec AudioChunk 리스트.

    tools/vad_chunk_ax_clova.py 의 build_chunks 로직 포팅:
    - 인접 발화를 ≤target 으로 greedy 묶고, 컷은 발화 사이 무음 gap 에 떨어진다.
    - 단일 발화가 target 초과면 overlap 을 주고 hard-split(이 경계만 is_overlap_seam=True).
    각 청크 ≤target<max_audio_clip_s 라 모델 내부 에너지 청커가 재분할하지 않음.
    """
    assert audio.ndim == 1, f"1D 배열만 지원. shape={audio.shape}"
    dur = len(audio) / float(sr)
    regions = regions or []
    # pad 적용 후 경계 클램프
    padded = [(max(0.0, s - pad_sec), min(dur, e + pad_sec)) for s, e in regions]

    spans: list[tuple[float, float, bool]] = []  # (start, end, is_overlap_seam)
    cur_s: float | None = None
    cur_e: float | None = None

    def flush() -> None:
        nonlocal cur_s, cur_e
        if cur_s is not None:
            spans.append((cur_s, cur_e, False))
            cur_s, cur_e = None, None

    for s, e in padded:
        if e - s > target_sec:                       # 초장발화 → hard-split (겹침)
            flush()
            t = s
            first = True
            while t < e:
                seg_end = min(t + target_sec, e)
                spans.append((t, seg_end, not first))
                first = False
                if seg_end >= e:
                    break
                t = seg_end - overlap_sec
            continue
        if cur_s is None:
            cur_s, cur_e = s, e
        elif e - cur_s <= target_sec:                # 같은 청크로 확장(내부 짧은 pause 포함)
            cur_e = e
        else:                                        # 무음 gap 에서 컷
            flush()
            cur_s, cur_e = s, e
    flush()

    chunks: list[AudioChunk] = []
    for idx, (s, e, seam) in enumerate(spans):
        a = int(round(s * sr))
        b = int(round(e * sr))
        chunks.append(AudioChunk(
            index=idx,
            start_sec=s,
            end_sec=e,
            samples=audio[a:b],
            is_overlap_seam=seam,
        ))
    return chunks


def merge_vad_segments(segments: list[Segment]) -> list[Segment]:
    """VAD 분할 청크 Segment 병합. overlap seam 청크만 앞 청크 꼬리와 단어 중복제거.

    tools/vad_chunk_ax_clova.py 의 merge_texts 로직 포팅(SEAM_DEDUP_MAX_WORDS).
    seam 이 아닌 청크는 dedup 없이 그대로 이어붙임(컷이 항상 무음 경계라 중복 없음).
    is_overlap_seam 은 Segment.meta["is_overlap_seam"] 로 전달받는다.
    """
    if not segments:
        return []
    merged: list[Segment] = [segments[0]]
    for nxt in segments[1:]:
        prev = merged[-1]
        text = nxt.text
        if nxt.meta.get("is_overlap_seam"):
            prev_w = prev.text.split()
            w = text.split()
            if prev_w and w:
                maxk = min(SEAM_DEDUP_MAX_WORDS, len(prev_w), len(w))
                best = 0
                for k in range(maxk, 0, -1):
                    if prev_w[-k:] == w[:k]:
                        best = k
                        break
                text = " ".join(w[best:])
        merged.append(Segment(
            start=nxt.start,
            end=nxt.end,
            text=text,
            confidence=nxt.confidence,
            speaker=nxt.speaker,
            meta=nxt.meta,
        ))
    return merged


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
