"""표준 출력 계약 어댑터 — 파이프라인 산출 → 웹(meetscript-ai) Meeting JSON.

웹 프로토타입의 `/api/ai/process` 응답 스키마(= types.ts 의 Meeting 부분집합)에 맞춘다:
  { "summary": str, "actionItems": ActionItem[], "transcript": TranscriptEntry[] }
  TranscriptEntry = { speakerId, text, timestamp("MM:SS") }

이 어댑터가 **단일 출력 양식**의 경계다 — 파이프라인(정제본 cleaned.json + 액션아이템
actionitems.json)을 웹/Jira 등 다운스트림이 그대로 쓰는 형태로 변환한다. 파싱·LLM 교체가
이 계약을 깨지 않는다.

미구현 표시(정직):
  - speakerId: 화자분리 미적용 → "" (빈 문자열). diarization 도입 시 채움.
  - summary  : 요약 구조체(MeetingSummary). off(passthrough/None) → 빈 구조체(meta/agenda_index/agenda 비움).
  - actionItems[].owner: 화자분리 전까지 null 가능(extract 규칙과 동일).
"""
from __future__ import annotations

import json
from pathlib import Path

from src.postprocess.extract_schema import seconds_to_timestamp
from src.postprocess.summarize_schema import MeetingSummary


def _summary_or_empty(summary: dict | None) -> dict:
    """summary(구조체 dict) 정규화. None/빈 값 → 빈 요약 구조체(타입 일관, 설계 §4·§6)."""
    if not summary:
        return MeetingSummary.empty().to_dict()
    return summary


def _transcript_from_segments(segments: list[dict]) -> list[dict]:
    """segment 목록 → 웹 TranscriptEntry 목록. cleaned 우선, 없으면 text. 빈 줄 제외."""
    out = []
    for s in segments:
        text = str(s.get("cleaned", s.get("text", ""))).strip()
        if not text:
            continue
        out.append(
            {
                "speakerId": "",  # 화자분리 미적용
                "text": text,
                "timestamp": seconds_to_timestamp(float(s["start"])),
            }
        )
    return out


def _action_items_from_payload(ai: dict) -> list[dict]:
    """추출 산출 dict → 웹 actionItems(표준 필드 노출)."""
    out = []
    for it in ai.get("action_items", ai.get("actionItems", [])):
        out.append(
            {
                "text": it.get("text", ""),
                "owner": it.get("owner"),
                "owner_source": it.get("owner_source"),
                "due": it.get("due"),
                "anchor": it.get("anchor"),
                "evidence_seg_ids": it.get("evidence_seg_ids", []),
                "flag": it.get("flag"),
            }
        )
    return out


def build_meeting_contract_from_segments(
    segments: list[dict],
    action_items: list[dict] | None = None,
    *,
    summary: dict | None = None,
) -> dict:
    """segment 목록(+ 선택 actionItems)에서 직접 웹 Meeting 계약 생성.

    FastAPI 서비스의 부분 E2E 경로(파일 없이 메모리 segment)에서 사용.
    summary 는 요약 구조체(dict) 또는 None(빈 요약, SummarizeStage off).
    """
    return {
        "summary": _summary_or_empty(summary),
        "actionItems": list(action_items or []),
        "transcript": _transcript_from_segments(segments),
    }


def build_meeting_contract(
    cleaned_json: Path | str,
    actionitems_json: Path | str | None = None,
    *,
    summary: dict | None = None,
) -> dict:
    """cleaned.json (+ actionitems.json) → 웹 Meeting 계약 dict.

    transcript 는 정제본 segment 를 {speakerId:"", text, timestamp} 로 매핑(타임스탬프 보존).
    actionItems 는 표준 추출 스키마를 그대로 노출(text/owner/due/anchor/evidence_seg_ids).
    summary 는 요약 구조체(dict) 또는 None(빈 요약, SummarizeStage off).
    """
    cleaned = json.loads(Path(cleaned_json).read_text(encoding="utf-8"))
    action_items: list[dict] = []
    if actionitems_json is not None and Path(actionitems_json).exists():
        ai = json.loads(Path(actionitems_json).read_text(encoding="utf-8"))
        action_items = _action_items_from_payload(ai)
    return {
        "summary": _summary_or_empty(summary),
        "actionItems": action_items,
        "transcript": _transcript_from_segments(cleaned.get("segments", [])),
    }


def write_meeting_contract(
    cleaned_json: Path | str,
    actionitems_json: Path | str | None,
    out_path: Path | str,
    *,
    summary: dict | None = None,
) -> dict:
    contract = build_meeting_contract(cleaned_json, actionitems_json, summary=summary)
    Path(out_path).write_text(
        json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return contract
