"""다중 agent core A/B 결과 비교 리포트 — 객관 지표만 계산한다.

tools/eval_core_ab.py 산출물(output/eval-core/*.json)을 읽어 A(구버전) vs B(신버전)을 비교한다.

**정답(ground truth)이 없다**는 점을 전제로 설계했다. "요약 품질이 좋아졌다" 같은 주장은 이
스크립트가 하지 않는다. 대신 검증 가능한 사실만 센다:

  1. 환각 인용     : 알려진 STT 환각 세그먼트를 근거로 삼은 항목 수 (낮을수록 좋음, 자명한 오류)
  2. 근거 무결성   : evidence 가 실제 입력 세그먼트에 존재하는 비율 / 항목당 근거 수
  3. 출력 언어     : 라틴 우세(한국어가 아닌) 산출 항목 수 (한국어 회의록이므로 낮을수록 좋음)
  4. 산출량        : 안건·논의·결정·이슈·액션 개수 (많다 != 좋다 — 과잉생성 판단 재료)
  5. flag 분포     : 확인필요/약함확인/추정
  6. 비용          : LLM 콜 수, wall-clock
  7. 항목 대조     : 액션 텍스트 기준 A∩B / A만 / B만 (사람이 판단할 재료)

실행:
  sudo PYTHONPATH=/app .venv/bin/python tools/eval_core_report.py --dataset asr_test
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.eval_core_ab import DATASETS, load_segments  # noqa: E402

_HANGUL = re.compile(r"[가-힣]")
_LATIN = re.compile(r"[A-Za-z]")

# 사람이 확인한 STT 환각 세그먼트(위치 인덱스 = id). tools/bench_language_gate.py 와 같은 구간.
KNOWN_HALLUCINATION_IDS = {"asr_test": {41}, "asr_test_wpe": {41}, "ax": {158}}


def _summary_items(summary: dict) -> list[dict]:
    """요약 dict → 평탄화된 항목 목록(섹션 태그 포함)."""
    out = []
    for blk in (summary or {}).get("agenda") or []:
        for section in ("points", "decisions", "issues"):
            for it in blk.get(section) or []:
                out.append(
                    {
                        "section": section,
                        "agenda": blk.get("title"),
                        "text": str(it.get("text") or ""),
                        "evidence": [int(x) for x in (it.get("evidence_seg_ids") or [])],
                    }
                )
    return out


def _action_items(payload: dict) -> list[dict]:
    out = []
    for it in payload.get("actionItems") or []:
        out.append(
            {
                "text": str(it.get("text") or ""),
                "owner": it.get("owner"),
                "flag": it.get("flag"),
                "evidence": [int(x) for x in (it.get("evidence_seg_ids") or [])],
            }
        )
    return out


def _is_latin_dominant(text: str) -> bool:
    h, latin = len(_HANGUL.findall(text)), len(_LATIN.findall(text))
    return (h + latin) > 0 and h / (h + latin) < 0.5


def analyze(payload: dict, valid_ids: set[int], hallu_ids: set[int]) -> dict:
    s_items = _summary_items(payload.get("summary") or {})
    a_items = _action_items(payload)
    items = s_items + a_items

    ev_total = sum(len(i["evidence"]) for i in items)
    ev_valid = sum(len([e for e in i["evidence"] if e in valid_ids]) for i in items)
    hallu_cited = [i for i in items if set(i["evidence"]) & hallu_ids]
    no_evidence = [i for i in items if not i["evidence"]]
    latin_items = [i for i in items if _is_latin_dominant(i["text"])]

    agenda = (payload.get("summary") or {}).get("agenda") or []
    calls = (payload.get("coreMeta") or {}).get("calls") or {}
    return {
        "elapsedSec": (payload.get("_eval") or {}).get("elapsedSec"),
        "calls": calls.get("total") or sum(v for v in calls.values() if isinstance(v, int)),
        "nAgenda": len(agenda),
        "nPoints": sum(1 for i in s_items if i["section"] == "points"),
        "nDecisions": sum(1 for i in s_items if i["section"] == "decisions"),
        "nIssues": sum(1 for i in s_items if i["section"] == "issues"),
        "nSummaryItems": len(s_items),
        "nActions": len(a_items),
        "halluCited": len(hallu_cited),
        "halluCitedItems": [i["text"][:70] for i in hallu_cited],
        "evidenceValidRatio": round(ev_valid / ev_total, 4) if ev_total else None,
        "evidencePerItem": round(ev_total / len(items), 2) if items else 0.0,
        "noEvidenceItems": len(no_evidence),
        "latinDominantItems": len(latin_items),
        "latinExamples": [i["text"][:70] for i in latin_items[:3]],
        "flags": {
            f: sum(1 for i in a_items if i["flag"] == f)
            for f in ("확인필요", "약함확인", "추정")
        },
        "ownersFilled": sum(1 for i in a_items if i["owner"]),
        "actionTexts": [i["text"] for i in a_items],
    }


def _norm(text: str) -> str:
    return re.sub(r"[\s·.,()]+", "", text).lower()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", choices=sorted(DATASETS), default="asr_test")
    ap.add_argument("--run", type=int, default=1)
    args = ap.parse_args()

    segments = load_segments(DATASETS[args.dataset])
    valid_ids = {s["id"] for s in segments}
    hallu_ids = KNOWN_HALLUCINATION_IDS.get(args.dataset, set())
    base = ROOT / "output" / "eval-core"

    reports: dict[str, dict] = {}
    for ver in ("A", "B"):
        path = base / f"{args.dataset}-{ver}-run{args.run}.json"
        if not path.exists():
            print(f"[skip] {path.name} 없음")
            continue
        reports[ver] = analyze(json.loads(path.read_text(encoding="utf-8")), valid_ids, hallu_ids)

    if len(reports) < 2:
        print("A/B 둘 다 필요합니다.")
        return 1

    a, b = reports["A"], reports["B"]
    print(f"\n=== {args.dataset} (세그먼트 {len(segments)}, 환각 id={sorted(hallu_ids)}) run{args.run} ===\n")
    rows = [
        ("환각 세그먼트 인용 항목", "halluCited", "낮을수록 좋음(자명한 오류)"),
        ("근거 없는 항목", "noEvidenceItems", "낮을수록 좋음"),
        ("근거 유효율", "evidenceValidRatio", "1.0 이 정상"),
        ("항목당 근거 수", "evidencePerItem", "중립"),
        ("라틴 우세 산출 항목", "latinDominantItems", "낮을수록 좋음(한국어 회의록)"),
        ("안건 수", "nAgenda", "중립"),
        ("논의(points)", "nPoints", "중립"),
        ("결정(decisions)", "nDecisions", "중립"),
        ("이슈(issues)", "nIssues", "중립"),
        ("요약 항목 합", "nSummaryItems", "중립"),
        ("액션아이템", "nActions", "중립"),
        ("owner 채워진 액션", "ownersFilled", "중립(오귀속 위험 있음)"),
        ("LLM 콜 수", "calls", "낮을수록 저렴"),
        ("wall-clock(초)", "elapsedSec", "낮을수록 빠름"),
    ]
    print(f"{'지표':26s} {'A(구)':>10s} {'B(신)':>10s}   해석")
    print("-" * 78)
    for label, key, note in rows:
        av, bv = a.get(key), b.get(key)
        print(f"{label:26s} {str(av):>10s} {str(bv):>10s}   {note}")
    print(f"\nflag 분포   A={a['flags']}  B={b['flags']}")
    if a["halluCitedItems"]:
        print("\n[A] 환각 근거 항목:")
        for t in a["halluCitedItems"]:
            print(f"  - {t}")
    if b["halluCitedItems"]:
        print("\n[B] 환각 근거 항목:")
        for t in b["halluCitedItems"]:
            print(f"  - {t}")
    if a["latinExamples"] or b["latinExamples"]:
        print(f"\n라틴 우세 예시  A={a['latinExamples']}  B={b['latinExamples']}")

    an = {_norm(t): t for t in a["actionTexts"]}
    bn = {_norm(t): t for t in b["actionTexts"]}
    common = set(an) & set(bn)
    print(f"\n액션 대조: 공통(문자열 동일) {len(common)}  A만 {len(set(an) - common)}  B만 {len(set(bn) - common)}")
    print("  ※ 표현만 달라도 '다름'으로 세므로, 아래 목록은 사람이 직접 대조할 재료다.")
    print("\n[A만 있는 액션]")
    for k in sorted(set(an) - common):
        print(f"  - {an[k]}")
    print("\n[B만 있는 액션]")
    for k in sorted(set(bn) - common):
        print(f"  - {bn[k]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
