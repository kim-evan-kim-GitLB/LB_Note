"""Cohere transcribe-03-2026 백엔드. trust_remote_code 로 모델 측 커스텀 클래스 로딩.

config.json:auto_map → AutoModelForSpeechSeq2Seq=modeling_cohere_asr.CohereAsrForConditionalGeneration
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import torch

from src.backends.base import STTBackend
from src.types import Segment


class CohereASRBackend(STTBackend):
    name = "cohere"

    def __init__(self, model_path: Path, dtype: str = "bfloat16", quantization: str = ""):
        self.model_path = Path(model_path)
        self.dtype_name = dtype
        self.quantization = quantization
        self._model = None
        self._processor = None

    def _load_bf16(self) -> None:
        # README 공식 경로: transformers native 클래스 직접 import.
        # `trust_remote_code=True` + AutoModel* 우회 경로는 weight 매핑이 buggy.
        from transformers import AutoProcessor, CohereAsrForConditionalGeneration

        self._processor = AutoProcessor.from_pretrained(str(self.model_path))
        # device_map="auto" 는 4GB VRAM 환경에서 CPU offload 를 강제해 추론 결과를 망친다.
        # 전체를 cuda:0 에 강제 → 초과분은 NVIDIA driver 의 system memory fallback (driver 545+) 사용.
        self._model = CohereAsrForConditionalGeneration.from_pretrained(
            str(self.model_path),
            device_map={"": "cuda:0"},
            torch_dtype=getattr(torch, self.dtype_name),
        )
        self._model.eval()

    def _load_int8(self) -> None:
        from transformers import (
            AutoProcessor,
            BitsAndBytesConfig,
            CohereAsrForConditionalGeneration,
        )

        bnb = BitsAndBytesConfig(load_in_8bit=True)
        self._processor = AutoProcessor.from_pretrained(str(self.model_path))
        self._model = CohereAsrForConditionalGeneration.from_pretrained(
            str(self.model_path),
            device_map={"": "cuda:0"},
            quantization_config=bnb,
        )
        self._model.eval()

    def load(self) -> None:
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
        try:
            if self.quantization == "int8":
                self._load_int8()
            else:
                self._load_bf16()
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            print("[CohereASRBackend] BF16 OOM → INT8 양자화 재시도")
            self._load_int8()
            self.quantization = "int8"

    def unload(self) -> None:
        self._model = None
        self._processor = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def transcribe(self, audio: Path, language: str = "Korean") -> list[Segment]:
        assert self._model is not None and self._processor is not None, "load() 먼저 호출"
        import librosa

        lang = "ko" if language.lower() in ("korean", "ko") else language.lower()
        wav, _ = librosa.load(str(audio), sr=16000, mono=True)

        inputs = self._processor(wav, sampling_rate=16000, return_tensors="pt", language=lang)
        audio_chunk_index = inputs.get("audio_chunk_index")
        inputs = inputs.to(self._model.device, dtype=self._model.dtype)

        with torch.inference_mode():
            outputs = self._model.generate(**inputs, max_new_tokens=256)

        text = self._processor.decode(
            outputs,
            skip_special_tokens=True,
            audio_chunk_index=audio_chunk_index,
            language=lang,
        )
        if isinstance(text, list):
            text = text[0] if text else ""
        return [Segment(start=0.0, end=0.0, text=text)]

    def transcribe_array(
        self,
        audio: np.ndarray,
        sr: int = 16000,
        start_offset: float = 0.0,
        language: str = "Korean",
    ) -> list[Segment]:
        assert self._model is not None and self._processor is not None, "load() 먼저 호출"
        lang = "ko" if language.lower() in ("korean", "ko") else language.lower()

        inputs = self._processor(audio, sampling_rate=sr, return_tensors="pt", language=lang)
        audio_chunk_index = inputs.get("audio_chunk_index")
        inputs = inputs.to(self._model.device, dtype=self._model.dtype)

        with torch.inference_mode():
            outputs = self._model.generate(**inputs, max_new_tokens=512)

        text = self._processor.decode(
            outputs,
            skip_special_tokens=True,
            audio_chunk_index=audio_chunk_index,
            language=lang,
        )
        if isinstance(text, list):
            text = text[0] if text else ""

        duration = len(audio) / float(sr)
        return [Segment(
            start=start_offset,
            end=start_offset + duration,
            text=text.strip(),
        )]

    def vram_peak_mb(self) -> int | None:
        if not torch.cuda.is_available():
            return None
        return torch.cuda.max_memory_allocated() // (1024 * 1024)
