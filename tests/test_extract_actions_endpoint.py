"""/api/ai/extract-actions 엔드포인트 회귀 테스트 — 텍스트→string[] 평탄화 계약.

프론트 계약: `POST /api/ai/extract-actions {text} → string[]`. raw text 라 anchor/owner/evidence 는
못 만들고 item.text 만 평탄화. passthrough/빈 입력/예외 → [](graceful).

격리: src.web.app 은 모듈 로드 시 store=MeetingStore()/users=auth.init() 를 실행하므로,
**실 DB(output/web/meetings.db)를 절대 건드리지 않게** store/auth 의 DEFAULT_DB_PATH 를 임시
경로로 패치한 뒤에야 app 을 import 한다. 패치 전역은 모듈 teardown 에서 try/finally 로 원복한다.

실행: sudo MEETSCRIPT_BLOCK_DEFAULT_DB=1 PYTHONPATH=/app .venv/bin/python tests/test_extract_actions_endpoint.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ---- 격리된 app 로드: DEFAULT_DB_PATH 패치 후 import (실 DB 미접촉) ----
_tmpdir = tempfile.TemporaryDirectory()
_tmp_db = Path(_tmpdir.name) / "meetings.db"

import importlib  # noqa: E402

import src.web.store as _storemod  # noqa: E402
import src.web.auth as _authmod  # noqa: E402

_store_orig = _storemod.DEFAULT_DB_PATH
_auth_orig = _authmod.DEFAULT_DB_PATH

# auth.init() 는 JWT_SECRET 이 없으면 즉시 실패 → 테스트용 시크릿/사용자 시드 주입.
os.environ.setdefault("JWT_SECRET", "test-secret-extract-actions")
os.environ.setdefault("WEB_AUTH_USERS", "tester:pw1")

_storemod.DEFAULT_DB_PATH = _tmp_db   # MeetingStore() 임시 경로
_authmod.DEFAULT_DB_PATH = _tmp_db    # auth.init() no-arg 폴백 임시 경로
importlib.reload(_authmod)            # 패치된 DEFAULT_DB_PATH 재바인딩
_authmod.DEFAULT_DB_PATH = _tmp_db    # reload 로 되돌아온 모듈상수 다시 임시 경로로

from src.web import app as webapp  # noqa: E402  (이 시점엔 임시 DB 만 사용)


def _restore_globals() -> None:
    """전역 원복 — 후속 테스트/임포트가 실 DB 경로 오염을 보지 않게."""
    _storemod.DEFAULT_DB_PATH = _store_orig
    _authmod.DEFAULT_DB_PATH = _auth_orig
    _tmpdir.cleanup()


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
    try:
        for fn in fns:
            fn()
            print(f"  ok: {fn.__name__}")
        print(f"PASS test_extract_actions_endpoint ({len(fns)} cases)")
    finally:
        _restore_globals()


if __name__ == "__main__":
    _run()
