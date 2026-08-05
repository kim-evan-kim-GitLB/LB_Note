"""[A] 결정적 용어 교정 (설계 §3).

외래어/고유명사는 정답이 정해진 '치환 문제'이지 추론 문제가 아니다 →
LLM 이전에 사전으로 결정적 치환하여 가장 불안정한 부분을 모델-독립으로 만든다.
100% 재현(같은 입력 → 같은 출력). stdlib json 로 사전 로드(pyyaml 미설치).

매칭 규칙 (2026-08-05 개정 — 사용자 편집 사전 대비, 실측 버그 3종 수정):

- **ASCII 키**(Quan, QWEN): 앞뒤가 ASCII 단어문자가 아닐 때만 매칭.
  종전 `\\b` 방식은 한글 조사 앞에서 조용히 실패했다 — 파이썬 정규식에서 한글도 `\\w` 라
  "Quan은" 의 'n' 과 '은' 사이에 경계가 서지 않는다(실측: "Quan은" 미치환 / "Quan 은" 치환).
  한국어 회의록에서 영문 용어 뒤에 조사가 붙는 건 예외가 아니라 기본형이다.

- **한글 키**: 길이로 규칙을 나눈다. 오염 사고는 전부 **짧은 키**에서 나왔기 때문이다.
  * 3음절 이상(채찌피티, 오퍼스, 유니프로): 종전처럼 substring. 표면형이 충분히 특이해서
    다른 단어에 박힐 확률이 낮다. 여기에 경계를 걸었더니 오히려 정상 교정이 깨졌다
    (실측: "오퍼스별"→"Opus별", "위스퍼라는"→"Whisper라는" 이 막힘 — 실제 전사 342세그먼트
    중 사전 적중 23건에서 4건 손실).
  * 2음절 이하(회의, 소타, 콴): 앞뒤 경계를 요구한다.
    - 앞: 한글이 앞서면 매칭하지 않는다 → "국제회의"가 "국제미팅"이 되지 않는다.
    - 뒤: 한글이 이어지면 **조사·어미의 첫 음절일 때만** 허용 → "회의록"이 "미팅록"이 되지 않고,
      사전 주석이 경고하는 '환'→'환경'→'Qwen경' 오염도 코드 차원에서 막힌다.
  경계 판정은 형태소 분석이 아니라 휴리스틱이다. 짧은 키는 애초에 사전에 넣지 않는 게 맞고,
  `validate_glossary()` 가 저장 전에 경고한다.

- **단일 패스**: 모든 키를 하나의 교대(alternation) 패턴으로 합쳐 한 번에 치환한다.
  키마다 순차 치환하면 앞 규칙의 결과가 뒤 규칙에 다시 걸린다(미팅→회의→미팅 왕복, 실측).
  긴 키를 앞에 두어 짧은 키에 먹히지 않게 한다.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

# 저장소 루트 기준 기본 사전 경로 (src/postprocess/glossary.py → ../../config)
DEFAULT_GLOSSARY_PATH = Path(__file__).resolve().parents[2] / "config" / "glossary.ko.json"

# 이 길이 이하의 한글 키는 앞쪽 경계도 요구한다(복합명사 구성요소일 확률이 높다).
SHORT_KEY_LEN = 2

_ASCII_WORD = r"[A-Za-z0-9_]"

# 짧은 한글 키 뒤에 이어질 수 있는 조사·접미의 **첫 음절**. 여기 없는 한글이 뒤따르면 조사가
# 아니라 복합명사의 뒷부분으로 보고 치환하지 않는다(회의+록, 출장+자). 목록에 없어서 교정이
# 안 되는 경우가 생기면 여기에 추가한다 — 반대 방향(오염)보다 복구가 쉽다.
_JOSA_HEADS = frozenset(
    "은는이가을를의에와과도만로으랑나든뿐밖조마커대보처부까야아여요님들께더치라"  # 조사
    "하한할함해했합히된될돼됐인입임였있없"  # 용언화 접미(회의하다·회의했다)
    "별씩째쯤당용측급"  # 수·분류 접미(오퍼스별·1인당)
)
_JOSA_CLASS = "".join(sorted(_JOSA_HEADS))


def load_glossary(path: Path | str | None = None) -> dict[str, str]:
    """glossary JSON 을 읽어 {오인식표기: 정답표기} dict 반환.

    스키마: {"terms": {"채찌피티": "ChatGPT", ...}}. '_'로 시작하는 메타 키는 무시.
    """
    p = Path(path) if path else DEFAULT_GLOSSARY_PATH
    data = json.loads(p.read_text(encoding="utf-8"))
    terms = data.get("terms", data)  # terms 래퍼 없으면 최상위를 사전으로 간주
    return {k: v for k, v in terms.items() if not k.startswith("_") and k != "version"}


def load_glossary_version(path: Path | str | None = None) -> str:
    """glossary JSON 의 "version" 필드 반환(버전 스탬프용, 설계 §10). 없으면 'unknown'."""
    p = Path(path) if path else DEFAULT_GLOSSARY_PATH
    data = json.loads(p.read_text(encoding="utf-8"))
    return str(data.get("version", "unknown"))


def _is_ascii(s: str) -> bool:
    return s.isascii()


def _key_pattern(key: str) -> str:
    """키 하나의 경계 규칙을 포함한 패턴 조각. 경계는 전부 **너비 0**(lookaround)이라
    매칭 결과(`group(0)`)는 항상 키 그 자체 → 치환할 때 dict 로 되찾을 수 있다."""
    esc = re.escape(key)
    if _is_ascii(key):
        # ASCII 단어문자만 경계로 본다 → 뒤에 한글 조사가 붙어도 매칭된다.
        return rf"(?<!{_ASCII_WORD}){esc}(?!{_ASCII_WORD})"
    if len(key) > SHORT_KEY_LEN:
        return esc  # 긴 한글 키는 표면형이 특이하다 → substring(종전 동작 유지)
    # 짧은 키만: 앞에 한글이 없고, 뒤는 한글이 아니거나 / 조사·접미 첫 음절이거나 / 문자열 끝
    return rf"(?<![가-힣]){esc}(?=[^가-힣]|[{_JOSA_CLASS}]|\Z)"


@lru_cache(maxsize=64)
def _compiled(items: tuple[tuple[str, str], ...]) -> re.Pattern[str] | None:
    """사전 → 단일 교대 패턴(긴 키 우선). 세그먼트마다 재컴파일하지 않도록 캐시한다."""
    keys = sorted((k for k, _ in items if k), key=len, reverse=True)
    if not keys:
        return None
    return re.compile("|".join(_key_pattern(k) for k in keys))


def apply_corrections(
    text: str, glossary: dict[str, str]
) -> tuple[str, list[str]]:
    """text 에 결정적 용어 교정 적용(단일 패스).

    Returns:
        (교정된 텍스트, 실제 적용된 정답표기 목록[중복제거·본문 등장 순서])
    """
    if not text or not glossary:
        return text, []
    items = tuple(sorted((k, v) for k, v in glossary.items() if k))
    pattern = _compiled(items)
    if pattern is None:
        return text, []
    mapping = dict(items)
    applied: list[str] = []

    def _replace(m: re.Match[str]) -> str:
        repl = mapping[m.group(0)]
        if repl not in applied:
            applied.append(repl)
        return repl

    return pattern.sub(_replace, text), applied


def find_matches(text: str, glossary: dict[str, str]) -> list[tuple[int, str]]:
    """(위치, 매칭된 원래 표기) 목록.

    미리보기(사용자가 항목을 저장하기 전에 "내 회의록 몇 군데가 바뀌는지" 보는 화면)가
    **실제 치환과 같은 규칙**을 쓰도록 하는 진입점이다. 미리보기가 따로 세면 경계 규칙이
    갈라져서 "미리보기엔 3건인데 실제로는 0건" 같은 신뢰 붕괴가 생긴다.
    """
    if not text or not glossary:
        return []
    pattern = _compiled(tuple(sorted((k, v) for k, v in glossary.items() if k)))
    if pattern is None:
        return []
    return [(m.start(), m.group(0)) for m in pattern.finditer(text)]


def validate_glossary(glossary: dict[str, str]) -> list[str]:
    """사전 항목의 위험 신호를 한글 경고 목록으로 반환(빈 목록 = 이상 없음).

    사용자가 직접 편집하는 사전에는 코드 주석으로 정책을 강제할 수 없다. 저장 전에 이 함수로
    걸러서 "항목 하나로 전사 전체가 오염되는" 사고를 막는다. 차단이 아니라 경고이므로
    호출부가 표시 방식(경고 배너 / 저장 거부)을 정한다.
    """
    warnings: list[str] = []
    keys = [k for k in glossary if k]
    for key in keys:
        value = glossary[key]
        if not str(value).strip():
            warnings.append(f"'{key}': 바꿀 값이 비어 있습니다.")
            continue
        if key == value:
            warnings.append(f"'{key}': 원래 표기와 바꿀 값이 같아 효과가 없습니다.")
        if not _is_ascii(key) and len(key) == 1:
            warnings.append(
                f"'{key}': 한 글자 한글 항목은 다른 단어 안에서도 바뀝니다"
                f"(예: '환'→'Qwen' 이 '환경'을 'Qwen경'으로). 더 긴 표기를 쓰세요."
            )
        if value in keys:
            warnings.append(
                f"'{key}'→'{value}': 바꾼 값이 다른 항목의 원래 표기입니다. 한 번만 치환하므로"
                f" 되돌아가진 않지만, 의도한 결과가 아닐 수 있습니다."
            )
        for other in keys:
            if other != key and other in key:
                warnings.append(f"'{key}': '{other}' 항목과 겹칩니다(긴 항목이 먼저 적용됩니다).")
    return warnings
