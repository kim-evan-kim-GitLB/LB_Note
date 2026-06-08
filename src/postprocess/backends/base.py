"""[B] LLMBackend 어댑터 — 교체가능성의 핵심 (설계 §4).

STT의 get_backend("cohere") 추상화와 동일 철학: 단일 인터페이스 뒤에 모든 모델을 둔다.
모델은 '교체 가능한 부품', 안정성은 바깥의 고정 계약 + 검증 루프에서 온다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LLMCapabilities:
    """모델별 능력 정규화. 어댑터가 차이를 흡수한다(설계 §4 capability 정규화).

    - json_mode    : 구조화 JSON 출력(스키마 강제) 지원 여부. 미지원이면
                     정규식 추출 + 리페어로 폴백.
    - ctx_window   : 토큰 컨텍스트 한계. [C]의 segment 단위 청킹이 이를 흡수.
    - tool_call    : tool/function calling 지원 여부.
    - determinism  : 결정성 실태(설계 §7, 재구성). 과약속 금지 — best-effort.
        - "none"        : 결정성 제어 수단 없음(예: seed 없는 API).
        - "best_effort" : temperature=0/seed 로 노력하나 cross-run/cross-backend
                          비트동일 재현은 보장 못 함(vLLM 연속배칭, OpenAI fingerprint 변동).
        - "reproducible": 같은 입력→같은 출력 보장(예: passthrough echo).
      재현 가능한 유일한 운영 축은 캐싱(설계 §7)이며, determinism 은 보장이 아니라 실태 기록.
    """

    json_mode: bool = False
    ctx_window: int = 8192
    tool_call: bool = False
    determinism: str = "best_effort"


class LLMBackend(ABC):
    """모든 LLM provider 의 공통 계약.

    구현체는 generate() 로 messages → 텍스트(가능시 schema 강제 JSON)를 반환하고,
    capabilities() 로 자신의 능력을 노출한다. 결정성을 위해 temperature=0,
    가능 모델은 seed 고정(설계 §7).
    """

    name: str = "base"

    @abstractmethod
    def generate(
        self,
        messages: list[dict],
        *,
        schema: dict | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        seed: int | None = 0,
    ) -> str:
        """messages(OpenAI 형식: [{"role","content"}, ...]) → 생성 텍스트.

        schema 가 주어지고 capabilities().json_mode 면 해당 스키마의 JSON 문자열을
        반환해야 한다. 미지원이면 어댑터가 정규식 추출/리페어로 폴백한다.
        """
        raise NotImplementedError

    @abstractmethod
    def capabilities(self) -> LLMCapabilities:
        """이 백엔드의 능력 반환."""
        raise NotImplementedError
