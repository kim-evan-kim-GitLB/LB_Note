"""정제 출력 스키마 (계약, 설계 §5).

LLM이 무엇이든 다운스트림은 항상 이 구조를 받는다 → 모델 교체에 안 깨짐.
pydantic 미설치 venv → stdlib dataclasses 로 구현.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


def normalize_segments(segments: list[dict]) -> list[dict]:
    """입력 계약 정규화 (설계 §5 입력계약). 프로듀서 둘의 필드명을 흡수한다.

    - 메인 파이프라인(src/pipeline.py): segment = {start, end, text}
    - 실험 도구(tools/vad_chunk_*.py): segment = {start_sec, end_sec, start_ts, text}
    → 내부 표준 {id, start, end, text} 로 변환.

    무음 폴백 금지(설계 §5): start/start_sec, end/end_sec 가 둘 다 없으면 0.0 으로
    조용히 채우지 않고 ValueError 를 던진다(타임스탬프 침묵 손실 방지).
    """
    out: list[dict] = []
    for i, seg in enumerate(segments):
        if "start" in seg and seg["start"] is not None:
            start = seg["start"]
        elif "start_sec" in seg and seg["start_sec"] is not None:
            start = seg["start_sec"]
        else:
            raise ValueError(
                f"segment[{i}] 에 start/start_sec 가 없음 — 타임스탬프 무음 폴백 금지(설계 §5)."
            )
        if "end" in seg and seg["end"] is not None:
            end = seg["end"]
        elif "end_sec" in seg and seg["end_sec"] is not None:
            end = seg["end_sec"]
        else:
            raise ValueError(
                f"segment[{i}] 에 end/end_sec 가 없음 — 타임스탬프 무음 폴백 금지(설계 §5)."
            )
        out.append(
            {
                "id": i,
                "start": float(start),
                "end": float(end),
                "text": str(seg.get("text", "")),
            }
        )
    return out


@dataclass
class CleanedSegment:
    """정제된 segment 1개. 입력 segment 와 1:1, 타임스탬프 정렬 보존."""

    id: int
    start: float
    end: float
    original: str  # 원문(= glossary 교정 후, LLM 입력 텍스트)
    cleaned: str  # 정제문(LLM 출력 → 게이트 통과본). 실패 시 original 유지.
    edits: list[str] = field(default_factory=list)  # 분류 태그(filler_removed 등)
    edit_ratio: float = 0.0  # original→cleaned 편집비율(설계 §5 스키마). 사람 diff 검토 거친 가드.
    flag: str | None = None  # "확인필요" | None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CleanedSegment":
        """LLM/JSON 출력 dict → CleanedSegment. 누락 필드는 기본값으로 보정."""
        return cls(
            id=int(data["id"]),
            start=float(data["start"]),
            end=float(data["end"]),
            original=str(data.get("original", "")),
            cleaned=str(data.get("cleaned", "")),
            edits=list(data.get("edits", []) or []),
            edit_ratio=float(data.get("edit_ratio", 0.0)),
            flag=data.get("flag"),
        )


@dataclass
class CleanResult:
    """정제 스테이지 전체 결과. segments[].text 와 동일 개수·정렬."""

    segments: list[CleanedSegment] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"segments": [s.to_dict() for s in self.segments]}

    @classmethod
    def from_dict(cls, data: dict) -> "CleanResult":
        return cls(segments=[CleanedSegment.from_dict(s) for s in data.get("segments", [])])

    @property
    def transcript(self) -> str:
        """정제본을 이어 붙인 transcript(빈 segment 제외)."""
        return " ".join(s.cleaned for s in self.segments if s.cleaned).strip()
