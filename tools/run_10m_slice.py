"""2시간 wav 를 10분 슬라이스(+5s overlap)로 잘라 학습된 머지 흐름 그대로 호출.

각 슬라이스 = 600s wav → processor 가 내부 청크 약 18개 자동 생성 → encoder batch 처리
→ model.generate(max_new_tokens=4096) → decode 가 audio_chunk_index 활용해 학습된 머지.
외부 슬라이스 경계는 11개만 발생.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import torch  # noqa: E402

from src import scoring  # noqa: E402
from src.audio_io import duration_seconds, load_audio  # noqa: E402
from src.chunker import merge_segments  # noqa: E402
from src.stt import get_backend  # noqa: E402
from src.types import Segment  # noqa: E402

SLICE_SEC = 600.0
OVERLAP_SEC = 5.0
# 디코더 position embedding 테이블 = config.transf_decoder.max_sequence_length = 1024행.
# generate 는 내부 30초 청크(batch) 단위로 독립 생성하며, 정상 발화는 수백 토큰이면 충분.
# 과거 4096 은 모델 설계 한계(1024)의 4배라, 반복 루프에 빠진 청크가 1024 를 넘기면
# pos_emb 인덱스 초과 → device-side assert (modeling_cohere_asr.py:387) 로 크래시했다.
# 프롬프트 prefix 여유를 두고 1024 미만으로 cap → 크래시 차단 + 정상 출력 영향 없음.
MAX_NEW_TOKENS = 1000
# 반복 hallucination(무음/저정보 구간에서 greedy 디코더가 한 토큰에 갇힘) 억제.
# A/B 검증(tools/test_rep_penalty.py): 최악 반복 구간(97%) → 0%, 정상 발화 보존.
# 1.3+no_repeat_ngram_size 는 이름 garbling 증가 + 정당한 짧은 반복 손상 위험이라 1.2 단독 채택.
REPETITION_PENALTY = 1.2
ANSWER_JSON = Path("/home/evan/Claude_workspace/lb-note-archive/samples/ko_office_answer.json")
SOURCE_DUR = 182.091
GAP = 0.5
UNIT = SOURCE_DUR + GAP


def slice_audio(audio: np.ndarray, sr: int, slice_sec: float, overlap_sec: float):
    total = len(audio)
    slice_n = int(slice_sec * sr)
    step_n = int((slice_sec - overlap_sec) * sr)
    if total <= slice_n:
        return [(0.0, total / sr, audio)]
    out = []
    start = 0
    while start < total:
        end = min(start + slice_n, total)
        out.append((start / sr, end / sr, audio[start:end]))
        if end >= total:
            break
        start += step_n
    return out


def transcribe_slice(backend, samples, sr, language="ko"):
    inputs = backend._processor(samples, sampling_rate=sr, return_tensors="pt", language=language)
    aci = inputs.get("audio_chunk_index")
    inputs = inputs.to(backend._model.device, dtype=backend._model.dtype)
    with torch.inference_mode():
        outputs = backend._model.generate(
            **inputs, max_new_tokens=MAX_NEW_TOKENS, repetition_penalty=REPETITION_PENALTY
        )
    text = backend._processor.decode(
        outputs, skip_special_tokens=True, audio_chunk_index=aci, language=language
    )
    if isinstance(text, list):
        text = text[0] if text else ""
    result = text.strip()
    del inputs, outputs, aci
    if torch.cuda.is_available():
        torch.cuda.synchronize()
        torch.cuda.empty_cache()
    import gc
    gc.collect()
    return result


def build_accurate_reference(duration: float) -> str:
    ref_data = json.loads(ANSWER_JSON.read_text(encoding="utf-8"))
    dialogs = sorted(ref_data["Dialogs"], key=lambda x: x["DialogNum"])
    full_text = " ".join(scoring.normalize(d["Speakertext"]) for d in dialogs)
    n_full = int(duration // UNIT)
    last_start = n_full * UNIT
    last_wav_in = min(duration - last_start, SOURCE_DUR)
    parts = [full_text] * n_full
    partial = []
    for d in dialogs:
        st, en = float(d["StartTime"]), float(d["EndTime"])
        text = scoring.normalize(d["Speakertext"])
        if not text:
            continue
        if en <= last_wav_in:
            partial.append(text)
        elif st >= last_wav_in:
            break
        else:
            ratio = (last_wav_in - st) / (en - st)
            n_chars = max(0, int(round(len(text) * ratio)))
            if n_chars > 0:
                partial.append(text[:n_chars])
    if partial:
        parts.append(" ".join(partial))
    return " ".join(parts)


def main() -> int:
    audio_path = Path("samples/long_synth_120m.wav")
    out_dir = Path("output")
    out_dir.mkdir(exist_ok=True)

    samples, sr = load_audio(audio_path)
    duration = duration_seconds(samples, sr)
    print(f"[slice10m] audio={duration:.2f}s sr={sr}")

    slices = slice_audio(samples, sr, SLICE_SEC, OVERLAP_SEC)
    print(f"[slice10m] n_slices={len(slices)} (slice={SLICE_SEC}s overlap={OVERLAP_SEC}s)")

    backend = get_backend("cohere")
    backend.load()
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()

    raw_segments = []
    t0 = time.perf_counter()
    try:
        for i, (start, end, sl) in enumerate(slices):
            t_s = time.perf_counter()
            text = transcribe_slice(backend, sl, sr)
            raw_segments.append(Segment(start=start, end=end, text=text))
            print(
                f"[slice10m] {i + 1}/{len(slices)} {start:.1f}-{end:.1f}s "
                f"text_len={len(text)} {time.perf_counter() - t_s:.1f}s"
            )
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
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
    print(f"[slice10m] elapsed={elapsed:.1f}s rtfx={rtfx:.2f} vram={vram}MB text_len={len(transcript)}")

    ref_accurate = build_accurate_reference(duration)
    hyp = scoring.normalize(transcript)
    wer = scoring.wer(ref_accurate, hyp)
    cer = scoring.cer(ref_accurate, hyp)
    print(f"[slice10m] WER={wer:.4f}, CER={cer:.4f} (timestamp-accurate ref)")

    payload = {
        "schema_version": "1.0",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": "slice_10m",
        "audio": {"source_path": str(audio_path), "duration_seconds": round(duration, 2)},
        "pipeline": {
            "slice_sec": SLICE_SEC,
            "overlap_sec": OVERLAP_SEC,
            "n_slices": len(slices),
            "max_new_tokens": MAX_NEW_TOKENS,
        },
        "performance": {
            "elapsed_seconds": round(elapsed, 2),
            "rtfx": round(rtfx, 2) if rtfx else None,
            "vram_peak_mb": vram,
        },
        "segments": [
            {"start": round(s.start, 2), "end": round(s.end, 2), "text_len": len(s.text)}
            for s in merged
        ],
        "transcript": transcript,
        "evaluation": {
            "ref_source": "timestamp_accurate",
            "ref_tokens": len(ref_accurate.split()),
            "hyp_tokens": len(hyp.split()),
            "wer": round(wer, 4),
            "cer": round(cer, 4),
        },
    }
    out = out_dir / "text-long_synth_120m_slice10m.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[slice10m] saved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
