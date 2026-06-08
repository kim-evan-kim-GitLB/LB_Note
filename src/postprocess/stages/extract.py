"""[E] 액션아이템 추출 스테이지 (Phase 1-b, 설계 §5 후속).

정제본(cleaned.json) 전체 transcript에서 실행 가능한 액션아이템을 추출한다. 정제(clean)가
segment 1:1 인 것과 달리 추출은 **회의 단위(meeting-level)** — 한 과제가 여러 segment에 걸쳐
있으므로 segment 1:1 이 아니다. 그래서 Stage(per-segment CleanResult) 계약을 따르지 않고
독립 run 시그니처를 가진다(base.py 의 action_items 계획 참조).

배선 규칙:
- transcript 본문 = `<<<TRANSCRIPT>>>` 구분자로 격리(인젝션 방어, clean과 동일 방침).
- 규칙/few-shot 은 system, transcript(각 줄 `[id] 본문`)는 user 로 주입.
- 실제 추출은 backend(JSON mode) 또는 2-phase 핸드오프(extract_handoff)가 수행한다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from src.postprocess.backends.base import LLMBackend
from src.postprocess.extract_schema import ExtractResult, transcript_with_ids
from src.postprocess.stages.clean import _load_prompt, _split_sections

# prompts/extract.ko.md (src/postprocess/stages/extract.py → ../../../prompts)
DEFAULT_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "extract.ko.md"

_PROMPT_VERSION_RE = re.compile(r"prompt_version:\s*([^\s>]+)")

TRANSCRIPT_OPEN = "<<<TRANSCRIPT>>>"
TRANSCRIPT_CLOSE = "<<<END>>>"


def load_extract_prompt_version(path: Path | str | None = None) -> str:
    """extract.ko.md 헤더의 prompt_version 주석 반환(버전 스탬프, 설계 §10)."""
    text = _load_prompt(path or DEFAULT_PROMPT_PATH)
    m = _PROMPT_VERSION_RE.search(text)
    return m.group(1) if m else "unknown"


def load_extract_rules(path: Path | str | None = None) -> str:
    """extract.ko.md 의 SYSTEM 섹션(추출 규칙) 반환. 핸드오프 work-order가 운반한다."""
    system, _user = _split_sections(_load_prompt(path or DEFAULT_PROMPT_PATH))
    return system


def parse_extract_output(raw: str) -> ExtractResult:
    """LLM/에이전트 출력 문자열 → ExtractResult. 코드펜스·머리말 견고 처리.

    ```json ... ``` 펜스나 앞뒤 잡텍스트가 있어도 첫 '{'~마지막 '}' 구간을 파싱한다
    (clean의 게이트가 없는 추출 경로라 파싱을 방어적으로).
    """
    t = raw.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    if not t.startswith("{"):
        lo, hi = t.find("{"), t.rfind("}")
        if lo != -1 and hi != -1 and hi > lo:
            t = t[lo : hi + 1]
    data = json.loads(t)
    return ExtractResult.from_dict(data)


def build_messages(segments: list[dict], system_tmpl: str, user_tmpl: str) -> list[dict]:
    """추출용 (system, user) 메시지. user = 구분자로 격리한 transcript(각 줄 `[id] 본문`)."""
    body = transcript_with_ids(segments)
    user = user_tmpl.replace("{{TRANSCRIPT_WITH_IDS}}", body)
    if TRANSCRIPT_OPEN not in user:
        # 템플릿에 구분자가 없으면 명시적으로 격리(방어).
        user = f"{TRANSCRIPT_OPEN}\n{body}\n{TRANSCRIPT_CLOSE}\n{user}"
    return [
        {"role": "system", "content": system_tmpl},
        {"role": "user", "content": user},
    ]


class ExtractStage:
    """추출 스테이지(회의 단위). 실제 LLM 백엔드(JSON mode) 연결 시 사용.

    기본 경로는 2-phase 핸드오프(extract_handoff)이며, 이 클래스는 backend.generate 로
    한 번에 추출하는 직접 경로다(passthrough 는 추출 불가 → 빈 결과).
    """

    name = "extract"

    def __init__(self, prompt_path: Path | str | None = None) -> None:
        self._prompt_path = prompt_path or DEFAULT_PROMPT_PATH

    def run(
        self,
        segments: list[dict],
        backend: LLMBackend,
        ctx: dict | None = None,
    ) -> ExtractResult:
        ctx = ctx or {}
        system_tmpl, user_tmpl = _split_sections(_load_prompt(self._prompt_path))
        messages = build_messages(segments, system_tmpl, user_tmpl)
        raw = backend.generate(
            messages,
            schema=None,
            temperature=ctx.get("temperature", 0.0),
            max_tokens=ctx.get("max_tokens", 4096),
            seed=ctx.get("seed", 0),
        )
        try:
            return parse_extract_output(raw)
        except (json.JSONDecodeError, ValueError):
            # passthrough/비-JSON 백엔드 → 빈 결과(추출은 JSON mode 백엔드/핸드오프 전제).
            return ExtractResult(items=[])
