"""[M] 구간 요약 병합(reduce) 스테이지 — 다중 agent core 의 long_form 대응.

장시간 회의는 구간별로 병렬 요약(map)한 뒤 이 스테이지가 하나의 회의록 요약으로 병합한다.
map 창은 서로 겹치므로(meeting_profile.split_windows) 같은 논의가 두 번 나올 수 있고, 안건 번호도
구간 내 번호라 충돌한다 — 그 정리가 이 단계의 책임이다.

내용 생성은 하지 않는다(병합·중복제거·재번호만). 그라운딩(anchor/time_range/근거 검증)은 호출부의
ground_summary 가 이후에 결정적으로 수행한다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from src.postprocess.backends.base import LLMBackend
from src.postprocess.stages.clean import _load_prompt, _split_sections
from src.postprocess.stages.summarize import parse_summarize_output
from src.postprocess.summarize_schema import MeetingSummary

# prompts/reduce.ko.md (src/postprocess/stages/reduce.py → ../../../prompts)
DEFAULT_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "reduce.ko.md"

_PROMPT_VERSION_RE = re.compile(r"prompt_version:\s*([^\s>]+)")

PARTS_OPEN = "<<<PARTS>>>"
PARTS_CLOSE = "<<<END>>>"


def load_reduce_prompt_version(path: Path | str | None = None) -> str:
    """reduce.ko.md 헤더의 prompt_version 주석 반환(버전 스탬프)."""
    m = _PROMPT_VERSION_RE.search(_load_prompt(path or DEFAULT_PROMPT_PATH))
    return m.group(1) if m else "unknown"


def build_messages(parts: list[dict], system_tmpl: str, user_tmpl: str) -> list[dict]:
    """병합용 (system, user) 메시지. user = 구분자로 격리한 부분 요약 JSON 배열."""
    body = json.dumps(parts, ensure_ascii=False, indent=1)
    user = user_tmpl.replace("{{SUMMARY_PARTS_JSON}}", body)
    if PARTS_OPEN not in user:  # 템플릿에 구분자가 없으면 명시적으로 격리(방어)
        user = f"{PARTS_OPEN}\n{body}\n{PARTS_CLOSE}\n{user}"
    return [
        {"role": "system", "content": system_tmpl},
        {"role": "user", "content": user},
    ]


class ReduceStage:
    """부분 요약 목록 → 병합된 MeetingSummary(회의 단위 1콜)."""

    name = "reduce"

    def __init__(self, prompt_path: Path | str | None = None) -> None:
        self._prompt_path = prompt_path or DEFAULT_PROMPT_PATH

    def run(
        self,
        parts: list[dict],
        backend: LLMBackend,
        ctx: dict | None = None,
    ) -> MeetingSummary:
        ctx = ctx or {}
        system_tmpl, user_tmpl = _split_sections(_load_prompt(self._prompt_path))
        if ctx.get("extra_directives"):
            system_tmpl = f"{system_tmpl}\n\n{ctx['extra_directives']}"
        messages = build_messages(parts, system_tmpl, user_tmpl)
        raw = backend.generate(
            messages,
            schema=None,
            temperature=ctx.get("temperature", 0.0),
            max_tokens=ctx.get("max_tokens", 8192),
            seed=ctx.get("seed", 0),
        )
        try:
            summary = parse_summarize_output(raw)
        except (json.JSONDecodeError, ValueError):
            # 병합 실패는 회의를 죽이지 않는다 — 호출부가 결정적 병합으로 폴백한다.
            return MeetingSummary.empty()
        summary.prompt_version = load_reduce_prompt_version(self._prompt_path)
        summary.backend = backend.name
        return summary
