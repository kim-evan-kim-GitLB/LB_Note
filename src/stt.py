from __future__ import annotations

from src import config
from src.backends.base import STTBackend
from src.backends.cohere import CohereASRBackend
from src.backends.enhancer_base import AudioEnhancer
from src.backends.vad_base import VADBackend


def get_backend(name: str | None = None) -> STTBackend:
    name = (name or config.STT_BACKEND or "cohere").lower()
    if name == "cohere":
        return CohereASRBackend(
            config.COHERE_MODEL_PATH,
            dtype=config.COHERE_DTYPE,
            quantization=config.COHERE_QUANTIZATION,
        )
    raise ValueError(f"이 venv 는 cohere 전용입니다. 받은 backend={name!r}")


def get_enhancer(name: str) -> AudioEnhancer:
    """이름 → AudioEnhancer 인스턴스. 'wpe'(dereverb) | 'gtcrn'(denoise)."""
    name = name.strip().lower()
    if name == "wpe":
        from src.backends.wpe_dereverb import WPEDereverb
        return WPEDereverb()
    if name == "gtcrn":
        from src.backends.gtcrn_denoiser import GTCRNDenoiser
        return GTCRNDenoiser(config.GTCRN_MODEL_PATH)
    raise ValueError(f"알 수 없는 enhancer: {name!r} (지원: wpe, gtcrn)")


def get_vad(name: str | None = None) -> VADBackend | None:
    """이름 → VADBackend. 빈 값/None 이면 None(VAD 비활성)."""
    name = (name or "").strip().lower()
    if not name:
        return None
    if name == "silero":
        from src.backends.silero_vad import SileroVAD
        return SileroVAD(
            threshold=config.VAD_THRESHOLD,
            min_speech_sec=config.VAD_MIN_SPEECH_SEC,
            min_silence_sec=config.VAD_MIN_SILENCE_SEC,
        )
    raise ValueError(f"알 수 없는 VAD: {name!r} (지원: silero)")
