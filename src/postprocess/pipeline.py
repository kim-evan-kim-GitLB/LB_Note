"""후처리 오케스트레이션 (설계 §3 전체 흐름).

text-{stem}.json → [A]glossary → [C]CleanStage(backend) → [D]validate gate → 산출:
  - text-{stem}.cleaned.json (정제본 스키마, segment 정렬 보존, original 동반)
  - 회의록-{stem}.md          (사람이 읽는 정제 회의록)
  - text-{stem}.diff.md       (original↔cleaned diff, 사람 그라운딩 검토용, 설계 §2·F7)

모든 LLM 출력은 [D] 게이트 통과. segment 단위 graceful degrade(실패 시 원문 유지 + 확인필요).
멈추지 않는다(STT 소스 폴백 원칙과 동일).

입력 계약(설계 §5·F1): 프로듀서 둘(메인 {start,end} / 도구 {start_sec,end_sec})을
schema.normalize_segments 로 흡수. 타임스탬프 무음(0.0) 폴백 금지.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

from src.postprocess.backends import get_llm_backend
from src.postprocess.backends.base import LLMBackend
from src.postprocess.glossary import (
    apply_corrections,
    load_glossary,
    load_glossary_version,
)
from src.postprocess.schema import CleanedSegment, CleanResult, normalize_segments
from src.postprocess.stages.clean import CleanStage, load_prompt_version
from src.postprocess.validate import FLAG_REVIEW, SemanticCheck, repair_or_degrade

SCHEMA_VERSION = "pp-1.0"


def _glossary_block(glossary: dict[str, str]) -> str:
    """프롬프트 주입용 고정표기 블록(정답표기 목록, 중복제거)."""
    canon: list[str] = []
    for v in glossary.values():
        if v not in canon:
            canon.append(v)
    return ", ".join(canon)


def _apply_glossary(
    segments: list[dict], glossary: dict[str, str]
) -> tuple[list[dict], list[list[str]]]:
    """[A] 각 정규화 segment.text 에 결정적 교정. (교정된 segments, segment별 적용용어) 반환."""
    corrected: list[dict] = []
    applied_per_seg: list[list[str]] = []
    for seg in segments:
        text = seg.get("text", "")
        new_text, applied = apply_corrections(text, glossary)
        new_seg = dict(seg)
        new_seg["text"] = new_text
        corrected.append(new_seg)
        applied_per_seg.append(applied)
    return corrected, applied_per_seg


def _observability_summary(segments: list[CleanedSegment], retry_count: int,
                           n_flagged: int, n_glossary_terms: int) -> dict:
    """run 요약 지표(설계 §10 관측성): edit_ratio 분포·flag율·재시도·glossary 적용 수."""
    ratios = [s.edit_ratio for s in segments]
    n = len(segments)
    if ratios:
        er_min, er_max = min(ratios), max(ratios)
        er_med = statistics.median(ratios)
    else:
        er_min = er_med = er_max = 0.0
    return {
        "n_segments": n,
        "edit_ratio_min": round(er_min, 4),
        "edit_ratio_median": round(er_med, 4),
        "edit_ratio_max": round(er_max, 4),
        "flag_rate": round(n_flagged / n, 4) if n else 0.0,
        "n_flagged": n_flagged,
        "retry_count": retry_count,
        "n_glossary_terms_applied": n_glossary_terms,
    }


def _write_diff_md(path: Path, stem: str, segments: list[CleanedSegment]) -> None:
    """original↔cleaned diff 를 사람이 검토(그라운딩, 설계 §2·F7)할 수 있게 emit."""
    lines = [f"# 정제 diff (그라운딩 검토) — {stem}", ""]
    lines.append("`original`(glossary 교정 후) ↔ `cleaned`(정제문) 비교. "
                 "내용 변조 여부를 사람이 diff 로 최종 승인한다(설계 §2).")
    lines.append("")
    for s in segments:
        tag = f" `{s.flag}`" if s.flag else ""
        changed = "변경" if s.original != s.cleaned else "동일"
        lines.append(f"## [{s.id}] {s.start:.2f}-{s.end:.2f}s "
                     f"(편집비율 {s.edit_ratio:.2f}, {changed}){tag}")
        lines.append(f"- 원문: {s.original or '(빈)'}")
        lines.append(f"- 정제: {s.cleaned or '(빈)'}")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def gate_segments(
    result: CleanResult,
    applied_per_seg: list[list[str]],
    *,
    edit_lo: float,
    edit_hi: float,
    require_edit: bool,
    semantic_check: SemanticCheck | None,
) -> tuple[list[CleanedSegment], int]:
    """[D] 검증·리페어·그라운딩 게이트 (segment 단위 graceful degrade). auto/collect 공용.

    각 segment 를 repair_or_degrade 로 통과시키고, 실패 시 원문 유지 + 확인필요 flag.
    (검증된 segment 목록, flag 개수) 반환. 멈추지 않는다(STT 소스 폴백 원칙과 동일).
    """
    validated: list[CleanedSegment] = []
    n_flagged = 0
    for seg, applied in zip(result.segments, applied_per_seg):
        gated = repair_or_degrade(
            seg,
            retry=None,  # 스텁 단계: 재시도 훅 없음. 실제 모델 연결 시 backend 재요청 주입.
            max_retries=0,
            must_keep_terms=applied,  # glossary 적용 용어는 정제 후에도 보존 필수
            lo=edit_lo,
            hi=edit_hi,
            require_edit=require_edit,
            semantic_check=semantic_check,
        )
        if gated.flag == FLAG_REVIEW:
            n_flagged += 1
        validated.append(gated)
    return validated, n_flagged


def derive_stem(text_json: Path | str) -> str:
    """text-{stem}.json → {stem}. 'text-' 접두 제거(산출 파일명 일관화)."""
    stem = Path(text_json).stem
    if stem.startswith("text-"):
        stem = stem[len("text-"):]
    return stem


def write_outputs(
    out_dir: Path,
    stem: str,
    final: CleanResult,
    *,
    backend_name: str,
    glossary_version: str,
    prompt_version: str,
    source: str,
    glossary_terms_applied: list[str],
    n_flagged: int,
    retry_count: int = 0,
    overwrite: bool = True,
) -> dict:
    """3종 산출(cleaned.json / 회의록.md / diff.md) + 관측성 요약을 쓴다. auto/collect 공용.

    멱등성(설계 §10): 기존 cleaned.json 덮어쓰기 전 알림. overwrite=False 면 skip.
    반환 dict 는 산출 경로·통계·관측성. (skip 시 skipped=True.)
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    json_out = out_dir / f"text-{stem}.cleaned.json"

    # [멱등성] 재실행 덮어쓰기 전 알림(설계 §10).
    if json_out.exists():
        if overwrite:
            print(f"[postprocess] 경고: 기존 산출 덮어씀 → {json_out}", file=sys.stderr)
        else:
            print(f"[postprocess] 기존 산출 존재(덮어쓰기 비활성): {json_out}", file=sys.stderr)
            return {
                "json_out": str(json_out),
                "skipped": True,
                "reason": "exists_and_no_overwrite",
            }

    obs = _observability_summary(
        final.segments, retry_count, n_flagged, len(glossary_terms_applied)
    )

    # 산출 1: 정제본 스키마 JSON (segment 정렬·original·edit_ratio 보존, 버전 스탬프)
    out_payload = {
        "schema_version": SCHEMA_VERSION,
        "glossary_version": glossary_version,
        "prompt_version": prompt_version,
        "source": source,
        "backend": backend_name,
        "glossary_terms_applied": glossary_terms_applied,
        "observability": obs,
        "n_segments": len(final.segments),
        "n_flagged": n_flagged,
        **final.to_dict(),
    }
    json_out.write_text(
        json.dumps(out_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 산출 2: 회의록 markdown
    md_out = out_dir / f"회의록-{stem}.md"
    lines = [f"# 회의록 (정제) — {stem}", ""]
    lines.append(f"- 백엔드: {backend_name}")
    lines.append(f"- 버전: schema={SCHEMA_VERSION} glossary={glossary_version} "
                 f"prompt={prompt_version}")
    lines.append(f"- segment: {len(final.segments)}개 (확인필요 {n_flagged}개)")
    lines.append("")
    lines.append("## 정제 transcript")
    lines.append("")
    lines.append(final.transcript or "(빈 transcript)")
    lines.append("")
    lines.append("## segment 상세")
    lines.append("")
    for s in final.segments:
        tag = f" `{s.flag}`" if s.flag else ""
        lines.append(f"- [{s.start:.2f}-{s.end:.2f}]{tag} {s.cleaned}")
    md_out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 산출 3: original↔cleaned diff(그라운딩 검토용, 설계 §2·F7)
    diff_out = out_dir / f"text-{stem}.diff.md"
    _write_diff_md(diff_out, stem, final.segments)

    print(f"[postprocess] backend={backend_name} segments={len(final.segments)} "
          f"flagged={n_flagged}")
    print(f"[postprocess] 버전: schema={SCHEMA_VERSION} glossary={glossary_version} "
          f"prompt={prompt_version}")
    print(f"[postprocess] 관측성: edit_ratio(min/median/max)="
          f"{obs['edit_ratio_min']}/{obs['edit_ratio_median']}/{obs['edit_ratio_max']} "
          f"flag율={obs['flag_rate']} 재시도={obs['retry_count']} "
          f"glossary적용={obs['n_glossary_terms_applied']}개")
    print(f"[postprocess] cleaned json → {json_out}")
    print(f"[postprocess] 회의록 md   → {md_out}")
    print(f"[postprocess] diff md     → {diff_out}")

    return {
        "json_out": str(json_out),
        "md_out": str(md_out),
        "diff_out": str(diff_out),
        "n_segments": len(final.segments),
        "n_flagged": n_flagged,
        "glossary_terms_applied": glossary_terms_applied,
        "glossary_version": glossary_version,
        "prompt_version": prompt_version,
        "observability": obs,
    }


def run_postprocess(
    text_json: Path | str,
    out_dir: Path | str,
    backend: str | LLMBackend = "passthrough",
    glossary_path: Path | str | None = None,
    edit_lo: float = 0.0,
    edit_hi: float = 0.6,
    group_chars: int = 0,
    semantic_check: SemanticCheck | None = None,
    overwrite: bool = True,
) -> dict:
    """후처리 파이프라인 실행. 산출 경로/통계를 담은 dict 반환.

    passthrough 백엔드: require_edit=False(무편집을 정상 허용, 스모크 경로).
    실제 모델 백엔드: require_edit=True(무편집을 '정제 실패'로 게이트에서 잡음, 설계 §6-2·F5).
    backend 정체성 검사가 아니라 명시적 require_edit 파라미터로 분기한다.
    """
    text_json = Path(text_json)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = json.loads(text_json.read_text(encoding="utf-8"))
    raw_segments = payload.get("segments", [])

    # [F1] 입력 계약 정규화 — 두 프로듀서 스키마 흡수, 타임스탬프 무음 폴백 금지.
    segments = normalize_segments(raw_segments)

    backend_obj = backend if isinstance(backend, LLMBackend) else get_llm_backend(backend)
    is_passthrough = backend_obj.name == "passthrough"
    # passthrough 만 무편집 허용. 정체성 검사로 안전성을 판단하지 않고,
    # 이 분기 결과를 명시적 require_edit 파라미터로만 [D] 게이트에 전달한다.
    require_edit = not is_passthrough

    # [A] 결정적 용어 교정 (LLM 이전, 100% 재현)
    glossary = load_glossary(glossary_path)
    glossary_version = load_glossary_version(glossary_path)
    prompt_version = load_prompt_version()
    corrected_segments, applied_per_seg = _apply_glossary(segments, glossary)

    # [C] 정제 스테이지 (backend 경유, group_chars 로 인접 segment 묶음 완화)
    stage = CleanStage(group_chars=group_chars)
    result = stage.run(
        corrected_segments,
        backend_obj,
        ctx={"glossary_block": _glossary_block(glossary)},
    )

    # [D] 검증·리페어·그라운딩 게이트 (segment 단위 graceful degrade)
    validated, n_flagged = gate_segments(
        result,
        applied_per_seg,
        edit_lo=edit_lo,
        edit_hi=edit_hi,
        require_edit=require_edit,
        semantic_check=semantic_check,
    )
    final = CleanResult(segments=validated)

    glossary_terms_applied = sorted(
        {t for applied in applied_per_seg for t in applied}
    )

    # 산출(cleaned.json / 회의록.md / diff.md) + 관측성 — auto/collect 공용 writer.
    return write_outputs(
        out_dir,
        derive_stem(text_json),
        final,
        backend_name=backend_obj.name,
        glossary_version=glossary_version,
        prompt_version=prompt_version,
        source=str(text_json),
        glossary_terms_applied=glossary_terms_applied,
        n_flagged=n_flagged,
        retry_count=0,  # 스텁 단계: 재시도 훅 미주입 → 0. 실제 모델 연결 시 누적.
        overwrite=overwrite,
    )
