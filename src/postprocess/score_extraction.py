"""액션아이템 추출 **결정적 회귀 스코어러** (재현 가능, CI 친화).

평가(EVAL_FINAL_3run)의 회수율은 LLM-judge(의미 매칭)였다 → CI에 라이브 LLM을 넣을 수 없다.
그래서 골드 항목마다 **변별 키워드 그룹**을 부여하고, 추출 항목 텍스트가 그 그룹을 충족하는지로
회수(recall)를 **결정적**으로 판정한다. 같은 입력 → 같은 점수(100% 재현). 이 점수는 LLM-judge보다
보수적인 '바닥값'이며, 회귀 고정(골드·스코어러·수용 추출이 안 바뀌었는지)의 잠금 장치다.

골드 스키마(eval/gold_actionitems.json):
  items[].keywords : 그룹 목록. 각 그룹 = 동의어 substring 목록(그룹 내 OR).
  items[].min_match: 충족해야 할 최소 그룹 수.
  items[].required : 반드시 충족해야 할 그룹 인덱스(변별자, 예 gid4의 '서버/클라우드').
"""
from __future__ import annotations

import json
from pathlib import Path


def load_gold(gold_json: Path | str) -> dict:
    return json.loads(Path(gold_json).read_text(encoding="utf-8"))


def load_gold_dir(gold_dir: Path | str, pattern: str = "*.json") -> dict[str, dict]:
    """[E] 멀티도메인 정답셋 로드: eval/gold/*.json → {stem: gold}.

    회의 유형(벤더·운영·경영·CS) 별 미니 골드를 한 번에 채점하기 위한 글롭 로더.
    단일 GOLD_PATH 에 묶여 S2~S5 사각이 회귀에서 침묵하던 문제(결정문서 §E)를 푼다.
    """
    out: dict[str, dict] = {}
    for p in sorted(Path(gold_dir).glob(pattern)):
        out[p.stem] = load_gold(p)
    return out


def _group_satisfied(group: list[str], text: str) -> bool:
    return any(syn in text for syn in group)


def _covers(item_text: str, gold_item: dict) -> bool:
    """추출 항목 텍스트가 골드 항목을 '커버'하는가(결정적)."""
    groups: list[list[str]] = gold_item.get("keywords", [])
    if not groups:
        return False
    satisfied = [i for i, g in enumerate(groups) if _group_satisfied(g, item_text)]
    required = gold_item.get("required", [])
    if not all(r in satisfied for r in required):
        return False
    min_match = int(gold_item.get("min_match", len(groups)))
    return len(satisfied) >= min_match


def _item_texts(extracted: dict | list) -> list[str]:
    """추출 산출(actionitems.json / eval extracted_*.json / 리스트) → 텍스트 목록."""
    if isinstance(extracted, list):
        items = extracted
    else:
        items = extracted.get("action_items", extracted.get("actionItems", []))
    out: list[str] = []
    for it in items:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            out.append(str(it.get("text", "")))
    return out


def score(extracted: dict | list, gold: dict) -> dict:
    """결정적 회수율. {recall, n_gold, n_covered, covered_gids, by_gid} 반환."""
    texts = _item_texts(extracted)
    gold_items = gold.get("items", [])
    by_gid: dict[int, dict] = {}
    covered_gids: list[int] = []
    for gi in gold_items:
        gid = int(gi["gid"])
        match_idx = next(
            (j for j, t in enumerate(texts) if _covers(t, gi)), None
        )
        hit = match_idx is not None
        by_gid[gid] = {"covered": hit, "matched_item": match_idx}
        if hit:
            covered_gids.append(gid)
    n_gold = len(gold_items)
    n_cov = len(covered_gids)
    return {
        "recall": round(n_cov / n_gold, 4) if n_gold else 0.0,
        "n_gold": n_gold,
        "n_covered": n_cov,
        "covered_gids": covered_gids,
        "by_gid": by_gid,
    }


def _ambiguous_flagged(extracted: dict | list) -> list[bool]:
    """추출 항목별 '약함확인'(모호 캡처) 여부. 리스트/문자열 입력은 전부 False."""
    if isinstance(extracted, list):
        items = extracted
    else:
        items = extracted.get("action_items", extracted.get("actionItems", []))
    out: list[bool] = []
    for it in items:
        out.append(isinstance(it, dict) and it.get("flag") == "약함확인")
    return out


def score_precision(extracted: dict | list, gold: dict) -> dict:
    """결정적 **정밀도**. negatives[](오탐 라벨)로 과추출을 정량화한다(LLM-judge 불필요).

    각 추출 텍스트를 3분류한다(positives 우선):
      - 어떤 positive 를 cover  → TP 기여
      - 아니면서 어떤 negative 를 match → confirmed_FP(라벨된 명백 비액션)
      - 둘 다 아님 → unmatched(병합 변형/미라벨 회색)

    flag='약함확인'(모호 캡처) 항목은 strict 분모에서 분리해, C 안건의 캡처가 precision 을
    부당하게 깎지 않게 한다(lenient/strict 이원 집계).
    반환: {precision_strict, confirmed_FP, fp_rate, unmatched_rate, n_extracted, n_ambiguous}.
    """
    texts = _item_texts(extracted)
    amb = _ambiguous_flagged(extracted)
    positives = gold.get("items", [])
    negatives = gold.get("negatives", [])
    n_ext = len(texts)

    tp = fp = unmatched = 0
    tp_strict = 0  # 약함확인 제외한 '확정' 추출 중 positive cover 수(분자도 분모와 동일 집합)
    for t, a in zip(texts, amb):
        if any(_covers(t, p) for p in positives):
            tp += 1
            if not a:
                tp_strict += 1
        elif any(_covers(t, neg) for neg in negatives):
            fp += 1
        else:
            unmatched += 1
    n_amb = sum(1 for a in amb if a)
    # strict 분자·분모 모두 약함확인 제외(확정 캡처만). 분모 0 보호. → precision_strict ≤ 1.0.
    strict_den = n_ext - n_amb
    return {
        "precision_strict": round(tp_strict / strict_den, 4) if strict_den else 0.0,
        "confirmed_FP": fp,
        "fp_rate": round(fp / n_ext, 4) if n_ext else 0.0,
        "unmatched_rate": round(unmatched / n_ext, 4) if n_ext else 0.0,
        "n_extracted": n_ext,
        "n_ambiguous": n_amb,
        # 약함확인 비율 — 예산(~0.25) 초과 시 남발 신호(비파괴적 관찰값).
        "ambiguous_rate": round(n_amb / n_ext, 4) if n_ext else 0.0,
        "over_flag_budget": bool(n_ext) and (n_amb / n_ext) > 0.25,
    }


def score_file(extracted_json: Path | str, gold_json: Path | str) -> dict:
    extracted = json.loads(Path(extracted_json).read_text(encoding="utf-8"))
    gold = load_gold(gold_json)
    out = score(extracted, gold)
    out["precision"] = score_precision(extracted, gold)
    return out
