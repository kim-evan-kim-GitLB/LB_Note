"""언어 게이트 실측 회귀 — 기존 STT 산출물에 게이트를 돌려 탐지/오탐을 센다.

설계 docs/2026-07-30-영어환각-언어게이트-설계.md §8 의 회귀 기준을 실제 데이터로 확인한다:
  - 알려진 영어 환각 세그먼트를 전부 잡는가(탐지)
  - 정상 발화를 하나도 버리지 않는가(**오탐 0** — 이게 더 중요하다)

실행:
  sudo PYTHONPATH=/app .venv/bin/python tools/bench_language_gate.py
  sudo PYTHONPATH=/app .venv/bin/python tools/bench_language_gate.py --show-all
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.postprocess import language_gate as lg  # noqa: E402

# 계측 대상(전처리 on/off 쌍 + 다른 음원) — 설계 §1-2 캘리브레이션에 쓴 것과 동일 집합.
TARGETS = [
    ("output/asr_test/text-asr test.json", "WPE off"),
    ("output/asr_test_wpe/text-asr test.json", "WPE on"),
    ("output/verify/text-ax과제회의(클로바노트)_음성파일.json", "WPE off"),
]

# 사람이 확인한 환각 세그먼트(시작 초). 이 구간을 못 잡으면 회귀다.
KNOWN_HALLUCINATIONS = {1106.62, 3767.59}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show-all", action="store_true", help="판정된 모든 세그먼트 본문 출력")
    args = ap.parse_args()

    total = n_excluded = n_low_conf = 0
    hits: set[float] = set()
    suspicious: list[tuple[str, float, str, str]] = []

    for rel, preprocess in TARGETS:
        path = ROOT / rel
        if not path.exists():
            print(f"[skip] {rel} 없음")
            continue
        segs = json.loads(path.read_text(encoding="utf-8")).get("segments", [])
        ex = lc = 0
        for s in segs:
            text = s.get("cleaned") or s.get("text") or ""
            start = float(s.get("start") or 0.0)
            end = float(s.get("end") or start)
            verdict, reason = lg.classify(text, start, end)
            total += 1
            if verdict == lg.EXCLUDE:
                ex += 1
                if start in KNOWN_HALLUCINATIONS:
                    hits.add(start)
                else:
                    suspicious.append((rel, start, reason or "", text[:80]))
            elif verdict == lg.LOW_CONF:
                lc += 1
                if args.show_all:
                    suspicious.append((rel, start, f"low_conf/{reason}", text[:80]))
        n_excluded += ex
        n_low_conf += lc
        print(f"{Path(rel).parent.name:16s} ({preprocess:8s}) 세그먼트 {len(segs):4d}  제외 {ex}  저신뢰 {lc}")

    print()
    print(f"총 세그먼트         : {total}")
    print(f"제외(exclude)       : {n_excluded}")
    print(f"저신뢰(low_conf)    : {n_low_conf}")
    print(f"알려진 환각 탐지    : {len(hits)}/{len(KNOWN_HALLUCINATIONS)} (start={sorted(hits)})")
    if suspicious:
        print("\n검토 필요(알려진 환각 외 판정):")
        for rel, start, reason, text in suspicious:
            print(f"  {Path(rel).parent.name} @{start:8.2f} [{reason}] {text}")
    else:
        print("\n알려진 환각 외 제외 없음 — 오탐 0")
    # 알려진 환각을 다 잡고 그 외 제외가 없으면 성공
    ok = len(hits) == len(KNOWN_HALLUCINATIONS) and not [s for s in suspicious if not s[2].startswith("low_conf")]
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
