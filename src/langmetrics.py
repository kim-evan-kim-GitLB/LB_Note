"""텍스트 언어 계측 — 한글 비율(결정적, LLM 없음).

`src/postprocess/language_gate.py` 에도 같은 이름의 지표가 있지만 **의미가 다르다**:
게이트 쪽은 판정 불가(한글·라틴 둘 다 없음)를 `1.0` 으로 둬서 "개입하지 않는다"를 표현한다
(짧은 "네.", "3시" 를 환각으로 오판하지 않기 위함). 그 규약은 게이트 안에서는 옳다.

반면 **품질 판단**에는 그 값이 위험하다 — 빈 문자열이 "완벽한 한국어"로 집계된다.
실제로 진단 중 27.7초 오디오에서 10글자만 나온 실패 청크가 비율 1.00 으로 '정상' 분류됐다.
그래서 여기서는 판정 불가를 `None` 으로 돌려주고, 호출부가 명시적으로 처리하게 한다.
"""
from __future__ import annotations

import re

_HANGUL_RE = re.compile(r"[가-힣]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def hangul_ratio(text: str) -> float | None:
    """한글 / (한글 + 라틴문자). 둘 다 없으면 판정 불가 → None.

    숫자·기호만 있는 텍스트, 빈 문자열이 None 이다. 호출부는 None 을
    "좋다/나쁘다" 어느 쪽으로도 해석하지 말고 판단 대상에서 빼야 한다.
    """
    t = text or ""
    hangul = len(_HANGUL_RE.findall(t))
    latin = len(_LATIN_RE.findall(t))
    denom = hangul + latin
    if denom == 0:
        return None
    return hangul / denom
