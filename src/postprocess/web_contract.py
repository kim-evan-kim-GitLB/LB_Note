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

import datetime as _dt
import json
import uuid
from pathlib import Path

from src.postprocess.extract_schema import seconds_to_timestamp
from src.postprocess.summarize_schema import MeetingSummary


def _summary_or_empty(summary: dict | None) -> dict:
    """summary(구조체 dict) 정규화. None/빈 값 → 빈 요약 구조체(타입 일관, 설계 §4·§6)."""
    if not summary:
        return MeetingSummary.empty().to_dict()
    return summary


def _transcript_from_segments(segments: list[dict]) -> list[dict]:
    """segment 목록 → 웹 TranscriptEntry 목록. cleaned 우선, 없으면 text. 빈 줄 제외.

    segmentId(원본 STT segment id)를 함께 노출한다 — summary/actionItem 의 evidence_seg_ids 가
    이 id 를 참조하므로, transcript↔근거 매핑과 (향후) 재요약의 evidence 정합에 필요하다.
    빈 줄 제외로 위치 인덱스는 시프트되지만 segmentId 는 원본 id 라 매핑이 보존된다.
    """
    out = []
    for s in segments:
        text = str(s.get("cleaned", s.get("text", ""))).strip()
        if not text:
            continue
        entry = {
            "speakerId": "",  # 화자분리 미적용
            "text": text,
            "timestamp": seconds_to_timestamp(float(s["start"])),
        }
        if s.get("id") is not None:
            entry["segmentId"] = int(s["id"])
        out.append(entry)
    return out


def _action_items_from_payload(ai: dict) -> list[dict]:
    """추출 산출 dict → 웹 actionItems(표준 필드 노출).

    item_id(uuid): 웹 계약 경계에서 부여하는 안정 식별자(summary item_id 와 대칭). 편집·(향후)
    재요약 item_id 단위 대조의 조인키. 원본 추출 스키마(ExtractResult, golden 비교)는 건드리지
    않고 여기서만 부여해 결정성을 유지한다. 기존 값이 있으면 보존(멱등).
    """
    out = []
    for it in ai.get("action_items", ai.get("actionItems", [])):
        out.append(
            {
                "item_id": str(it.get("item_id") or uuid.uuid4().hex),
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


def ensure_action_item_ids(items: object) -> list[dict]:
    """actionItems 의 item_id 무결성 보장(재요약 P8 조인키 선결).

    actionItems 는 UI 에서 자유롭게 추가/삭제/편집/확정토글되므로 summary 처럼 구조(개수/순서)를
    잠그지 않는다 — 구조 검증은 부적절(정상 추가·삭제를 막음). 대신 재요약 item_id 단위 대조의
    안정 조인키만 보장한다: 각 항목에 **고유 item_id** 부여 — 기존 값은 보존(불변), 부재/중복
    (UI 신규 추가·위조·복제)은 새 uuid 부여. 순서·개수·text·기타 필드는 그대로 둔다(비파괴, 멱등).

    create_meeting(POST)·patch_meeting(PATCH) 양 저장 경로에서 호출돼, 어느 경로로 저장하든 저장본
    actionItems 가 항상 유일 item_id 를 갖도록 한다. dict 가 아닌 항목은 무시(그대로 통과).
    """
    out: list[dict] = []
    seen: set[str] = set()
    for it in items or []:  # type: ignore[union-attr]
        if not isinstance(it, dict):
            out.append(it)
            continue
        entry = dict(it)
        iid = entry.get("item_id")
        if not iid or not isinstance(iid, str) or iid in seen:
            iid = uuid.uuid4().hex
        entry["item_id"] = iid
        seen.add(iid)
        out.append(entry)
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


class SummaryStructureError(ValueError):
    """summary 편집 구조보존 검증 위반. 호출부(app.py)가 422 로 변환한다."""


def _now_iso_micro() -> str:
    """edited_at 용 타임스탬프(UTC·마이크로초). store._now_iso_micro 와 동일 포맷."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="microseconds")


def _changed(new: dict, old: dict, key: str) -> bool:
    """incoming 이 key 를 **명시적으로** 다른 값으로 보냈는지(불변 위반 후보).

    key 가 incoming 에 없으면 변경 아님(저장본 보존). 출력은 항상 저장본 베이스라 누락은
    안전하게 보존되고, 명시적 위조만 거부 대상이 된다.
    """
    if key not in new:
        return False
    return str(new.get(key) or "") != str(old.get(key) or "")


def _validate_summary_items(stored_items: list, incoming_items: list, *, path: str) -> list[dict]:
    """한 섹션(points/decisions/issues) SummaryItem 리스트 편집 검증·정규화.

    구조 보존: 개수·anchor·evidence_seg_ids·item_id 불변, text 만 편집. 결과 항목은 **저장본
    베이스**(dict(old))로 만들어 anchor/evidence/미지 필드를 보존하고 검증된 새 text·edited
    메타만 덮어쓴다. item_id 부재(레거시)는 여기서 lazy 부여한다(무파괴 마이그레이션).
    """
    if len(incoming_items) != len(stored_items):
        raise SummaryStructureError(
            f"{path} 항목 개수 불변 위반: 저장본 {len(stored_items)} != 요청 {len(incoming_items)}"
        )
    out: list[dict] = []
    for idx, (old, new) in enumerate(zip(stored_items, incoming_items)):
        if not isinstance(new, dict):
            raise SummaryStructureError(f"{path}[{idx}] 항목 형식 오류")
        if _changed(new, old, "anchor"):
            raise SummaryStructureError(f"{path}[{idx}] anchor 불변 위반")
        if "evidence_seg_ids" in new:
            try:
                new_ev = [int(x) for x in (new.get("evidence_seg_ids") or [])]
            except (TypeError, ValueError):
                raise SummaryStructureError(f"{path}[{idx}] evidence_seg_ids 형식 오류")
            old_ev = [int(x) for x in (old.get("evidence_seg_ids") or [])]
            # 순서 무시 비교: 근거 집합(중복 포함)만 같으면 정상 — 출력은 어차피 저장본 스냅샷으로
            # 동결되므로 클라 직렬화 순서 차이([0,1]↔[1,0])로 정상 편집을 422 로 막지 않는다.
            if sorted(new_ev) != sorted(old_ev):
                raise SummaryStructureError(f"{path}[{idx}] evidence_seg_ids 불변 위반(근거 게이트)")
        # item_id: 저장본 우선. 부재(레거시)면 lazy 부여. incoming 이 다른 값을 주면 위조로 거부.
        stored_id = old.get("item_id")
        if not stored_id:
            out_id = uuid.uuid4().hex
        else:
            out_id = str(stored_id)
            incoming_id = new.get("item_id")
            if incoming_id is not None and str(incoming_id) != out_id:
                raise SummaryStructureError(f"{path}[{idx}] item_id 불변 위반")

        new_text = str(new.get("text", "")).strip()
        old_text = str(old.get("text", ""))
        entry = dict(old)  # 저장본 베이스: evidence 스냅샷·anchor·미지 필드 동결(grounding 우회)
        entry["text"] = new_text
        entry["item_id"] = out_id
        # 변경 판정은 양쪽 strip 기준(저장본의 선·후행 공백 차이만으로 edited 오설정 방지).
        if new_text != old_text.strip():
            # 서버 set: 교정 표시 + 최초 original_text 동결 + edited_at 갱신. 클라 edited 무시.
            entry["edited"] = True
            entry["edited_at"] = _now_iso_micro()
            entry["original_text"] = (
                old["original_text"] if old.get("original_text") is not None else old_text
            )
        elif old.get("edited"):
            entry["edited"] = True  # 기존 교정 항목은 유지(edited_at/original_text 저장본 보존)
        else:
            entry.pop("edited", None)
            entry.pop("edited_at", None)
            entry.pop("original_text", None)
        out.append(entry)
    return out


def validate_summary_edit(stored: dict, incoming: dict) -> dict:
    """summary 항목 text 교정 구조보존 검증(편집 시에만, 후방호환). P6.

    저장본 summary 에 agenda 블록이 있을 때만 적용한다. 구조(블록 개수·no·title, 각 섹션 항목
    개수·anchor·evidence_seg_ids·item_id)는 불변이고 **SummaryItem.text 만** 편집 허용한다.
    text 가 바뀐 항목은 서버가 edited=True/edited_at/original_text(최초 동결)를 set 하고
    evidence_seg_ids 는 저장본 그대로 동결한다 — 게이트(ground_summary)는 **생성 시점** 산출이며
    편집 PATCH 는 grounding 을 우회한다(재드롭/anchor·time_range 재산출 없음). 근거 게이트 면제는
    "근거 실재(저장본 evidence 보존) + text-edit 드롭만 면제"로 제한된다(D5).

    결과는 **저장본(stored) 베이스**로 만들고 검증된 새 text·edited 메타만 덮어쓴다. meta·
    agenda_index·블록 메타·미지 필드는 저장본에서 보존하며 incoming 의 임의 필드(위조)는 반영하지
    않는다 → summary 편집으로 변조 가능한 표면을 항목 text 로 제한한다. item_id 부재(레거시 회의)는
    이 시점에 lazy 부여(무파괴 마이그레이션)된다. 위반 시 SummaryStructureError(호출부 422).
    """
    stored_agenda = stored.get("agenda") or []
    incoming_agenda = incoming.get("agenda")
    if incoming_agenda is None:
        return stored  # agenda 미포함 patch → 구조 편집 아님, 저장본 보존
    if not isinstance(incoming_agenda, list):
        raise SummaryStructureError("agenda 형식 오류")
    if len(incoming_agenda) != len(stored_agenda):
        raise SummaryStructureError(
            f"agenda 블록 개수 불변 위반: 저장본 {len(stored_agenda)} != 요청 {len(incoming_agenda)}"
        )
    out = dict(stored)  # schema_version/meta/agenda_index 등 저장본 보존
    out_blocks: list[dict] = []
    for bidx, (old_blk, new_blk) in enumerate(zip(stored_agenda, incoming_agenda)):
        if not isinstance(new_blk, dict):
            raise SummaryStructureError(f"agenda[{bidx}] 블록 형식 오류")
        if "no" in new_blk and str(new_blk.get("no")) != str(old_blk.get("no")):
            raise SummaryStructureError(f"agenda[{bidx}] no 불변 위반")
        if _changed(new_blk, old_blk, "title"):
            raise SummaryStructureError(f"agenda[{bidx}] title 불변 위반")
        blk = dict(old_blk)  # 블록 메타(no/title/time_range/evidence) 저장본 보존
        for sec in ("points", "decisions", "issues"):
            blk[sec] = _validate_summary_items(
                old_blk.get(sec) or [], new_blk.get(sec) or [], path=f"agenda[{bidx}].{sec}"
            )
        out_blocks.append(blk)
    out["agenda"] = out_blocks
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
