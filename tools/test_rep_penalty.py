"""repetition_penalty A/B 검증.

두 구간 × 여러 generate 설정으로 transcribe 해서 반복 garbage 제거 + 정상발화 보존을 비교.
- 보존 검증: 20-80s (반복 0% 인 정상 발화 구간) — 설정 적용 후에도 본문이 유지되는가
- 제거 검증: 3100-3160s (반복 97% 인 'baseline 루프' 구간) — 반복이 사라지는가

generate() 만 호출(모델 1회 로드 후 설정별 반복). CUDA assert 무관.

사용:
  uv run python tools/test_rep_penalty.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch  # noqa: E402

from src.audio_io import load_audio  # noqa: E402
from src.stt import get_backend  # noqa: E402

AUDIO = Path("samples/ax과제회의(클로바노트)_음성파일.m4a")
WINDOWS = [(20, 80, "보존(정상발화)"), (3100, 3160, "제거(반복97%)")]
CONFIGS = [
    ("baseline", {}),
    ("rp1.2", {"repetition_penalty": 1.2}),
    ("rp1.3+norep3", {"repetition_penalty": 1.3, "no_repeat_ngram_size": 3}),
]
MAX_NEW_TOKENS = 512


def rep_rate(txt: str) -> float:
    toks = txt.split()
    def n(t): return t.strip(" ,.?!").strip()
    w = i = 0
    while i < len(toks):
        j = i; b = n(toks[i])
        if b and len(b) <= 3:
            while j + 1 < len(toks) and n(toks[j + 1]) == b:
                j += 1
        c = j - i + 1
        if c >= 4 and b:
            w += c
        i = j + 1
    return 100 * w / max(1, len(toks))


def transcribe(backend, samples, sr, gen_kwargs):
    inputs = backend._processor(samples, sampling_rate=sr, return_tensors="pt", language="ko")
    aci = inputs.get("audio_chunk_index")
    inputs = inputs.to(backend._model.device, dtype=backend._model.dtype)
    with torch.inference_mode():
        out = backend._model.generate(**inputs, max_new_tokens=MAX_NEW_TOKENS, **gen_kwargs)
    text = backend._processor.decode(out, skip_special_tokens=True, audio_chunk_index=aci, language="ko")
    if isinstance(text, list):
        text = text[0] if text else ""
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    return text.strip()


def main() -> int:
    samples, sr = load_audio(AUDIO)
    backend = get_backend("cohere")
    backend.load()
    print("[ab] model loaded\n", flush=True)
    try:
        for st, en, label in WINDOWS:
            clip = samples[int(st * sr):int(en * sr)]
            print(f"\n{'='*70}\n[{st}-{en}s] {label}\n{'='*70}", flush=True)
            for name, cfg in CONFIGS:
                t = time.perf_counter()
                text = transcribe(backend, clip, sr, cfg)
                dt = time.perf_counter() - t
                print(f"\n--- {name} ({dt:.0f}s, len={len(text)}, 반복={rep_rate(text):.0f}%) ---")
                print(f"  head: {text[:140]}")
                print(f"  mid : {text[len(text)//2:len(text)//2+140]}", flush=True)
    finally:
        backend.unload()
    return 0


if __name__ == "__main__":
    sys.exit(main())
