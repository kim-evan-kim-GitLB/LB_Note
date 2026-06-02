"""ax 과제회의(클로바노트) m4a — 음향 향상 적용 후 단일 호출 STT + 평가.

single_call_ax_clova.py(전처리 없는 baseline)와 동일 경로지만, generate 전에
src.preprocess 로 향상 파이프라인을 적용한다: dereverb(WPE) → denoise(GTCRN) → VAD(무음압축).
나머지(단일 generate, audio_chunk_index long-form 디코딩, 평가)는 baseline 과 동일하게 맞춰
WER/CER/repetition 을 1:1 비교할 수 있게 한다.

VAD 무음압축 시 처리 음성 길이가 줄어든다(타임라인 변형). RTFx 는 원본 길이 기준,
repetition burst 의 approx_time 은 offset_map 으로 압축→원본 시각 복원해 기록한다.

사용 예 (CLAUDE.md: venv python 은 sudo 필요):
    sudo .venv/bin/python tools/single_call_ax_clova_enhanced.py --enhancers wpe,gtcrn --vad silero

Reference: answer/ax_tf_클로바.txt (Clova Note export, 화자 헤더 제거)
출력: output/text-ax_single_call_enhanced.json, output/score-ax_single_call_enhanced.md
"""
from __future__ import annotations

import argparse
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
from src.preprocess import preprocess, remap_time  # noqa: E402
from src.stt import get_backend, get_enhancer, get_vad  # noqa: E402

AUDIO = ROOT / "samples" / "ax과제회의(클로바노트)_음성파일.m4a"
REF_PATH = ROOT / "answer" / "ax_tf_클로바.txt"
OUT_JSON = ROOT / "output" / "text-ax_single_call_enhanced.json"
OUT_SCORE = ROOT / "output" / "score-ax_single_call_enhanced.md"
BASELINE_JSON = ROOT / "output" / "text-ax_single_call.json"

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
    ap = argparse.ArgumentParser()
    ap.add_argument("--enhancers", default="wpe,gtcrn",
                    help="쉼표 구분 향상 순서. 예: 'wpe,gtcrn' | 'gtcrn' | '' (none)")
    ap.add_argument("--vad", default="silero",
                    help="VAD 백엔드. 'silero' | '' (off)")
    args = ap.parse_args()

    if not AUDIO.exists():
        print(f"입력 음성 없음: {AUDIO}", file=sys.stderr)
        return 2
    if not REF_PATH.exists():
        print(f"reference 없음: {REF_PATH}", file=sys.stderr)
        return 2

    enh_names = config.parse_enhancers(args.enhancers)
    vad_name = (args.vad or "").strip().lower() or None

    backend = get_backend("cohere")
    backend.load()
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()

    t0 = time.perf_counter()
    wav, _ = librosa.load(str(AUDIO), sr=16000, mono=True)
    load_t = time.perf_counter() - t0
    duration = len(wav) / 16000.0
    print(f"[enh] load={load_t:.1f}s samples={len(wav)} duration={duration:.1f}s ({duration/60:.1f}분)")

    # --- 향상 파이프라인 (dereverb → denoise → VAD 무음압축) ---
    pre_t0 = time.perf_counter()
    pre = preprocess(
        wav, sr=16000,
        enhancers=[get_enhancer(n) for n in enh_names],
        vad=get_vad(vad_name),
        vad_pad_sec=config.VAD_PAD_SEC,
        vad_max_silence_sec=config.VAD_MAX_SILENCE_SEC,
    )
    pre_t = time.perf_counter() - pre_t0
    proc_wav = pre.samples
    proc_dur = len(proc_wav) / 16000.0
    print(f"[enh] preprocess={pre_t:.1f}s applied={pre.applied} "
          f"orig={pre.original_sec:.1f}s comp={pre.compressed_sec:.1f}s")

    inputs = backend._processor(proc_wav, sampling_rate=16000, return_tensors="pt", language="ko")
    aci = inputs.get("audio_chunk_index")
    if aci is None:
        aci_info = None
    elif isinstance(aci, list):
        aci_info = f"list(len={len(aci)})"
    elif hasattr(aci, "shape"):
        aci_info = f"tensor{tuple(aci.shape)}"
    else:
        aci_info = type(aci).__name__
    print(f"[enh] audio_chunk_index: {aci_info}")
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

    print(f"[enh] elapsed={elapsed:.1f}s (load={load_t:.1f}s preprocess={pre_t:.1f}s generate={gen_t:.1f}s)")
    print(f"[enh] vram_peak={vram} MB text_len={len(text)}")

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
    # 토큰 인덱스 → 압축 타임라인 시각 → offset_map 으로 원본 시각 복원
    for b in bursts:
        comp_t = (b["start_index"] / n_tokens * proc_dur) if n_tokens else 0.0
        b["approx_time_seconds"] = round(remap_time(pre.offset_map, comp_t), 1)
    top_bursts = sorted(bursts, key=lambda b: b["run_length"], reverse=True)[:10]

    rtfx = round(duration / elapsed, 2) if elapsed > 0 else None
    print(f"[enh] WER={w:.4f} CER={c:.4f} rep_ratio={rep_ratio:.3f} bursts={len(bursts)} RTFx={rtfx}")

    # --- baseline(전처리 없음) 지표 로드 → Δ 비교 ---
    base = None
    if BASELINE_JSON.exists():
        try:
            base = json.loads(BASELINE_JSON.read_text(encoding="utf-8")).get("evaluation")
        except Exception:
            base = None

    payload = {
        "schema_version": "1.0",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": "single_call_enhanced",
        "audio": {
            "source_path": str(AUDIO.relative_to(ROOT)),
            "duration_seconds": round(duration, 2),
            "processed_seconds": round(proc_dur, 2),
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
        "preprocess": {
            "applied": pre.applied,
            "enhancers": enh_names,
            "vad": vad_name,
            "original_sec": pre.original_sec,
            "compressed_sec": pre.compressed_sec,
            "preprocess_seconds": round(pre_t, 2),
        },
        "pipeline": {
            "mode": "single_call",
            "sliced": False,
            "audio_chunk_index": aci_info,
        },
        "performance": {
            "elapsed_seconds": round(elapsed, 2),
            "librosa_load_seconds": round(load_t, 2),
            "preprocess_seconds": round(pre_t, 2),
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
        "baseline_no_preprocess": base,
        "transcript": text,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def delta(cur: float, key: str) -> str:
        if not base or key not in base:
            return ""
        d = cur - base[key]
        sign = "개선" if d < 0 else ("악화" if d > 0 else "동일")
        return f" ({d:+.3f} {sign} vs baseline)"

    lines = [
        "# Score — ax 과제회의 음성 (단일 호출 / 음향 향상 적용)",
        "",
        f"- generated_at: {dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- audio: `{AUDIO.relative_to(ROOT)}`",
        f"- reference: `{REF_PATH.relative_to(ROOT)}` (Clova Note, 화자 헤더 제거)",
        f"- mode: **single_call_enhanced** (향상 적용: {' → '.join(pre.applied) or 'none'})",
        f"- enhancers: `{','.join(enh_names) or 'none'}`, VAD: `{vad_name or 'off'}`",
        f"- duration: {duration:.2f}s ({duration/60:.1f}분) → 처리길이 {proc_dur:.2f}s ({proc_dur/60:.1f}분)",
        f"- elapsed: {elapsed:.2f}s (load={load_t:.2f}s, preprocess={pre_t:.2f}s, generate={gen_t:.2f}s)",
        f"- RTFx: {rtfx} (원본 길이 기준)",
        f"- VRAM peak: {vram} MB",
        f"- max_new_tokens: {MAX_NEW_TOKENS}, repetition_penalty: {backend.REPETITION_PENALTY}",
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
        f"| repetition tokens | {rep_token_count} |",
        f"| **repetition_ratio** ↓ | **{rep_ratio:.3f}**{delta(rep_ratio, 'repetition_ratio')} |",
        "",
        "## Top repetition / hallucination bursts",
        "",
        "| # | token | run_length | approx_time (s, 원본) |",
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
        "- reference 또한 Clova STT 결과이므로 ground truth 가 아님 → WER 절대값보다 패턴·Δ 신호로 해석.",
        f"- 향상 파이프라인: {' → '.join(pre.applied) or 'none'} 적용 후 전체 음성을 단일 generate 로 처리.",
        "- VAD 무음압축 적용 시 처리 길이가 단축됨(approx_time 은 원본 타임라인으로 복원).",
        "",
    ]
    OUT_SCORE.write_text("\n".join(lines), encoding="utf-8")
    print(f"[enh] saved → {OUT_JSON.relative_to(ROOT)}, {OUT_SCORE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
