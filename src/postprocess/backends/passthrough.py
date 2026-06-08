"""passthrough 테스트 백엔드 (REAL).

모델/API 없이 파이프라인 배선을 스모크 테스트하기 위한 백엔드.
마지막 user 메시지의 content 를 그대로 되돌려준다 → 정제 결과 == 원문.
이렇게 하면 [A]glossary → [C]stage → [D]validate 배선을 모델 없이 end-to-end 검증할 수 있다.
"""
from __future__ import annotations

from src.postprocess.backends.base import LLMBackend, LLMCapabilities


class PassthroughBackend(LLMBackend):
    """user 메시지를 echo 하는 무모델 백엔드."""

    name = "passthrough"

    def generate(
        self,
        messages: list[dict],
        *,
        schema: dict | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        seed: int | None = 0,
    ) -> str:
        # 마지막 user 메시지 content 를 그대로 반환(정제 없음). 없으면 빈 문자열.
        for msg in reversed(messages):
            if msg.get("role") == "user":
                return str(msg.get("content", ""))
        return ""

    def capabilities(self) -> LLMCapabilities:
        # json_mode=False: CleanStage 가 평문 echo 를 그대로 cleaned 로 쓰는 경로를 타게 한다.
        # determinism="reproducible": echo 라 같은 입력→같은 출력이 항상 성립(설계 §7).
        return LLMCapabilities(
            json_mode=False, ctx_window=10**9, tool_call=False, determinism="reproducible"
        )
