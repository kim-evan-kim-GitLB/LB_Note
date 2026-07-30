"""[K] 비한국어 산출 수리(localize) 스테이지 — 출력 언어 보장.

회의록은 한국어 산출물이다. 프롬프트에 "한국어로 써라"를 넣어두었지만 **지시 준수에 기대지 않고**
호출부가 한글비를 재검사하고(language_gate.is_korean_output), 비한국어로 판정된 항목만 이 스테이지가
한국어로 옮긴다. 정상 항목만 나온 회의에서는 호출 자체가 없다(평시 콜 0회).

번역만 한다 — 요약·상세화·정보 추가는 금지다(근거 evidence_seg_ids 와 어긋나면 그라운딩이 깨진다).
수리에 실패한 항목은 결과에서 빠지고, 호출부가 요약=드롭 / 액션=flag'확인필요' 로 마감한다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from src.postprocess.backends.base import LLMBackend
from src.postprocess.stages.clean import _load_prompt, _split_sections

# prompts/localize.ko.md (src/postprocess/stages/localize.py → ../../../prompts)
DEFAULT_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "localize.ko.md"

_PROMPT_VERSION_RE = re.compile(r"prompt_version:\s*([^\s>]+)")

ITEMS_OPEN = "<<<ITEMS>>>"
ITEMS_CLOSE = "<<<END_ITEMS>>>"


def load_localize_prompt_version(path: Path | str | None = None) -> str:
    """localize.ko.md 헤더의 prompt_version 주석 반환(버전 스탬프)."""
    m = _PROMPT_VERSION_RE.search(_load_prompt(path or DEFAULT_PROMPT_PATH))
    return m.group(1) if m else "unknown"


def parse_localize_output(raw: str) -> dict[str, str]:
    """수리 출력 → {id: 한국어 text}. 코드펜스·머리말 견고 처리(다른 스테이지와 동일)."""
    t = raw.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    if not t.startswith("{"):
        lo, hi = t.find("{"), t.rfind("}")
        if lo != -1 and hi != -1 and hi > lo:
            t = t[lo : hi + 1]
    data = json.loads(t)
    out: dict[str, str] = {}
    for entry in data.get("items") or []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("id") or "").strip()
        text = str(entry.get("text") or "").strip()
        if key and text:
            out[key] = text
    return out


def build_messages(items: list[dict], system_tmpl: str, user_tmpl: str) -> list[dict]:
    """수리용 (system, user) 메시지. 항목 JSON 을 구분자로 격리(인젝션 방어)."""
    body = json.dumps({"items": items}, ensure_ascii=False, indent=1)
    user = user_tmpl.replace("{{ITEMS_JSON}}", body)
    if ITEMS_OPEN not in user:
        user = f"{ITEMS_OPEN}\n{body}\n{ITEMS_CLOSE}\n{user}"
    return [
        {"role": "system", "content": system_tmpl},
        {"role": "user", "content": user},
    ]


class LocalizeStage:
    """비한국어 산출 항목 → 한국어 텍스트 맵({id: text}). 회의 단위 1콜(필요할 때만)."""

    name = "localize"

    def __init__(self, prompt_path: Path | str | None = None) -> None:
        self._prompt_path = prompt_path or DEFAULT_PROMPT_PATH

    def run(
        self,
        items: list[dict],
        backend: LLMBackend,
        ctx: dict | None = None,
    ) -> dict[str, str]:
        ctx = ctx or {}
        if not items:
            return {}
        system_tmpl, user_tmpl = _split_sections(_load_prompt(self._prompt_path))
        messages = build_messages(items, system_tmpl, user_tmpl)
        raw = backend.generate(
            messages,
            schema=None,
            temperature=ctx.get("temperature", 0.0),
            max_tokens=ctx.get("max_tokens", 4096),
            seed=ctx.get("seed", 0),
        )
        try:
            return parse_localize_output(raw)
        except (json.JSONDecodeError, ValueError):
            # 수리 실패 → 빈 맵. 호출부가 드롭/flag 로 마감한다(멈춤 없음).
            return {}
