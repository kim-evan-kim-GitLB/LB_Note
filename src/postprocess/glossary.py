"""[A] 결정적 용어 교정 (설계 §3).

외래어/고유명사는 정답이 정해진 '치환 문제'이지 추론 문제가 아니다 →
LLM 이전에 사전으로 결정적 치환하여 가장 불안정한 부분을 모델-독립으로 만든다.
100% 재현(같은 입력 → 같은 출력). stdlib json 로 사전 로드(pyyaml 미설치).

매칭 규칙(설계: case-sensitive 한글/외래어):
- ASCII 키(예: Quan, QWEN): 단어경계(\b) 기준 whole-word 치환.
  영문 토큰 경계를 존중해 'Quantum' 의 'Quan' 같은 부분일치를 막는다.
- 비-ASCII 키(예: 채찌피티): 한글은 단어 사이 공백·\b 가 없으므로 substring 치환.
  키를 길이 내림차순으로 적용해 더 긴 키가 짧은 키에 먹히지 않게 한다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# 저장소 루트 기준 기본 사전 경로 (src/postprocess/glossary.py → ../../config)
DEFAULT_GLOSSARY_PATH = Path(__file__).resolve().parents[2] / "config" / "glossary.ko.json"


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


def _compile_rules(glossary: dict[str, str]) -> list[tuple[re.Pattern[str], str, str]]:
    """(컴파일 패턴, 원본 키, 치환값) 목록. 긴 키 우선(겹침 방지)."""
    rules: list[tuple[re.Pattern[str], str, str]] = []
    for key in sorted(glossary, key=len, reverse=True):
        repl = glossary[key]
        if _is_ascii(key):
            # 영문 토큰: 단어경계로 whole-word 매칭(부분일치 차단)
            pattern = re.compile(rf"\b{re.escape(key)}\b")
        else:
            # 한글/외래어: 공백 없으므로 substring 매칭
            pattern = re.compile(re.escape(key))
        rules.append((pattern, key, repl))
    return rules


def apply_corrections(
    text: str, glossary: dict[str, str]
) -> tuple[str, list[str]]:
    """text 에 결정적 용어 교정 적용.

    Returns:
        (교정된 텍스트, 실제 적용된 정답표기 목록[중복제거·순서보존])
    """
    if not text:
        return text, []
    applied: list[str] = []
    out = text
    for pattern, _key, repl in _compile_rules(glossary):
        new_out, n = pattern.subn(repl, out)
        if n > 0 and repl not in applied:
            applied.append(repl)
        out = new_out
    return out, applied
