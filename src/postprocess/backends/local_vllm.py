"""로컬 vLLM 백엔드 (STUB).

TODO: vLLM OpenAI-호환 서버(base_url, model)로 chat completions 호출 구현.
의도한 구현:
  - .env/config 에서 base_url(예: http://localhost:8000/v1), model(예: Qwen 계열) 로드.
  - openai 클라이언트(또는 httpx) 로 /chat/completions POST.
  - schema 주어지면 vLLM guided_json(grammar) 로 JSON mode 강제 → capabilities().json_mode=True.
  - temperature=0, seed 전달(설계 §7). 단 vLLM 연속배칭은 비결정적이라
    cross-run 비트동일 재현은 보장 못 함 → capabilities().determinism="best_effort".
의존성: 현재 venv 미설치(openai/httpx). 추가 시 pyproject 검토 필요.
"""
from __future__ import annotations

from src.postprocess.backends.base import LLMBackend, LLMCapabilities


class LocalVLLMBackend(LLMBackend):
    name = "local_vllm"

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
            "TODO: vLLM OpenAI-호환 /chat/completions 호출 구현 (guided_json 으로 JSON mode)."
        )

    def capabilities(self) -> LLMCapabilities:
        # 구현 시: determinism="best_effort"(연속배칭 비결정, 설계 §7).
        raise NotImplementedError("TODO: vLLM 서버 모델의 capability(ctx_window 등) 노출.")
