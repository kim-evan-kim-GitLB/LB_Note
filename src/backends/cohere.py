"""Cohere transcribe-03-2026 백엔드. trust_remote_code 로 모델 측 커스텀 클래스 로딩.

config.json:auto_map → AutoModelForSpeechSeq2Seq=modeling_cohere_asr.CohereAsrForConditionalGeneration
"""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import torch

from src.backends.base import STTBackend
from src.cancellation import raise_if_cancelled
from src.types import Segment


_vram_cap_applied = False
_vram_cap_lock = threading.Lock()


def apply_vram_cap() -> str | None:
    """이 프로세스의 VRAM 상한(config.STT_VRAM_CAP_MB)을 1회 적용 → 적용 내역(없으면 None).

    GPU 를 다른 작업자와 공유하는 환경 전제다. 상한이 없으면 STT 가 남의 학습을 OOM 으로
    죽일 수 있는데, 그쪽 피해(수 시간치 학습 유실)가 우리 잡 1건 실패보다 훨씬 크다. 상한을
    걸면 초과 시 **우리가 먼저** OutOfMemoryError 로 실패하므로 사고가 이쪽에서 멈춘다.

    상한이 장치 용량보다 크면 1.0 으로 clamp(작은 GPU 에서 fraction>1 예외 방지).
    """
    global _vram_cap_applied
    from src import config

    cap = config.STT_VRAM_CAP_MB
    if cap <= 0 or not torch.cuda.is_available():
        return None
    with _vram_cap_lock:
        if _vram_cap_applied:
            return None
        total_mb = torch.cuda.get_device_properties(0).total_memory / (1024 * 1024)
        frac = min(1.0, cap / total_mb)
        torch.cuda.set_per_process_memory_fraction(frac)
        _vram_cap_applied = True
    return f"VRAM 상한 {cap} MiB (장치 {total_mb:.0f} MiB 의 {frac * 100:.1f}%)"


class CohereASRBackend(STTBackend):
    name = "cohere"

    # 반복 hallucination 억제 (A/B 검증: 반복 97%→0%, 정상발화 보존). tools/test_rep_penalty.py 참조.
    REPETITION_PENALTY = 1.2

    # 모델 로드 직렬화용 프로세스 전역 락(load() 주석 참조). 인스턴스가 여러 개여도 하나를 공유해야
    # 하므로 클래스 속성이다 — 웹 잡은 잡마다 새 인스턴스를 만든다(src/stt.py:get_backend).
    _LOAD_LOCK = threading.Lock()

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
        # 로드는 프로세스 전역으로 직렬화한다. transformers 의 from_pretrained 는 로딩 중
        # 프로세스 전역 dtype 상태를 건드려서, 두 스레드가 동시에 로드하면 서로의 상태를
        # 덮어쓴다 → 한쪽 모델이 BFloat16/Float 이 섞인 채로 올라가고 추론에서
        # "mat1 and mat2 must have the same dtype" 로 터진다(웹 슬롯 2개로 올리며 실측).
        # 디코딩 자체는 인스턴스별이라 락 밖이다 — 로드만 줄 세우면 동시 추론은 그대로 된다.
        with self._LOAD_LOCK:
            applied = apply_vram_cap()  # 남의 작업을 죽이지 않게 이 프로세스 몫을 먼저 제한
            if applied:
                print(f"[CohereASRBackend] {applied}")
            if torch.cuda.is_available():
                torch.cuda.reset_peak_memory_stats()
            try:
                if self.quantization == "int8":
                    self._load_int8()
                else:
                    self._load_bf16()
            except torch.cuda.OutOfMemoryError as oom:
                torch.cuda.empty_cache()
                print("[CohereASRBackend] BF16 OOM → INT8 양자화 재시도")
                try:
                    self._load_int8()
                except Exception as fallback_err:  # noqa: BLE001
                    # 폴백 실패가 원래 원인(VRAM 부족)을 덮지 않게 한다. 이 환경의 bitsandbytes 는
                    # torch cu130 과 맞지 않아(libnvJitLink.so.13 없음) int8 경로가 아예 죽어 있고,
                    # 예전에는 그 실패가 RuntimeError("weights 변환 문제")로 바뀌어 나가서 로그만
                    # 봐서는 VRAM 문제인지 모델 파일 문제인지 알 수 없었다(실측 2026-07-28).
                    print(
                        "[CohereASRBackend] INT8 폴백 실패 — 원인은 VRAM 부족입니다. "
                        f"(폴백 오류: {type(fallback_err).__name__}: {fallback_err})"
                    )
                    raise oom from fallback_err
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
            outputs = self._model.generate(
                **inputs, max_new_tokens=256, repetition_penalty=self.REPETITION_PENALTY
            )

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
            outputs = self._model.generate(
                **inputs, max_new_tokens=512, repetition_penalty=self.REPETITION_PENALTY
            )

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

    def transcribe_arrays(
        self,
        audios: list[np.ndarray],
        sr: int = 16000,
        start_offsets: list[float] | None = None,
        language: str = "Korean",
        batch_size: int = 8,
        max_new_tokens: int = 1024,
    ) -> list[Segment]:
        """청크 배열들을 배치로 디코딩(순서 보존). 청크당 Segment 1개 반환.

        tools/vad_chunk_ax_clova.py 의 decode_chunks 포팅. 각 청크 ≤target<35s 라
        내부 재분할 없음(배치 row 1개 = 청크 1개).
        배치 구성은 결과에 '완전히' 중립은 아니다 — 길이가 다른 청크를 묶으면 패딩이 달라져
        텍스트가 미세하게 바뀐다(실측: bs=8 vs bs=64 에서 청크 단위 완전일치 49/64). 다만
        품질 지표 차이는 노이즈 수준이다(WER 0.397~0.400, bs=1~128).
        batch_size<=1 이면 단일 경로(decode_chunk 미러) 폴백.
        """
        assert self._model is not None and self._processor is not None, "load() 먼저 호출"
        lang = "ko" if language.lower() in ("korean", "ko") else language.lower()
        n = len(audios)
        offsets = start_offsets if start_offsets is not None else [0.0] * n
        texts: list[str] = []

        if batch_size <= 1:                       # 안전 폴백(단일 경로)
            for cw in audios:
                raise_if_cancelled()              # 청크 경계 취소 지점(아래 배치 경로와 동일)
                inputs = self._processor(
                    cw, sampling_rate=sr, return_tensors="pt", language=lang
                )
                aci = inputs.get("audio_chunk_index")
                inputs = inputs.to(self._model.device, dtype=self._model.dtype)
                with torch.inference_mode():
                    outputs = self._model.generate(
                        **inputs, max_new_tokens=max_new_tokens,
                        repetition_penalty=self.REPETITION_PENALTY,
                    )
                text = self._processor.decode(
                    outputs, skip_special_tokens=True,
                    audio_chunk_index=aci, language=lang,
                )
                if isinstance(text, list):
                    text = text[0] if text else ""
                texts.append(text.strip())
        else:
            for i in range(0, n, batch_size):
                # 배치 경계 취소 지점 — STT 추론 자체는 중단 불가라, 여기서 끊지 않으면 취소한
                # 잡이 전체 오디오를 다 디코딩할 때까지 GPU 슬롯을 물고 뒤 사용자를 막는다.
                # 배치 1개는 초 단위(≤35s 청크 × batch)라 취소 반응도 그 수준이다.
                raise_if_cancelled()
                batch = audios[i:i + batch_size]
                inputs = self._processor(
                    batch, sampling_rate=sr, return_tensors="pt", language=lang
                )
                inputs = inputs.to(self._model.device, dtype=self._model.dtype)
                with torch.inference_mode():
                    outputs = self._model.generate(
                        **inputs, max_new_tokens=max_new_tokens,
                        repetition_penalty=self.REPETITION_PENALTY,
                    )
                decoded = self._processor.batch_decode(outputs, skip_special_tokens=True)
                texts.extend((t or "").strip() for t in decoded)
                print(f"[pipeline]   decoded {min(i + batch_size, n)}/{n}")

        segments: list[Segment] = []
        for audio, off, text in zip(audios, offsets, texts):
            duration = len(audio) / float(sr)
            segments.append(Segment(start=off, end=off + duration, text=text))
        return segments

    def vram_peak_mb(self) -> int | None:
        if not torch.cuda.is_available():
            return None
        return torch.cuda.max_memory_allocated() // (1024 * 1024)
