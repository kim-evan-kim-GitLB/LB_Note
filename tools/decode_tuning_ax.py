"""ax 회의 음성 — 디코딩 파라미터 A/B (vad_chunk 표준 위에서).

VAD 분할은 한 번만 수행하고, 동일 청크 집합을 여러 디코딩 설정으로 디코딩해
WER/CER/repetition 을 비교한다. 음향 향상 없음, 청킹 동일 → 변수는 '디코딩 설정' 하나뿐.

비교 설정:
  greedy_rp1.2        : 현재 표준(control). greedy + repetition_penalty=1.2
  greedy_rp1.2_nrng3  : + no_repeat_ngram_size=3 (구 단위 반복 차단)
  beam5_rp1.2         : beam search(num_beams=5) — 모호 구간 전역 최적
  beam5_rp1.2_nrng3   : beam5 + no_repeat_ngram_size=3

출력: output/score-ax_decode_tuning.md, output/text-ax_decode_tuning.json
사용: sudo .venv/bin/python tools/decode_tuning_ax.py
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import librosa  # noqa: E402
import torch  # noqa: E402

from src import scoring  # noqa: E402
from src.stt import get_backend, get_vad  # noqa: E402
from tools.vad_chunk_ax_clova import (  # noqa: E402
    AUDIO,
    MAX_NEW_TOKENS,
    OVERLAP_SEC,
    PAD_SEC,
    REF_PATH,
    TARGET_SEC,
    build_chunks,
    load_reference,
    merge_texts,
    repetition_bursts,
)

OUT_JSON = ROOT / "output" / "text-ax_decode_tuning.json"
OUT_SCORE = ROOT / "output" / "score-ax_decode_tuning.md"
BASELINE_JSON = ROOT / "output" / "text-ax_single_call.json"
MIN_RUN = 5

# (이름, generate kwargs). max_new_tokens 는 공통으로 덧붙임.
CONFIGS = [
    ("greedy_rp1.2", {"repetition_penalty": 1.2}),
    ("greedy_rp1.2_nrng3", {"repetition_penalty": 1.2, "no_repeat_ngram_size": 3}),
    ("beam5_rp1.2", {"repetition_penalty": 1.2, "num_beams": 5}),
    ("beam5_rp1.2_nrng3", {"repetition_penalty": 1.2, "num_beams": 5, "no_repeat_ngram_size": 3}),
]


def decode_chunk(backend, wav, gen_kwargs, lang="ko") -> str:
    inputs = backend._processor(wav, sampling_rate=16000, return_tensors="pt", language=lang)
    aci = inputs.get("audio_chunk_index")
    inputs = inputs.to(backend._model.device, dtype=backend._model.dtype)
    with torch.inference_mode():
        outputs = backend._model.generate(
            **inputs, max_new_tokens=MAX_NEW_TOKENS, **gen_kwargs
        )
    text = backend._processor.decode(
        outputs, skip_special_tokens=True, audio_chunk_index=aci, language=lang
    )
    if isinstance(text, list):
        text = text[0] if text else ""
    return text.strip()


def score(text: str, ref: str, duration: float) -> dict:
    hyp = scoring.normalize(text)
    w, c = scoring.wer(ref, hyp), scoring.cer(ref, hyp)
    ref_tok, hyp_tok = ref.split(), hyp.split()
    n = len(hyp_tok)
    bursts = repetition_bursts(hyp_tok, MIN_RUN)
    rep = sum(b["run_length"] for b in bursts)
    return {
        "wer": round(w, 4), "cer": round(c, 4),
        "hyp_tokens": n, "ref_tokens": len(ref_tok),
        "tok_ratio": round(n / len(ref_tok), 4) if ref_tok else 0.0,
        "rep_bursts": len(bursts), "rep_ratio": round(rep / n, 4) if n else 0.0,
    }


def main() -> int:
    if not AUDIO.exists() or not REF_PATH.exists():
        print("입력/reference 없음", file=sys.stderr)
        return 2

    backend = get_backend("cohere")
    backend.load()

    wav, _ = librosa.load(str(AUDIO), sr=16000, mono=True)
    duration = len(wav) / 16000.0

    # --- VAD 분할 (한 번만) ---
    vad = get_vad("silero")
    vad.load()
    try:
        regions = vad.detect(wav, sr=16000)
    finally:
        vad.unload()
    chunks = build_chunks(regions, duration, TARGET_SEC, PAD_SEC, OVERLAP_SEC)
    chunk_wavs = [wav[int(round(s * 16000)):int(round(e * 16000))] for s, e, _ in chunks]
    print(f"[decode-tune] duration={duration:.1f}s chunks={len(chunks)} "
          f"(VAD regions={len(regions)})")

    ref = load_reference(REF_PATH)
    base = None
    if BASELINE_JSON.exists():
        try:
            base = json.loads(BASELINE_JSON.read_text(encoding="utf-8")).get("evaluation")
        except Exception:
            base = None

    results = []
    for name, gk in CONFIGS:
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
        t0 = time.perf_counter()
        texts = []
        try:
            for i, cw in enumerate(chunk_wavs):
                texts.append(decode_chunk(backend, cw, gk))
        except Exception as ex:  # 설정이 호환 안 되면 기록하고 다음으로
            print(f"[decode-tune] {name} 실패: {type(ex).__name__}: {ex}")
            results.append({"name": name, "gen_kwargs": gk, "error": f"{type(ex).__name__}: {ex}"})
            continue
        elapsed = time.perf_counter() - t0
        text, _ = merge_texts(texts, chunks)
        sc = score(text, ref, duration)
        vram = torch.cuda.max_memory_allocated() // (1024 * 1024) if torch.cuda.is_available() else None
        rtfx = round(duration / elapsed, 2) if elapsed > 0 else None
        row = {"name": name, "gen_kwargs": gk, **sc,
               "decode_sec": round(elapsed, 1), "rtfx": rtfx, "vram_mb": vram,
               "transcript": text}
        results.append(row)
        print(f"[decode-tune] {name:<20} WER={sc['wer']:.4f} CER={sc['cer']:.4f} "
              f"rep={sc['rep_ratio']:.3f} tok_ratio={sc['tok_ratio']:.3f} "
              f"decode={elapsed:.0f}s RTFx={rtfx}")

    backend.unload()

    payload = {
        "schema_version": "1.0",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": "decode_tuning",
        "audio": {"source_path": str(AUDIO.relative_to(ROOT)), "duration_seconds": round(duration, 2)},
        "chunking": {"method": "silero_vad_segmentation", "chunk_count": len(chunks),
                     "target_sec": TARGET_SEC},
        "baseline_single_call": base,
        "results": [{k: v for k, v in r.items() if k != "transcript"} for r in results],
        "transcripts": {r["name"]: r.get("transcript", "") for r in results if "transcript" in r},
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- 리포트 ---
    ok = [r for r in results if "error" not in r]
    ctrl = next((r for r in ok if r["name"] == "greedy_rp1.2"), None)

    def dlt(r, key, ref_row):
        if not ref_row:
            return ""
        d = r[key] - ref_row[key]
        return f" ({d:+.3f})"

    lines = [
        "# Score — ax 회의 디코딩 파라미터 A/B (vad_chunk 표준)",
        "",
        f"- generated_at: {dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- audio: `{AUDIO.relative_to(ROOT)}` ({duration/60:.1f}분), chunks={len(chunks)} (VAD 분할, 향상 없음)",
        f"- reference: `{REF_PATH.relative_to(ROOT)}` (Clova, ground truth 아님)",
        f"- baseline(single_call) WER={base.get('wer') if base else '?'} / "
        f"vad_chunk greedy 가 control",
        "",
        "## 결과 (Δ = vs greedy_rp1.2 control)",
        "",
        "| config | WER ↓ | CER ↓ | rep_ratio | tok_ratio | decode(s) | RTFx | VRAM |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if "error" in r:
            lines.append(f"| {r['name']} | — 실패: {r['error']} |||||||")
            continue
        lines.append(
            f"| {r['name']} | {r['wer']:.3f}{dlt(r,'wer',ctrl)} | "
            f"{r['cer']:.3f}{dlt(r,'cer',ctrl)} | {r['rep_ratio']:.3f} | "
            f"{r['tok_ratio']:.3f} | {r['decode_sec']:.0f} | {r['rtfx']} | {r['vram_mb']}MB |"
        )
    lines += [
        "",
        "## 비고",
        "",
        "- 동일 VAD 청크·동일 음원, 변수는 디코딩 설정뿐. WER 은 Clova 대비라 ±0.01 은 노이즈.",
        "- beam search 는 num_beams 배 느려지고 VRAM 증가 — 정확도 이득이 그만한 값을 하는지로 판단.",
        "- no_repeat_ngram_size 는 구 단위 반복 차단(rep_penalty 가 못 잡는 루프). 정당한 짧은 반복 손상 주의.",
        "",
    ]
    OUT_SCORE.write_text("\n".join(lines), encoding="utf-8")
    print(f"[decode-tune] saved → {OUT_SCORE.relative_to(ROOT)}, {OUT_JSON.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
