"""장시간 음성 파일을 10분 슬라이스로 Cohere transcribe 수행.

지원 입력: .wav, .m4a, .mp3, .aac, .amr (audio_io.SUPPORTED_EXTS 와 동일).
길이 한계: 180분 (audio_io.MAX_DURATION_SEC).
WER/CER 평가 없음. transcript + elapsed + VRAM 만 출력/저장.

사용:
  uv run python tools/run_long_slice10m.py "samples/foo.m4a"
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch  # noqa: E402

from src.audio_io import duration_seconds, load_audio  # noqa: E402
from src.chunker import merge_segments  # noqa: E402
from src.stt import get_backend  # noqa: E402
from src.types import Segment  # noqa: E402
from tools.run_10m_slice import (  # noqa: E402
    MAX_NEW_TOKENS,
    OVERLAP_SEC,
    SLICE_SEC,
    slice_audio,
    transcribe_slice,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", type=Path, help="입력 파일 (wav/mp3/mp4/m4a/etc.)")
    ap.add_argument("--out", type=Path, default=Path("output"), help="출력 디렉토리")
    ap.add_argument("--language", default="ko")
    args = ap.parse_args()

    args.out.mkdir(exist_ok=True)

    print(f"[slice10m] loading audio: {args.audio}")
    t_load = time.perf_counter()
    samples, sr = load_audio(args.audio)
    load_elapsed = time.perf_counter() - t_load
    duration = duration_seconds(samples, sr)
    print(f"[slice10m] audio_load={load_elapsed:.1f}s duration={duration:.2f}s sr={sr}")

    slices = slice_audio(samples, sr, SLICE_SEC, OVERLAP_SEC)
    print(f"[slice10m] n_slices={len(slices)} (slice={SLICE_SEC}s overlap={OVERLAP_SEC}s)")

    backend = get_backend("cohere")
    print("[slice10m] loading model...")
    t_model = time.perf_counter()
    backend.load()
    model_load_elapsed = time.perf_counter() - t_model
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()
    print(f"[slice10m] model_load={model_load_elapsed:.1f}s")

    raw_segments = []
    t0 = time.perf_counter()
    try:
        for i, (start, end, sl) in enumerate(slices):
            t_s = time.perf_counter()
            text = transcribe_slice(backend, sl, sr, language=args.language)
            raw_segments.append(Segment(start=start, end=end, text=text))
            print(
                f"[slice10m] {i + 1}/{len(slices)} {start:.1f}-{end:.1f}s "
                f"text_len={len(text)} {time.perf_counter() - t_s:.1f}s",
                flush=True,
            )
            (args.out / f"partial-{args.audio.stem.replace(' ', '_')}_slice10m.txt").write_text(
                "\n\n".join(f"[{int(s.start)}-{int(s.end)}s]\n{s.text}" for s in raw_segments),
                encoding="utf-8",
            )
        elapsed = time.perf_counter() - t0
        vram = (
            torch.cuda.max_memory_allocated() // (1024 * 1024)
            if torch.cuda.is_available()
            else None
        )
    finally:
        backend.unload()

    merged = merge_segments(raw_segments)
    transcript = " ".join(s.text for s in merged if s.text).strip()
    rtfx = duration / elapsed if elapsed > 0 else None
    print(
        f"[slice10m] elapsed={elapsed:.1f}s rtfx={rtfx:.2f} vram_peak={vram}MB "
        f"text_len={len(transcript)}"
    )

    stem = args.audio.stem.replace(" ", "_")
    payload = {
        "schema_version": "1.0",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": "slice_10m_no_eval",
        "audio": {
            "source_path": str(args.audio),
            "duration_seconds": round(duration, 2),
            "audio_load_seconds": round(load_elapsed, 2),
        },
        "pipeline": {
            "slice_sec": SLICE_SEC,
            "overlap_sec": OVERLAP_SEC,
            "n_slices": len(slices),
            "max_new_tokens": MAX_NEW_TOKENS,
        },
        "performance": {
            "model_load_seconds": round(model_load_elapsed, 2),
            "elapsed_seconds": round(elapsed, 2),
            "rtfx": round(rtfx, 2) if rtfx else None,
            "vram_peak_mb": vram,
        },
        "segments": [
            {"start": round(s.start, 2), "end": round(s.end, 2), "text_len": len(s.text)}
            for s in merged
        ],
        "transcript": transcript,
    }
    out_json = args.out / f"text-{stem}_slice10m.json"
    out_md = args.out / f"transcript-{stem}_slice10m.md"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(transcript + "\n", encoding="utf-8")
    print(f"[slice10m] saved: {out_json}")
    print(f"[slice10m] saved: {out_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
