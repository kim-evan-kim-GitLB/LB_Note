"""OpenAI 클라우드 백엔드 (STUB).

TODO: OpenAI Chat Completions / Responses API 호출 구현.
의도한 구현:
  - .env 에서 OPENAI_API_KEY, model(예: gpt-4.1) 로드.
  - response_format={"type":"json_schema", ...} 로 schema 강제 → json_mode=True.
  - temperature=0, seed 전달(설계 §7). seed 는 best-effort 이고 system_fingerprint 가
    변동하면 재현 안 됨 → determinism="best_effort". 비결정 잔여는 [D] 검증·리페어로 흡수.
의존성: openai SDK 현재 venv 미설치. 추가 시 pyproject 검토 필요.
"""
from __future__ import annotations

from src.postprocess.backends.base import LLMBackend, LLMCapabilities


class OpenAIBackend(LLMBackend):
    name = "openai"

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
            "TODO: OpenAI chat.completions 호출 구현 (response_format json_schema 로 schema 강제)."
        )

    def capabilities(self) -> LLMCapabilities:
        # 구현 시: determinism="best_effort"(seed best-effort+fingerprint 변동, 설계 §7).
        raise NotImplementedError("TODO: OpenAI 모델 capability(ctx_window 등) 노출.")
