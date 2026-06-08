"""STT + 후처리(in-memory) 서비스 — 기존 파이프라인 헬퍼 재사용(중복 최소).

흐름: audio bytes → (임시파일) run_pipeline(온프렘 STT) → segments
          → normalize → [A]glossary 교정 → CleanStage(backend) → [D]게이트
          → build_meeting_contract_from_segments → {summary, actionItems, transcript}

v1(plan D1=passthrough): CleanStage가 정제를 안 하므로 transcript=원문(+glossary 교정),
actionItems/summary는 빈 값. v2에서 backend_name을 로컬 LLM으로 바꾸면 정제·추출이 살아난다.

주의: run_pipeline은 요청마다 Cohere 모델을 load/unload 한다(요청 사이 VRAM 미점유 → 공유 GPU
친화적, plan 비고). 상주 로드 최적화는 v2.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from src.pipeline import run_pipeline
from src.postprocess.backends import get_llm_backend
from src.postprocess.glossary import load_glossary
from src.postprocess.pipeline import _apply_glossary, _glossary_block, gate_segments
from src.postprocess.schema import CleanResult, normalize_segments
from src.postprocess.stages.clean import CleanStage
from src.postprocess.web_contract import build_meeting_contract_from_segments

# MIME → 확장자(파일명이 없을 때 폴백). load_audio 가 ffmpeg로 디코딩하는 포맷들.
_MIME_EXT = {
    "audio/webm": ".webm", "audio/ogg": ".ogg", "audio/opus": ".opus",
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/m4a": ".m4a",
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav",
    "audio/aac": ".aac",
}


def _suffix_for(mime_type: str | None, filename: str | None) -> str:
    """업로드 파일명 우선, 없으면 MIME, 둘 다 없으면 .webm(브라우저 녹음 기본)."""
    if filename and "." in filename:
        return "." + filename.rsplit(".", 1)[1].lower()
    return _MIME_EXT.get((mime_type or "").lower(), ".webm")


def transcribe_bytes(
    audio_bytes: bytes, *, mime_type: str | None = None, filename: str | None = None
) -> tuple[list[dict], float | None]:
    """오디오 bytes → (STT segments[{start,end,text}], duration_seconds).

    bytes 를 임시파일로 떨군 뒤 기존 run_pipeline(파일 기반 STT 단일 진입점)을 재사용한다.
    """
    suffix = _suffix_for(mime_type, filename)
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        audio_path = tdp / f"upload{suffix}"
        audio_path.write_bytes(audio_bytes)
        payload = run_pipeline(audio_path=audio_path, out_dir=tdp / "out")
    duration = payload.get("audio", {}).get("duration_seconds")
    return payload.get("segments", []), duration


def clean_segments(raw_segments: list[dict], backend_name: str = "passthrough") -> CleanResult:
    """STT segments → 정제 결과(CleanResult). run_postprocess의 in-memory 경로 재사용.

    [A]glossary(결정적 교정) → CleanStage(backend) → [D]게이트. 파일 입출력 없음.
    """
    segments = normalize_segments(raw_segments)
    backend = get_llm_backend(backend_name)
    glossary = load_glossary(None)
    corrected, applied_per_seg = _apply_glossary(segments, glossary)
    stage = CleanStage()
    result = stage.run(
        corrected, backend, ctx={"glossary_block": _glossary_block(glossary)}
    )
    validated, _n_flagged = gate_segments(
        result,
        applied_per_seg,
        edit_lo=0.0,
        edit_hi=0.6,
        require_edit=(backend.name != "passthrough"),
        semantic_check=None,
    )
    return CleanResult(segments=validated)


def process_audio_to_contract(
    audio_bytes: bytes,
    *,
    mime_type: str | None = None,
    filename: str | None = None,
    backend_name: str = "passthrough",
) -> dict:
    """오디오 bytes → 웹 Meeting 계약 {summary, actionItems, transcript} (+ _duration_seconds)."""
    raw_segments, duration = transcribe_bytes(
        audio_bytes, mime_type=mime_type, filename=filename
    )
    final = clean_segments(raw_segments, backend_name=backend_name)
    seg_dicts = [
        {"id": s.id, "start": s.start, "end": s.end, "cleaned": s.cleaned, "text": s.original}
        for s in final.segments
    ]
    # v1: actionItems=[] (extract는 LLM 필요 → v2), summary="" (SummarizeStage 미구현 → v2)
    contract = build_meeting_contract_from_segments(seg_dicts, action_items=[], summary="")
    contract["_duration_seconds"] = duration
    return contract
