"""산출 JSON(text-*, audio_quality-*) 을 데이터에서 귀납한 '정규 형태'로 변환.

배경: output/*.json 들은 스키마가 드리프트되어 있다(같은 개념이 여러 키로):
  - 적용 향상   : preprocess.applied | preprocess.enhancers | preprocess.enhancers_applied
  - baseline    : baseline_single_call | baseline_no_preprocess
  - 분할 설정   : pipeline{slice_*|mode/sliced} | chunking{vad...}
  - duration    : audio.duration_seconds | duration_sec  (audio 가 객체이기도/문자열이기도)
이 스크립트는 사전 규약을 강요하지 않고, 관측된 필드들을 'fallback 체인'으로 흡수해
소비에 가장 자연스러운 4개 뷰로 분해한다:

  1) runs.json        — run 당 정규 레코드(식별/설정/지표/성능/참조/페이로드 요약)
  2) comparison.csv/md— run × 핵심지표 1행 (분석·비교용 denormalized 뷰)
  3) metrics_long.csv — (run_id, metric, value) tidy/long (플로팅·집계용)
  4) audio_quality.json — audio 당 음질 레코드 (run 과 audio_id 로 조인)

run_id 는 파일명에서 도출(mode 가 충돌하므로: vad_chunk vs vad_chunk_enh).
출력: output/normalized/
사용: python3 tools/normalize_runs.py
"""
from __future__ import annotations

import csv
import glob
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "output" / "normalized"


def first(*vals):
    """비어있지 않은 첫 값 반환 (드리프트 키 fallback 체인)."""
    for v in vals:
        if v not in (None, "", [], {}):
            return v
    return None


def get(d: dict, *path, default=None):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def audio_id_of(d: dict) -> str | None:
    a = d.get("audio")
    if isinstance(a, dict):
        a = a.get("source_path")
    if isinstance(a, str):
        return Path(a).name
    return None


def norm_enhancers(d: dict) -> list:
    pre = d.get("preprocess") or {}
    return first(pre.get("applied"), pre.get("enhancers"),
                 pre.get("enhancers_applied"), []) or []


def norm_segmentation(d: dict) -> dict:
    """pipeline{} / chunking{} 를 단일 segmentation 블록으로 통일."""
    ch = d.get("chunking")
    pl = d.get("pipeline") or {}
    if ch:
        return {"strategy": ch.get("method", "vad_chunk"),
                "n_units": ch.get("chunk_count"),
                "target_sec": ch.get("target_sec"),
                "overlap_sec": ch.get("overlap_sec"),
                "speech_sec": ch.get("speech_sec"),
                "vad_regions": ch.get("vad_regions")}
    if "slice_sec" in pl:
        return {"strategy": "slice", "n_units": pl.get("n_slices"),
                "slice_sec": pl.get("slice_sec"), "overlap_sec": pl.get("overlap_sec")}
    # 단일 호출
    return {"strategy": "single_call", "n_units": 1,
            "audio_chunk_index": pl.get("audio_chunk_index")}


def norm_baseline(d: dict) -> dict | None:
    b = first(d.get("baseline_single_call"), d.get("baseline_no_preprocess"))
    if not b:
        return None
    return {"wer": b.get("wer"), "cer": b.get("cer"),
            "repetition_ratio": b.get("repetition_ratio")}


def norm_run(path: Path) -> dict:
    d = json.load(open(path, encoding="utf-8"))
    run_id = path.stem.replace("text-ax_", "").replace("text-", "")
    ev = d.get("evaluation") or {}
    perf = d.get("performance") or {}
    model = d.get("model") or {}
    pre = d.get("preprocess") or {}
    seg = d.get("segments") or []
    base = norm_baseline(d)
    metrics = {
        "wer": ev.get("wer"), "cer": ev.get("cer"),
        "repetition_ratio": ev.get("repetition_ratio"),
        "repetition_bursts": ev.get("repetition_burst_count"),
        "hyp_tokens": ev.get("hyp_tokens"), "ref_tokens": ev.get("ref_tokens"),
        "hyp_ref_token_ratio": ev.get("hyp_ref_token_ratio"),
    }
    # baseline 대비 Δ (있을 때만)
    if base and metrics["wer"] is not None and base["wer"] is not None:
        metrics["wer_delta_vs_baseline"] = round(metrics["wer"] - base["wer"], 4)
        metrics["cer_delta_vs_baseline"] = round(metrics["cer"] - base["cer"], 4)
    return {
        "run_id": run_id,
        "audio_id": audio_id_of(d),
        "generated_at": d.get("generated_at"),
        "mode": d.get("mode"),
        "config": {
            "backend": model.get("backend"),
            "model": model.get("name"),
            "max_new_tokens": model.get("max_new_tokens"),
            "repetition_penalty": model.get("repetition_penalty"),
            "quantization": model.get("quantization"),
            "enhancers": norm_enhancers(d),
            "vad": pre.get("vad"),
            "segmentation": norm_segmentation(d),
        },
        "metrics": metrics,
        "performance": {
            "elapsed_s": perf.get("elapsed_seconds"),
            "decode_s": first(perf.get("decode_seconds"), perf.get("generate_seconds")),
            "preprocess_s": first(perf.get("preprocess_seconds"), perf.get("enhance_seconds")),
            "rtfx": perf.get("rtfx"),
            "vram_mb": perf.get("vram_peak_mb"),
        },
        "duration_sec": first(get(d, "audio", "duration_seconds"), d.get("duration_sec")),
        "refs": {
            "reference_path": ev.get("reference_path"),
            "ref_source": ev.get("ref_source"),
            "has_baseline": base is not None,
        },
        "payload": {
            "transcript_chars": len(d.get("transcript") or ""),
            "n_segments": len(seg),
            "source_json": path.name,
        },
    }


def norm_audio_quality(path: Path) -> dict:
    d = json.load(open(path, encoding="utf-8"))
    return {
        "audio_id": audio_id_of(d),
        "duration_sec": d.get("duration_sec"),
        "snr_db": get(d, "snr", "snr_db"),
        "snr_grade": get(d, "snr", "grade"),
        "speech_ratio_pct": get(d, "snr", "speech_ratio_pct"),
        "clipping_pct": get(d, "amplitude", "clipping_pct"),
        "peak_dbfs": get(d, "amplitude", "peak_dbfs"),
        "rms_dbfs": get(d, "amplitude", "rms_dbfs"),
        "dynamic_spread_db": get(d, "dynamics", "dynamic_spread_db"),
        "highfreq_cutoff_hz": get(d, "spectrum", "highfreq_cutoff_hz"),
        "band_energy_pct": get(d, "spectrum", "band_energy_pct"),
        "source_json": path.name,
    }


def main() -> int:
    files = sorted(glob.glob(str(ROOT / "output" / "*.json")))
    runs, aq = [], []
    for f in files:
        p = Path(f)
        d = json.load(open(f, encoding="utf-8"))
        if "evaluation" in d and "mode" in d:          # STT run
            runs.append(norm_run(p))
        elif "snr" in d or "amplitude" in d:            # audio quality
            aq.append(norm_audio_quality(p))
    runs.sort(key=lambda r: r["run_id"])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "runs.json").write_text(
        json.dumps(runs, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "audio_quality.json").write_text(
        json.dumps(aq, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- comparison.csv (denormalized, run 당 1행) ---
    cmp_cols = ["run_id", "mode", "enhancers", "segmentation", "n_units",
                "wer", "cer", "repetition_ratio", "hyp_ref_token_ratio",
                "rtfx", "vram_mb", "elapsed_s", "n_segments"]
    with open(OUT_DIR / "comparison.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cmp_cols)
        for r in runs:
            seg = r["config"]["segmentation"]
            w.writerow([
                r["run_id"], r["mode"], "+".join(r["config"]["enhancers"]) or "none",
                seg.get("strategy"), seg.get("n_units"),
                r["metrics"]["wer"], r["metrics"]["cer"],
                r["metrics"]["repetition_ratio"], r["metrics"]["hyp_ref_token_ratio"],
                r["performance"]["rtfx"], r["performance"]["vram_mb"],
                r["performance"]["elapsed_s"], r["payload"]["n_segments"],
            ])

    # --- metrics_long.csv (tidy) ---
    long_metrics = ["wer", "cer", "repetition_ratio", "hyp_ref_token_ratio"]
    long_perf = ["rtfx", "vram_mb", "elapsed_s"]
    with open(OUT_DIR / "metrics_long.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["run_id", "metric", "value"])
        for r in runs:
            for m in long_metrics:
                w.writerow([r["run_id"], m, r["metrics"].get(m)])
            for m in long_perf:
                w.writerow([r["run_id"], m, r["performance"].get(m)])

    # --- comparison.md (사람용) ---
    lines = ["# STT runs — 정규화 비교 (normalize_runs.py 생성)", "",
             "| run_id | enhancers | segmentation | WER | CER | rep | RTFx | VRAM(MB) | segments |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in runs:
        seg = r["config"]["segmentation"]
        lines.append(
            f"| {r['run_id']} | {'+'.join(r['config']['enhancers']) or 'none'} | "
            f"{seg.get('strategy')}({seg.get('n_units')}) | {r['metrics']['wer']} | "
            f"{r['metrics']['cer']} | {r['metrics']['repetition_ratio']} | "
            f"{r['performance']['rtfx']} | {r['performance']['vram_mb']} | "
            f"{r['payload']['n_segments']} |")
    if aq:
        lines += ["", "## audio_quality", "",
                  "| audio_id | SNR(dB) | cutoff(Hz) | clip% | dyn(dB) | speech% |",
                  "|---|---|---|---|---|---|"]
        for a in aq:
            lines.append(f"| {a['audio_id']} | {a['snr_db']} | {a['highfreq_cutoff_hz']} | "
                         f"{a['clipping_pct']} | {a['dynamic_spread_db']} | {a['speech_ratio_pct']} |")
    (OUT_DIR / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"[normalize] runs={len(runs)} audio_quality={len(aq)} → {OUT_DIR.relative_to(ROOT)}/")
    print(f"[normalize] 산출: runs.json, audio_quality.json, comparison.csv, comparison.md, metrics_long.csv")
    print("\n=== comparison (검증용 stdout) ===")
    print(f"{'run_id':<22}{'enh':<10}{'seg':<14}{'WER':>7}{'CER':>7}{'RTFx':>8}{'VRAM':>8}{'seg#':>6}")
    for r in runs:
        seg = r["config"]["segmentation"]
        print(f"{r['run_id']:<22}{('+'.join(r['config']['enhancers']) or 'none'):<10}"
              f"{str(seg.get('strategy')):<14}{r['metrics']['wer']!s:>7}{r['metrics']['cer']!s:>7}"
              f"{r['performance']['rtfx']!s:>8}{r['performance']['vram_mb']!s:>8}"
              f"{r['payload']['n_segments']!s:>6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
