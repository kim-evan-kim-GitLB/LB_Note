"""회의 요약 출력 스키마 (계약, 설계 docs/2026-06-09-summarize-stage-design.md §4).

이 구조가 **표준 요약 양식**이며, 웹(meetscript-ai) Meeting.summary 계약이다(string → 구조체).
실제 회의록 양식(samples/EYEL-S3000ABR 데모시연 회의록.pdf)을 목표 포맷으로 한다:
  헤더(meta) → 안건 목록(agenda_index) → 상세 논의(agenda: points + decisions/issues).

설계 원칙(추출 스키마와 동일):
  - anchor / time_range 는 **LLM 출력을 신뢰하지 않고** evidence_seg_ids 로 호출부가 결정적 산출.
  - 모든 요약 항목은 evidence_seg_ids(>=1) 그라운딩 — 근거 없는 항목은 게이트에서 드롭(환각 차단).
  - pydantic 미설치 venv → stdlib dataclasses(정제/추출 스키마와 동일 방침).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from src.postprocess.extract_schema import seconds_to_timestamp


def _coerce_int_list(raw: object) -> list[int]:
    """evidence_seg_ids 등을 int 리스트로 강제(비정상 토큰 스킵)."""
    out: list[int] = []
    for x in raw or []:  # type: ignore[union-attr]
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _coerce_no(raw: object, fallback: int) -> int:
    """안건 번호(no) → int. LLM 이 보낸 값 우선(문자열 "7" 도 흡수), 없거나 비정상이면 위치 fallback.

    no 는 agenda_index ↔ agenda 의 조인 키이므로 양쪽이 같은 값을 갖도록 LLM 값을 보존한다
    (위치로 덮어쓰면 LLM 이 의도한 안건 번호가 소실됨 — 리뷰 합의).
    """
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


@dataclass
class SummaryItem:
    """요약 항목 1개(논의 불릿 / 결정 / 이슈). anchor 는 호출부가 결정적 산출."""

    text: str
    anchor: str | None = None
    evidence_seg_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SummaryItem":
        return cls(
            text=str(data.get("text", "")).strip(),
            anchor=None,  # LLM anchor 무시 — ground_summary 가 채운다.
            evidence_seg_ids=_coerce_int_list(data.get("evidence_seg_ids")),
        )


@dataclass
class AgendaBlock:
    """상세 논의 안건 블록 1개. time_range 는 호출부가 evidence min/max 로 산출."""

    no: int
    title: str
    time_range: str | None = None
    evidence_seg_ids: list[int] = field(default_factory=list)
    points: list[SummaryItem] = field(default_factory=list)
    decisions: list[SummaryItem] = field(default_factory=list)
    issues: list[SummaryItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "no": self.no,
            "title": self.title,
            "time_range": self.time_range,
            "evidence_seg_ids": self.evidence_seg_ids,
            "points": [it.to_dict() for it in self.points],
            "decisions": [it.to_dict() for it in self.decisions],
            "issues": [it.to_dict() for it in self.issues],
        }

    @classmethod
    def from_dict(cls, data: dict, *, no_fallback: int = 0) -> "AgendaBlock":
        def items(key: str) -> list[SummaryItem]:
            return [SummaryItem.from_dict(d) for d in (data.get(key) or [])]

        return cls(
            no=_coerce_no(data.get("no"), no_fallback),
            title=str(data.get("title", "")).strip(),
            time_range=None,  # LLM 값 무시 — ground_summary 가 채운다.
            evidence_seg_ids=[],  # ground_summary 가 항목 evidence 합집합으로 채운다.
            points=items("points"),
            decisions=items("decisions"),
            issues=items("issues"),
        )


@dataclass
class AgendaIndexEntry:
    """안건 목록(인덱스 테이블) 한 줄."""

    no: int
    title: str
    summary: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict, *, no_fallback: int = 0) -> "AgendaIndexEntry":
        return cls(
            no=_coerce_no(data.get("no"), no_fallback),
            title=str(data.get("title", "")).strip(),
            summary=str(data.get("summary", "")).strip(),
        )


@dataclass
class MeetingMeta:
    """STT 밖 메타(업로드 시 제공). 없으면 빈 값(graceful). subject 만 LLM 추론 허용."""

    datetime: str = ""
    department: str = ""
    attendees: list[str] = field(default_factory=list)
    subject: str = ""
    author: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MeetingMeta":
        data = data or {}
        atts = data.get("attendees") or []
        return cls(
            datetime=str(data.get("datetime", "")).strip(),
            department=str(data.get("department", "")).strip(),
            attendees=[str(a).strip() for a in atts if str(a).strip()],
            subject=str(data.get("subject", "")).strip(),
            author=str(data.get("author", "")).strip(),
        )


@dataclass
class MeetingSummary:
    """요약 스테이지 전체 결과 = 웹 Meeting.summary 계약(구조체)."""

    schema_version: str = "sum-1.0"
    prompt_version: str = "unknown"
    backend: str = ""
    meta: MeetingMeta = field(default_factory=MeetingMeta)
    agenda_index: list[AgendaIndexEntry] = field(default_factory=list)
    agenda: list[AgendaBlock] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "prompt_version": self.prompt_version,
            "backend": self.backend,
            "meta": self.meta.to_dict(),
            "agenda_index": [e.to_dict() for e in self.agenda_index],
            "agenda": [b.to_dict() for b in self.agenda],
        }

    @classmethod
    def empty(cls) -> "MeetingSummary":
        """요약 off(passthrough/실패) 시 빈 구조체 — summary 타입 일관(설계 §4·§6)."""
        return cls()

    @classmethod
    def from_dict(cls, data: dict) -> "MeetingSummary":
        data = data or {}
        idx_raw = data.get("agenda_index") or []
        ag_raw = data.get("agenda") or []
        return cls(
            meta=MeetingMeta.from_dict(data.get("meta") or {}),
            agenda_index=[
                AgendaIndexEntry.from_dict(d if isinstance(d, dict) else {}, no_fallback=i + 1)
                for i, d in enumerate(idx_raw)
            ],
            agenda=[
                AgendaBlock.from_dict(d if isinstance(d, dict) else {}, no_fallback=i + 1)
                for i, d in enumerate(ag_raw)
            ],
        )


def _time_range(starts: list[float], ends: list[float]) -> str | None:
    """evidence start/end → 'MM:SS ~ MM:SS'. 근거 없으면 None.

    end<start(STT 노이즈 구간) 방어: hi 는 lo 이상으로 정규화(역순 구간 방지).
    """
    if not starts:
        return None
    lo_sec = min(starts)
    hi_sec = max(ends) if ends else lo_sec
    if hi_sec < lo_sec:
        hi_sec = lo_sec
    return f"{seconds_to_timestamp(lo_sec)} ~ {seconds_to_timestamp(hi_sec)}"


def ground_summary(
    summary: MeetingSummary, segments: list[dict]
) -> MeetingSummary:
    """요약 게이트 [D'] + 결정적 산출(설계 §7).

    - evidence_seg_ids 를 실제 입력 segment id 로 필터(환각 인용 제거).
    - 유효 근거가 0인 요약 항목은 드롭(그라운딩 필수 = 환각 차단).
    - 각 항목 anchor = min(evidence start), 각 안건 time_range = min(start)~max(end).
    - 빈 안건 블록(세 섹션 모두 비고 근거 없음)은 드롭.

    LLM 이 준 anchor/time_range/evidence_seg_ids(블록)는 사용하지 않고 여기서 재산출한다.
    """
    start_by_id = {int(s["id"]): float(s["start"]) for s in segments}
    end_by_id = {int(s["id"]): float(s.get("end", s["start"])) for s in segments}

    def ground_items(items: list[SummaryItem]) -> list[SummaryItem]:
        out: list[SummaryItem] = []
        for it in items:
            # 멤버십 필터 + 중복 제거(순서 보존): 환각 인용·중복 인용 정리.
            valid = list(dict.fromkeys(s for s in it.evidence_seg_ids if s in start_by_id))
            if not valid:
                continue  # 근거 없는 항목 드롭(그라운딩 필수)
            it.evidence_seg_ids = valid
            it.anchor = seconds_to_timestamp(min(start_by_id[s] for s in valid))
            out.append(it)
        return out

    grounded_blocks: list[AgendaBlock] = []
    for blk in summary.agenda:
        blk.points = ground_items(blk.points)
        blk.decisions = ground_items(blk.decisions)
        blk.issues = ground_items(blk.issues)
        all_ev = sorted(
            {sid for grp in (blk.points, blk.decisions, blk.issues) for it in grp for sid in it.evidence_seg_ids}
        )
        if not all_ev:
            continue  # 근거 0 블록 드롭
        blk.evidence_seg_ids = all_ev
        blk.time_range = _time_range(
            [start_by_id[s] for s in all_ev], [end_by_id[s] for s in all_ev]
        )
        grounded_blocks.append(blk)

    summary.agenda = grounded_blocks
    # agenda_index 동기화: 살아남은 안건(no)만 유지(드롭된 블록의 인덱스 줄도 제거).
    # no 는 index↔agenda 조인 키(_coerce_no 가 LLM 값 보존).
    surviving_nos = {b.no for b in grounded_blocks}
    summary.agenda_index = [e for e in summary.agenda_index if e.no in surviving_nos]
    return summary
