"""표준 출력 계약 어댑터 테스트 — 파이프라인 산출 → 웹 Meeting JSON.

웹(meetscript-ai)의 Meeting 계약 {summary, actionItems[], transcript[{speakerId,text,timestamp}]}
형태·타임스탬프 보존·미구현 표시(speakerId="")를 잠근다.

실행: sudo .venv/bin/python tests/test_web_contract.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.postprocess.web_contract import build_meeting_contract  # noqa: E402

CLEANED = {
    "segments": [
        {"id": 0, "start": 12.0, "end": 30.0, "cleaned": "다음 주까지 모델 확정하겠습니다."},
        {"id": 1, "start": 95.0, "end": 130.0, "cleaned": ""},  # 빈 → 제외
        {"id": 2, "start": 200.0, "end": 220.0, "cleaned": "회의록 양식을 정의합시다."},
    ]
}
ACTIONITEMS = {
    "action_items": [
        {"text": "모델 확정", "owner": None, "due": "다음 주", "anchor": "00:12",
         "evidence_seg_ids": [0], "flag": None},
    ]
}


def test_contract_shape_and_timestamps() -> None:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        cj = d / "text-x.cleaned.json"
        aj = d / "text-x.actionitems.json"
        cj.write_text(json.dumps(CLEANED, ensure_ascii=False), encoding="utf-8")
        aj.write_text(json.dumps(ACTIONITEMS, ensure_ascii=False), encoding="utf-8")

        c = build_meeting_contract(cj, aj)
        assert set(c.keys()) == {"summary", "actionItems", "transcript"}, c
        # 빈 segment 제외 → 2개, 타임스탬프 MM:SS 보존
        assert len(c["transcript"]) == 2, c["transcript"]
        assert c["transcript"][0] == {"speakerId": "", "text": "다음 주까지 모델 확정하겠습니다.", "timestamp": "00:12"}
        assert c["transcript"][1]["timestamp"] == "03:20", c["transcript"][1]
        # 화자분리 미적용 → speakerId 빈 문자열
        assert all(t["speakerId"] == "" for t in c["transcript"]), c
        # actionItems 표준 필드 노출
        assert c["actionItems"][0]["text"] == "모델 확정", c
        assert c["actionItems"][0]["anchor"] == "00:12", c
        # summarize 미구현 → 빈 summary
        assert c["summary"] == "", c


def test_contract_without_actionitems() -> None:
    """actionitems 없이도 transcript 만으로 계약 생성(부분 파이프라인)."""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        cj = d / "text-x.cleaned.json"
        cj.write_text(json.dumps(CLEANED, ensure_ascii=False), encoding="utf-8")
        c = build_meeting_contract(cj, None)
        assert c["actionItems"] == [], c
        assert len(c["transcript"]) == 2, c


def _run() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_web_contract ({len(fns)} cases)")


if __name__ == "__main__":
    _run()
