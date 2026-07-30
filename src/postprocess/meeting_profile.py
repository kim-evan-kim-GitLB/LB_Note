"""[R] 회의 프로파일링 + 라우팅 — 다중 agent core 의 결정적 앞단.

설계: docs/2026-07-30-영어환각-언어게이트-설계.md §2 및 다중 agent core 결정(2026-07-30).

**케이스 판정을 LLM 에 맡기지 않는다.** 회의 길이·세그먼트 수·언어 게이트 신호·중복률처럼
싸게 셀 수 있는 것만으로 case 를 정하면 재현성이 있고 LLM 콜이 늘지 않는다. LLM 은
"이 회의를 어떻게 요약할까"를 결정하지 않고, 라우터가 정한 좁은 일만 수행한다.

대응 case 4종(사용자 확정):
  - hallucination_risk : 영어 환각·무음 구간 -> 게이트가 제외/표시한 것이 있으면 critic 을 엄격히.
  - long_form          : 1시간+ 회의 -> 구간 분할(map) + 병합(reduce). 단일 콜의 맥락·토큰 한계 회피.
  - multi_topic        : 다주제·다부서 -> 안건 분리 강화 + owner 추론 억제(오귀속 비용이 큼).
  - low_quality        : 전사 품질 저하 -> 보수 모드(근거 약한 항목을 만들지 말고 정직하게 비운다).

multi_topic 은 결정적으로 완전 판별할 수 없어 **세그먼트 수를 프록시**로 쓴다(길고 세그먼트가
많은 회의일수록 주제가 갈린다). 실제 안건 분리는 reduce 단계가 병합하며 정리한다 — 프록시가
틀려도 산출이 깨지지 않고 프롬프트 강도만 달라지는 설계다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src import config
from src.postprocess.language_gate import segment_stats

CASE_HALLUCINATION = "hallucination_risk"
CASE_LONG_FORM = "long_form"
CASE_MULTI_TOPIC = "multi_topic"
CASE_LOW_QUALITY = "low_quality"


@dataclass
class MeetingProfile:
    """회의 1건의 결정적 계측 + 판정된 case 목록."""

    n_segments: int = 0
    duration_sec: float = 0.0
    hangul_ratio: float = 1.0        # 전체 본문 기준(세그먼트 평균이 아니라 문자 합계 기준)
    low_conf_ratio: float = 0.0      # kept 중 저신뢰 비율
    excluded_count: int = 0          # 언어 게이트가 주입 자체를 막은 세그먼트 수
    duplicate_ratio: float = 0.0     # 동일 본문 반복 비율(반복 환각 프록시)
    cases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "nSegments": self.n_segments,
            "durationSec": round(self.duration_sec, 1),
            "hangulRatio": round(self.hangul_ratio, 4),
            "lowConfRatio": round(self.low_conf_ratio, 4),
            "excludedCount": self.excluded_count,
            "duplicateRatio": round(self.duplicate_ratio, 4),
            "cases": list(self.cases),
        }


@dataclass
class Plan:
    """라우팅 결과. 실행부(orchestrator)는 이 값만 보고 움직인다(분기 로직 집중)."""

    windows: list[list[dict]] = field(default_factory=list)  # 1개면 단일 패스, 2+면 map->reduce
    conservative: bool = False   # 저품질: 근거 약한 항목 생성 금지(정직하게 비운다)
    strict_owner: bool = False   # 다주제: owner 본문앵커 추론 금지(명시만 인정)
    critic: bool = True          # 검증 1패스 수행
    strict_critic: bool = False  # 환각 위험: critic 을 엄격 모드로
    cases: list[str] = field(default_factory=list)

    @property
    def is_map_reduce(self) -> bool:
        return len(self.windows) > 1

    def to_dict(self) -> dict:
        return {
            "windows": len(self.windows),
            "windowSizes": [len(w) for w in self.windows],
            "conservative": self.conservative,
            "strictOwner": self.strict_owner,
            "critic": self.critic,
            "strictCritic": self.strict_critic,
            "cases": list(self.cases),
        }


def profile_meeting(
    kept: list[dict],
    low_conf: dict[int, str] | None = None,
    excluded: dict[int, str] | None = None,
) -> MeetingProfile:
    """언어 게이트 통과분(kept) + 게이트 판정맵 → MeetingProfile(결정적).

    duration 은 세그먼트 start/end 의 스팬으로 잰다(오디오 길이를 따로 받지 않아도 되게).
    hangul_ratio 는 세그먼트 평균이 아니라 **문자 합계 기준** — 짧은 세그먼트가 평균을 흔드는
    왜곡을 피한다.
    """
    low_conf = low_conf or {}
    excluded = excluded or {}
    texts = [str(s.get("text") or "") for s in kept]
    hangul = latin = 0
    for t in texts:
        st = segment_stats(t)
        hangul += st["hangul"]
        latin += st["latin"]
    starts = [float(s.get("start") or 0.0) for s in kept]
    ends = [float(s.get("end") or s.get("start") or 0.0) for s in kept]
    duration = (max(ends) - min(starts)) if kept else 0.0
    norm = [t.strip() for t in texts if t.strip()]
    dup = (len(norm) - len(set(norm))) / len(norm) if norm else 0.0

    prof = MeetingProfile(
        n_segments=len(kept),
        duration_sec=max(0.0, duration),
        hangul_ratio=(hangul / (hangul + latin)) if (hangul + latin) else 1.0,
        low_conf_ratio=(len(low_conf) / len(kept)) if kept else 0.0,
        excluded_count=len(excluded),
        duplicate_ratio=dup,
    )

    cases: list[str] = []
    if prof.excluded_count > 0 or prof.low_conf_ratio > 0:
        cases.append(CASE_HALLUCINATION)
    if (
        prof.duration_sec > config.CORE_LONG_FORM_SEC
        or prof.n_segments > config.CORE_WINDOW_SEGMENTS
    ):
        cases.append(CASE_LONG_FORM)
    if prof.n_segments >= config.CORE_MULTI_TOPIC_SEGMENTS:
        cases.append(CASE_MULTI_TOPIC)
    if (
        prof.low_conf_ratio >= config.CORE_LOW_QUALITY_RATIO
        or prof.duplicate_ratio >= config.CORE_DUPLICATE_RATIO
        or prof.hangul_ratio < config.LANG_GATE_LOW_CONF_RATIO
    ):
        cases.append(CASE_LOW_QUALITY)
    prof.cases = cases
    return prof


def split_windows(segments: list[dict], size: int, overlap: int) -> list[list[dict]]:
    """세그먼트를 크기 size 창으로 분할(앞뒤 overlap 개 겹침).

    겹침을 두는 이유: 창 경계에 걸친 논의가 양쪽에서 모두 잘려 사라지는 것을 막는다.
    중복 산출은 reduce 단계와 critic 이 정리한다(결정적 dedup 은 텍스트가 달라 위험).
    size<=0 이면 분할하지 않는다.
    """
    if size <= 0 or len(segments) <= size:
        return [segments] if segments else []
    step = max(1, size - max(0, overlap))
    out: list[list[dict]] = []
    i = 0
    while i < len(segments):
        window = segments[i : i + size]
        if window:
            out.append(window)
        if i + size >= len(segments):
            break
        i += step
    return out


def route(profile: MeetingProfile, kept: list[dict]) -> Plan:
    """MeetingProfile → Plan(결정적). case 별 대응을 여기 한곳에 모은다."""
    long_form = CASE_LONG_FORM in profile.cases
    windows = (
        split_windows(kept, config.CORE_WINDOW_SEGMENTS, config.CORE_WINDOW_OVERLAP)
        if long_form
        else ([kept] if kept else [])
    )
    return Plan(
        windows=windows,
        conservative=CASE_LOW_QUALITY in profile.cases,
        strict_owner=CASE_MULTI_TOPIC in profile.cases,
        critic=config.CORE_CRITIC_ENABLED and bool(kept),
        strict_critic=CASE_HALLUCINATION in profile.cases,
        cases=list(profile.cases),
    )
