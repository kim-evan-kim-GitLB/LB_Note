"""ax 과제회의 음성 — VAD 분할 기반 청킹 STT + 평가.

핵심 아이디어(사용자 제안의 '되는 버전'):
  모델 내부 에너지 청커는 우리 VAD 경계를 모르고 자기 ~32~35s 그리드로 재분할한다.
  그래서 VAD '압축'(무음 제거 후 한 덩어리)은 경계 문제를 못 고친다.
  → 대신 Silero VAD 로 발화 경계에서 ≤TARGET_SEC(<35s) 청크로 '분할'해 각각 따로 넣으면,
    내부 청커는 ≤max_audio_clip_s 인 입력을 재분할하지 않으므로(코드: total<=chunk_size → 1청크)
    컷 지점이 항상 진짜 무음에 떨어진다(단어 중간 절단 제거). 병합도 우리가 장악.

비교 대상: output/text-ax_single_call.json (전처리 없음 baseline, WER 0.420 / CER 0.302).
공정 비교를 위해 음향 향상(denoise/dereverb)은 적용하지 않고, 디코딩 파라미터도
baseline 과 동일하게 둔다(greedy + repetition_penalty=1.2). 변수는 '청킹 방식' 하나뿐.

출력: output/text-ax_vad_chunk.json, output/score-ax_vad_chunk.md
사용 예 (venv python 은 sudo 필요):
    sudo .venv/bin/python tools/vad_chunk_ax_clova.py
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
from src.stt import get_backend, get_enhancer, get_vad  # noqa: E402

AUDIO = ROOT / "samples" / "ax과제회의(클로바노트)_음성파일.m4a"
REF_PATH = ROOT / "answer" / "ax_tf_클로바.txt"
BASELINE_JSON = ROOT / "output" / "text-ax_single_call.json"

SPEAKER_HEADER_RE = re.compile(r"^참석자\s+\d+\s+\d{1,2}:\d{2}\s*$")
MIN_RUN = 5
MAX_NEW_TOKENS = 1024            # ≤30s 청크면 충분(baseline 단일배치는 8192였음)
TARGET_SEC = 30.0                # < max_audio_clip_s(35) → 내부 청커가 재분할 안 함
PAD_SEC = 0.2                    # 발화 구간 앞뒤 여유
OVERLAP_SEC = 2.0               # 초장발화 hard-split 시에만 사용(겹침+dedup)
SEAM_DEDUP_MAX_WORDS = 12        # overlap seam 단어 중복제거 탐색 한도


def fmt_ts(sec: float) -> str:
    """초 → [HH:MM:SS] (VAD 경계 기반 청크 시작 시각)."""
    s = int(round(sec))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def load_reference(path: Path) -> str:
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


def build_chunks(
    regions: list[tuple[float, float]], dur: float,
    target: float, pad: float, overlap: float,
) -> list[tuple[float, float, bool]]:
    """VAD 발화 구간 → [(start_sec, end_sec, is_overlap_seam_with_prev), ...].

    - 인접 발화를 ≤target 으로 greedy 묶고, 컷은 발화 사이 무음 gap 에 떨어진다.
    - 단일 발화가 target 초과면 overlap 을 주고 hard-split(이 경계만 seam=True, dedup 대상).
    """
    regions = [(max(0.0, s - pad), min(dur, e + pad)) for s, e in regions]
    chunks: list[tuple[float, float, bool]] = []
    cur_s: float | None = None
    cur_e: float | None = None

    def flush():
        nonlocal cur_s, cur_e
        if cur_s is not None:
            chunks.append((cur_s, cur_e, False))
            cur_s, cur_e = None, None

    for s, e in regions:
        if e - s > target:                       # 초장발화 → hard-split (겹침)
            flush()
            t = s
            first = True
            while t < e:
                seg_end = min(t + target, e)
                chunks.append((t, seg_end, not first))
                first = False
                if seg_end >= e:
                    break
                t = seg_end - overlap
            continue
        if cur_s is None:
            cur_s, cur_e = s, e
        elif e - cur_s <= target:                # 같은 청크로 확장(내부 짧은 pause 포함)
            cur_e = e
        else:                                    # 무음 gap 에서 컷
            flush()
            cur_s, cur_e = s, e
    flush()
    return chunks


def merge_texts(
    texts: list[str], chunks: list[tuple[float, float, bool]],
) -> tuple[str, list[float]]:
    """청크 텍스트 결합. overlap seam 청크는 앞 청크 꼬리와 단어 중복제거.
    반환: (합쳐진 텍스트, 각 단어의 원본 시작초 리스트)."""
    words: list[str] = []
    word_times: list[float] = []
    for i, t in enumerate(texts):
        w = t.split()
        seam = chunks[i][2]
        if i > 0 and seam and words and w:
            maxk = min(SEAM_DEDUP_MAX_WORDS, len(words), len(w))
            best = 0
            for k in range(maxk, 0, -1):
                if words[-k:] == w[:k]:
                    best = k
                    break
            w = w[best:]
        words.extend(w)
        word_times.extend([chunks[i][0]] * len(w))
    return " ".join(words), word_times


def decode_chunk(backend, wav, lang="ko") -> str:
    """단일 청크(≤30s) STT — single_call 디코딩 경로를 그대로 미러링."""
    inputs = backend._processor(wav, sampling_rate=16000, return_tensors="pt", language=lang)
    aci = inputs.get("audio_chunk_index")
    inputs = inputs.to(backend._model.device, dtype=backend._model.dtype)
    with torch.inference_mode():
        outputs = backend._model.generate(
            **inputs, max_new_tokens=MAX_NEW_TOKENS,
            repetition_penalty=backend.REPETITION_PENALTY,
        )
    text = backend._processor.decode(
        outputs, skip_special_tokens=True, audio_chunk_index=aci, language=lang
    )
    if isinstance(text, list):
        text = text[0] if text else ""
    return text.strip()


def decode_chunks(backend, chunk_wavs, batch_size: int, lang="ko") -> list[str]:
    """청크들을 배치로 디코딩(순서 보존). 각 청크 ≤target<35s 라 내부 재분할 없음 →
    배치 row 1개 = 청크 1개. greedy 라 배치 여부와 무관하게 결과 동일."""
    if batch_size <= 1:   # 안전 폴백(단일 경로)
        out = []
        for i, cw in enumerate(chunk_wavs):
            out.append(decode_chunk(backend, cw, lang=lang))
            if (i + 1) % 25 == 0 or i + 1 == len(chunk_wavs):
                print(f"[vadchunk]   decoded {i + 1}/{len(chunk_wavs)}")
        return out
    texts: list[str] = []
    n = len(chunk_wavs)
    for i in range(0, n, batch_size):
        batch = chunk_wavs[i:i + batch_size]
        inputs = backend._processor(batch, sampling_rate=16000, return_tensors="pt", language=lang)
        inputs = inputs.to(backend._model.device, dtype=backend._model.dtype)
        with torch.inference_mode():
            outputs = backend._model.generate(
                **inputs, max_new_tokens=MAX_NEW_TOKENS,
                repetition_penalty=backend.REPETITION_PENALTY,
            )
        decoded = backend._processor.batch_decode(outputs, skip_special_tokens=True)
        texts.extend((t or "").strip() for t in decoded)
        print(f"[vadchunk]   decoded {min(i + batch_size, n)}/{n}")
    return texts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-sec", type=float, default=TARGET_SEC)
    ap.add_argument("--enhancers", default="",
                    help="쉼표 구분 향상 순서(분할 전 파형에 적용). 예: 'wpe,gtcrn' | '' (none)")
    ap.add_argument("--vad", default="silero", help="경계 검출 VAD: silero | energy(~15x 빠름)")
    ap.add_argument("--batch-size", type=int, default=8,
                    help="청크 디코딩 배치 크기(>1=배치 디코딩으로 가속, greedy 라 결과 동일)")
    args = ap.parse_args()
    target = args.target_sec
    enh_names = config.parse_enhancers(args.enhancers)
    tag = ("_enh" if enh_names else "") + ("" if args.vad == "silero" else f"_{args.vad}")

    out_json = ROOT / "output" / f"text-ax_vad_chunk{tag}.json"
    out_score = ROOT / "output" / f"score-ax_vad_chunk{tag}.md"
    out_transcript = ROOT / "output" / f"transcript-ax_vad_chunk{tag}.txt"

    if not AUDIO.exists() or not REF_PATH.exists():
        print("입력/ reference 없음", file=sys.stderr)
        return 2

    backend = get_backend("cohere")
    backend.load()
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()

    t0 = time.perf_counter()
    wav, _ = librosa.load(str(AUDIO), sr=16000, mono=True)
    load_t = time.perf_counter() - t0
    duration = len(wav) / 16000.0
    print(f"[vadchunk] load={load_t:.1f}s duration={duration:.1f}s ({duration/60:.1f}분)")

    # --- (옵션) 음향 향상: 분할/디코딩 전 전체 파형에 적용 (WPE→GTCRN, 압축 아님) ---
    applied: list[str] = []
    pre_t = 0.0
    if enh_names:
        pre_t0 = time.perf_counter()
        for n in enh_names:
            enh = get_enhancer(n)
            enh.load()
            try:
                wav = enh.process(wav, sr=16000)
            finally:
                enh.unload()
            applied.append(enh.name)
        pre_t = time.perf_counter() - pre_t0
        print(f"[vadchunk] enhance={pre_t:.1f}s applied={applied}")

    # --- VAD 발화 구간 검출 → 청크 경계 결정 (비파괴: 압축 아님, 분할) ---
    vad_t0 = time.perf_counter()
    vad = get_vad(args.vad)
    vad.load()
    try:
        regions = vad.detect(wav, sr=16000)
    finally:
        vad.unload()
    vad_t = time.perf_counter() - vad_t0
    print(f"[vadchunk] VAD={args.vad} detect={vad_t:.1f}s")
    speech_sec = sum(e - s for s, e in regions)
    chunks = build_chunks(regions, duration, target, PAD_SEC, OVERLAP_SEC)
    seam_count = sum(1 for c in chunks if c[2])
    chunk_lens = [e - s for s, e, _ in chunks]
    print(f"[vadchunk] VAD regions={len(regions)} speech={speech_sec:.1f}s "
          f"({speech_sec/duration*100:.0f}%) → chunks={len(chunks)} "
          f"(overlap-seam={seam_count}, 평균 {sum(chunk_lens)/len(chunk_lens):.1f}s, "
          f"max {max(chunk_lens):.1f}s)")

    # --- 청크 디코딩 (배치, 경계가 항상 무음이라 단어 절단 없음) ---
    gen_t0 = time.perf_counter()
    chunk_wavs = [wav[int(round(s * 16000)):int(round(e * 16000))] for s, e, _ in chunks]
    texts = decode_chunks(backend, chunk_wavs, batch_size=max(1, args.batch_size))
    gen_t = time.perf_counter() - gen_t0

    text, word_times = merge_texts(texts, chunks)
    # 타임스탬프 세그먼트 (VAD 경계 = 청크 start/end) — 회의록·정렬용
    segments = [
        {"start_sec": round(s, 2), "end_sec": round(e, 2),
         "start_ts": fmt_ts(s), "text": txt}
        for (s, e, _), txt in zip(chunks, texts) if txt
    ]
    elapsed = time.perf_counter() - t0
    vram = torch.cuda.max_memory_allocated() // (1024 * 1024) if torch.cuda.is_available() else None
    backend.unload()
    print(f"[vadchunk] elapsed={elapsed:.1f}s (decode={gen_t:.1f}s) vram={vram}MB len={len(text)}")

    # --- 평가 ---
    ref = load_reference(REF_PATH)
    hyp = scoring.normalize(text)
    w, c = scoring.wer(ref, hyp), scoring.cer(ref, hyp)
    ref_tokens, hyp_tokens = ref.split(), hyp.split()
    n_tokens = len(hyp_tokens)
    ratio_len = (n_tokens / len(ref_tokens)) if ref_tokens else 0.0

    bursts = repetition_bursts(hyp_tokens, MIN_RUN)
    rep_token_count = sum(b["run_length"] for b in bursts)
    rep_ratio = rep_token_count / n_tokens if n_tokens else 0.0
    for b in bursts:
        idx = b["start_index"]
        b["approx_time_seconds"] = round(word_times[idx], 1) if idx < len(word_times) else 0.0
    top_bursts = sorted(bursts, key=lambda b: b["run_length"], reverse=True)[:10]

    rtfx = round(duration / elapsed, 2) if elapsed > 0 else None
    print(f"[vadchunk] WER={w:.4f} CER={c:.4f} rep_ratio={rep_ratio:.3f} "
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
        "mode": "vad_chunk",
        "audio": {"source_path": str(AUDIO.relative_to(ROOT)),
                  "duration_seconds": round(duration, 2)},
        "model": {"backend": "cohere",
                  "name": Path(str(config.COHERE_MODEL_PATH)).name,
                  "max_new_tokens": MAX_NEW_TOKENS,
                  "repetition_penalty": backend.REPETITION_PENALTY},
        "preprocess": {"enhancers_applied": applied, "enhance_seconds": round(pre_t, 2),
                       "note": "분할 전 파형에 적용(압축 아님). 빈 리스트면 향상 없음."},
        "chunking": {
            "method": f"{args.vad}_vad_segmentation",
            "vad_backend": args.vad,
            "batch_size": max(1, args.batch_size),
            "target_sec": target,
            "pad_sec": PAD_SEC,
            "overlap_sec": OVERLAP_SEC,
            "vad_regions": len(regions),
            "speech_sec": round(speech_sec, 1),
            "chunk_count": len(chunks),
            "overlap_seam_chunks": seam_count,
            "chunk_len_avg_sec": round(sum(chunk_lens) / len(chunk_lens), 2),
            "chunk_len_max_sec": round(max(chunk_lens), 2),
            "note": "각 청크 ≤target<35s → 모델 내부 에너지 청커 재분할 없음. 컷은 항상 VAD 무음 경계.",
        },
        "performance": {"elapsed_seconds": round(elapsed, 2),
                        "vad_seconds": round(vad_t, 2),
                        "decode_seconds": round(gen_t, 2),
                        "rtfx": rtfx, "vram_peak_mb": vram},
        "evaluation": {
            "reference_path": str(REF_PATH.relative_to(ROOT)),
            "ref_source": "clova_note_txt (화자 헤더 제거)",
            "wer": round(w, 4), "cer": round(c, 4),
            "ref_tokens": len(ref_tokens), "hyp_tokens": n_tokens,
            "hyp_ref_token_ratio": round(ratio_len, 4),
            "repetition_burst_count": len(bursts),
            "repetition_tokens": rep_token_count,
            "repetition_ratio": round(rep_ratio, 4),
        },
        "baseline_single_call": base,
        "segments": segments,
        "transcript": text,
    }
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # 타임스탬프 transcript (VAD 경계 기반) — [HH:MM:SS] 발화
    out_transcript.write_text(
        "\n".join(f"[{seg['start_ts']}] {seg['text']}" for seg in segments) + "\n",
        encoding="utf-8",
    )

    def delta(cur: float, key: str) -> str:
        if not base or key not in base:
            return ""
        d = cur - base[key]
        sign = "개선" if d < 0 else ("악화" if d > 0 else "동일")
        return f" ({d:+.3f} {sign} vs baseline)"

    lines = [
        "# Score — ax 과제회의 음성 (VAD 분할 청킹)",
        "",
        f"- generated_at: {dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- audio: `{AUDIO.relative_to(ROOT)}`",
        f"- reference: `{REF_PATH.relative_to(ROOT)}` (Clova Note, 화자 헤더 제거)",
        "- mode: **vad_chunk** (Silero VAD 발화 경계 분할 → 청크별 STT → 병합)",
        f"- 청킹: target≤{target:.0f}s, VAD regions={len(regions)} → chunks={len(chunks)} "
        f"(overlap-seam={seam_count}, 평균 {sum(chunk_lens)/len(chunk_lens):.1f}s)",
        f"- 음향 향상: 없음 / 디코딩: greedy + rep_penalty {backend.REPETITION_PENALTY} (baseline 과 동일)",
        f"- duration: {duration:.2f}s ({duration/60:.1f}분), speech {speech_sec:.0f}s "
        f"({speech_sec/duration*100:.0f}%)",
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
        "- 각 청크 ≤target(<35s) 이라 모델 내부 에너지 청커가 재분할하지 않음 → 컷이 항상 VAD 무음 경계.",
        "- 음향 향상·디코딩 파라미터를 baseline 과 동일하게 두어 '청킹 방식' 효과만 격리.",
        f"- VAD 경계 = 청크 start/end → 타임스탬프 transcript 생성: `{out_transcript.relative_to(ROOT)}` "
        f"(세그먼트 {len(segments)}개, single_call 은 타임스탬프 불가).",
        "",
    ]
    out_score.write_text("\n".join(lines), encoding="utf-8")
    print(f"[vadchunk] saved → {out_json.relative_to(ROOT)}, {out_score.relative_to(ROOT)}, "
          f"{out_transcript.relative_to(ROOT)} (segments={len(segments)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
