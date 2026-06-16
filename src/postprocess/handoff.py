"""인-세션 핸드오프 모드 (REAL, 2-phase). 설계 §3·§5 의 [C] 정제를 모델 백엔드 대신
**이 세션의 코딩 에이전트(Claude Code / Codex)** 가 파일 기반 핸드오프로 수행한다.

정제는 두 phase 사이의 out-of-band 작업이므로 이것을 backend 가 아니라 파이프라인 MODE 로 모델링한다:

  emit  : text-{stem}.json → [A]glossary 교정 → work-order(JSON+MD) 발행.
          각 segment 의 cleaned 는 null(에이전트가 채울 슬롯).
  (에이전트가 work-order 의 cleaned 를 채움 — 코드 밖 작업)
  collect: 채워진 work-order → [D]validate 게이트 → 정상 파이프라인과 동일 산출
           (cleaned.json / 회의록.md / diff.md / 관측성).

타임스탬프는 두 phase 를 1:1 통과한다(emit 가 start/end 운반, collect 가 보존).
기존 auto/passthrough 경로와 모든 선행 수정(F1–F8)은 그대로 유지한다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from src.postprocess.glossary import (
    apply_corrections,
    load_glossary,
    load_glossary_version,
)
from src.postprocess.pipeline import (
    SCHEMA_VERSION,
    _apply_glossary,
    derive_stem,
    gate_segments,
    write_outputs,
)
from src.postprocess.schema import (
    CleanedSegment,
    CleanResult,
    normalize_segments,
)
from src.postprocess.stages.clean import (
    _load_prompt,
    _split_sections,
    load_prompt_version,
)
from src.postprocess.validate import FLAG_REVIEW, SemanticCheck

WORKORDER_SCHEMA_VERSION = "workorder-1.0"


def _load_rules(prompt_path: Path | str | None = None) -> str:
    """정제 규칙 텍스트(설계 §5 정제 규칙)를 prompts/clean.ko.md 의 SYSTEM 섹션에서 추출.

    에이전트가 work-order 만 보고도 허용/금지 편집을 알 수 있도록 규칙을 운반한다.
    """
    system, _user = _split_sections(_load_prompt(prompt_path))
    return system


def emit_workorder(
    text_json: Path | str,
    out_dir: Path | str,
    glossary_path: Path | str | None = None,
    prompt_path: Path | str | None = None,
    context_window: int = 1,
) -> dict:
    """emit phase: 입력 정규화 → [A]glossary → work-order(JSON+MD) 발행.

    - 입력 계약 정규화(F1): start|start_sec / end|end_sec 흡수, 타임스탬프 무음 폴백 금지.
    - 각 segment 의 `original` = glossary 교정 후 본문. `cleaned` = null(에이전트가 채움).
    - context_prev/context_next = 이웃 segment 의 original(읽기전용 컨텍스트).
    - JSON 이 권위 산출(collect 가 파싱), MD 는 사람/에이전트 가독용 동반본.
    """
    text_json = Path(text_json)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = json.loads(text_json.read_text(encoding="utf-8"))
    raw_segments = payload.get("segments", [])
    segments = normalize_segments(raw_segments)  # F1

    glossary = load_glossary(glossary_path)
    glossary_version = load_glossary_version(glossary_path)
    prompt_version = load_prompt_version(prompt_path)
    rules = _load_rules(prompt_path)

    # [A] 결정적 용어 교정 → original 확정(LLM/에이전트 입력 텍스트).
    corrected_segments, _applied_per_seg = _apply_glossary(segments, glossary)

    originals = [s["text"] for s in corrected_segments]
    n = len(corrected_segments)
    wo_segments: list[dict] = []
    for i, seg in enumerate(corrected_segments):
        w = context_window
        prev = " / ".join(originals[max(0, i - w):i])
        nxt = " / ".join(originals[i + 1: i + 1 + w])
        wo_segments.append(
            {
                "id": seg["id"],
                "start": float(seg["start"]),
                "end": float(seg["end"]),
                "context_prev": prev,
                "context_next": nxt,
                "original": originals[i],
                "cleaned": None,  # 에이전트가 채울 슬롯
            }
        )

    stem = derive_stem(text_json)
    wo_json = out_dir / f"text-{stem}.workorder.json"
    wo_md = out_dir / f"text-{stem}.workorder.md"

    if wo_json.exists():
        print(f"[handoff:emit] 경고: 기존 work-order 덮어씀 → {wo_json}", file=sys.stderr)

    header = {
        "workorder_schema_version": WORKORDER_SCHEMA_VERSION,
        "schema_version": SCHEMA_VERSION,
        "glossary_version": glossary_version,
        "prompt_version": prompt_version,
        "source": str(text_json),
        "source_stem": stem,
        "rules": rules,
    }
    wo_payload = {**header, "segments": wo_segments}
    wo_json.write_text(
        json.dumps(wo_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 사람/에이전트 가독용 동반 MD(권위 산출은 JSON).
    _write_workorder_md(wo_md, stem, rules, wo_segments)

    print(f"[handoff:emit] work-order JSON → {wo_json}")
    print(f"[handoff:emit] work-order MD   → {wo_md}")
    print(f"[handoff:emit] segment {n}개, glossary 교정 적용 후 original 확정.")
    print(
        "[handoff:emit] 이 세션의 Claude Code/Codex 가 각 segment 의 `cleaned` 를 채운 뒤 "
        f"`collect` 를 실행하세요: python run_postprocess.py collect {wo_json} --out {out_dir}"
    )

    return {
        "workorder_json": str(wo_json),
        "workorder_md": str(wo_md),
        "n_segments": n,
        "glossary_version": glossary_version,
        "prompt_version": prompt_version,
    }


def _write_workorder_md(
    path: Path, stem: str, rules: str, wo_segments: list[dict]
) -> None:
    """사람/에이전트가 눈으로 보고 cleaned 슬롯을 채울 수 있는 가독용 동반 MD.

    상단에 정제 규칙, 이어서 segment 별 ORIGINAL + 빈 CLEANED 슬롯.
    권위 산출은 JSON 이며 collect 는 JSON 만 파싱한다(이 MD 는 참고용).
    """
    lines = [f"# 정제 work-order — {stem}", ""]
    lines.append("> 권위 산출은 `text-{stem}.workorder.json` 입니다. 이 MD 는 사람/에이전트")
    lines.append("> 가독용 동반본이며, 실제 정제 결과는 JSON 의 `cleaned` 에 채워 `collect` 하세요.")
    lines.append("")
    lines.append("## 정제 규칙 (설계 §5)")
    lines.append("")
    lines.append(rules)
    lines.append("")
    lines.append("## segment (ORIGINAL → CLEANED 슬롯)")
    lines.append("")
    for s in wo_segments:
        lines.append(f"### [{s['id']}] {s['start']:.2f}-{s['end']:.2f}s")
        if s["context_prev"]:
            lines.append(f"- 이전 컨텍스트(읽기전용): {s['context_prev']}")
        if s["context_next"]:
            lines.append(f"- 다음 컨텍스트(읽기전용): {s['context_next']}")
        lines.append(f"- ORIGINAL: {s['original'] or '(빈)'}")
        lines.append("- CLEANED: ")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def collect_workorder(
    workorder_json: Path | str,
    out_dir: Path | str,
    glossary_path: Path | str | None = None,
    edit_lo: float = 0.0,
    edit_hi: float = 0.6,
    require_edit: bool = False,
    semantic_check: SemanticCheck | None = None,
    overwrite: bool = True,
) -> dict:
    """collect phase: 채워진 work-order → [D]게이트 → 정상 파이프라인과 동일 산출.

    - cleaned 가 null/빈 segment → 무편집으로 취급(원문 유지 + 확인필요 flag, 실패 안 함).
    - 기존 validate 게이트(편집비율·glossary 용어 보존·숫자 보존·require_edit·semantic_check)
      를 그대로 적용. 실패 시 graceful degrade(원문 + flag).
    - 산출: text-{stem}.cleaned.json / 회의록-{stem}.md / text-{stem}.diff.md / 관측성 요약.
      writer 는 auto 경로와 공용(write_outputs). 타임스탬프 1:1 보존.

    require_edit 기본 False: 핸드오프는 에이전트가 무편집(원문 적합)을 정당하게 선택할 수
    있으므로 무편집을 '실패'로 잡지 않는다(설계 §6-2, lo 경계 처리). 필요 시 True 로 강제 가능.
    """
    workorder_json = Path(workorder_json)
    out_dir = Path(out_dir)

    wo = json.loads(workorder_json.read_text(encoding="utf-8"))
    wo_segments = wo.get("segments", [])
    source_stem = wo.get("source_stem") or derive_stem(workorder_json)
    glossary_version = wo.get("glossary_version", "unknown")
    prompt_version = wo.get("prompt_version", "unknown")

    # glossary 용어 보존 검사용: 각 segment 의 original 에 실제 등장한 정답 용어.
    glossary = load_glossary(glossary_path)
    canonical_terms = sorted({v for v in glossary.values()})

    seg_objs: list[CleanedSegment] = []
    applied_per_seg: list[list[str]] = []
    unfilled_ids: set[int] = set()  # cleaned 미기입(null/빈) segment id → 확인필요 강제.
    for s in wo_segments:
        sid = int(s["id"])
        original = str(s.get("original", ""))
        raw_cleaned = s.get("cleaned")
        # cleaned 가 null/빈 → 에이전트 미기입. 무편집(원문)으로 degrade + 확인필요(설계 §6-5).
        cleaned = str(raw_cleaned).strip() if raw_cleaned is not None else ""
        if not cleaned:
            cleaned = original
            unfilled_ids.add(sid)
        edits = ["text_edited"] if cleaned != original else []
        seg_objs.append(
            CleanedSegment(
                id=sid,
                start=float(s["start"]),
                end=float(s["end"]),
                original=original,
                cleaned=cleaned,
                edits=edits,
                flag=None,
            )
        )
        # original 에 실제 등장한 정답 용어만 보존 검사 대상으로(설계 §8 동어반복 회피).
        applied_per_seg.append([t for t in canonical_terms if t and t in original])

    result = CleanResult(segments=seg_objs)

    # [D] 게이트 — auto 경로와 동일 함수.
    validated, n_flagged = gate_segments(
        result,
        applied_per_seg,
        edit_lo=edit_lo,
        edit_hi=edit_hi,
        require_edit=require_edit,
        semantic_check=semantic_check,
    )

    # 미기입 슬롯은 게이트 결과와 무관하게 원문 유지 + 확인필요(설계: collect 명세).
    for seg in validated:
        if seg.id in unfilled_ids and seg.flag != FLAG_REVIEW:
            seg.cleaned = seg.original
            seg.edits = []
            seg.edit_ratio = 0.0
            seg.flag = FLAG_REVIEW
            n_flagged += 1
    final = CleanResult(segments=validated)

    glossary_terms_applied = sorted(
        {t for applied in applied_per_seg for t in applied}
    )

    out = write_outputs(
        out_dir,
        source_stem,
        final,
        backend_name="agent_handoff",
        glossary_version=glossary_version,
        prompt_version=prompt_version,
        source=str(workorder_json),
        glossary_terms_applied=glossary_terms_applied,
        n_flagged=n_flagged,
        retry_count=0,
        overwrite=overwrite,
    )
    return out
