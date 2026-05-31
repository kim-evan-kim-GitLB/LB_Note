"""[음성] → [변환] → [STT] → [text.json] 통합 파이프라인.

CLI: run.py 에서 호출. 단일 entrypoint = `run_pipeline()`.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

from src import config
from src.audio_io import duration_seconds, load_audio
from src.chunker import (
    DEFAULT_CHUNK_SEC,
    DEFAULT_OVERLAP_SEC,
    chunk_audio,
    merge_segments,
)
from src.preprocess import PreprocessResult, preprocess, remap_time
from src.stt import get_backend, get_enhancer, get_vad
from src.types import Segment

SCHEMA_VERSION = "1.1"


def _build_payload(
    audio_path: Path,
    duration: float,
    backend_name: str,
    backend_quant: str,
    n_chunks: int,
    chunk_sec: float,
    overlap_sec: float,
    elapsed: float,
    vram_peak: int | None,
    merged: list[Segment],
    transcript: str,
    pre: PreprocessResult | None = None,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "audio": {
            "source_path": str(audio_path),
            "duration_seconds": round(duration, 3),
            "sample_rate_normalized": 16000,
            "channels_normalized": 1,
        },
        "model": {
            "backend": backend_name,
            "name": Path(str(config.COHERE_MODEL_PATH)).name,
            "quantization": backend_quant or "bf16",
        },
        "preprocess": {
            "applied": pre.applied if pre else [],
            "n_speech_regions": len(pre.speech_regions) if pre else 0,
            "compressed_seconds": pre.compressed_sec if pre else round(duration, 3),
        },
        "pipeline": {
            "chunk_seconds": chunk_sec,
            "chunk_overlap_seconds": overlap_sec,
            "n_chunks": n_chunks,
        },
        "performance": {
            "elapsed_seconds": round(elapsed, 2),
            "rtfx": round(duration / elapsed, 2) if elapsed > 0 else None,
            "vram_peak_mb": vram_peak,
        },
        "segments": [
            {
                "start": round(s.start, 2),
                "end": round(s.end, 2),
                "text": s.text,
                **({"speaker": s.speaker} if s.speaker else {}),
            }
            for s in merged
        ],
        "transcript": transcript,
    }


def run_pipeline(
    audio_path: Path,
    reference_path: Path | None = None,
    out_dir: Path | None = None,
    chunk_sec: float = DEFAULT_CHUNK_SEC,
    overlap_sec: float = DEFAULT_OVERLAP_SEC,
    backend_name: str = "cohere",
    language: str | None = None,
    enhancers: list[str] | None = None,
    vad: str | None = None,
) -> dict:
    out_dir = Path(out_dir) if out_dir else config.OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    language = language or config.STT_LANGUAGE
    audio_path = Path(audio_path)

    print(f"[pipeline] load audio: {audio_path}")
    samples, sr = load_audio(audio_path)
    duration = duration_seconds(samples, sr)
    print(f"[pipeline] duration={duration:.2f}s sr={sr}")

    # 프론트엔드 전처리 (opt-in). 기본값이면 no-op → 기존 동작 보존.
    enhancers = enhancers if enhancers is not None else config.parse_enhancers(config.ENHANCERS)
    vad = vad if vad is not None else (config.VAD_BACKEND or None)
    pre = preprocess(
        samples, sr,
        enhancers=[get_enhancer(n) for n in enhancers],
        vad=get_vad(vad),
        vad_pad_sec=config.VAD_PAD_SEC,
        vad_max_silence_sec=config.VAD_MAX_SILENCE_SEC,
    )
    if pre.applied:
        print(f"[pipeline] preprocess applied={pre.applied} "
              f"{pre.original_sec:.1f}s→{pre.compressed_sec:.1f}s "
              f"speech_regions={len(pre.speech_regions)}")
    proc_samples = pre.samples

    chunks = chunk_audio(proc_samples, sr=sr, chunk_sec=chunk_sec, overlap_sec=overlap_sec)
    print(f"[pipeline] n_chunks={len(chunks)} (chunk={chunk_sec}s, overlap={overlap_sec}s)")

    config.assert_cuda_or_raise()
    backend = get_backend(backend_name)
    backend.load()
    print(f"[pipeline] {backend_name} loaded")

    raw_segments: list[Segment] = []
    t0 = time.perf_counter()
    try:
        for ch in chunks:
            t_chunk = time.perf_counter()
            segs = backend.transcribe_array(
                ch.samples, sr=sr, start_offset=ch.start_sec, language=language
            )
            raw_segments.extend(segs)
            dt_chunk = time.perf_counter() - t_chunk
            print(
                f"[pipeline] chunk {ch.index + 1}/{len(chunks)} "
                f"({ch.start_sec:.1f}-{ch.end_sec:.1f}s) {dt_chunk:.1f}s"
            )
        elapsed = time.perf_counter() - t0
        vram_peak = backend.vram_peak_mb()
    finally:
        backend.unload()

    merged = merge_segments(raw_segments)
    # VAD 무음압축 시 청크 타임스탬프는 압축 타임라인 → 원본 타임라인으로 remap
    # (offset_map 항등이면 no-op). 향후 diarization 정렬 호환.
    merged = [
        Segment(
            start=round(remap_time(pre.offset_map, s.start), 2),
            end=round(remap_time(pre.offset_map, s.end), 2),
            text=s.text, confidence=s.confidence, speaker=s.speaker, meta=s.meta,
        )
        for s in merged
    ]
    transcript = " ".join(s.text for s in merged if s.text).strip()

    payload = _build_payload(
        audio_path=audio_path,
        duration=duration,
        backend_name=backend_name,
        backend_quant=getattr(backend, "quantization", ""),
        n_chunks=len(chunks),
        chunk_sec=chunk_sec,
        overlap_sec=overlap_sec,
        elapsed=elapsed,
        vram_peak=vram_peak,
        merged=merged,
        transcript=transcript,
        pre=pre,
    )

    if reference_path:
        from src import scoring
        eval_result = scoring.evaluate(transcript, Path(reference_path))
        payload["evaluation"] = eval_result

    stem = audio_path.stem
    json_path = out_dir / f"text-{stem}.json"
    md_path = out_dir / f"transcript-{stem}.md"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(
        f"# {backend_name} — {audio_path.name}\n"
        f"- duration: {duration:.2f}s\n"
        f"- elapsed: {elapsed:.2f}s\n"
        f"- rtfx: {payload['performance']['rtfx']}\n"
        f"- vram_peak: {vram_peak} MB\n"
        f"- chunks: {len(chunks)} (chunk={chunk_sec}s, overlap={overlap_sec}s)\n\n"
        f"## Transcript\n\n{transcript}\n",
        encoding="utf-8",
    )

    if "evaluation" in payload:
        score_path = out_dir / f"score-{stem}.md"
        ev = payload["evaluation"]
        score_path.write_text(
            f"# Score — {audio_path.name}\n\n"
            f"- reference: `{ev['reference_path']}` ({ev['ref_source']})\n"
            f"- duration: {duration:.2f}s, elapsed: {elapsed:.2f}s\n\n"
            f"| Metric | Value |\n|---|---|\n"
            f"| WER ↓ | {ev['wer']:.3f} |\n"
            f"| CER ↓ | {ev['cer']:.3f} |\n"
            f"| RTFx ↑ | {payload['performance']['rtfx']} |\n"
            f"| VRAM peak | {vram_peak} MB |\n",
            encoding="utf-8",
        )
        print(f"[pipeline] score → {score_path}")

    print(f"[pipeline] text.json → {json_path}")
    print(f"[pipeline] transcript.md → {md_path}")
    return payload


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Cohere STT 통합 파이프라인")
    ap.add_argument("audio", type=Path)
    ap.add_argument("--reference", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--chunk-sec", type=float, default=DEFAULT_CHUNK_SEC)
    ap.add_argument("--overlap-sec", type=float, default=DEFAULT_OVERLAP_SEC)
    ap.add_argument("--language", default=config.STT_LANGUAGE)
    ap.add_argument("--dereverb", action="store_true", help="WPE dereverb 적용")
    ap.add_argument("--denoise", action="store_true", help="GTCRN denoise 적용")
    ap.add_argument("--vad", action="store_true", help="Silero VAD 무음압축 적용")
    args = ap.parse_args()

    if not args.audio.exists():
        print(f"입력 파일 없음: {args.audio}", file=sys.stderr)
        return 2

    enhancers = [n for n, on in (("wpe", args.dereverb), ("gtcrn", args.denoise)) if on]

    run_pipeline(
        audio_path=args.audio,
        reference_path=args.reference,
        out_dir=args.out,
        chunk_sec=args.chunk_sec,
        overlap_sec=args.overlap_sec,
        language=args.language,
        enhancers=enhancers,
        vad="silero" if args.vad else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
