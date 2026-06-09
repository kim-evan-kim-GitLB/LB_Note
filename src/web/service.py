"""STT + 후처리(in-memory) 서비스 — 기존 파이프라인 헬퍼 재사용(중복 최소).

흐름: audio bytes → (임시파일) run_pipeline(온프렘 STT) → segments
          → normalize → [A]glossary 교정 → CleanStage(backend) → [D]게이트
          → ExtractStage(backend, 회의 단위 액션아이템)
          → build_meeting_contract_from_segments → {summary, actionItems, transcript}

backend_name="passthrough"(기본): CleanStage가 정제를 안 하므로 transcript=원문(+glossary 교정),
추출도 건너뜀(actionItems=[]). 실 백엔드(agent_cli/로컬 LLM)면 정제·추출이 살아난다.
summary 는 SummarizeStage 미구현이라 항상 "" (추출과는 별개 스테이지).

주의: run_pipeline은 요청마다 Cohere 모델을 load/unload 한다(요청 사이 VRAM 미점유 → 공유 GPU
친화적, plan 비고). 상주 로드 최적화는 v2.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from src.pipeline import run_pipeline
from src.postprocess.backends import get_llm_backend
from src.postprocess.extract_schema import seconds_to_timestamp
from src.postprocess.glossary import load_glossary
from src.postprocess.pipeline import _apply_glossary, _glossary_block, gate_segments
from src.postprocess.schema import CleanResult, normalize_segments
from src.postprocess.stages.clean import CleanStage
from src.postprocess.stages.extract import ExtractStage
from src.postprocess.web_contract import (
    _action_items_from_payload,
    build_meeting_contract_from_segments,
)

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


def extract_action_items(
    cleaned_segments: list[dict], backend_name: str = "passthrough"
) -> list[dict]:
    """정제 segment → 웹 actionItems 목록. ExtractStage(회의 단위) 를 backend 로 직접 호출.

    추출은 segment 1:1 이 아니라 회의 전체 transcript 단위다(한 과제가 여러 segment에 걸침).
    ExtractStage 입력은 [{id, text}] — load_cleaned_segments 와 동일하게 정제 본문(cleaned)을
    text 로 노출한다. ExtractResult → 웹 계약 필드(text/owner/due/anchor/...)로 정규화.

    passthrough 등 JSON 출력이 안 되는 백엔드는 빈 결과가 나오므로 호출부에서 건너뛴다.
    """
    ex_input = [
        {
            "id": s["id"],
            "start": s["start"],
            "end": s["end"],
            "text": (s.get("cleaned") or s.get("text") or ""),
        }
        for s in cleaned_segments
    ]
    backend = get_llm_backend(backend_name)
    result = ExtractStage().run(ex_input, backend)
    # anchor 결정적 산출: LLM 출력 anchor 는 신뢰하지 않는다(보통 null/추측). 계약대로
    # evidence_seg_ids 의 최소 start 에서 호출부가 직접 MM:SS 로 채운다(설계 §5, ActionItem.anchor).
    start_by_id = {s["id"]: s["start"] for s in ex_input}
    for it in result.items:
        ev_starts = [start_by_id[sid] for sid in it.evidence_seg_ids if sid in start_by_id]
        it.anchor = seconds_to_timestamp(min(ev_starts)) if ev_starts else None
    return _action_items_from_payload(result.to_dict())


def process_audio_to_contract(
    audio_bytes: bytes,
    *,
    mime_type: str | None = None,
    filename: str | None = None,
    backend_name: str = "passthrough",
    extract_backend_name: str | None = None,
) -> dict:
    """오디오 bytes → 웹 Meeting 계약 {summary, actionItems, transcript} (+ _duration_seconds).

    backend_name 은 정제(CleanStage) 백엔드. extract_backend_name 은 추출 백엔드로 **정제와
    독립 설정**할 수 있다(미지정 시 정제 백엔드를 따른다). 분리 이유: 정제는 segment당 1콜이라
    클라우드면 비싸지만(≈$4~5/회의), 추출은 회의당 1콜이라 클라우드도 ≈$0.06 → "정제=passthrough,
    추출=agent_cli" 같은 저비용 구성이 가능. 백엔드는 backend-agnostic(추후 ollama 로 교체 용이).
    """
    raw_segments, duration = transcribe_bytes(
        audio_bytes, mime_type=mime_type, filename=filename
    )
    final = clean_segments(raw_segments, backend_name=backend_name)
    seg_dicts = [
        {"id": s.id, "start": s.start, "end": s.end, "cleaned": s.cleaned, "text": s.original}
        for s in final.segments
    ]
    # 액션아이템 추출: passthrough 는 추출 불가(빈 값) 이므로 건너뛰고, 실 백엔드면 ExtractStage 가동.
    ex_backend = extract_backend_name or backend_name
    action_items: list[dict] = []
    if ex_backend != "passthrough":
        action_items = extract_action_items(seg_dicts, backend_name=ex_backend)
    # summary 는 SummarizeStage 미구현 → "" 유지(추출과 별개 스테이지).
    contract = build_meeting_contract_from_segments(
        seg_dicts, action_items=action_items, summary=""
    )
    contract["_duration_seconds"] = duration
    return contract
