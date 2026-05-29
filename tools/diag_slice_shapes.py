"""진단: m4a 각 10분 슬라이스의 processor 출력 shape 를 비교.

generate() 는 호출하지 않으므로 CUDA assert 없이 안전 (feature extraction = CPU).
가설: 슬라이스마다 attention_mask.shape[1] 이 다르면 SDPA mask invariant 위반의 원인 확정.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.audio_io import duration_seconds, load_audio  # noqa: E402
from src.stt import get_backend  # noqa: E402
from tools.run_10m_slice import OVERLAP_SEC, SLICE_SEC, slice_audio  # noqa: E402

AUDIO = Path("samples/ax과제회의(클로바노트)_음성파일.m4a")


def main() -> int:
    samples, sr = load_audio(AUDIO)
    print(f"[diag] duration={duration_seconds(samples, sr):.1f}s sr={sr}")

    slices = slice_audio(samples, sr, SLICE_SEC, OVERLAP_SEC)
    print(f"[diag] n_slices={len(slices)} (slice={SLICE_SEC}s overlap={OVERLAP_SEC}s)")

    backend = get_backend("cohere")
    # 모델 weight 로드 없이 processor 만 필요 → processor 직접 로드
    from transformers import AutoProcessor
    proc = AutoProcessor.from_pretrained(str(backend.model_path))

    rows = []
    for i, (start, end, sl) in enumerate(slices):
        inputs = proc(sl, sampling_rate=sr, return_tensors="pt", language="ko")
        am = inputs.get("attention_mask")
        feat = inputs.get("input_features")
        aci = inputs.get("audio_chunk_index")
        aci_len = (len(aci) if isinstance(aci, list) else
                   (tuple(aci.shape) if hasattr(aci, "shape") else None))
        am_shape = tuple(am.shape) if am is not None else None
        feat_shape = tuple(feat.shape) if feat is not None else None
        rows.append((i, start, end, am_shape, feat_shape, aci_len))
        print(f"[diag] slice {i} {start:.0f}-{end:.0f}s "
              f"attention_mask={am_shape} input_features={feat_shape} aci={aci_len}")

    seqlens = {r[3][1] for r in rows if r[3]}
    print(f"\n[diag] attention_mask seq_len 종류: {sorted(seqlens)}")
    print(f"[diag] 최대 seq_len: {max(seqlens) if seqlens else None}")
    if len(seqlens) > 1:
        print("[diag] ✅ 가설 확정: 슬라이스마다 seq_len 다름 → 고정 패딩 필요")
    else:
        print("[diag] ⚠️ 모든 슬라이스 seq_len 동일 → 가설과 다름, 재검토 필요")
    return 0


if __name__ == "__main__":
    sys.exit(main())
