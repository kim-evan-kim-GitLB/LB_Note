"""자동 요약 가용 상태 공개 계약 — GET /api/ai/summary-availability.

배경(실사고): 배포 후 "전사만 있고 요약이 없다" 는 신고가 반복됐다. 원인은 사용자별 claude
자격증명 미등록이었는데, 화면은 "이 버전(v1)은 전사만 제공합니다" 라는 낡은 문구를 띄웠다 —
자동 요약은 켜져 있었고, 사용자가 설정에서 1분이면 고칠 수 있는 문제였는데 고칠 방법도,
재요약을 시도할 이유도 알 수 없었다.

저장된 회의록에는 "그때 왜 비었는지"(_core_meta·diag)가 남지 않는다(잡 폴링 응답 전용).
그래서 검토 화면은 과거 원인을 댈 수 없고, 대신 **현재 상태**를 알려야 한다.

검증 불변식:
  - 문구는 **서버가 확정**한다. 프론트가 사유코드로 문구를 만들면, 사유를 추가할 때 화면이
    서버를 못 따라와 빈칸이 뜬다.
  - 모르는 것을 "안 된다" 고 단정하지 않는다(헬스 스윕 전 = unchecked → available).
  - claude 가 아닌 요약 백엔드에는 인증 안내를 띄우지 않는다 — 거짓 안내가 된다.

실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_summary_availability.py -q
"""
from __future__ import annotations

import contextlib
import importlib
import os
import tempfile
from pathlib import Path

ENDPOINT = "/api/ai/summary-availability"


@contextlib.contextmanager
def _client(*, summarize_backend: str):
    """요약 백엔드를 지정해 앱을 새로 적재한다.

    SUMMARIZE_BACKEND 는 **모듈 임포트 시점**에 env 에서 읽히므로(app.py 상단 상수),
    reload 전에 env 를 세팅해야 한다. 이 순서가 뒤집히면 테스트가 조용히 .env 값을 검사한다.
    """
    from fastapi.testclient import TestClient

    saved = {k: os.environ.get(k) for k in
             ("JWT_SECRET", "WEB_AUTH_USERS", "WEB_AUTH_ADMINS", "WEB_SUMMARIZE_BACKEND",
              "WEB_EXTRACT_BACKEND", "MEETSCRIPT_BLOCK_DEFAULT_DB")}
    with tempfile.TemporaryDirectory() as td:
        tmp_db = Path(td) / "meetings.db"
        os.environ["JWT_SECRET"] = "test-secret-summary-availability"
        os.environ["WEB_AUTH_USERS"] = "admin:pw1,alice:pw2"  # "user:pass" 형식(단일 진실원천)
        os.environ["WEB_AUTH_ADMINS"] = "admin"
        os.environ["WEB_SUMMARIZE_BACKEND"] = summarize_backend
        os.environ["WEB_EXTRACT_BACKEND"] = "passthrough"
        os.environ["MEETSCRIPT_BLOCK_DEFAULT_DB"] = "1"
        import src.web.store as storemod
        store_orig = storemod.DEFAULT_DB_PATH
        try:
            storemod.DEFAULT_DB_PATH = tmp_db
            import src.web.auth as auth
            importlib.reload(auth)
            auth.DEFAULT_DB_PATH = tmp_db
            import src.web.app as appmod
            importlib.reload(appmod)
            with TestClient(appmod.app) as client:
                yield auth, appmod, client
        finally:
            storemod.DEFAULT_DB_PATH = store_orig
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def _headers(auth, appmod, user="alice"):
    appmod.users.set_password(user, "newpassword123")  # must_change_password 해제(403 방지)
    return {"Authorization": f"Bearer {auth.make_token(user)}"}


def _get(client, auth, appmod, user="alice") -> dict:
    res = client.get(ENDPOINT, headers=_headers(auth, appmod, user))
    assert res.status_code == 200, res.text
    return res.json()


# ------------------------------------------------------------------ 서버 설정
def test_요약백엔드가_꺼져_있으면_관리자_안내():
    """사용자가 인증을 등록해도 해결되지 않는 경우 — 인증 안내를 띄우면 헛수고를 시킨다."""
    with _client(summarize_backend="passthrough") as (auth, appmod, client):
        out = _get(client, auth, appmod)
        assert out["available"] is False
        assert out["reason"] == "backend_off"
        assert "관리자" in out["message"]


def test_claude가_아닌_백엔드는_인증과_무관():
    """로컬 LLM 요약에는 사용자별 claude 자격증명이 필요 없다 — 인증 안내는 거짓이 된다."""
    with _client(summarize_backend="ollama") as (auth, appmod, client):
        out = _get(client, auth, appmod)           # 자격증명 미등록 상태
        assert out == {"available": True, "reason": "ok", "message": None}


# ------------------------------------------------------------------ 사용자 자격증명
def test_자격증명_미등록이면_설정_안내():
    """실사고의 진짜 원인. 사용자가 스스로 고칠 수 있게 어디서 무엇을 할지 말해야 한다."""
    with _client(summarize_backend="agent_cli") as (auth, appmod, client):
        out = _get(client, auth, appmod)
        assert out["available"] is False
        assert out["reason"] == "not_configured"
        assert "설정" in out["message"] and "재요약" in out["message"]


def test_검증전이면_안된다고_단정하지_않는다():
    """헬스 스윕 전에는 유효성을 모른다 — 모르는 것을 경고로 띄우면 거짓 경고가 상시화된다."""
    with _client(summarize_backend="agent_cli") as (auth, appmod, client):
        auth.set_credential("alice", "oauth_token", "dummy-token")
        out = _get(client, auth, appmod)
        assert out == {"available": True, "reason": "unchecked", "message": None}


def test_만료된_자격증명은_갱신_안내():
    with _client(summarize_backend="agent_cli") as (auth, appmod, client):
        auth.set_credential("alice", "oauth_token", "dummy-token")
        with appmod._cred_health_lock:
            appmod._claude_cred_health["alice"] = {"valid": False, "reason": "verify_failed"}
        out = _get(client, auth, appmod)
        assert out["available"] is False
        assert out["reason"] == "invalid"
        assert "갱신" in out["message"]


def test_복호실패는_별도_사유로_알린다():
    """CRED_ENC_KEY 불일치 — 사용자가 토큰을 다시 등록하거나 관리자가 키를 복원해야 한다."""
    with _client(summarize_backend="agent_cli") as (auth, appmod, client):
        auth.set_credential("alice", "oauth_token", "dummy-token")
        with appmod._cred_health_lock:
            appmod._claude_cred_health["alice"] = {"valid": False, "reason": "decrypt_failed"}
        out = _get(client, auth, appmod)
        assert out["reason"] == "decrypt_failed"
        assert out["available"] is False


def test_유효하면_안내하지_않는다():
    """요약이 비는 이유가 인증이 아닐 때 인증 안내를 띄우면 엉뚱한 곳을 고치게 만든다."""
    with _client(summarize_backend="agent_cli") as (auth, appmod, client):
        auth.set_credential("alice", "oauth_token", "dummy-token")
        with appmod._cred_health_lock:
            appmod._claude_cred_health["alice"] = {"valid": True, "reason": "ok"}
        out = _get(client, auth, appmod)
        assert out == {"available": True, "reason": "ok", "message": None}


def test_사용자별로_판정한다():
    """한 사람이 등록했다고 전원이 되는 게 아니다(전역 키 주입 없음)."""
    with _client(summarize_backend="agent_cli") as (auth, appmod, client):
        auth.set_credential("admin", "oauth_token", "dummy-token")
        with appmod._cred_health_lock:
            appmod._claude_cred_health["admin"] = {"valid": True, "reason": "ok"}
        assert _get(client, auth, appmod, "admin")["available"] is True
        assert _get(client, auth, appmod, "alice")["reason"] == "not_configured"


# ------------------------------------------------------------------ 계약 형태
def test_모든_불가사유에_문구가_있다():
    """사유를 추가하고 문구를 빠뜨리면 화면에 빈 안내가 뜬다(문구는 서버가 확정한다)."""
    with _client(summarize_backend="agent_cli") as (auth, appmod, client):
        msgs = appmod.SUMMARY_UNAVAILABLE_MESSAGES
        assert set(msgs) == {"backend_off", "not_configured", "invalid", "decrypt_failed"}
        assert [k for k, v in msgs.items() if not (v or "").strip()] == []


def test_응답_키는_항상_같다():
    """available/reason/message 3키 고정 — 프론트가 분기마다 다른 모양을 방어하지 않게."""
    with _client(summarize_backend="agent_cli") as (auth, appmod, client):
        assert set(_get(client, auth, appmod)) == {"available", "reason", "message"}
        auth.set_credential("alice", "oauth_token", "dummy-token")
        with appmod._cred_health_lock:
            appmod._claude_cred_health["alice"] = {"valid": True, "reason": "ok"}
        assert set(_get(client, auth, appmod)) == {"available", "reason", "message"}


def test_인증없는_조회는_거부():
    """자격증명 설정 여부는 사용자별 정보다 — 익명에게 알려줄 이유가 없다."""
    with _client(summarize_backend="agent_cli") as (_auth, _appmod, client):
        assert client.get(ENDPOINT).status_code in (401, 403)
