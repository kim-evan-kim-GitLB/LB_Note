"""[L] 세그먼트 언어 게이트 — 영어 환각 전사 방어 (설계 docs/2026-07-30-영어환각-언어게이트-설계.md).

한국어 회의 STT 에서 무음·저SNR 구간이 **유창한 영어 문장으로 환각**되는 실패 모드를
결정적(non-LLM) 규칙으로 걸러낸다. 언어는 이미 `ko` 로 강제돼 있으므로(src/backends/cohere.py)
이 모듈이 막는 것은 "언어 힌트 누락"이 아니라 "없는 발화를 지어낸 출력"이다.

임계값 캘리브레이션(설계 §1-2, 실측 457 세그먼트):
  - 환각 세그먼트: 한글비율 0.00~0.01, cps(문자/초) 14.0~14.9
  - 정상 세그먼트: 한글비율 최저 0.59(영어 기술용어 다수 발화), cps 중앙값 6.7~7.7 / p90 8.6~9.4
양쪽에 마진이 크므로 산술 규칙으로 충분하다. 임계값은 config 로 노출해 조정 가능하게 두고,
`LANG_GATE_ENABLED=0` 으로 전체를 끌 수 있다(안전 스위치).

판정 3종:
  - `exclude`  : 프롬프트에 **주입하지 않는다**. 요약/추출 입력에서 아예 빠지므로 그 id 를
                 인용하는 항목은 기존 그라운딩 필터가 자동으로 떨군다.
  - `low_conf` : 주입하되 `[id]~` 로 표시. 프롬프트가 "단독 근거 금지"를 강제하고, 호출부의
                 결정적 게이트가 저신뢰 단독근거 항목을 드롭/플래그한다.
  - `ok`       : 통과.

과잉 차단 금지가 핵심이다 — 영어 기술용어가 섞인 정상 한국어 발화(한글비율 0.59~0.66)를
버리면 회의 내용이 유실된다. 그래서 `exclude` 는 "한글이 사실상 0"인 경우로만 좁혀 둔다.
"""
from __future__ import annotations

import re

from src import config

# 판정 결과 상수
OK = "ok"
LOW_CONF = "low_conf"
EXCLUDE = "exclude"

# 사유 코드(관측성·UI 배지용)
REASON_NON_KOREAN = "non_korean"            # 한글이 사실상 없음(환각/외국어)
REASON_SPEECH_RATE = "speech_rate_burst"    # 세그먼트 길이 대비 문자 폭주
REASON_BOILERPLATE = "boilerplate"          # 자막·영상 상용구(학습데이터 유래)
REASON_MOSTLY_NON_KO = "mostly_non_korean"  # 비한국어 우세(회색지대)

# 사용자에게 보여줄 한글 사유. 코드 옆에 두는 이유 — 사유를 추가하고 문구를 빠뜨리면
# 화면에 영문 코드가 그대로 뜬다. 프론트는 이 문구를 **그대로 표시**한다(재가공 금지 규약).
REASON_LABELS = {
    REASON_NON_KOREAN: "한국어가 아닌 문장",
    REASON_MOSTLY_NON_KO: "한국어보다 외국어가 많음",
    REASON_SPEECH_RATE: "말 속도에 비해 글자가 지나치게 많음",
    REASON_BOILERPLATE: "자막 상용구(회의 발화 아님)",
}


def reason_label(code: str) -> str:
    """사유 코드 → 한글 문구. 모르는 코드는 코드를 그대로 보여준다(조용히 감추지 않는다)."""
    return REASON_LABELS.get(code, code)

_HANGUL_RE = re.compile(r"[가-힣]")
_LATIN_RE = re.compile(r"[A-Za-z]")

# 자막·영상 상용구: 회의 발화가 아니라 학습 데이터에서 새어나온 문구.
# 한국어 회의록에 등장할 이유가 없으므로 문구가 보이면 그 세그먼트를 버린다.
_BOILERPLATE_RE = re.compile(
    r"(thank you for watching|thanks for watching|please subscribe|"
    r"subtitles? (?:by|provided by)|amara\.org|캡션 제공|자막 제공)",
    re.IGNORECASE,
)


def segment_stats(text: str, start: float = 0.0, end: float = 0.0) -> dict:
    """세그먼트 계측(결정적). 판정 근거를 그대로 노출해 관측·회귀에 쓴다.

    hangul_ratio = 한글 / (한글 + 라틴문자). 한글·라틴이 모두 없으면(숫자·기호만) 1.0 으로
    둬서 게이트가 개입하지 않게 한다(짧은 "네.", "3시" 등을 환각으로 오판하지 않기 위함).
    chars_per_sec = 전체 문자수 / 세그먼트 길이. 길이가 0 이하면 0.0(0 division 방어).
    """
    t = text or ""
    hangul = len(_HANGUL_RE.findall(t))
    latin = len(_LATIN_RE.findall(t))
    denom = hangul + latin
    dur = float(end) - float(start)
    return {
        "hangul": hangul,
        "latin": latin,
        "hangul_ratio": (hangul / denom) if denom else 1.0,
        "chars": len(t.strip()),
        "chars_per_sec": (len(t.strip()) / dur) if dur > 0 else 0.0,
    }


def is_korean_output(text: str) -> bool:
    """**산출물**(요약·액션 텍스트)이 한국어인가 — 출력 언어 보장용 결정적 검사.

    입력 세그먼트 판정(classify)과 목적이 다르다: 이건 우리가 만든 결과물이 한국어 회의록인지를
    본다. 프롬프트에 "한국어로 써라"를 넣어두었지만 지시 준수에 기대지 않고 코드가 재검사한다.

    임계값 캘리브레이션(실측 958개 산출 텍스트, 7런):
      정상 한국어 항목의 한글비 **최저 0.308**("Gemini API 비용 이슈") / 중앙값 1.000.
      영어 산출은 0.00 이므로 기본 임계 0.20 이면 오탐 0에 여유가 남는다.
    한글·라틴 문자가 전혀 없으면(숫자·기호만) True — 개입 대상이 아니다.
    """
    t = (text or "").strip()
    if not t:
        return True
    st = segment_stats(t)
    if st["hangul"] + st["latin"] == 0:
        return True
    return st["hangul_ratio"] >= config.LANG_OUT_MIN_RATIO


def classify(text: str, start: float = 0.0, end: float = 0.0) -> tuple[str, str | None]:
    """세그먼트 1개 판정 → (OK | LOW_CONF | EXCLUDE, 사유코드 | None).

    게이트가 꺼져 있으면(`LANG_GATE_ENABLED=0`) 항상 OK. 빈 텍스트도 OK 로 두고
    기존 경로(빈 줄 skip)에 맡긴다 — 판정 책임을 겹치지 않게 한다.
    """
    if not config.LANG_GATE_ENABLED:
        return OK, None
    t = (text or "").strip()
    if not t:
        return OK, None
    if _BOILERPLATE_RE.search(t):
        return EXCLUDE, REASON_BOILERPLATE

    st = segment_stats(t, start, end)
    ratio, hangul, cps = st["hangul_ratio"], st["hangul"], st["chars_per_sec"]

    # 한글이 사실상 없다 → 한국어 회의 내용으로 쓸 수 없다(환각 또는 순수 외국어 발화).
    # 단 **짧은 조각은 제외하지 않는다**: 실측 환각은 365~427자로 길었고, 짧은 라틴 조각은
    # 오히려 진짜 발화("OK", "LGTM 네")일 확률이 높다. 신호가 부족한 구간을 버리면 회의 내용이
    # 유실되므로 표시(low_conf)만 하고 판단은 critic·사람에게 넘긴다.
    if ratio < config.LANG_GATE_EXCLUDE_RATIO and hangul < config.LANG_GATE_MIN_HANGUL:
        if st["chars"] >= config.LANG_GATE_MIN_CHARS:
            return EXCLUDE, REASON_NON_KOREAN
        return LOW_CONF, REASON_MOSTLY_NON_KO
    # 문자 폭주 + 비한국어 우세 → 무음 구간에 긴 영어를 쏟는 환각 특성.
    # 한글비율 조건을 AND 로 걸어 '빠른 한국어 발화'를 오탐하지 않는다.
    if cps > config.LANG_GATE_MAX_CPS and ratio < config.LANG_GATE_CPS_RATIO:
        return EXCLUDE, REASON_SPEECH_RATE
    # 회색지대: 버리지 않고 저신뢰로 표시(정상 발화의 실측 최저는 0.59).
    if ratio < config.LANG_GATE_LOW_CONF_RATIO:
        return LOW_CONF, REASON_MOSTLY_NON_KO
    return OK, None


def partition(segments: list[dict]) -> tuple[list[dict], dict[int, str], dict[int, str]]:
    """세그먼트 목록 → (프롬프트 주입용, 저신뢰 사유맵, 제외 사유맵).

    Returns:
        kept: `exclude` 를 제외한 segment 목록(원본 dict 그대로 — 비파괴).
        low_conf: {seg_id: 사유코드} — kept 안에 있으나 저신뢰. 프롬프트 표시 + 결정적 게이트 대상.
        excluded: {seg_id: 사유코드} — 주입 자체를 막은 것. 관측·UI 배지용.
    """
    kept: list[dict] = []
    low_conf: dict[int, str] = {}
    excluded: dict[int, str] = {}
    for s in segments:
        try:
            sid = int(s.get("id"))
        except (TypeError, ValueError):
            kept.append(s)  # id 를 못 읽으면 판정 대상에서 제외(기존 동작 보존)
            continue
        verdict, reason = classify(
            str(s.get("text") or ""), float(s.get("start") or 0.0), float(s.get("end") or 0.0)
        )
        if verdict == EXCLUDE:
            excluded[sid] = reason or REASON_NON_KOREAN
            continue
        if verdict == LOW_CONF:
            low_conf[sid] = reason or REASON_MOSTLY_NON_KO
        kept.append(s)
    return kept, low_conf, excluded
