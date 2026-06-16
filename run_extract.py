"""액션아이템 추출 CLI (정제 파이프라인 후속, 설계 §5 Phase 1-b).

모드:
  emit    : cleaned.json → 추출 work-order(JSON+MD) 발행. 에이전트가 action_items 채움.
  collect : 채워진 work-order → 그라운딩 검증·anchor 산출·중복병합 → 표준 출력
            (text-{stem}.actionitems.json + 액션아이템-{stem}.md).
  score   : 추출 산출(actionitems.json / eval extracted_*.json) → 골드셋 대비 결정적 회수율.

사용:
  python run_extract.py emit output/postprocess/text-axfull.cleaned.json --out output/extract
  #   (에이전트가 work-order 의 action_items 를 채움)
  python run_extract.py collect output/extract/text-axfull.extract.workorder.json --out output/extract
  python run_extract.py score output/extract/text-axfull.actionitems.json --gold eval/gold_actionitems.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.postprocess.extract_handoff import (  # noqa: E402
    collect_extract_workorder,
    emit_extract_workorder,
)
from src.postprocess.score_extraction import score_file  # noqa: E402

MODES = {"emit", "collect", "score"}
DEFAULT_GOLD = Path(__file__).resolve().parent / "eval" / "gold_actionitems.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="액션아이템 추출(정제본 → 액션아이템)")
    ap.add_argument("mode", choices=sorted(MODES), help="emit | collect | score")
    ap.add_argument("input", type=str, help="emit: cleaned.json | collect: work-order | score: actionitems json")
    ap.add_argument("--out", type=Path, default=Path("output/extract"), help="산출 디렉터리")
    ap.add_argument("--gold", type=Path, default=DEFAULT_GOLD, help="(score) 골드셋 경로")
    ap.add_argument("--glossary", type=Path, default=None, help="(emit) glossary 버전 스탬프용")
    ap.add_argument("--no-overwrite", action="store_true", help="기존 산출 보존")
    args = ap.parse_args(argv)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"입력 파일 없음: {input_path}", file=sys.stderr)
        return 2

    if args.mode == "emit":
        emit_extract_workorder(input_path, args.out, glossary_path=args.glossary)
        return 0

    if args.mode == "collect":
        collect_extract_workorder(input_path, args.out, overwrite=not args.no_overwrite)
        return 0

    # score
    res = score_file(input_path, args.gold)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    missing = [g for g, v in res["by_gid"].items() if not v["covered"]]
    print(f"\n회수율 {res['n_covered']}/{res['n_gold']} (recall={res['recall']}) "
          f"미회수 gid={missing}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
