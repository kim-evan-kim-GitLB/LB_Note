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


class TranscriptStructureError(ValueError):
    """transcript 구조보존 검증 위반. 호출부(app.py)가 422 로 변환한다."""


def validate_transcript_edit(stored: list[dict], incoming: list[dict]) -> list[dict]:
    """transcript 편집 구조보존 검증(편집 시에만, 후방호환).

    저장본(stored)에 이미 비어있지 않은 transcript 가 있을 때만 적용한다:
      - 엔트리 개수 불변
      - 각 엔트리의 timestamp · speakerId 불변
      - text 만 변경 허용
    위반(개수/타임스탬프/speakerId 변경) 시 TranscriptStructureError.

    text 가 실제로 바뀐 엔트리는 서버가 edited=True 를 set 한다(클라이언트 제공 edited 무시).
    저장본이 비어있던(초기 상태) 경우엔 제약 미적용 → 호출부에서 그대로 통과시킨다.

    필드 보존(M3): 결과 엔트리는 **저장본(old) 베이스**로 만들고 검증된 새 text 만 교체한다.
    timestamp·speakerId 및 저장본의 미지 필드(향후 confidence 등)는 old 에서 그대로 보존되며,
    incoming 의 임의 필드(위조·미지 키)는 반영하지 않는다 → 클라이언트가 transcript 편집으로
    변조할 수 있는 표면을 text 단 하나로 제한한다.

    반환: edited 플래그가 서버 기준으로 정규화된 새 transcript 리스트(원본 비파괴).
    """
    if len(incoming) != len(stored):
        raise TranscriptStructureError(
            f"transcript 엔트리 개수 불변 위반: 저장본 {len(stored)} != 요청 {len(incoming)}"
        )
    out: list[dict] = []
    for idx, (old, new) in enumerate(zip(stored, incoming)):
        if str(new.get("timestamp", "")) != str(old.get("timestamp", "")):
            raise TranscriptStructureError(f"transcript[{idx}] timestamp 불변 위반")
        if str(new.get("speakerId", "")) != str(old.get("speakerId", "")):
            raise TranscriptStructureError(f"transcript[{idx}] speakerId 불변 위반")
        new_text = str(new.get("text", ""))
        old_text = str(old.get("text", ""))
        # M3: 저장본 베이스 + 검증된 새 text 만 교체. 미지 필드/타임스탬프/speakerId 는 old 보존.
        entry = dict(old)
        entry["text"] = new_text
        # edited 는 누적: 저장본이 이미 edited 면 유지, 이번에 바뀌었으면 set. 클라 값 무시.
        if new_text != old_text or old.get("edited"):
            entry["edited"] = True
        else:
            entry.pop("edited", None)
        out.append(entry)
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
