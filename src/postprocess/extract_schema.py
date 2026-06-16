"""액션아이템 추출 출력 스키마 (계약, 설계 §5 후속 Phase 1-b).

이 구조가 **표준 출력 양식**이며, 동시에 웹 프론트(meetscript-ai)의 actionItems 계약이다.
LLM/추출기가 무엇이든 다운스트림(웹·Jira 등)은 항상 이 구조를 받는다 → 교체에 안 깨짐.
pydantic 미설치 venv → stdlib dataclasses 로 구현(정제 스키마와 동일 방침).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


def seconds_to_timestamp(sec: float) -> str:
    """초 → 'MM:SS' (1시간 이상은 'H:MM:SS'). anchor 표기 통일."""
    sec = max(0, int(round(sec)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def load_cleaned_segments(cleaned_json: Path | str) -> list[dict]:
    """정제본(text-{stem}.cleaned.json) → [{id, start, end, text}] 표준 형태.

    추출기 입력. `cleaned` 본문을 text 로 노출하고 타임스탬프를 보존한다.
    """
    data = json.loads(Path(cleaned_json).read_text(encoding="utf-8"))
    out: list[dict] = []
    for s in data.get("segments", []):
        out.append(
            {
                "id": int(s["id"]),
                "start": float(s["start"]),
                "end": float(s["end"]),
                "text": str(s.get("cleaned", s.get("text", ""))),
            }
        )
    return out


def transcript_with_ids(segments: list[dict]) -> str:
    """추출 프롬프트 주입용 본문: 각 줄 `[id] 본문`. 빈 segment 는 생략(근거 무의미)."""
    lines = [f"[{s['id']}] {s['text']}".rstrip() for s in segments if s.get("text")]
    return "\n".join(lines)


@dataclass
class ActionItem:
    """액션아이템 1개 (표준 출력 양식 = 웹 actionItems 계약).

    owner 는 화자분리 도입 전까지 역할 또는 null(추측 금지). anchor 는 evidence_seg_ids 로
    호출부가 결정적 산출(그라운딩). flag 는 게이트가 채운다('확인필요' | None).
    """

    id: int
    text: str
    owner: str | None = None
    due: str | None = None
    anchor: str | None = None  # evidence 의 최소 start 에서 결정적 산출(MM:SS)
    evidence_seg_ids: list[int] = field(default_factory=list)
    flag: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict, *, id: int | None = None) -> "ActionItem":
        """LLM/JSON 출력 dict → ActionItem. evidence_seg_ids 는 int 로 강제, 누락 보정."""
        ev_raw = data.get("evidence_seg_ids", []) or []
        ev: list[int] = []
        for x in ev_raw:
            try:
                ev.append(int(x))
            except (TypeError, ValueError):
                continue
        return cls(
            id=int(data["id"]) if id is None else id,
            text=str(data.get("text", "")).strip(),
            owner=(data.get("owner") or None),
            due=(data.get("due") or None),
            anchor=(data.get("anchor") or None),
            evidence_seg_ids=ev,
            flag=data.get("flag"),
        )


@dataclass
class ExtractResult:
    """추출 스테이지 전체 결과."""

    items: list[ActionItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"action_items": [it.to_dict() for it in self.items]}

    @classmethod
    def from_dict(cls, data: dict) -> "ExtractResult":
        raw = data.get("action_items", data.get("actionItems", []))
        items = [ActionItem.from_dict(d, id=i) for i, d in enumerate(raw)]
        return cls(items=items)
