"""LLMBackend 레지스트리 (설계 §4). STT 의 get_backend() 패턴 재사용.

get_llm_backend(name) → 인스턴스. 'passthrough' 는 모델 없는 테스트 백엔드(REAL),
나머지(local_vllm/ollama/openai/anthropic)는 스텁(호출 시 NotImplementedError).
"""
from __future__ import annotations

from src.postprocess.backends.base import LLMBackend, LLMCapabilities

__all__ = ["LLMBackend", "LLMCapabilities", "get_llm_backend"]


def get_llm_backend(name: str | None = None) -> LLMBackend:
    """이름 → LLMBackend 인스턴스. 기본 'passthrough'.

    스텁 백엔드도 '생성'은 되며(레지스트리 동작 확인 가능), 실제 generate()/capabilities()
    호출 시점에 NotImplementedError 를 던진다.
    """
    name = (name or "passthrough").strip().lower()
    if name == "passthrough":
        from src.postprocess.backends.passthrough import PassthroughBackend
        return PassthroughBackend()
    if name == "local_vllm":
        from src.postprocess.backends.local_vllm import LocalVLLMBackend
        return LocalVLLMBackend()
    if name == "ollama":
        from src.postprocess.backends.ollama import OllamaBackend
        return OllamaBackend()
    if name == "openai":
        from src.postprocess.backends.openai import OpenAIBackend
        return OpenAIBackend()
    if name == "anthropic":
        from src.postprocess.backends.anthropic import AnthropicBackend
        return AnthropicBackend()
    if name == "agent_cli":
        from src.postprocess.backends.agent_cli import AgentCLIBackend
        return AgentCLIBackend()
    raise ValueError(
        f"알 수 없는 LLM 백엔드: {name!r} "
        "(지원: passthrough, local_vllm, ollama, openai, anthropic, agent_cli)"
    )
