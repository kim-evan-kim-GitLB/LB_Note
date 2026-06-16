"""ax 과제회의 음성 — 10분 슬라이스(+5s overlap) STT + 평가.

baseline(single_call) / vad_chunk 와 같은 음원·reference 로 10분 슬라이스 모드 성능을 잰다.
각 600s 슬라이스 = processor 내부 청크 자동생성 → generate → decode(audio_chunk_index 머지),
슬라이스 간 5s overlap 은 merge_segments 로 dedupe. 외부 슬라이스 경계는 ~8개뿐.

음향 향상 없음 / 디코딩 greedy + rep_penalty=1.2 (다른 모드와 동일) → '슬라이스 길이' 효과만 비교.
출력: output/text-ax_slice10m.json, output/score-ax_slice10m.md
사용: sudo .venv/bin/python tools/slice10m_ax_clova.py
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import librosa  # noqa: E402
import torch  # noqa: E402

from src import config, scoring  # noqa: E402
from src.chunker import merge_segments  # noqa: E402
from src.stt import get_backend  # noqa: E402
from src.types import Segment  # noqa: E402
from tools.run_10m_slice import (  # noqa: E402
    OVERLAP_SEC,
    SLICE_SEC,
    slice_audio,
    transcribe_slice,
)

AUDIO = ROOT / "samples" / "ax과제회의(클로바노트)_음성파일.m4a"
REF_PATH = ROOT / "answer" / "ax_tf_클로바.txt"
OUT_JSON = ROOT / "output" / "text-ax_slice10m.json"
OUT_SCORE = ROOT / "output" / "score-ax_slice10m.md"
BASELINE_JSON = ROOT / "output" / "text-ax_single_call.json"

SPEAKER_HEADER_RE = re.compile(r"^참석자\s+\d+\s+\d{1,2}:\d{2}\s*$")
MIN_RUN = 5


def load_reference(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    utterances = [
        ln.strip() for ln in lines
        if ln.strip() and not SPEAKER_HEADER_RE.match(ln.strip())
    ]
    return scoring.normalize(" ".join(utterances))


def repetition_bursts(tokens: list[str], min_run: int) -> list[dict]:
    bursts, n, i = [], len(tokens), 0
    while i < n:
        j = i
        while j < n and tokens[j] == tokens[i]:
            j += 1
        if j - i >= min_run:
            bursts.append({"token": tokens[i], "run_length": j - i, "start_index": i})
        i = j
    return bursts


def main() -> int:
    if not AUDIO.exists() or not REF_PATH.exists():
        print("입력/reference 없음", file=sys.stderr)
        return 2

    backend = get_backend("cohere")
    backend.load()
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()

    t0 = time.perf_counter()
    wav, sr = librosa.load(str(AUDIO), sr=16000, mono=True)
    load_t = time.perf_counter() - t0
    duration = len(wav) / sr
    slices = slice_audio(wav, sr, SLICE_SEC, OVERLAP_SEC)
    print(f"[slice10m] duration={duration:.1f}s ({duration/60:.1f}분) "
          f"n_slices={len(slices)} (slice={SLICE_SEC}s overlap={OVERLAP_SEC}s)")

    raw_segments = []
    gen_t0 = time.perf_counter()
    for i, (s, e, sl) in enumerate(slices):
        ts = time.perf_counter()
        text = transcribe_slice(backend, sl, sr)
        raw_segments.append(Segment(start=s, end=e, text=text))
        print(f"[slice10m] {i+1}/{len(slices)} {s:.1f}-{e:.1f}s "
              f"len={len(text)} {time.perf_counter()-ts:.1f}s")
    gen_t = time.perf_counter() - gen_t0

    merged = merge_segments(raw_segments)
    transcript = " ".join(seg.text for seg in merged if seg.text).strip()
    elapsed = time.perf_counter() - t0
    vram = torch.cuda.max_memory_allocated() // (1024 * 1024) if torch.cuda.is_available() else None
    backend.unload()
    print(f"[slice10m] elapsed={elapsed:.1f}s (decode={gen_t:.1f}s) vram={vram}MB len={len(transcript)}")

    # --- 평가 ---
    ref = load_reference(REF_PATH)
    hyp = scoring.normalize(transcript)
    w, c = scoring.wer(ref, hyp), scoring.cer(ref, hyp)
    ref_tokens, hyp_tokens = ref.split(), hyp.split()
    n_tokens = len(hyp_tokens)
    ratio_len = (n_tokens / len(ref_tokens)) if ref_tokens else 0.0
    bursts = repetition_bursts(hyp_tokens, MIN_RUN)
    rep_token_count = sum(b["run_length"] for b in bursts)
    rep_ratio = rep_token_count / n_tokens if n_tokens else 0.0
    for b in bursts:
        b["approx_time_seconds"] = round((b["start_index"] / n_tokens * duration) if n_tokens else 0.0, 1)
    top_bursts = sorted(bursts, key=lambda b: b["run_length"], reverse=True)[:10]
    rtfx = round(duration / elapsed, 2) if elapsed > 0 else None
    print(f"[slice10m] WER={w:.4f} CER={c:.4f} rep_ratio={rep_ratio:.3f} "
          f"bursts={len(bursts)} RTFx={rtfx}")

    base = None
    if BASELINE_JSON.exists():
        try:
            base = json.loads(BASELINE_JSON.read_text(encoding="utf-8")).get("evaluation")
        except Exception:
            base = None

    payload = {
        "schema_version": "1.0",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": "slice_10m",
        "audio": {"source_path": str(AUDIO.relative_to(ROOT)), "duration_seconds": round(duration, 2)},
        "model": {"backend": "cohere", "name": Path(str(config.COHERE_MODEL_PATH)).name,
                  "repetition_penalty": backend.REPETITION_PENALTY},
        "pipeline": {"slice_sec": SLICE_SEC, "overlap_sec": OVERLAP_SEC, "n_slices": len(slices)},
        "performance": {"elapsed_seconds": round(elapsed, 2), "decode_seconds": round(gen_t, 2),
                        "rtfx": rtfx, "vram_peak_mb": vram},
        "evaluation": {
            "reference_path": str(REF_PATH.relative_to(ROOT)),
            "ref_source": "clova_note_txt (화자 헤더 제거)",
            "wer": round(w, 4), "cer": round(c, 4),
            "ref_tokens": len(ref_tokens), "hyp_tokens": n_tokens,
            "hyp_ref_token_ratio": round(ratio_len, 4),
            "repetition_burst_count": len(bursts), "repetition_tokens": rep_token_count,
            "repetition_ratio": round(rep_ratio, 4),
        },
        "baseline_single_call": base,
        "transcript": transcript,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def delta(cur, key):
        if not base or key not in base:
            return ""
        d = cur - base[key]
        sign = "개선" if d < 0 else ("악화" if d > 0 else "동일")
        return f" ({d:+.3f} {sign} vs baseline)"

    lines = [
        "# Score — ax 과제회의 음성 (10분 슬라이스)",
        "",
        f"- generated_at: {dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- audio: `{AUDIO.relative_to(ROOT)}`",
        f"- reference: `{REF_PATH.relative_to(ROOT)}` (Clova Note, 화자 헤더 제거)",
        f"- mode: **slice_10m** (slice={SLICE_SEC:.0f}s, overlap={OVERLAP_SEC:.0f}s, "
        f"n_slices={len(slices)})",
        "- 음향 향상: 없음 / 디코딩: greedy + rep_penalty 1.2 (다른 모드와 동일)",
        f"- duration: {duration:.2f}s ({duration/60:.1f}분)",
        f"- elapsed: {elapsed:.2f}s (decode={gen_t:.2f}s), RTFx: {rtfx}, VRAM peak: {vram} MB",
        "",
        "## 지표 (vs baseline = 전처리 없음 single_call)",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| WER ↓ | {w:.3f}{delta(w, 'wer')} |",
        f"| CER ↓ | {c:.3f}{delta(c, 'cer')} |",
        f"| ref tokens | {len(ref_tokens)} |",
        f"| hyp tokens | {n_tokens} |",
        f"| hyp/ref token ratio | {ratio_len:.3f} |",
        f"| repetition burst count (run≥{MIN_RUN}) | {len(bursts)} |",
        f"| **repetition_ratio** ↓ | **{rep_ratio:.3f}**{delta(rep_ratio, 'repetition_ratio')} |",
        "",
        "## Top repetition / hallucination bursts",
        "",
        "| # | token | run_length | approx_time (s) |",
        "|---|---|---|---|",
    ]
    for i, b in enumerate(top_bursts, start=1):
        lines.append(f"| {i} | `{b['token']}` | {b['run_length']} | {b['approx_time_seconds']:.0f} |")
    if not top_bursts:
        lines.append("| - | (none) | - | - |")
    lines += [
        "",
        "## 비고",
        "",
        "- reference 또한 Clova STT 결과이므로 ground truth 가 아님 → WER 절대값보다 Δ 신호로 해석.",
        "- 10분 슬라이스: 슬라이스당 모델 내부 청킹/머지를 그대로 활용, 외부 경계는 슬라이스 사이뿐.",
        "",
    ]
    OUT_SCORE.write_text("\n".join(lines), encoding="utf-8")
    print(f"[slice10m] saved → {OUT_JSON.relative_to(ROOT)}, {OUT_SCORE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
