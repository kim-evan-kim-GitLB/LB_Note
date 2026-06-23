"""인-세션 핸드오프 — 액션아이템 추출 (REAL, 2-phase). 정제 핸드오프(handoff.py)와 동형.

추출은 회의 단위 out-of-band 작업이므로 backend 가 아니라 파이프라인 MODE 로 모델링한다:

  emit   : cleaned.json → transcript(각 줄 `[id] 본문`) + 규칙 + 빈 action_items 슬롯을
           담은 work-order(JSON+MD) 발행.
  (에이전트가 work-order 의 action_items 를 채움 — 코드 밖 작업)
  collect: 채워진 work-order → 그라운딩 검증(evidence_seg_ids ⊆ 실존 id) → anchor 결정적
           산출 → 중복 병합 → 표준 출력(text-{stem}.actionitems.json + 액션아이템-{stem}.md).

그라운딩(설계 §2): 모든 항목은 evidence_seg_ids 로 본문 근거를 제시해야 한다. 근거가 없거나
실존하지 않는 id 만 인용하면 '확인필요' flag(환각 차단). anchor 는 근거 segment 의 최소 start
에서 결정적으로 산출한다(에이전트가 임의 타임스탬프를 못 지어내게).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from src.postprocess.extract_schema import (
    ActionItem,
    ExtractResult,
    load_cleaned_segments,
    seconds_to_timestamp,
    transcript_with_ids,
)
from src.postprocess.glossary import load_glossary_version
from src.postprocess.stages.extract import (
    load_extract_prompt_version,
    load_extract_rules,
)
from src.postprocess.validate import (
    AMBIGUOUS_FLAG,
    FLAG_REVIEW,
    INFERRED_FLAG,
)

WORKORDER_SCHEMA_VERSION = "extract-workorder-1.0"
EXTRACT_SCHEMA_VERSION = "extract-1.0"


def derive_extract_stem(cleaned_json: Path | str) -> str:
    """text-{stem}.cleaned.json → {stem}. 'text-' 접두·'.cleaned' 접미 제거."""
    stem = Path(cleaned_json).stem  # text-axfull.cleaned
    if stem.startswith("text-"):
        stem = stem[len("text-"):]
    if stem.endswith(".cleaned"):
        stem = stem[: -len(".cleaned")]
    return stem


def emit_extract_workorder(
    cleaned_json: Path | str,
    out_dir: Path | str,
    prompt_path: Path | str | None = None,
    glossary_path: Path | str | None = None,
) -> dict:
    """emit phase: cleaned.json → 추출 work-order(JSON+MD) 발행.

    work-order 는 (a) 근거 인용·anchor 산출용 segment 표, (b) 추출 규칙, (c) 빈 action_items
    슬롯을 운반한다. JSON 이 권위 산출, MD 는 가독용 동반본.
    """
    cleaned_json = Path(cleaned_json)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    segments = load_cleaned_segments(cleaned_json)
    rules = load_extract_rules(prompt_path)
    prompt_version = load_extract_prompt_version(prompt_path)
    try:
        glossary_version = load_glossary_version(glossary_path)
    except Exception:
        glossary_version = "unknown"

    stem = derive_extract_stem(cleaned_json)
    wo_json = out_dir / f"text-{stem}.extract.workorder.json"
    wo_md = out_dir / f"text-{stem}.extract.workorder.md"

    if wo_json.exists():
        print(f"[extract:emit] 경고: 기존 work-order 덮어씀 → {wo_json}", file=sys.stderr)

    header = {
        "workorder_schema_version": WORKORDER_SCHEMA_VERSION,
        "extract_schema_version": EXTRACT_SCHEMA_VERSION,
        "glossary_version": glossary_version,
        "prompt_version": prompt_version,
        "source": str(cleaned_json),
        "source_stem": stem,
        "rules": rules,
    }
    # segment 표(근거·anchor 산출용). text 도 운반해 에이전트가 work-order만 보고 채울 수 있게.
    wo_segments = [
        {"id": s["id"], "start": s["start"], "end": s["end"], "text": s["text"]}
        for s in segments
    ]
    wo_payload = {
        **header,
        "segments": wo_segments,
        "action_items": [],  # 에이전트가 채울 슬롯: [{text, owner, due, evidence_seg_ids}]
    }
    wo_json.write_text(
        json.dumps(wo_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_extract_workorder_md(wo_md, stem, rules, segments)

    print(f"[extract:emit] work-order JSON → {wo_json}")
    print(f"[extract:emit] work-order MD   → {wo_md}")
    print(f"[extract:emit] segment {len(segments)}개. 에이전트가 action_items 를 채운 뒤 collect:")
    print(f"  python run_extract.py collect {wo_json} --out {out_dir}")
    return {
        "workorder_json": str(wo_json),
        "workorder_md": str(wo_md),
        "n_segments": len(segments),
        "prompt_version": prompt_version,
    }


def _write_extract_workorder_md(
    path: Path, stem: str, rules: str, segments: list[dict]
) -> None:
    """가독용 동반 MD: 규칙 + transcript(각 줄 `[id] 본문`) + 채움 안내."""
    lines = [f"# 액션아이템 추출 work-order — {stem}", ""]
    lines.append("> 권위 산출은 `text-{stem}.extract.workorder.json` 입니다. 이 MD 는 가독용.")
    lines.append("> JSON 의 `action_items` 를 [{text, owner, due, evidence_seg_ids}] 로 채워 collect 하세요.")
    lines.append("")
    lines.append("## 추출 규칙")
    lines.append("")
    lines.append(rules)
    lines.append("")
    lines.append("## transcript (segment_id 로 근거 인용)")
    lines.append("")
    lines.append("```")
    lines.append(transcript_with_ids(segments))
    lines.append("```")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _normalize_text_key(text: str) -> str:
    """중복 병합용 정규화 키: 공백·구두점 제거 소문자."""
    return "".join(ch for ch in text.lower() if ch.isalnum())


def collect_extract_workorder(
    workorder_json: Path | str,
    out_dir: Path | str,
    overwrite: bool = True,
) -> dict:
    """collect phase: 채워진 work-order → 그라운딩 검증·anchor 산출·중복병합 → 표준 출력.

    - evidence_seg_ids ⊆ 실존 segment id 로 필터. 유효 근거가 0개면 '확인필요' flag(환각 의심).
    - anchor = 유효 근거 segment 의 최소 start → MM:SS(결정적).
    - 같은 정규화 텍스트는 한 항목으로 병합(evidence 합집합).
    """
    workorder_json = Path(workorder_json)
    out_dir = Path(out_dir)

    wo = json.loads(workorder_json.read_text(encoding="utf-8"))
    seg_index = {int(s["id"]): s for s in wo.get("segments", [])}
    source_stem = wo.get("source_stem") or derive_extract_stem(workorder_json)
    glossary_version = wo.get("glossary_version", "unknown")
    prompt_version = wo.get("prompt_version", "unknown")

    parsed = ExtractResult.from_dict(wo)

    items: list[ActionItem] = []
    by_key: dict[str, int] = {}  # 정규화 텍스트 → items 인덱스(병합용)
    for it in parsed.items:
        text = it.text.strip()
        if not text:
            continue
        valid_ev = sorted({e for e in it.evidence_seg_ids if e in seg_index})
        # flag 결정: grounding(근거 0=환각)이 최우선, 그 외엔 LLM 이 보낸 의미 flag(약함확인/추정)를
        # 보존한다. (구버그: flag 를 무조건 None 으로 초기화해 LLM flag 를 전량 폐기했음.)
        semantic_flag = it.flag if it.flag in (AMBIGUOUS_FLAG, INFERRED_FLAG) else None
        flag = FLAG_REVIEW if not valid_ev else semantic_flag
        # 추론 owner(owner_source='inferred')는 '추정' flag 로 격리 보장(LLM 이 빠뜨려도).
        if it.owner_source == "inferred" and valid_ev and flag is None:
            flag = INFERRED_FLAG
        anchor = None
        if valid_ev:
            anchor = seconds_to_timestamp(
                min(float(seg_index[e]["start"]) for e in valid_ev)
            )
        key = _normalize_text_key(text)
        if key in by_key:  # 중복 병합: evidence 합집합, anchor 는 이른 쪽
            tgt = items[by_key[key]]
            merged = sorted(set(tgt.evidence_seg_ids) | set(valid_ev))
            tgt.evidence_seg_ids = merged
            if merged:
                tgt.anchor = seconds_to_timestamp(
                    min(float(seg_index[e]["start"]) for e in merged)
                )
                # grounding flag(확인필요)만 근거 생기면 해제. 의미 flag(약함확인/추정)는 유지.
                if tgt.flag == FLAG_REVIEW:
                    tgt.flag = None
            # 병합 대상이 의미 flag 를 들고 왔는데 tgt 가 비었으면 승계(flag 소실 방지).
            if tgt.flag is None and semantic_flag is not None:
                tgt.flag = semantic_flag
            continue
        item = ActionItem(
            id=len(items),
            text=text,
            owner=it.owner,
            owner_source=it.owner_source,
            due=it.due,
            anchor=anchor,
            evidence_seg_ids=valid_ev,
            flag=flag,
        )
        by_key[key] = len(items)
        items.append(item)

    # n_flagged: 사유별 분리 집계(확인필요/약함확인/추정) — 병합 후 최종 상태 기준.
    flag_breakdown = {
        FLAG_REVIEW: sum(1 for it in items if it.flag == FLAG_REVIEW),
        AMBIGUOUS_FLAG: sum(1 for it in items if it.flag == AMBIGUOUS_FLAG),
        INFERRED_FLAG: sum(1 for it in items if it.flag == INFERRED_FLAG),
    }
    n_flagged = sum(flag_breakdown.values())

    result = ExtractResult(items=items)
    return write_extract_outputs(
        out_dir,
        source_stem,
        result,
        prompt_version=prompt_version,
        glossary_version=glossary_version,
        source=str(workorder_json),
        n_flagged=n_flagged,
        flag_breakdown=flag_breakdown,
        overwrite=overwrite,
    )


def write_extract_outputs(
    out_dir: Path,
    stem: str,
    result: ExtractResult,
    *,
    prompt_version: str,
    glossary_version: str,
    source: str,
    n_flagged: int,
    flag_breakdown: dict | None = None,
    overwrite: bool = True,
) -> dict:
    """표준 출력 2종: text-{stem}.actionitems.json (권위) + 액션아이템-{stem}.md (가독)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_out = out_dir / f"text-{stem}.actionitems.json"

    if json_out.exists():
        if overwrite:
            print(f"[extract] 경고: 기존 산출 덮어씀 → {json_out}", file=sys.stderr)
        else:
            print(f"[extract] 기존 산출 존재(덮어쓰기 비활성): {json_out}", file=sys.stderr)
            return {"json_out": str(json_out), "skipped": True}

    payload = {
        "extract_schema_version": EXTRACT_SCHEMA_VERSION,
        "glossary_version": glossary_version,
        "prompt_version": prompt_version,
        "source": source,
        "n_items": len(result.items),
        "n_flagged": n_flagged,
        "flag_breakdown": flag_breakdown or {},
        **result.to_dict(),
    }
    json_out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    md_out = out_dir / f"액션아이템-{stem}.md"
    lines = [f"# 액션아이템 — {stem}", ""]
    lines.append(f"- 버전: schema={EXTRACT_SCHEMA_VERSION} glossary={glossary_version} "
                 f"prompt={prompt_version}")
    if flag_breakdown:
        bd = " ".join(f"{k} {v}" for k, v in flag_breakdown.items() if v)
        lines.append(f"- 항목: {len(result.items)}개 (검토 {n_flagged}개{': ' + bd if bd else ''})")
    else:
        lines.append(f"- 항목: {len(result.items)}개 (검토 {n_flagged}개)")
    lines.append("")
    for it in result.items:
        tag = f" `{it.flag}`" if it.flag else ""
        who = it.owner or "(미상)"
        due = f" · 기한: {it.due}" if it.due else ""
        anc = f" · {it.anchor}" if it.anchor else ""
        ev = ",".join(str(e) for e in it.evidence_seg_ids)
        lines.append(f"- [{who}]{anc}{due}{tag} {it.text}  _(근거 seg: {ev})_")
    md_out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"[extract] actionitems json → {json_out}")
    print(f"[extract] 액션아이템 md     → {md_out}")
    print(f"[extract] 항목={len(result.items)} 확인필요={n_flagged} "
          f"prompt={prompt_version}")
    return {
        "json_out": str(json_out),
        "md_out": str(md_out),
        "n_items": len(result.items),
        "n_flagged": n_flagged,
        "prompt_version": prompt_version,
    }
