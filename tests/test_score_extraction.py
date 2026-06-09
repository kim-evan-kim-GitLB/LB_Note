"""액션아이템 추출 **회귀 고정** 테스트 (결정적 스코어러 + 골든 픽스처).

LLM 추출 자체는 비결정적이라 CI에서 라이브로 못 돌린다. 대신:
  1) 결정적 스코어러(score_extraction)가 골든 픽스처에서 **정확히 같은 회수율**을 내는지,
  2) 프로젝트 결론(axfull ≥ axenh)이 유지되는지
를 잠근다. 골드셋·스코어러·수용 추출 중 하나라도 바뀌면 이 테스트가 깨진다(회귀 가드).

값은 EVAL_FINAL_3run 의 수용 추출(round-1 axfull 10/10·axenh 7/10)을 결정적 채점한 결과로 고정.
이 결정적 회수율은 LLM-judge(9.67/9.0)보다 보수적인 '바닥값'이다(주석으로 명시).

실행: sudo .venv/bin/python tests/test_score_extraction.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.postprocess.score_extraction import _covers, load_gold, score  # noqa: E402

GOLD_PATH = ROOT / "eval" / "gold_actionitems.json"
FIX = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def test_axfull_golden_full_recall() -> None:
    """axfull(원본, 채택) 골든 → 결정적 회수율 10/10 고정."""
    res = score(_load("extracted_axfull.golden.json"), load_gold(GOLD_PATH))
    assert res["n_gold"] == 10, res
    assert res["n_covered"] == 10, res  # 전 골드 항목 커버
    assert res["recall"] == 1.0, res
    assert res["covered_gids"] == list(range(1, 11)), res


def test_axenh_golden_recall_locked() -> None:
    """axenh(향상) 골든 → 7/10 고정, 미회수 = 자동저장·서버vs로컬·케이스확장(변별자)."""
    res = score(_load("extracted_axenh.golden.json"), load_gold(GOLD_PATH))
    assert res["n_covered"] == 7, res
    assert res["recall"] == 0.7, res
    missing = sorted(g for g, v in res["by_gid"].items() if not v["covered"])
    # GT4(서버vs로컬)·GT6(자동저장)·GT9(케이스확장) — 평가에서 axfull 우위를 가른 항목들
    assert missing == [4, 6, 9], missing


def test_verdict_direction_holds() -> None:
    """프로젝트 결론: axfull 회수율 ≥ axenh (음향향상 net-negative와 일치)."""
    gold = load_gold(GOLD_PATH)
    full = score(_load("extracted_axfull.golden.json"), gold)
    enh = score(_load("extracted_axenh.golden.json"), gold)
    assert full["n_covered"] >= enh["n_covered"], (full, enh)


def test_covers_unit() -> None:
    """_covers 단위: required 그룹·min_match 의미 검증."""
    gold = load_gold(GOLD_PATH)
    g4 = next(g for g in gold["items"] if g["gid"] == 4)  # 서버 required
    # 서버/클라우드(required[0]) 없으면 로컬+제안이 있어도 커버 안 됨
    assert not _covers("로컬에서 돌려보고 대표에게 제안", g4)
    assert _covers("로컬 vs 서버(클라우드) 비용 정리해 대표 제안", g4)
    g8 = next(g for g in gold["items"] if g["gid"] == 8)  # 마일스톤 단일
    assert _covers("마일스톤 초안 작성", g8)
    assert not _covers("그냥 잡담", g8)


def test_empty_extraction_zero_recall() -> None:
    """빈 추출 → 회수율 0(경계)."""
    res = score({"action_items": []}, load_gold(GOLD_PATH))
    assert res["n_covered"] == 0 and res["recall"] == 0.0, res


def _run() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_score_extraction ({len(fns)} cases)")


if __name__ == "__main__":
    _run()
