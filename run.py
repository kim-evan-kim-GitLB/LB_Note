"""Cohere STT 진입점.

기본 모드(기존 호환): 단일 wav transcribe → stdout
파이프라인 모드: --pipeline 또는 --reference / --out 중 하나라도 지정 시
  → audio_io + chunker + scoring 까지 통합 처리, text.json + transcript.md 생성
  → 청킹 기본값은 vad_chunk(energy): VAD 발화 경계 분할 + 배치 디코딩 + seam dedup
    (검증: WER 0.417 / CER 0.299, VRAM 4GB, RTFx 232, 타임스탬프 지원).
    기존 고정 길이 청킹은 --segmentation fixed 로 사용(하위호환).
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
                    help="audio_io+chunker+scoring 통합 파이프라인(기본 청킹=vad_chunk(energy))")
    ap.add_argument("--reference", type=Path, default=None,
                    help="reference 지정 시 자동으로 파이프라인 모드")
    ap.add_argument("--out", type=Path, default=None,
                    help="출력 디렉토리. 지정 시 자동으로 파이프라인 모드")
    ap.add_argument("--chunk-sec", type=float, default=60.0)
    ap.add_argument("--overlap-sec", type=float, default=10.0)
    ap.add_argument("--dereverb", action="store_true", help="WPE dereverb 적용(파이프라인)")
    ap.add_argument("--denoise", action="store_true", help="GTCRN denoise 적용(파이프라인)")
    ap.add_argument("--vad", action="store_true", help="Silero VAD 무음압축 적용(파이프라인, 분할과 무관)")
    # --- VAD 분할 청킹(기본 vad_chunk(energy)) ---
    ap.add_argument("--segmentation", choices=["vad", "fixed"], default="vad",
                    help="청킹 방식: vad(VAD 분할, 기본) | fixed(고정 길이, 기존 동작)")
    ap.add_argument("--vad-backend", choices=["energy", "silero"], default="energy",
                    help="분할용 VAD 백엔드: energy(기본, ~15x 빠름) | silero")
    ap.add_argument("--batch-size", type=int, default=8,
                    help="VAD 분할 청크 배치 디코딩 크기(greedy 라 결과 동일, 가속용)")
    ap.add_argument("--target-sec", type=float, default=30.0,
                    help="VAD 분할 청크 목표 최대 길이(초). <35s 라야 내부 청커 재분할 없음")
    args = ap.parse_args()

    if not args.audio.exists():
        print(f"입력 파일 없음: {args.audio}", file=sys.stderr)
        return 2

    # 전처리 플래그가 있으면 자동으로 파이프라인 모드
    use_pipeline = (
        args.pipeline or args.reference is not None or args.out is not None
        or args.dereverb or args.denoise or args.vad
    )
    if use_pipeline:
        from src.pipeline import run_pipeline
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
            segmentation=args.segmentation,
            vad_backend=args.vad_backend,
            batch_size=args.batch_size,
            target_sec=args.target_sec,
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
