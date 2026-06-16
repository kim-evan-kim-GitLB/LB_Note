"""/api/ai/extract-actions 엔드포인트 회귀 테스트 — 텍스트→string[] 평탄화 계약.

프론트 계약: `POST /api/ai/extract-actions {text} → string[]`. raw text 라 anchor/owner/evidence 는
못 만들고 item.text 만 평탄화. passthrough/빈 입력/예외 → [](graceful).

실행: sudo .venv/bin/python tests/test_extract_actions_endpoint.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.web import app as webapp  # noqa: E402


def _call(text: str) -> list[str]:
    # 엔드포인트를 DI 없이 직접 호출 → require_user 가 주입할 user 를 수동 전달.
    # (사용자별 자격증명 주입 추가로 user["username"] 를 읽으므로 가짜 user 필요.)
    fake_user = {"id": "tester", "username": "tester", "role": "developer"}
    return webapp.ai_extract_actions(webapp.ExtractRequest(text=text), user=fake_user)


def test_passthrough_returns_empty() -> None:
    orig = webapp.EXTRACT_BACKEND
    webapp.EXTRACT_BACKEND = "passthrough"
    try:
        assert _call("뭔가 하기로 했다") == []
    finally:
        webapp.EXTRACT_BACKEND = orig


def test_empty_text_returns_empty() -> None:
    orig = webapp.EXTRACT_BACKEND
    webapp.EXTRACT_BACKEND = "fake"
    try:
        assert _call("   ") == []
    finally:
        webapp.EXTRACT_BACKEND = orig


def test_flattens_to_text_list() -> None:
    orig_be, orig_fn = webapp.EXTRACT_BACKEND, webapp.extract_action_items
    webapp.EXTRACT_BACKEND = "fake"
    # raw text 라 anchor/owner 는 무의미 → text 만 노출되는지 검증
    webapp.extract_action_items = lambda segs, backend_name: [
        {"text": "모델 확정", "owner": None, "anchor": None},
        {"text": "보고서 작성", "owner": "SW2팀", "anchor": None},
        {"text": "", "owner": None},  # 빈 text 는 제외
    ]
    try:
        out = _call("줄1\n줄2")
        assert out == ["모델 확정", "보고서 작성"], out
    finally:
        webapp.EXTRACT_BACKEND, webapp.extract_action_items = orig_be, orig_fn


def test_extract_exception_is_graceful() -> None:
    orig_be, orig_fn = webapp.EXTRACT_BACKEND, webapp.extract_action_items

    def _boom(segs, backend_name):
        raise RuntimeError("backend down")

    webapp.EXTRACT_BACKEND = "fake"
    webapp.extract_action_items = _boom
    try:
        assert _call("아무 텍스트") == []  # 예외 → [] (멈춤 없음)
    finally:
        webapp.EXTRACT_BACKEND, webapp.extract_action_items = orig_be, orig_fn


def _run() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_extract_actions_endpoint ({len(fns)} cases)")


if __name__ == "__main__":
    _run()
