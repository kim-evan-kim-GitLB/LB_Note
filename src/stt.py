from __future__ import annotations

from src import config
from src.backends.base import STTBackend
from src.backends.cohere import CohereASRBackend


def get_backend(name: str | None = None) -> STTBackend:
    name = (name or config.STT_BACKEND or "cohere").lower()
    if name == "cohere":
        return CohereASRBackend(
            config.COHERE_MODEL_PATH,
            dtype=config.COHERE_DTYPE,
            quantization=config.COHERE_QUANTIZATION,
        )
    raise ValueError(f"이 venv 는 cohere 전용입니다. 받은 backend={name!r}")
