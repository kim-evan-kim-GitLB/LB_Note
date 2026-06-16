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


def score_file(extracted_json: Path | str, gold_json: Path | str) -> dict:
    extracted = json.loads(Path(extracted_json).read_text(encoding="utf-8"))
    return score(extracted, load_gold(gold_json))
