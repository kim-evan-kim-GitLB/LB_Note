"""Anthropic 클라우드 백엔드 (STUB).

TODO: Anthropic Messages API 호출 구현.
의도한 구현:
  - .env 에서 ANTHROPIC_API_KEY, model(예: claude-…) 로드.
  - system 프롬프트는 별도 system 파라미터로 분리(Anthropic 형식).
  - JSON 강제는 tool_use(입력 스키마) 또는 prefill 기법으로 → json_mode 매핑.
  - temperature=0 (Anthropic 는 seed 미지원 → 결정성 제어 수단 없음,
    determinism="none". 비결정은 [D] 검증·리페어로 흡수).
의존성: anthropic SDK 현재 venv 미설치. 추가 시 pyproject 검토 필요.
"""
from __future__ import annotations

from src.postprocess.backends.base import LLMBackend, LLMCapabilities


class AnthropicBackend(LLMBackend):
    name = "anthropic"

    def generate(
        self,
        messages: list[dict],
        *,
        schema: dict | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        seed: int | None = 0,
    ) -> str:
        raise NotImplementedError(
            "TODO: Anthropic messages.create 호출 구현 (tool_use 로 schema 강제)."
        )

    def capabilities(self) -> LLMCapabilities:
        # 구현 시: determinism="none"(seed 미지원, 설계 §7).
        raise NotImplementedError("TODO: Anthropic 모델 capability 노출.")
