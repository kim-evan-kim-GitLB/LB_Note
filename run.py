"""Cohere STT 진입점.

기본 모드(기존 호환): 단일 wav transcribe → stdout
파이프라인 모드: --pipeline 또는 --reference / --out 중 하나라도 지정 시
  → audio_io + chunker + scoring 까지 통합 처리, text.json + transcript.md 생성
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src import config
from src.stt import get_backend


def main() -> int:
    ap = argparse.ArgumentParser(description="Cohere STT (단일 또는 파이프라인)")
    ap.add_argument("audio", type=Path)
    ap.add_argument("--language", default=config.STT_LANGUAGE)
    ap.add_argument("--pipeline", action="store_true",
                    help="audio_io+chunker+scoring 통합 파이프라인으로 처리")
    ap.add_argument("--reference", type=Path, default=None,
                    help="reference 지정 시 자동으로 파이프라인 모드")
    ap.add_argument("--out", type=Path, default=None,
                    help="출력 디렉토리. 지정 시 자동으로 파이프라인 모드")
    ap.add_argument("--chunk-sec", type=float, default=60.0)
    ap.add_argument("--overlap-sec", type=float, default=10.0)
    args = ap.parse_args()

    if not args.audio.exists():
        print(f"입력 파일 없음: {args.audio}", file=sys.stderr)
        return 2

    use_pipeline = args.pipeline or args.reference is not None or args.out is not None
    if use_pipeline:
        from src.pipeline import run_pipeline
        run_pipeline(
            audio_path=args.audio,
            reference_path=args.reference,
            out_dir=args.out,
            chunk_sec=args.chunk_sec,
            overlap_sec=args.overlap_sec,
            language=args.language,
        )
        return 0

    b = get_backend("cohere")
    b.load()
    try:
        for s in b.transcribe(args.audio, language=args.language):
            print(s.text)
    finally:
        b.unload()
    return 0


if __name__ == "__main__":
    sys.exit(main())
