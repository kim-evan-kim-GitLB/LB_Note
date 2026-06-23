"""[D] 검증·리페어·그라운딩 게이트 (설계 §6·§8).

LLM 출력은 통과 전 모두 검사. 실패 시 단계적 대응: 재시도 → 완화 → 원문 유지 + flag.
에러로 멈추지 않는다(STT 소스 폴백 원칙과 동일).

deterministic 부분(편집비율·내용보존)은 여기서 REAL 구현.
편집거리는 src.scoring.levenshtein 재사용.

설계 §8 지표 정의와 코드 일치(F2):
- 용어 보존 = glossary 정답 용어가 original 에 있고 cleaned 에도 남아있는 비율
  (동어반복 아님 — original 에 실제 등장한 정답 용어만 분모로 본다).
- 숫자 보존 = original 숫자 토큰이 cleaned 에 보존된 비율.
두 지표 모두 비율(float)로 노출한다(불리언 아님).
"""
from __future__ import annotations

import re
from typing import Callable

from src.postprocess.homophone import excused_digits
from src.postprocess.schema import CleanedSegment
from src.scoring import levenshtein

FLAG_REVIEW = "확인필요"      # 그라운딩 실패(근거 0 = 환각 의심) — 게이트가 채움
AMBIGUOUS_FLAG = "약함확인"   # 모호 발화 회색지대 캡처(확정·합의 강도 약함) — LLM이 채움
INFERRED_FLAG = "추정"        # owner 를 본문 앵커로 추론(명시 아님) — LLM이 채움

# flag 3종: 사유 코드가 다르며 모두 사람 검토 큐로 보낸다. n_flagged 는 사유별 분리 집계.
KNOWN_FLAGS = frozenset({FLAG_REVIEW, AMBIGUOUS_FLAG, INFERRED_FLAG})

# 내용보존 검사용 토큰 추출: 한글/영문/숫자 덩어리(구두점·간투사 영향 최소화)
_TOKEN_RE = re.compile(r"[0-9]+|[A-Za-z]+|[가-힣]+")

# 의미보존 검사 hook 타입(설계 §6-4). (original, cleaned)->통과여부.
# 기본 None → 사람 diff 검토를 backstop 으로 삼는다(무의존성 기본).
SemanticCheck = Callable[[str, str], bool]


def edit_ratio(original: str, cleaned: str) -> float:
    """문자 단위 편집거리 / 원문 길이. 0=무편집, 클수록 많이 바뀜.

    원문이 비면: cleaned 도 비면 0.0, 아니면 1.0.
    """
    orig_c = list(original.strip())
    clean_c = list(cleaned.strip())
    if not orig_c:
        return 0.0 if not clean_c else 1.0
    return levenshtein(orig_c, clean_c) / len(orig_c)


def within_edit_band(
    original: str, cleaned: str, lo: float = 0.0, hi: float = 0.6
) -> bool:
    """편집비율이 [lo, hi] 밴드 안인가 (설계 §6-2, 거친 1차 가드).

    밴드는 길이/문자 변화량 기반의 **거친 가드일 뿐**이다 — 유창한 의미 변조
    (할루시네이션)는 이 밴드로 잡지 못한다(설계 §6-4). 무편집/과편집 판정은
    validate_segment 의 require_edit·semantic_check 와 합산해서 쓴다.
    """
    return lo <= edit_ratio(original, cleaned) <= hi


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def glossary_terms_preserved(
    original: str, cleaned: str, terms: list[str] | None = None
) -> float:
    """glossary 정답 용어 보존 비율(설계 §8 "용어 보존", F2).

    분모 = terms 중 original 에 실제 등장하는 용어 수(동어반복 회피 — terms 가
    무조건 cleaned 에 있어야 한다는 식이 아니라, 원문에 있던 것이 살아남았는지를 본다).
    분자 = 그 중 cleaned 에도 남아있는 용어 수.

    Returns:
        보존 비율(0.0~1.0). 검사 대상(original 에 등장한 정답 용어)이 없으면 1.0.
    """
    present = [t for t in (terms or []) if t and t in original]
    if not present:
        return 1.0
    kept = sum(1 for t in present if t in cleaned)
    return kept / len(present)


def number_tokens_preserved(original: str, cleaned: str) -> float:
    """숫자 토큰 보존 비율(설계 §8 "숫자 보존", F2).

    분모 = original 의 고유 숫자 토큰 수. 분자 = cleaned 에도 있는 수.
    original 에 숫자가 없으면 1.0.

    예외(동음 오인식 교정): 화이트리스트(homophone.HOMOPHONE_MAP)로 설명되는 숫자
    (예: 5탐→오탐 의 '5')는 정당하게 한글로 바뀐 것이므로 분모에서 제외한다 → 등재된
    숫자 오인식 교정을 '숫자 누락'으로 오판해 게이트가 되돌리지 않게 한다. 미등재 숫자는
    종전과 동일하게 보존을 강제한다(1차/5분/6월 등 진짜 숫자 보호).
    """
    excused = excused_digits(original, cleaned)
    orig_nums = {t for t in _tokens(original) if t.isdigit() and t not in excused}
    if not orig_nums:
        return 1.0
    cleaned_nums = {t for t in _tokens(cleaned) if t.isdigit()}
    kept = len(orig_nums & cleaned_nums)
    return kept / len(orig_nums)


def content_preserved(
    original: str, cleaned: str, must_keep_terms: list[str] | None = None
) -> bool:
    """내용보존 게이트(불리언, 설계 §6-3). 위 두 비율 지표를 합쳐 판정.

    glossary 용어 보존 100% 그리고 숫자 보존 100% 일 때만 통과.
    (지표 자체는 glossary_terms_preserved/number_tokens_preserved 가 비율로 노출.)
    """
    if glossary_terms_preserved(original, cleaned, must_keep_terms) < 1.0:
        return False
    if number_tokens_preserved(original, cleaned) < 1.0:
        return False
    return True


def validate_segment(
    seg: CleanedSegment,
    *,
    must_keep_terms: list[str] | None = None,
    lo: float = 0.0,
    hi: float = 0.6,
    require_edit: bool = True,
    semantic_check: SemanticCheck | None = None,
) -> tuple[bool, list[str]]:
    """단일 segment 게이트. (통과여부, 실패사유 목록) 반환.

    require_edit (설계 §6-2, lo 모순 해소·F5):
      - True(실제 모델 경로 기본): 무편집(edit_ratio==0)은 '정제 실패'로 잡는다.
      - False(passthrough 경로): 무편집을 정상으로 허용. **백엔드 정체성 검사로
        분기하지 않고** 이 명시적 파라미터로만 분기한다.
    semantic_check (설계 §6-4·F5):
      - 의미보존 hook. None 이면 자동 의미검사 없이 사람 diff 검토를 backstop 으로 둔다.
        edit_ratio 는 거친 가드라 유창한 할루시네이션을 못 잡으므로, 이 hook 으로
        임베딩/NLI 등 의미 동치 검사를 주입할 수 있다.
    """
    reasons: list[str] = []
    ratio = edit_ratio(seg.original, seg.cleaned)

    if not seg.cleaned.strip():
        reasons.append("empty_cleaned")
    if ratio > hi:
        reasons.append(f"over_edit({ratio:.2f}>{hi})")
    if ratio < lo:
        reasons.append(f"under_edit({ratio:.2f}<{lo})")
    if require_edit and ratio == 0.0:
        reasons.append("no_edit")
    if not content_preserved(seg.original, seg.cleaned, must_keep_terms):
        reasons.append("content_dropped")
    if semantic_check is not None and not semantic_check(seg.original, seg.cleaned):
        reasons.append("semantic_drift")

    return (not reasons), reasons


def repair_or_degrade(
    seg: CleanedSegment,
    *,
    retry: Callable[[CleanedSegment, list[str]], CleanedSegment] | None = None,
    max_retries: int = 0,
    must_keep_terms: list[str] | None = None,
    lo: float = 0.0,
    hi: float = 0.6,
    require_edit: bool = True,
    semantic_check: SemanticCheck | None = None,
) -> CleanedSegment:
    """리페어 루프 스캐폴드 (설계 §6): 검증 → 재시도 훅 → 완화 → 원문 유지 + flag.

    retry 훅이 주어지면 max_retries 회까지 재요청(스텁 단계에선 보통 None).
    최종 실패 시 graceful degrade: cleaned=original, flag='확인필요'. 멈추지 않는다.
    통과/실패 무관하게 edit_ratio 를 segment 에 스탬프한다(설계 §5 스키마).
    """
    ok, reasons = validate_segment(
        seg,
        must_keep_terms=must_keep_terms,
        lo=lo,
        hi=hi,
        require_edit=require_edit,
        semantic_check=semantic_check,
    )
    attempts = 0
    while not ok and retry is not None and attempts < max_retries:
        seg = retry(seg, reasons)
        ok, reasons = validate_segment(
            seg,
            must_keep_terms=must_keep_terms,
            lo=lo,
            hi=hi,
            require_edit=require_edit,
            semantic_check=semantic_check,
        )
        attempts += 1

    if not ok:
        # graceful degrade: 원문 유지 + 확인필요 flag(자동등록 없음, 사람 검토로 회수)
        return CleanedSegment(
            id=seg.id,
            start=seg.start,
            end=seg.end,
            original=seg.original,
            cleaned=seg.original,
            edits=[],
            edit_ratio=0.0,  # degrade 시 cleaned==original → 편집비율 0
            flag=FLAG_REVIEW,
        )
    seg.edit_ratio = round(edit_ratio(seg.original, seg.cleaned), 4)
    return seg
