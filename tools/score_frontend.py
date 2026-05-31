"""임의 transcript json 을 클로바 reference 로 채점 (WER/CER/rep_ratio).

전처리 A/B 비교용. run_long_slice10m.py / pipeline.py 출력 json 의
payload["transcript"] 를 읽어 동일 reference 로 점수를 낸다.

사용:
  uv run python tools/score_frontend.py output/fullstack/text-..._slice10m.json \
      --label "+WPE+GTCRN+VAD" --baseline 0.529
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.scoring import cer, normalize, wer  # noqa: E402

DEFAULT_REF = ROOT / "answer" / "ax_tf_클로바.txt"
SPEAKER_RE = re.compile(r"^참석자\s+\d+\s+\d{1,2}:\d{2}\s*$")
MIN_RUN = 5


def load_reference(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    utt = [ln.strip() for ln in lines if ln.strip() and not SPEAKER_RE.match(ln.strip())]
    return normalize(" ".join(utt))


def repetition_ratio(tokens: list[str], min_run: int = MIN_RUN) -> tuple[float, int]:
    n = len(tokens)
    i = rep = bursts = 0
    while i < n:
        j = i
        while j < n and tokens[j] == tokens[i]:
            j += 1
        run = j - i
        if run >= min_run:
            rep += run
            bursts += 1
        i = j
    return (rep / n if n else 0.0), bursts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("hyp_json", type=Path, help="transcript 포함 json")
    ap.add_argument("--ref", type=Path, default=DEFAULT_REF)
    ap.add_argument("--label", default="hyp")
    ap.add_argument("--baseline", type=float, default=None, help="비교용 baseline WER")
    ap.add_argument("--out", type=Path, default=None, help="md 출력 경로(append)")
    args = ap.parse_args()

    data = json.loads(args.hyp_json.read_text(encoding="utf-8"))
    transcript = data.get("transcript", "")
    ref = load_reference(args.ref)
    hyp = normalize(transcript)
    w, c = wer(ref, hyp), cer(ref, hyp)
    rr, nb = repetition_ratio(hyp.split())
    ref_tok, hyp_tok = len(ref.split()), len(hyp.split())
    pre = data.get("preprocess", {})
    perf = data.get("performance", {})

    print(f"=== {args.label} ===")
    print(f"applied        : {pre.get('applied')}")
    print(f"WER            : {w:.4f}" + (f"  (Δ vs {args.baseline}: {w-args.baseline:+.4f})"
                                         if args.baseline is not None else ""))
    print(f"CER            : {c:.4f}")
    print(f"rep_ratio      : {rr:.4f}  (bursts={nb})")
    print(f"ref/hyp tokens : {ref_tok} / {hyp_tok}  (ratio {hyp_tok/ref_tok:.3f})")
    print(f"RTFx           : {perf.get('rtfx')}  vram={perf.get('vram_peak_mb')}MB "
          f"elapsed={perf.get('elapsed_seconds')}s")
    print(f"compressed_sec : {pre.get('compressed_seconds')} "
          f"(speech_regions={pre.get('n_speech_regions')})")

    if args.out:
        line = (f"| {args.label} | {pre.get('applied')} | {w:.4f} | {c:.4f} | "
                f"{rr:.4f} | {hyp_tok/ref_tok:.3f} | {perf.get('rtfx')} | "
                f"{perf.get('elapsed_seconds')} | {pre.get('compressed_seconds')} |")
        header = ("| label | applied | WER↓ | CER↓ | rep_ratio↓ | tok_ratio | "
                  "RTFx↑ | elapsed | comp_sec |\n|---|---|---|---|---|---|---|---|---|")
        p = Path(args.out)
        if not p.exists():
            p.write_text(f"# 프론트엔드 전처리 A/B (ref={args.ref.name})\n\n{header}\n",
                         encoding="utf-8")
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(f"→ appended {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
