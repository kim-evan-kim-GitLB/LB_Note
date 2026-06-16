"""Ollama 백엔드 (STUB).

TODO: Ollama 로컬 데몬(http://localhost:11434)으로 chat 호출 구현.
의도한 구현:
  - config 에서 model(예: qwen2.5) 로드, /api/chat POST(stream=False).
  - schema 주어지면 Ollama 'format' 파라미터(json/JSON schema)로 JSON mode 강제.
  - options 에 temperature=0, seed 전달(설계 §7). Ollama 는 seed 지원하나
    런타임/모델 버전 차이로 비트동일 재현은 보장 못 함 → determinism="best_effort".
의존성: stdlib http 로 가능(httpx 불필요). 추가 의존성 없이 구현 권장.
"""
from __future__ import annotations

from src.postprocess.backends.base import LLMBackend, LLMCapabilities


class OllamaBackend(LLMBackend):
    name = "ollama"

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
            "TODO: Ollama /api/chat 호출 구현 (format 파라미터로 JSON mode)."
        )

    def capabilities(self) -> LLMCapabilities:
        # 구현 시: determinism="best_effort"(seed 지원하나 비트동일 미보장, 설계 §7).
        raise NotImplementedError("TODO: Ollama 모델 capability 노출.")
