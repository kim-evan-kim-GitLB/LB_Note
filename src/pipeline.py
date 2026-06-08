"""[음성] → [변환] → [STT] → [text.json] 통합 파이프라인.

CLI: run.py 에서 호출. 단일 entrypoint = `run_pipeline()`.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

from src import config
from src.audio_io import duration_seconds, load_audio
from src.chunker import (
    DEFAULT_CHUNK_SEC,
    DEFAULT_OVERLAP_SEC,
    DEFAULT_PAD_SEC,
    DEFAULT_SEG_OVERLAP_SEC,
    DEFAULT_TARGET_SEC,
    chunk_audio,
    merge_segments,
    merge_vad_segments,
    vad_segment_chunks,
)
from src.preprocess import PreprocessResult, preprocess, remap_time
from src.repetition import collapse_repetitions
from src.stt import get_backend, get_enhancer, get_vad
from src.types import Segment

SCHEMA_VERSION = "1.1"


def _build_payload(
    audio_path: Path,
    duration: float,
    backend_name: str,
    backend_quant: str,
    n_chunks: int,
    chunk_sec: float,
    overlap_sec: float,
    elapsed: float,
    vram_peak: int | None,
    merged: list[Segment],
    transcript: str,
    pre: PreprocessResult | None = None,
    chunking: dict | None = None,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "audio": {
            "source_path": str(audio_path),
            "duration_seconds": round(duration, 3),
            "sample_rate_normalized": 16000,
            "channels_normalized": 1,
        },
        "model": {
            "backend": backend_name,
            "name": Path(str(config.COHERE_MODEL_PATH)).name,
            "quantization": backend_quant or "bf16",
        },
        "preprocess": {
            "applied": pre.applied if pre else [],
            "n_speech_regions": len(pre.speech_regions) if pre else 0,
            "compressed_seconds": pre.compressed_sec if pre else round(duration, 3),
        },
        "pipeline": {
            "chunk_seconds": chunk_sec,
            "chunk_overlap_seconds": overlap_sec,
            "n_chunks": n_chunks,
        },
        "chunking": chunking or {
            "method": "fixed",
            "chunk_sec": chunk_sec,
            "overlap_sec": overlap_sec,
            "n_chunks": n_chunks,
        },
        "performance": {
            "elapsed_seconds": round(elapsed, 2),
            "rtfx": round(duration / elapsed, 2) if elapsed > 0 else None,
            "vram_peak_mb": vram_peak,
        },
        "segments": [
            {
                "start": round(s.start, 2),
                "end": round(s.end, 2),
                "text": s.text,
                **({"speaker": s.speaker} if s.speaker else {}),
                **({"flag": s.meta["flag"]} if s.meta.get("flag") else {}),
                **({"original_text": s.meta["original_text"]}
                   if s.meta.get("repetition_collapsed") else {}),
            }
            for s in merged
        ],
        "transcript": transcript,
    }


def run_pipeline(
    audio_path: Path,
    reference_path: Path | None = None,
    out_dir: Path | None = None,
    chunk_sec: float = DEFAULT_CHUNK_SEC,
    overlap_sec: float = DEFAULT_OVERLAP_SEC,
    backend_name: str = "cohere",
    language: str | None = None,
    enhancers: list[str] | None = None,
    vad: str | None = None,
    segmentation: str = "vad",
    vad_backend: str = "energy",
    batch_size: int = 8,
    target_sec: float = DEFAULT_TARGET_SEC,
    auto_enhance: bool | None = None,
) -> dict:
    out_dir = Path(out_dir) if out_dir else config.OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    language = language or config.STT_LANGUAGE
    audio_path = Path(audio_path)

    print(f"[pipeline] load audio: {audio_path}")
    samples, sr = load_audio(audio_path)
    duration = duration_seconds(samples, sr)
    print(f"[pipeline] duration={duration:.2f}s sr={sr}")

    # 프론트엔드 전처리 (opt-in). 기본값이면 no-op → 기존 동작 보존.
    enhancers = enhancers if enhancers is not None else config.parse_enhancers(config.ENHANCERS)
    vad = vad if vad is not None else (config.VAD_BACKEND or None)

    # P5: 증거기반 향상 라우팅(opt-in). enhancers 가 명시되지 않은 경우에만 자동 판단.
    # "품질 낮음→향상"이 아니라 대역제한이면 향상 생략(net-negative), 노이즈우세+대역양호만 denoise.
    auto_enhance = config.AUTO_ENHANCE if auto_enhance is None else auto_enhance
    quality_info: dict | None = None
    if auto_enhance and not enhancers:
        from src.quality import compute_quality_metrics, decide_enhancers
        metrics = compute_quality_metrics(samples, sr)
        chosen, reason = decide_enhancers(
            metrics,
            snr_lo=config.AUTO_ENHANCE_SNR_LO,
            cutoff_ok_hz=config.AUTO_ENHANCE_CUTOFF_OK_HZ,
        )
        sn = metrics["snr"]["snr_db"]
        cut = metrics["spectrum"]["highfreq_cutoff_hz"]
        print(f"[pipeline] auto-enhance: SNR={sn}dB cutoff={cut}Hz "
              f"speech={metrics['snr']['speech_ratio_pct']}% → enhancers={chosen} ({reason})")
        enhancers = chosen
        quality_info = {"metrics": metrics, "decision": chosen, "reason": reason}

    pre = preprocess(
        samples, sr,
        enhancers=[get_enhancer(n) for n in enhancers],
        vad=get_vad(vad),
        vad_pad_sec=config.VAD_PAD_SEC,
        vad_max_silence_sec=config.VAD_MAX_SILENCE_SEC,
    )
    if pre.applied:
        print(f"[pipeline] preprocess applied={pre.applied} "
              f"{pre.original_sec:.1f}s→{pre.compressed_sec:.1f}s "
              f"speech_regions={len(pre.speech_regions)}")
    proc_samples = pre.samples

    segmentation = (segmentation or "vad").lower()
    chunking_info: dict
    if segmentation == "vad":
        # VAD '분할'(무음압축 아님): 발화 경계에서 ≤target 청크로 나눠 컷이 항상 무음에 떨어지게 함.
        seg_vad = get_vad(vad_backend)
        if seg_vad is None:
            raise ValueError(f"segmentation=vad 에는 vad_backend 필요(받음: {vad_backend!r})")
        seg_vad.load()
        try:
            regions = seg_vad.detect(proc_samples, sr=sr)
        finally:
            seg_vad.unload()
        chunks = vad_segment_chunks(
            proc_samples, sr=sr, regions=regions, target_sec=target_sec,
        )
        seam_count = sum(1 for c in chunks if c.is_overlap_seam)
        chunk_lens = [c.end_sec - c.start_sec for c in chunks] or [0.0]
        speech_sec = sum(e - s for s, e in regions)
        print(f"[pipeline] segmentation=vad backend={vad_backend} regions={len(regions)} "
              f"speech={speech_sec:.1f}s → chunks={len(chunks)} "
              f"(seam={seam_count}, avg {sum(chunk_lens)/len(chunk_lens):.1f}s, "
              f"max {max(chunk_lens):.1f}s, batch={batch_size})")
        chunking_info = {
            "method": f"{vad_backend}_vad_segmentation",
            "vad_backend": vad_backend,
            "batch_size": batch_size,
            "target_sec": target_sec,
            "pad_sec": DEFAULT_PAD_SEC,
            "overlap_sec": DEFAULT_SEG_OVERLAP_SEC,
            "vad_regions": len(regions),
            "speech_sec": round(speech_sec, 1),
            "chunk_count": len(chunks),
            "overlap_seam_chunks": seam_count,
            "chunk_len_avg_sec": round(sum(chunk_lens) / len(chunk_lens), 2),
            "chunk_len_max_sec": round(max(chunk_lens), 2),
        }
    else:
        chunks = chunk_audio(proc_samples, sr=sr, chunk_sec=chunk_sec, overlap_sec=overlap_sec)
        print(f"[pipeline] segmentation=fixed n_chunks={len(chunks)} "
              f"(chunk={chunk_sec}s, overlap={overlap_sec}s)")
        chunking_info = {
            "method": "fixed",
            "chunk_sec": chunk_sec,
            "overlap_sec": overlap_sec,
            "n_chunks": len(chunks),
        }

    config.assert_cuda_or_raise()
    backend = get_backend(backend_name)
    backend.load()
    print(f"[pipeline] {backend_name} loaded")

    raw_segments: list[Segment] = []
    t0 = time.perf_counter()
    try:
        if segmentation == "vad":
            # P2 소스층: max_new_tokens 를 청크 길이 비례로 캡 → 디코딩 루프 폭주를 짧게 자름.
            # 분포 불변(정상 청크는 캡에 안 닿음), 폭주 토큰 수만 제한. collapse(후처리)가 본 방어.
            max_chunk_sec = max((c.end_sec - c.start_sec for c in chunks), default=0.0)
            mnt = int(max_chunk_sec * config.REPETITION_TOKENS_PER_SEC)
            mnt = max(config.REPETITION_MNT_FLOOR, min(config.REPETITION_MNT_CEIL, mnt))
            batch_segs = backend.transcribe_arrays(
                [c.samples for c in chunks], sr=sr,
                start_offsets=[c.start_sec for c in chunks],
                language=language, batch_size=batch_size,
                max_new_tokens=mnt,
            )
            # is_overlap_seam 를 meta 로 전달(merge_vad_segments 가 seam 만 dedup).
            for ch, seg in zip(chunks, batch_segs):
                meta = dict(seg.meta)
                meta["is_overlap_seam"] = ch.is_overlap_seam
                raw_segments.append(Segment(
                    start=seg.start, end=seg.end, text=seg.text,
                    confidence=seg.confidence, speaker=seg.speaker, meta=meta,
                ))
        else:
            for ch in chunks:
                t_chunk = time.perf_counter()
                segs = backend.transcribe_array(
                    ch.samples, sr=sr, start_offset=ch.start_sec, language=language
                )
                raw_segments.extend(segs)
                dt_chunk = time.perf_counter() - t_chunk
                print(
                    f"[pipeline] chunk {ch.index + 1}/{len(chunks)} "
                    f"({ch.start_sec:.1f}-{ch.end_sec:.1f}s) {dt_chunk:.1f}s"
                )
        elapsed = time.perf_counter() - t0
        vram_peak = backend.vram_peak_mb()
    finally:
        backend.unload()

    merged = merge_vad_segments(raw_segments) if segmentation == "vad" else merge_segments(raw_segments)
    # VAD 무음압축 시 청크 타임스탬프는 압축 타임라인 → 원본 타임라인으로 remap
    # (offset_map 항등이면 no-op). 향후 diarization 정렬 호환.
    merged = [
        Segment(
            start=round(remap_time(pre.offset_map, s.start), 2),
            end=round(remap_time(pre.offset_map, s.end), 2),
            text=s.text, confidence=s.confidence, speaker=s.speaker, meta=s.meta,
        )
        for s in merged
    ]

    # P2 후처리층(주 방어): 반복-환각을 결정적으로 collapse. 접힌 세그먼트는 원문 보존 +
    # '확인필요' 플래그. 임계 미만은 no-op라 정상 출력 불변. web/postprocess 경로도 자동 수혜.
    rep_collapsed = 0
    if config.REPETITION_GUARD:
        guarded: list[Segment] = []
        for s in merged:
            new_text, collapsed = collapse_repetitions(
                s.text, max_repeat=config.REPETITION_MAX_REPEAT
            )
            if collapsed:
                rep_collapsed += 1
                meta = dict(s.meta)
                meta["repetition_collapsed"] = True
                meta["original_text"] = s.text
                meta["flag"] = "확인필요"
                guarded.append(Segment(
                    start=s.start, end=s.end, text=new_text,
                    confidence=s.confidence, speaker=s.speaker, meta=meta,
                ))
            else:
                guarded.append(s)
        merged = guarded
        if rep_collapsed:
            print(f"[pipeline] repetition guard: {rep_collapsed}/{len(merged)} "
                  f"세그먼트 반복-환각 collapse(확인필요 플래그)")

    transcript = " ".join(s.text for s in merged if s.text).strip()

    payload = _build_payload(
        audio_path=audio_path,
        duration=duration,
        backend_name=backend_name,
        backend_quant=getattr(backend, "quantization", ""),
        n_chunks=len(chunks),
        chunk_sec=chunk_sec,
        overlap_sec=overlap_sec,
        elapsed=elapsed,
        vram_peak=vram_peak,
        merged=merged,
        transcript=transcript,
        pre=pre,
        chunking=chunking_info,
    )

    payload["repetition_guard"] = {
        "enabled": config.REPETITION_GUARD,
        "collapsed_segments": rep_collapsed,
        "max_new_tokens_cap": mnt if segmentation == "vad" else None,
    }
    if quality_info is not None:
        payload["audio_quality"] = quality_info

    if reference_path:
        from src import scoring
        eval_result = scoring.evaluate(transcript, Path(reference_path))
        payload["evaluation"] = eval_result

    stem = audio_path.stem
    json_path = out_dir / f"text-{stem}.json"
    md_path = out_dir / f"transcript-{stem}.md"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(
        f"# {backend_name} — {audio_path.name}\n"
        f"- duration: {duration:.2f}s\n"
        f"- elapsed: {elapsed:.2f}s\n"
        f"- rtfx: {payload['performance']['rtfx']}\n"
        f"- vram_peak: {vram_peak} MB\n"
        f"- chunks: {len(chunks)} (chunk={chunk_sec}s, overlap={overlap_sec}s)\n\n"
        f"## Transcript\n\n{transcript}\n",
        encoding="utf-8",
    )

    if "evaluation" in payload:
        score_path = out_dir / f"score-{stem}.md"
        ev = payload["evaluation"]
        score_path.write_text(
            f"# Score — {audio_path.name}\n\n"
            f"- reference: `{ev['reference_path']}` ({ev['ref_source']})\n"
            f"- duration: {duration:.2f}s, elapsed: {elapsed:.2f}s\n\n"
            f"| Metric | Value |\n|---|---|\n"
            f"| WER ↓ | {ev['wer']:.3f} |\n"
            f"| CER ↓ | {ev['cer']:.3f} |\n"
            f"| RTFx ↑ | {payload['performance']['rtfx']} |\n"
            f"| VRAM peak | {vram_peak} MB |\n",
            encoding="utf-8",
        )
        print(f"[pipeline] score → {score_path}")

    print(f"[pipeline] text.json → {json_path}")
    print(f"[pipeline] transcript.md → {md_path}")
    return payload


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Cohere STT 통합 파이프라인")
    ap.add_argument("audio", type=Path)
    ap.add_argument("--reference", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--chunk-sec", type=float, default=DEFAULT_CHUNK_SEC)
    ap.add_argument("--overlap-sec", type=float, default=DEFAULT_OVERLAP_SEC)
    ap.add_argument("--language", default=config.STT_LANGUAGE)
    ap.add_argument("--dereverb", action="store_true", help="WPE dereverb 적용")
    ap.add_argument("--denoise", action="store_true", help="GTCRN denoise 적용")
    ap.add_argument("--vad", action="store_true", help="Silero VAD 무음압축 적용(분할과 무관)")
    # --- VAD 분할 청킹(기본 vad_chunk) ---
    ap.add_argument("--segmentation", choices=["vad", "fixed"], default="vad",
                    help="청킹 방식: vad(VAD 분할, 기본) | fixed(고정 길이, 기존 동작)")
    ap.add_argument("--vad-backend", choices=["energy", "silero"], default="energy",
                    help="분할용 VAD 백엔드: energy(기본, ~15x 빠름) | silero")
    ap.add_argument("--batch-size", type=int, default=8,
                    help="VAD 분할 청크 배치 디코딩 크기(greedy 라 결과 동일, 가속용)")
    ap.add_argument("--target-sec", type=float, default=DEFAULT_TARGET_SEC,
                    help="VAD 분할 청크 목표 최대 길이(초). <35s 라야 내부 청커 재분할 없음")
    ap.add_argument("--auto-enhance", action="store_true",
                    help="P5 증거기반 향상 라우팅: 품질 측정 후 노이즈우세+대역양호일 때만 denoise "
                         "(대역제한이면 향상 생략). --dereverb/--denoise 명시 시 무시.")
    args = ap.parse_args()

    if not args.audio.exists():
        print(f"입력 파일 없음: {args.audio}", file=sys.stderr)
        return 2

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
        auto_enhance=args.auto_enhance,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
