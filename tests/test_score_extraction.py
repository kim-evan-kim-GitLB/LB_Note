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

from src.postprocess.score_extraction import (  # noqa: E402
    _covers,
    load_gold,
    load_gold_dir,
    score,
    score_precision,
)

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


# ── [D] 정밀도(precision) 회귀 락 ───────────────────────────────────────────
# negatives[](오탐 라벨)로 과추출을 결정적으로 정량화. 동결 골든은 extract-ko-1.3 산출이라
# axfull idx12("프로 환경 양호 시 돌려보기"=조건부)가 1건 누수돼 있다. 이를 baseline 으로
# 박제하고, v1.4 재추출의 **승격 조건**은 recall=1.0 AND confirmed_FP=0 이다(docs 2026-06-17).

def test_axfull_precision_baseline_locked() -> None:
    """axfull 동결 골든 정밀도 baseline 고정(16건, TP기여 13, 조건부누수 1)."""
    p = score_precision(_load("extracted_axfull.golden.json"), load_gold(GOLD_PATH))
    assert p["n_extracted"] == 16, p
    assert p["precision_strict"] == 0.8125, p
    assert p["confirmed_FP"] == 1, p  # idx12 조건부 누수(extract-ko-1.3) — v1.4 목표=0
    assert p["unmatched_rate"] == 0.125, p


def test_axenh_precision_baseline_locked() -> None:
    """axenh 동결 골든 정밀도 baseline 고정(라벨된 명백 오탐 0)."""
    p = score_precision(_load("extracted_axenh.golden.json"), load_gold(GOLD_PATH))
    assert p["n_extracted"] == 10, p
    assert p["precision_strict"] == 0.8, p
    assert p["confirmed_FP"] == 0, p


def test_precision_direction_holds() -> None:
    """대칭 방향 불변식: axfull 정밀도 ≥ axenh 정밀도(회수율 방향과 동형)."""
    gold = load_gold(GOLD_PATH)
    full = score_precision(_load("extracted_axfull.golden.json"), gold)
    enh = score_precision(_load("extracted_axenh.golden.json"), gold)
    assert full["precision_strict"] >= enh["precision_strict"], (full, enh)


def test_negatives_do_not_match_positives() -> None:
    """변별력 검증: 어떤 negative 도 골드 positive 의 정답 text 를 오매칭하면 안 된다."""
    gold = load_gold(GOLD_PATH)
    negs = gold.get("negatives", [])
    assert negs, "negatives[] 가 비어있음"
    for pos in gold["items"]:
        for neg in negs:
            assert not _covers(pos["text"], neg), (pos["gid"], neg["nid"])


def test_ambiguous_flag_excluded_from_strict_denominator() -> None:
    """flag='약함확인'(모호 캡처)은 strict 분모에서 제외(lenient/strict 이원 집계)."""
    gold = load_gold(GOLD_PATH)
    ext = {"action_items": [
        {"text": "거래처와 납기 일정 협의", "flag": "약함확인"},  # 모호 캡처
        {"text": "마일스톤 초안 작성", "flag": None},            # 확정(gid8 cover)
    ]}
    p = score_precision(ext, gold)
    assert p["n_ambiguous"] == 1, p
    assert p["n_extracted"] == 2, p
    # strict 분모 = 2-1 = 1, 그 1건(마일스톤)이 positive cover → precision_strict=1.0
    assert p["precision_strict"] == 1.0, p


# ── [E] 멀티도메인 시드 정답셋 well-formedness ─────────────────────────────
# 실제 도메인 transcript 가 없어 회수율/정밀도 하드락은 아직 안 건다. 다만 시드 골드가
# 스키마를 지키고, negative 가 자기 positive 를 오매칭하지 않는지(변별력)는 잠가 둔다.

def test_multidomain_seed_golds_wellformed() -> None:
    gold_dir = ROOT / "eval" / "gold"
    golds = load_gold_dir(gold_dir)
    assert set(golds) >= {
        "gold_s2_vendor", "gold_s3_ops", "gold_s4_exec", "gold_s5_cs"
    }, sorted(golds)
    for name, gold in golds.items():
        items = gold.get("items", [])
        assert items, f"{name}: positives 비어있음"
        for it in items:
            assert it.get("keywords"), (name, it)
            assert isinstance(it.get("required", []), list), (name, it)
            assert int(it.get("min_match", 0)) >= 1, (name, it)
        # 변별력: negative 가 자기 도메인 positive 의 정답 text 를 오매칭하면 안 된다.
        for pos in items:
            for neg in gold.get("negatives", []):
                assert not _covers(pos["text"], neg), (name, pos.get("gid"), neg.get("nid"))


def _run() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_score_extraction ({len(fns)} cases)")


if __name__ == "__main__":
    _run()
