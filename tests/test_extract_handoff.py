"""액션아이템 추출 **핸드오프 배선** 회귀 테스트 (emit/collect 결정적 부분).

LLM 채움을 제외한 모든 결정적 플럼빙을 잠근다:
  - emit: cleaned.json → work-order(스키마·segment표·빈 슬롯·버전스탬프).
  - collect: 채워진 work-order → 그라운딩 검증(실존 evidence만)·anchor 결정적 산출·중복병합·
             환각(근거없음) 확인필요 flag.

실행: sudo .venv/bin/python tests/test_extract_handoff.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.postprocess.extract_handoff import (  # noqa: E402
    collect_extract_workorder,
    emit_extract_workorder,
)
from src.postprocess.extract_schema import seconds_to_timestamp  # noqa: E402

# 합성 cleaned.json (3 segment, 타임스탬프 명시)
CLEANED = {
    "schema_version": "pp-1.0",
    "segments": [
        {"id": 0, "start": 12.0, "end": 30.0, "cleaned": "다음 주까지 모델 확정하고 보고하겠습니다."},
        {"id": 1, "start": 95.0, "end": 130.0, "cleaned": "마일스톤 초안을 작성해서 공유할게요."},
        {"id": 2, "start": 200.0, "end": 220.0, "cleaned": "회의록 양식을 정의합시다."},
    ],
}


def _write_cleaned(d: Path) -> Path:
    p = d / "text-smoke.cleaned.json"
    p.write_text(json.dumps(CLEANED, ensure_ascii=False), encoding="utf-8")
    return p


def test_emit_structure() -> None:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        cleaned = _write_cleaned(d)
        res = emit_extract_workorder(cleaned, d)
        wo = json.loads(Path(res["workorder_json"]).read_text(encoding="utf-8"))
        assert wo["workorder_schema_version"] == "extract-workorder-1.0", wo
        # 프롬프트 버전은 extract.ko.md 헤더에서 동적으로 읽어 비교(버전업마다 테스트 안 깨지게).
        from src.postprocess.stages.extract import load_extract_prompt_version
        assert wo["prompt_version"] == load_extract_prompt_version(), wo
        assert wo["prompt_version"].startswith("extract-ko-"), wo
        assert wo["source_stem"] == "smoke", wo
        assert len(wo["segments"]) == 3, wo
        assert wo["action_items"] == [], wo  # 빈 슬롯
        assert wo["segments"][0]["text"].startswith("다음 주까지"), wo
        assert "rules" in wo and "추출" in wo["rules"], wo


def test_collect_grounding_anchor_dedup() -> None:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        cleaned = _write_cleaned(d)
        res = emit_extract_workorder(cleaned, d)
        wo_path = Path(res["workorder_json"])
        wo = json.loads(wo_path.read_text(encoding="utf-8"))
        # 에이전트 채움 시뮬레이션: 정상1 + 중복(병합대상) + 환각(없는 id 999)
        wo["action_items"] = [
            {"text": "모델 확정하고 보고", "owner": None, "due": "다음 주", "evidence_seg_ids": [0]},
            {"text": "모델 확정하고 보고", "owner": None, "due": None, "evidence_seg_ids": [1]},  # dup → merge
            {"text": "마일스톤 초안 작성 공유", "evidence_seg_ids": [1]},
            {"text": "근거 없는 환각 항목", "evidence_seg_ids": [999]},  # 실존X → flag
        ]
        wo_path.write_text(json.dumps(wo, ensure_ascii=False), encoding="utf-8")

        out = collect_extract_workorder(wo_path, d)
        data = json.loads(Path(out["json_out"]).read_text(encoding="utf-8"))
        items = data["action_items"]
        assert data["extract_schema_version"] == "extract-1.0", data

        # 중복 병합: '모델 확정하고 보고' 1개로(evidence 합집합 {0,1})
        model_items = [it for it in items if it["text"] == "모델 확정하고 보고"]
        assert len(model_items) == 1, items
        assert sorted(model_items[0]["evidence_seg_ids"]) == [0, 1], model_items
        # anchor = 합집합 최소 start(seg0=12.0s) → 결정적
        assert model_items[0]["anchor"] == seconds_to_timestamp(12.0) == "00:12", model_items

        # 환각 항목: 실존하지 않는 evidence → 확인필요 flag, evidence 비고 anchor None
        halluc = [it for it in items if it["text"] == "근거 없는 환각 항목"][0]
        assert halluc["flag"] == "확인필요", halluc
        assert halluc["evidence_seg_ids"] == [] and halluc["anchor"] is None, halluc
        assert data["n_flagged"] >= 1, data

        # 마일스톤 항목: 정상 anchor(seg1=95.0s → 01:35)
        ms = [it for it in items if "마일스톤" in it["text"]][0]
        assert ms["anchor"] == seconds_to_timestamp(95.0) == "01:35", ms


def test_timestamp_format() -> None:
    assert seconds_to_timestamp(0) == "00:00"
    assert seconds_to_timestamp(95) == "01:35"
    assert seconds_to_timestamp(3700) == "1:01:40"


def _run() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_extract_handoff ({len(fns)} cases)")


if __name__ == "__main__":
    _run()
