"""SAMPLES_DIR 의 모든 wav 를 Cohere 백엔드로 처리, output/ 에 결과 저장."""
from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path

from src import config
from src.config import OUTPUT_DIR, SAMPLES_DIR, assert_cuda_or_raise
from src.stt import get_backend


def _slug(p: Path) -> str:
    return p.stem.replace(" ", "_")


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    backend_name = "cohere"

    wavs = sorted(SAMPLES_DIR.glob("*.wav"))
    if not wavs:
        print(f"[evaluate] {SAMPLES_DIR}/*.wav 없음", file=sys.stderr)
        return 1

    gpu = assert_cuda_or_raise()
    print(f"[evaluate] backend={backend_name} | gpu={gpu} | wavs={len(wavs)}")

    backend = get_backend(backend_name)
    backend.load()
    print(f"[evaluate] {backend_name} loaded (quantization={backend.quantization or 'bf16'})")

    summary = [
        f"# Evaluate Report — {backend_name} @ {dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- GPU: {gpu}",
        f"- backend: {backend_name}",
        f"- quantization: {backend.quantization or 'bf16'}",
        f"- samples: {len(wavs)}",
        "",
    ]

    try:
        for wav in wavs:
            print(f"[evaluate] -> {wav.name}")
            t0 = time.perf_counter()
            segments = backend.transcribe(wav, language=config.STT_LANGUAGE)
            elapsed = time.perf_counter() - t0
            text = " ".join(s.text for s in segments)
            vram = backend.vram_peak_mb()

            out = OUTPUT_DIR / f"evaluate-{backend_name}-{_slug(wav)}.md"
            out.write_text(
                f"# {backend_name} — {wav.name}\n"
                f"- elapsed: {elapsed:.2f}s\n"
                f"- vram_peak: {vram} MB\n"
                f"- text_len: {len(text)}\n\n"
                f"## Transcript\n\n{text}\n",
                encoding="utf-8",
            )
            print(f"[evaluate]    {elapsed:.1f}s | {vram} MB | {len(text)} chars → {out.name}")
            summary += [
                f"## {wav.name}",
                f"- elapsed: **{elapsed:.2f}s**",
                f"- vram_peak: **{vram} MB**",
                f"- text_len: {len(text)}",
                f"- file: `{out.name}`",
                "",
            ]
    finally:
        backend.unload()

    out_summary = OUTPUT_DIR / f"evaluate-{backend_name}-summary.md"
    out_summary.write_text("\n".join(summary), encoding="utf-8")
    print(f"[evaluate] summary → {out_summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
