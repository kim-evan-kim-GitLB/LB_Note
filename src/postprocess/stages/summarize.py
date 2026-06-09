"""[S] 회의 요약 스테이지 (Phase 1-b, 설계 docs/2026-06-09-summarize-stage-design.md).

정제본(cleaned.json) 전체 transcript에서 회사 표준 회의록 양식의 요약(안건 목록 + 안건별 상세
논의)을 산출한다. 추출(extract)과 마찬가지로 **회의 단위(meeting-level)** — segment 1:1 이 아니라
한 회의를 한 번에 요약하므로 Stage(per-segment CleanResult) 계약을 따르지 않고 독립 run 시그니처를
가진다.

배선 규칙(extract 와 동일 방침):
- transcript 본문 = `<<<TRANSCRIPT>>>` 구분자로 격리(인젝션 방어).
- 규칙은 system, transcript(각 줄 `[id] 본문`)는 user 로 주입.
- anchor/time_range/evidence 검증은 호출부의 ground_summary 가 결정적으로 수행(LLM 불신).
- passthrough/비-JSON 백엔드 → 빈 요약(MeetingSummary.empty()).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from src.postprocess.backends.base import LLMBackend
from src.postprocess.stages.clean import _load_prompt, _split_sections
from src.postprocess.stages.extract import build_messages
from src.postprocess.summarize_schema import MeetingSummary

# prompts/summarize.ko.md (src/postprocess/stages/summarize.py → ../../../prompts)
DEFAULT_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "summarize.ko.md"

_PROMPT_VERSION_RE = re.compile(r"prompt_version:\s*([^\s>]+)")


def load_summarize_prompt_version(path: Path | str | None = None) -> str:
    """summarize.ko.md 헤더의 prompt_version 주석 반환(버전 스탬프, 설계 §2)."""
    text = _load_prompt(path or DEFAULT_PROMPT_PATH)
    m = _PROMPT_VERSION_RE.search(text)
    return m.group(1) if m else "unknown"


def parse_summarize_output(raw: str) -> MeetingSummary:
    """LLM/에이전트 출력 문자열 → MeetingSummary. 코드펜스·머리말 견고 처리(extract 와 동일)."""
    t = raw.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    if not t.startswith("{"):
        lo, hi = t.find("{"), t.rfind("}")
        if lo != -1 and hi != -1 and hi > lo:
            t = t[lo : hi + 1]
    data = json.loads(t)
    return MeetingSummary.from_dict(data)


class SummarizeStage:
    """요약 스테이지(회의 단위). 실제 LLM 백엔드(JSON mode) 연결 시 사용.

    passthrough 는 요약 불가(빈 결과). 그라운딩(anchor/time_range/근거검증)은 호출부가
    ground_summary 로 수행한다(stage 는 파싱까지만).
    """

    name = "summarize"

    def __init__(self, prompt_path: Path | str | None = None) -> None:
        self._prompt_path = prompt_path or DEFAULT_PROMPT_PATH

    def run(
        self,
        segments: list[dict],
        backend: LLMBackend,
        ctx: dict | None = None,
    ) -> MeetingSummary:
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
            summary = parse_summarize_output(raw)
        except (json.JSONDecodeError, ValueError):
            # passthrough/비-JSON 백엔드 → 빈 요약(요약은 JSON mode 백엔드/핸드오프 전제).
            return MeetingSummary.empty()
        summary.prompt_version = load_summarize_prompt_version(self._prompt_path)
        summary.backend = backend.name
        return summary
