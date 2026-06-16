"""숫자↔한글(한자음) 오인식 교정 화이트리스트 — 단일 진실원(SSOT).

STT가 한자음 형태소를 **동음 숫자**로 잘못 표기한 케이스만 좁게 교정한다. 예: 誤探(오탐,
false positive)의 '오'를 숫자 5로 받아써 "5탐"이 된다. 일반 수량·차수·날짜·시간(1차/5분/
6월/2단계…)은 **절대** 건드리지 않는다 — 정확히 등재된 패턴만 대상.

근거: output 산출물 빈도 분석(2026-06-09). "숫자+한글" 인접의 95%+는 진짜 숫자였고, 정상
단위/조사를 걸러낸 뒤 교정이 정당한 건 5탐→오탐(12건, FP 맥락 확정)뿐이었다. 그래서 일반
규칙이 아니라 화이트리스트로 좁힌다. 새 케이스는 빈도 근거를 확인한 뒤 여기에 추가한다.

이 맵을 프롬프트(prompts/clean.ko.md §숫자 오인식 교정)와 게이트(validate.number_tokens_preserved)가
공유한다 → 프롬프트가 시킨 교정을 게이트가 '숫자 누락'으로 오판해 되돌리지 않게 한다.
"""
from __future__ import annotations

import re

# {오인식 표기: 정답 표기}. 좁은 화이트리스트(부분 문자열 교정). 등재된 것만 교정한다.
HOMOPHONE_MAP: dict[str, str] = {
    "5탐": "오탐",  # 오탐(誤探, false positive)의 '오'를 숫자 5로 오인식. 예: "5탐을 줄여야"
}

# 한자음 동음 숫자 읽기(참고·확장용). 일반 교정엔 쓰지 않는다(화이트리스트만 적용).
SINO_DIGIT_READING: dict[str, str] = {
    "0": "영", "1": "일", "2": "이", "3": "삼", "4": "사",
    "5": "오", "6": "육", "7": "칠", "8": "팔", "9": "구",
}

_DIGIT_RE = re.compile(r"\d")


def apply_homophone(text: str) -> str:
    """등재된 동음 오인식 표기를 정답 표기로 치환(결정적). 미등재 숫자는 불변.

    부분 문자열 치환이라 "5탐을/5탐이니까/5탐인데" 등 조사 변형도 함께 잡힌다.
    안전장치: 앞에 다른 숫자나 '제'가 붙은 경우(예: 제5탐사대)는 차수일 수 있어 제외.
    """
    for mis, cor in HOMOPHONE_MAP.items():
        # (?<![\d제]) : 바로 앞이 숫자/'제'가 아니어야 함(15탐·제5탐 차수 오교정 방지)
        text = re.sub(rf"(?<![\d제]){re.escape(mis)}", cor, text)
    return text


def excused_digits(original: str, cleaned: str) -> set[str]:
    """화이트리스트 동음 교정으로 '설명되는' 숫자 토큰 집합.

    원문에 오인식 표기(mis)가 있고 정제문에 정답 표기(cor)가 있으면, mis 안의 숫자는
    정당하게 한글로 바뀐 것이므로 '숫자 누락'으로 보지 않는다(게이트 면제용).
    """
    excused: set[str] = set()
    for mis, cor in HOMOPHONE_MAP.items():
        if mis in original and cor in cleaned:
            excused.update(_DIGIT_RE.findall(mis))
    return excused
