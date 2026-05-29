"""AX TF 회의 STT 평가: 클로바 노트 ref vs 로컬 Cohere hyp.

전처리(denoise/dereverb) 필요 여부 판단용 일회성 평가 스크립트.

Reference: lb-note/answer/ax_tf_클로바.txt (Clova Note export, 화자 헤더 + 본문)
Hypothesis: lb-note/output/text-ax과제회의(클로바노트)_음성파일.json (Cohere, 60s/10s chunk, 전처리 없음)

WER/CER 외에 hallucination/repetition burst 비율을 함께 산출해
모델이 noise·reverb 로 collapse 한 구간을 정량화한다.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.scoring import cer, normalize, wer  # noqa: E402

REF_PATH = ROOT / "answer" / "ax_tf_클로바.txt"
HYP_PATH = ROOT / "output" / "text-ax과제회의(클로바노트)_음성파일.json"
OUT_PATH = ROOT / "output" / "score-ax_tf_clova.md"

SPEAKER_HEADER_RE = re.compile(r"^참석자\s+\d+\s+\d{1,2}:\d{2}\s*$")
MIN_RUN = 5


def load_reference(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    utterances = [
        ln.strip()
        for ln in lines
        if ln.strip() and not SPEAKER_HEADER_RE.match(ln.strip())
    ]
    return normalize(" ".join(utterances))


def load_hypothesis(path: Path) -> tuple[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    segments = data.get("segments", [])
    raw = " ".join(s.get("text", "") for s in segments)
    return normalize(raw), data


def repetition_bursts(tokens: list[str], min_run: int) -> list[dict]:
    bursts: list[dict] = []
    n = len(tokens)
    i = 0
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
    if not REF_PATH.exists():
        print(f"[score] reference 없음: {REF_PATH}", file=sys.stderr)
        return 1
    if not HYP_PATH.exists():
        print(f"[score] hypothesis 없음: {HYP_PATH}", file=sys.stderr)
        return 1

    ref = load_reference(REF_PATH)
    hyp, hyp_meta = load_hypothesis(HYP_PATH)

    w = wer(ref, hyp)
    c = cer(ref, hyp)

    ref_tokens = ref.split()
    hyp_tokens = hyp.split()
    n_tokens = len(hyp_tokens)
    ratio_len = (len(hyp_tokens) / len(ref_tokens)) if ref_tokens else 0.0

    bursts = repetition_bursts(hyp_tokens, MIN_RUN)
    rep_token_count = sum(b["run_length"] for b in bursts)
    rep_ratio = rep_token_count / n_tokens if n_tokens else 0.0

    duration = float(hyp_meta.get("audio", {}).get("duration_seconds", 0.0))
    for b in bursts:
        b["approx_time_seconds"] = (b["start_index"] / n_tokens * duration) if n_tokens else 0.0

    top_bursts = sorted(bursts, key=lambda b: b["run_length"], reverse=True)[:10]

    perf = hyp_meta.get("performance", {})
    pipe = hyp_meta.get("pipeline", {})
    model = hyp_meta.get("model", {})

    verdict_lines: list[str] = []
    if rep_ratio > 0.05 or len(bursts) >= 5:
        verdict_lines.append(
            f"- **전처리(denoise/dereverb) 권장**: repetition_ratio={rep_ratio:.3f} "
            f"(>0.05) 또는 burst {len(bursts)}회 (>=5) — long-form collapse 신호 다수 검출"
        )
    else:
        verdict_lines.append(
            f"- 전처리 효과 한계적: repetition_ratio={rep_ratio:.3f}, burst {len(bursts)}회 — "
            "collapse 신호 약함. 모델·청킹 튜닝 우선 검토 권장"
        )
    if w > 0.5:
        verdict_lines.append(
            f"- WER {w:.3f} — 자연 회의 음성으로는 다소 높음. ref(Clova STT)·hyp 양쪽 모두 "
            "STT 결과라 절대값보다 패턴/구간 분석에 가중치"
        )

    lines = [
        f"# Score — ax_tf 회의 음성 (Clova ref vs 로컬 Cohere)",
        "",
        f"- generated_at: {dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- reference: `{REF_PATH.relative_to(ROOT)}` (클로바 노트 export, 화자 헤더 제거)",
        f"- hypothesis: `{HYP_PATH.relative_to(ROOT)}` (전처리 없음)",
        f"- backend: {model.get('backend')} / {model.get('name')} ({model.get('quantization')})",
        f"- duration: {duration:.2f}s ({duration/60:.1f}분)",
        f"- chunks: {pipe.get('n_chunks')} (chunk={pipe.get('chunk_seconds')}s, "
        f"overlap={pipe.get('chunk_overlap_seconds')}s)",
        f"- elapsed: {perf.get('elapsed_seconds', 0):.2f}s",
        f"- RTFx: {perf.get('rtfx')}",
        f"- VRAM peak: {perf.get('vram_peak_mb')} MB",
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
        "## Top hallucination bursts",
        "",
        "| # | token | run_length | approx_time (s) |",
        "|---|---|---|---|",
    ]
    for i, b in enumerate(top_bursts, start=1):
        lines.append(
            f"| {i} | `{b['token']}` | {b['run_length']} | {b['approx_time_seconds']:.0f} |"
        )
    if not top_bursts:
        lines.append("| - | (none) | - | - |")

    lines += [
        "",
        "## 관찰 및 결론",
        "",
        *verdict_lines,
        "- **임계치는 휴리스틱**입니다. repetition_ratio>0.05, burst≥5 는 long-form Whisper/Cohere "
        "계열에서 noise/reverb 로 attention lock 이 풀린 collapse 패턴을 잡기 위한 경험값.",
        "- reference 도 Clova STT 결과이므로 ground truth 가 아닙니다. WER 절대값보다 "
        "burst 토큰·발생 구간 시간이 전처리 효과 판단의 1차 신호.",
        "",
        "## 다음 단계 제안",
        "",
        "- 동일 음성에 denoise(예: RNNoise/DeepFilterNet) + dereverb 후 재처리해 "
        "repetition_ratio, WER 변동 비교",
        "- 청크 길이/overlap 조정(예: 30s/5s) 실험과 분리해 측정",
        "- 필요 시 chunk별 WER 프로파일 추가 산출(향후 task)",
        "",
    ]

    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"[score] WER={w:.3f} CER={c:.3f} rep_ratio={rep_ratio:.3f} "
          f"bursts={len(bursts)} → {OUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
