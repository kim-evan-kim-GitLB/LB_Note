"""Cohere 단독 적재 검증 — 30초만 transcribe 해서 적재·CUDA·VRAM 확인."""
from __future__ import annotations

import datetime as dt
import sys
import time
import traceback
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

from src import config
from src.config import OUTPUT_DIR, SAMPLES_DIR, assert_cuda_or_raise, env_status
from src.stt import get_backend


def _ensure_tiny(sample_wavs: list[Path]) -> Path:
    """첫 wav 의 앞 30초를 _smoke_tiny.wav 로 잘라낸다."""
    tiny = OUTPUT_DIR / "_smoke_tiny.wav"
    if tiny.exists():
        return tiny
    src = sample_wavs[0]
    wav, sr = librosa.load(str(src), sr=16000, mono=True, duration=30.0)
    sf.write(str(tiny), wav.astype(np.float32), 16000, subtype="PCM_16")
    return tiny


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    report = OUTPUT_DIR / f"_smoke-cohere-{ts}.md"
    lines = [f"# Cohere Smoke — {ts}", ""]

    try:
        gpu = assert_cuda_or_raise()
        lines.append(f"- CUDA: True ({gpu})")
    except Exception as e:
        lines.append(f"- CUDA: **FAIL** ({e})")
        report.write_text("\n".join(lines), encoding="utf-8")
        print(report)
        return 1

    lines += ["", "## env", *(f"- {k}: {v}" for k, v in env_status().items())]

    wavs = sorted(SAMPLES_DIR.glob("*.wav"))
    if not wavs:
        lines += ["", f"**{SAMPLES_DIR}/*.wav 없음 — 메인 프로젝트의 samples/ 확인**"]
        report.write_text("\n".join(lines), encoding="utf-8")
        print(report)
        return 1

    tiny = _ensure_tiny(wavs)
    lines += ["", f"- tiny clip: `{tiny.name}` (30s from {wavs[0].name})"]

    backend = get_backend("cohere")
    t0 = time.perf_counter()
    status = "FAIL"
    err: str | None = None
    text_len = 0
    vram = None
    try:
        backend.load()
        segments = backend.transcribe(tiny, language=config.STT_LANGUAGE)
        text_len = sum(len(s.text) for s in segments)
        vram = backend.vram_peak_mb()
        status = "PASS"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    finally:
        backend.unload()

    elapsed = round(time.perf_counter() - t0, 2)
    lines += [
        "",
        "## cohere (30s clip)",
        f"- status: **{status}**",
        f"- quantization: {backend.quantization or 'bf16'}",
        f"- elapsed: {elapsed}s",
        f"- vram_peak: {vram} MB",
        f"- text_len: {text_len}",
    ]
    if err:
        lines.append(f"- error: `{err}`")

    report.write_text("\n".join(lines), encoding="utf-8")
    print(report)
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
