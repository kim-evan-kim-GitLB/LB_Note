"""ax 과제회의(클로바노트) m4a — 슬라이스/청킹 없이 단일 호출 STT + 평가.

기존 slice10m(9×600s 청크) 와 달리 전체 ~83분 음성을 한 번의 generate 로 처리.
모델 내부 audio_chunk_index 로 long-form 디코딩. 전처리(denoise/dereverb/VAD) 미적용.

Reference: answer/ax_tf_클로바.txt (Clova Note export, 화자 헤더 제거)
출력: output/text-ax_single_call.json, output/score-ax_single_call.md
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
from src.stt import get_backend  # noqa: E402

AUDIO = ROOT / "samples" / "ax과제회의(클로바노트)_음성파일.m4a"
REF_PATH = ROOT / "answer" / "ax_tf_클로바.txt"
OUT_JSON = ROOT / "output" / "text-ax_single_call.json"
OUT_SCORE = ROOT / "output" / "score-ax_single_call.md"

SPEAKER_HEADER_RE = re.compile(r"^참석자\s+\d+\s+\d{1,2}:\d{2}\s*$")
MIN_RUN = 5
MAX_NEW_TOKENS = 8192


def load_reference(path: Path) -> str:
    """Clova Note export 에서 화자 헤더(참석자 N MM:SS) 제거 후 정규화."""
    lines = path.read_text(encoding="utf-8").splitlines()
    utterances = [
        ln.strip() for ln in lines
        if ln.strip() and not SPEAKER_HEADER_RE.match(ln.strip())
    ]
    return scoring.normalize(" ".join(utterances))


def repetition_bursts(tokens: list[str], min_run: int) -> list[dict]:
    bursts: list[dict] = []
    n, i = len(tokens), 0
    while i < n:
        j = i
        while j < n and tokens[j] == tokens[i]:
            j += 1
        run = j - i
        if run >= min_run:
            bursts.append({"token": tokens[i], "run_length": run, "start_index": i})
        i = j
    return bursts


def main() -> int:
    if not AUDIO.exists():
        print(f"입력 음성 없음: {AUDIO}", file=sys.stderr)
        return 2
    if not REF_PATH.exists():
        print(f"reference 없음: {REF_PATH}", file=sys.stderr)
        return 2

    backend = get_backend("cohere")
    backend.load()
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()

    t0 = time.perf_counter()
    wav, _ = librosa.load(str(AUDIO), sr=16000, mono=True)
    load_t = time.perf_counter() - t0
    duration = len(wav) / 16000.0
    print(f"[single] load={load_t:.1f}s samples={len(wav)} duration={duration:.1f}s ({duration/60:.1f}분)")

    inputs = backend._processor(wav, sampling_rate=16000, return_tensors="pt", language="ko")
    aci = inputs.get("audio_chunk_index")
    if aci is None:
        aci_info = None
    elif isinstance(aci, list):
        aci_info = f"list(len={len(aci)})"
    elif hasattr(aci, "shape"):
        aci_info = f"tensor{tuple(aci.shape)}"
    else:
        aci_info = type(aci).__name__
    print(f"[single] audio_chunk_index: {aci_info}")
    inputs = inputs.to(backend._model.device, dtype=backend._model.dtype)

    gen_t0 = time.perf_counter()
    with torch.inference_mode():
        outputs = backend._model.generate(
            **inputs,
            max_new_tokens=MAX_NEW_TOKENS,
            repetition_penalty=backend.REPETITION_PENALTY,
        )
    gen_t = time.perf_counter() - gen_t0

    text = backend._processor.decode(
        outputs, skip_special_tokens=True, audio_chunk_index=aci, language="ko"
    )
    if isinstance(text, list):
        text = text[0] if text else ""
    text = text.strip()

    elapsed = time.perf_counter() - t0
    vram = torch.cuda.max_memory_allocated() // (1024 * 1024) if torch.cuda.is_available() else None
    backend.unload()

    print(f"[single] elapsed={elapsed:.1f}s (load={load_t:.1f}s generate={gen_t:.1f}s)")
    print(f"[single] vram_peak={vram} MB text_len={len(text)}")

    # --- 평가 ---
    ref = load_reference(REF_PATH)
    hyp = scoring.normalize(text)
    w = scoring.wer(ref, hyp)
    c = scoring.cer(ref, hyp)
    ref_tokens, hyp_tokens = ref.split(), hyp.split()
    n_tokens = len(hyp_tokens)
    ratio_len = (n_tokens / len(ref_tokens)) if ref_tokens else 0.0

    bursts = repetition_bursts(hyp_tokens, MIN_RUN)
    rep_token_count = sum(b["run_length"] for b in bursts)
    rep_ratio = rep_token_count / n_tokens if n_tokens else 0.0
    for b in bursts:
        b["approx_time_seconds"] = (b["start_index"] / n_tokens * duration) if n_tokens else 0.0
    top_bursts = sorted(bursts, key=lambda b: b["run_length"], reverse=True)[:10]

    rtfx = round(duration / elapsed, 2) if elapsed > 0 else None
    print(f"[single] WER={w:.4f} CER={c:.4f} rep_ratio={rep_ratio:.3f} bursts={len(bursts)} RTFx={rtfx}")

    payload = {
        "schema_version": "1.0",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": "single_call_no_slice",
        "audio": {
            "source_path": str(AUDIO.relative_to(ROOT)),
            "duration_seconds": round(duration, 2),
            "sample_rate_normalized": 16000,
            "channels_normalized": 1,
        },
        "model": {
            "backend": "cohere",
            "name": Path(str(config.COHERE_MODEL_PATH)).name,
            "quantization": getattr(backend, "quantization", "") or "bf16",
            "max_new_tokens": MAX_NEW_TOKENS,
            "repetition_penalty": backend.REPETITION_PENALTY,
        },
        "preprocess": {"applied": [], "note": "no denoise/dereverb/VAD"},
        "pipeline": {
            "mode": "single_call",
            "sliced": False,
            "audio_chunk_index": aci_info,
        },
        "performance": {
            "elapsed_seconds": round(elapsed, 2),
            "librosa_load_seconds": round(load_t, 2),
            "generate_seconds": round(gen_t, 2),
            "rtfx": rtfx,
            "vram_peak_mb": vram,
        },
        "evaluation": {
            "reference_path": str(REF_PATH.relative_to(ROOT)),
            "ref_source": "clova_note_txt (화자 헤더 제거)",
            "wer": round(w, 4),
            "cer": round(c, 4),
            "ref_tokens": len(ref_tokens),
            "hyp_tokens": n_tokens,
            "hyp_ref_token_ratio": round(ratio_len, 4),
            "repetition_burst_count": len(bursts),
            "repetition_tokens": rep_token_count,
            "repetition_ratio": round(rep_ratio, 4),
        },
        "transcript": text,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Score — ax 과제회의 음성 (단일 호출 / 슬라이스 없음)",
        "",
        f"- generated_at: {dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- audio: `{AUDIO.relative_to(ROOT)}`",
        f"- reference: `{REF_PATH.relative_to(ROOT)}` (Clova Note, 화자 헤더 제거)",
        "- mode: **single_call_no_slice** (청킹/슬라이스/전처리 없음)",
        f"- duration: {duration:.2f}s ({duration/60:.1f}분)",
        f"- elapsed: {elapsed:.2f}s (load={load_t:.2f}s, generate={gen_t:.2f}s)",
        f"- RTFx: {rtfx}",
        f"- VRAM peak: {vram} MB",
        f"- max_new_tokens: {MAX_NEW_TOKENS}, repetition_penalty: {backend.REPETITION_PENALTY}",
        "",
        "## 지표",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| WER ↓ | {w:.3f} |",
        f"| CER ↓ | {c:.3f} |",
        f"| ref tokens | {len(ref_tokens)} |",
        f"| hyp tokens | {n_tokens} |",
        f"| hyp/ref token ratio | {ratio_len:.3f} |",
        f"| repetition burst count (run≥{MIN_RUN}) | {len(bursts)} |",
        f"| repetition tokens | {rep_token_count} |",
        f"| **repetition_ratio** ↓ | **{rep_ratio:.3f}** |",
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
        "- reference 또한 Clova STT 결과이므로 ground truth 가 아님 → WER 절대값보다 패턴 신호로 해석.",
        "- 전처리(denoise/dereverb/VAD)·청킹 없이 전체 음성을 단일 generate 로 처리한 baseline.",
        "",
    ]
    OUT_SCORE.write_text("\n".join(lines), encoding="utf-8")
    print(f"[single] saved → {OUT_JSON.relative_to(ROOT)}, {OUT_SCORE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
