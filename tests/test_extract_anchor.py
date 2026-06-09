"""추출 anchor 결정적 산출 회귀 테스트 — service.extract_action_items.

버그(2026-06-09): anchor 는 evidence_seg_ids 의 최소 start 에서 호출부가 결정적으로 채워야
하는데(ActionItem.anchor 계약), 종전엔 LLM 출력 anchor(보통 null)를 그대로 통과시켜 **항상
null** 이었다. 이 테스트는 anchor 가 evidence 최소 start 의 MM:SS 로 채워지고, evidence 가
없으면 None 임을 잠근다. 가짜 백엔드라 클라우드 호출 없음.

실행: sudo .venv/bin/python tests/test_extract_anchor.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.postprocess.backends.base import LLMBackend, LLMCapabilities  # noqa: E402
from src.web import service  # noqa: E402

SEGS = [
    {"id": 0, "start": 12.0, "end": 30.0, "cleaned": "가", "text": "가"},
    {"id": 1, "start": 370.0, "end": 400.0, "cleaned": "나", "text": "나"},
    {"id": 2, "start": 1961.0, "end": 1980.0, "cleaned": "다", "text": "다"},
]

EXTRACT_OUT = {
    "action_items": [
        # evidence 최소 start = seg1(370s=06:10) (seg2 가 먼저 적혀도 min 으로 정렬)
        {"id": 0, "text": "A", "evidence_seg_ids": [2, 1], "anchor": None},
        # evidence 1건
        {"id": 1, "text": "B", "evidence_seg_ids": [0], "anchor": "잘못된값"},
        # evidence 없음 → anchor None
        {"id": 2, "text": "C", "evidence_seg_ids": [], "anchor": None},
    ]
}


class _FakeBackend(LLMBackend):
    name = "fake"

    def generate(self, messages, *, schema=None, temperature=0.0, max_tokens=2048, seed=0):
        return json.dumps(EXTRACT_OUT, ensure_ascii=False)

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(json_mode=True, ctx_window=200_000, tool_call=False, determinism="full")


def test_anchor_from_evidence_min_start() -> None:
    orig = service.get_llm_backend
    service.get_llm_backend = lambda name: _FakeBackend()
    try:
        items = service.extract_action_items(SEGS, backend_name="fake")
    finally:
        service.get_llm_backend = orig
    assert len(items) == 3, items
    assert items[0]["anchor"] == "06:10", items[0]  # min(370,1961)=370s
    assert items[1]["anchor"] == "00:12", items[1]  # 12s, LLM 의 잘못된값 덮어씀
    assert items[2]["anchor"] is None, items[2]      # evidence 없음


def _run() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_extract_anchor ({len(fns)} cases)")


if __name__ == "__main__":
    _run()
