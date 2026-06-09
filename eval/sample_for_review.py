"""사람 수용 평가용 표본 추출 (설계 §8 품질 게이트, F2).

런타임 per-segment 검증(validate.py)과 별개로, 정제 품질의 **최종 판정은 오프라인 사람
평가**다(설계 §8: 무작위 N=30 segment, 가독성·정확성 2점 척도). 이 스크립트는 cleaned json
에서 N개 segment 를 무작위 추출해 사람이 라벨링할 리뷰 파일을 만든다.

재현성: 시드는 **인자로 받는다**(Math.random/시간 기반 금지 — 설계 회귀 고정셋 요구).
같은 (입력, 시드, N) → 같은 표본.

사용:
  sudo .venv/bin/python eval/sample_for_review.py \
      output/postprocess/text-meeting.cleaned.json --n 30 --seed 42 \
      --out output/postprocess/review-meeting.md
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path


def sample_segments(segments: list[dict], n: int, seed: int) -> list[dict]:
    """segments 에서 무작위 n개를 시드 고정으로 추출(원순서 보존)."""
    rng = random.Random(seed)  # 명시적 시드 → 재현 가능(고정셋)
    k = min(n, len(segments))
    idxs = sorted(rng.sample(range(len(segments)), k))
    return [segments[i] for i in idxs]


def build_review_md(cleaned_path: Path, sampled: list[dict], n: int, seed: int) -> str:
    lines = [
        f"# 사람 수용 평가 표본 — {cleaned_path.name}",
        "",
        f"- 표본 추출: N={n}, seed={seed} (재현 가능, 회귀 고정셋)",
        "- 라벨: 각 항목에 가독성/정확성을 2점 척도(O/X)로 표기(설계 §8). 합격선 ≥ 90%.",
        "",
        "| id | start | end | original | cleaned | 가독성(O/X) | 정확성(O/X) | 비고 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in sampled:
        orig = str(s.get("original", "")).replace("|", "/").replace("\n", " ")
        clean = str(s.get("cleaned", "")).replace("|", "/").replace("\n", " ")
        lines.append(
            f"| {s.get('id')} | {s.get('start')} | {s.get('end')} "
            f"| {orig} | {clean} |  |  |  |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="정제본에서 사람 평가용 표본 추출(설계 §8)")
    ap.add_argument("cleaned_json", type=Path, help="text-{stem}.cleaned.json")
    ap.add_argument("--n", type=int, default=30, help="표본 개수(기본 30, 설계 §8)")
    ap.add_argument("--seed", type=int, required=True,
                    help="난수 시드(재현성 필수 — 시간/Math.random 금지).")
    ap.add_argument("--out", type=Path, default=None,
                    help="리뷰 md 출력 경로(기본: 입력 옆 review-{stem}.md)")
    args = ap.parse_args()

    if not args.cleaned_json.exists():
        print(f"입력 파일 없음: {args.cleaned_json}", file=sys.stderr)
        return 2

    payload = json.loads(args.cleaned_json.read_text(encoding="utf-8"))
    segments = payload.get("segments", [])
    sampled = sample_segments(segments, args.n, args.seed)

    out = args.out
    if out is None:
        stem = args.cleaned_json.stem.replace(".cleaned", "")
        if stem.startswith("text-"):
            stem = stem[len("text-"):]
        out = args.cleaned_json.parent / f"review-{stem}.md"

    out.write_text(build_review_md(args.cleaned_json, sampled, args.n, args.seed),
                   encoding="utf-8")
    print(f"[sample_for_review] {len(sampled)}/{len(segments)} segment 표본 "
          f"(seed={args.seed}) → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
