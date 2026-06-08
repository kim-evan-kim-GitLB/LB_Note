"""Stage ABC — 스테이지 공통 계약 (설계 §9).

clean / summarize / agenda / action_items 가 모두 이 계약을 구현하면
파이프라인은 스테이지 목록을 순회만 하면 된다(stage-pluggable).
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from src.postprocess.backends.base import LLMBackend
from src.postprocess.schema import CleanResult


class Stage(ABC):
    """후처리 스테이지 1개의 공통 인터페이스.

    run(segments, backend, ctx) → CleanResult.
    - segments: STT text.json 의 segments(각 {start, end, text})
    - backend : [B] LLM 어댑터(passthrough 포함)
    - ctx     : 스테이지별 부가 설정(glossary, 프롬프트 경로, 검증 밴드 등)
    """

    name: str = "base"

    @abstractmethod
    def run(
        self,
        segments: list[dict],
        backend: LLMBackend,
        ctx: dict | None = None,
    ) -> CleanResult:
        raise NotImplementedError
