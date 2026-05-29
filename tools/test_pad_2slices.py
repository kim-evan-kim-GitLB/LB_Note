"""de-risk / 진단: 지정한 슬라이스 인덱스들만 순서대로 generate.

사용:
  uv run python tools/test_pad_2slices.py            # 기본 0,1
  uv run python tools/test_pad_2slices.py 1          # 슬라이스 1 단독 (state vs content 판별)
  uv run python tools/test_pad_2slices.py 0 1 2      # 0,1,2 순서대로

원래 크래시는 2번째 호출(슬라이스 index 1)에서 발생.
- "1" 단독 통과 → 호출 간 누적 상태가 원인
- "1" 단독 크래시 → 슬라이스 1 콘텐츠/shape 자체가 원인
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.audio_io import load_audio  # noqa: E402
from src.stt import get_backend  # noqa: E402
from tools.run_10m_slice import (  # noqa: E402
    MAX_NEW_TOKENS,
    OVERLAP_SEC,
    SLICE_SEC,
    slice_audio,
    transcribe_slice,
)

AUDIO = Path("samples/ax과제회의(클로바노트)_음성파일.m4a")


def main() -> int:
    indices = [int(a) for a in sys.argv[1:]] or [0, 1]
    print(f"[test] MAX_NEW_TOKENS={MAX_NEW_TOKENS} indices={indices}")
    samples, sr = load_audio(AUDIO)
    slices = slice_audio(samples, sr, SLICE_SEC, OVERLAP_SEC)
    print(f"[test] n_slices={len(slices)}")

    backend = get_backend("cohere")
    backend.load()
    print("[test] model loaded", flush=True)

    try:
        for call_n, i in enumerate(indices):
            start, end, sl = slices[i]
            t = time.perf_counter()
            text = transcribe_slice(backend, sl, sr)
            dt = time.perf_counter() - t
            print(f"\n[test] ✅ call#{call_n} slice {i} {start:.0f}-{end:.0f}s PASSED "
                  f"({dt:.1f}s, text_len={len(text)})", flush=True)
            print(f"[test]   head: {text[:160]}")
            print(f"[test]   tail: {text[-160:]}", flush=True)
    finally:
        try:
            backend.unload()
        except Exception as e:
            print(f"[test] unload 중 예외(무시): {e}")

    print(f"\n[test] ✅✅ 지정 슬라이스 {indices} 모두 통과 — assert 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
