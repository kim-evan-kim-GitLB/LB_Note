"""짧은 wav 를 반복 concat 해서 임의 길이 합성 음성 생성 (테스트용).

예:
  uv run python tools/synth_long_audio.py \
      --source /home/evan/Claude_workspace/lb-note-archive/samples/ko_office_noise_off.wav \
      --target-minutes 120 \
      --out samples/long_synth_120m.wav
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np


def synth(source: Path, target_seconds: float, gap_seconds: float, out: Path) -> dict:
    import soundfile as sf

    samples, sr = sf.read(str(source), dtype="float32", always_2d=False)
    if samples.ndim == 2:
        samples = samples.mean(axis=1).astype(np.float32)
    src_dur = len(samples) / sr

    gap = np.zeros(int(gap_seconds * sr), dtype=np.float32)
    chunk = np.concatenate([samples, gap]) if gap_seconds > 0 else samples
    chunk_dur = len(chunk) / sr

    n_repeat = math.ceil(target_seconds / chunk_dur)
    long = np.tile(chunk, n_repeat)
    target_len = int(target_seconds * sr)
    long = long[:target_len]

    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), long, sr, subtype="PCM_16")
    return {
        "source": str(source),
        "source_duration_s": round(src_dur, 2),
        "sr": sr,
        "repeats": n_repeat,
        "gap_s": gap_seconds,
        "target_duration_s": round(len(long) / sr, 2),
        "output_path": str(out),
        "file_size_mb": round(out.stat().st_size / (1024 * 1024), 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="짧은 wav 반복 concat 으로 합성 장시간 wav 생성")
    ap.add_argument("--source", type=Path, required=True, help="입력 짧은 wav")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--target-minutes", type=float, help="목표 분")
    g.add_argument("--target-seconds", type=float, help="목표 초")
    ap.add_argument("--gap-seconds", type=float, default=0.5,
                    help="반복 사이 무음 길이 (기본 0.5s)")
    ap.add_argument("--out", type=Path, required=True, help="출력 wav 경로")
    args = ap.parse_args()

    if not args.source.exists():
        print(f"입력 없음: {args.source}", file=sys.stderr)
        return 2

    target_s = args.target_seconds if args.target_seconds else args.target_minutes * 60.0
    info = synth(args.source, target_s, args.gap_seconds, args.out)
    for k, v in info.items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
