from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

# Cohere 메인 승격(2026-05-27): 모델은 본 프로젝트 안 models/ 에서 자체 호스팅.
# samples 는 아직 archive(lb-note-archive/samples) 와 공유 중 — 후속 정리 시 이전 예정.
_ARCHIVE_PROJECT = Path("/home/evan/Claude_workspace/lb-note-archive")
SAMPLES_DIR = Path(os.getenv("SAMPLES_DIR", str(_ARCHIVE_PROJECT / "samples")))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(PROJECT_ROOT / "output")))

STT_BACKEND = os.getenv("STT_BACKEND", "cohere")
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "Korean")

COHERE_MODEL_PATH = Path(
    os.getenv(
        "COHERE_MODEL_PATH",
        str(PROJECT_ROOT / "models" / "cohere-transcribe-03-2026"),
    )
)
COHERE_DTYPE = os.getenv("COHERE_DTYPE", "bfloat16")
COHERE_QUANTIZATION = os.getenv("COHERE_QUANTIZATION", "")

HF_TOKEN = os.getenv("HF_TOKEN") or None


def env_status() -> dict:
    return {
        "stt_backend": STT_BACKEND,
        "stt_language": STT_LANGUAGE,
        "hf_token_set": HF_TOKEN is not None,
        "samples_dir_exists": SAMPLES_DIR.exists(),
        "cohere_model_exists": COHERE_MODEL_PATH.exists(),
    }


def assert_cuda_or_raise() -> str:
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA 미가용 — WSL2 GPU passthrough 확인 필요")
    return torch.cuda.get_device_name(0)
