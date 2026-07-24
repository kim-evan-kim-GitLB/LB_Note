"""요구사항 적재 인가(require_requirement_writer) 회귀 테스트.

검증 불변식:
  - 봇 intake 스코프 토큰(scope='requirement_intake') → 201(합성 주체, DB 사용자 불요).
  - 관리자 세션 토큰 → 201.
  - 비관리자(개발자) 세션 토큰 → 403.
  - 토큰 없음 → 401.
  - 다른 스코프 토큰(예: 'audio') → 401(제한 토큰 재사용 차단).
  - 저장된 요구사항의 source='slack', reporter 반영.

실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_requirement_intake_auth.py
"""
from __future__ import annotations

import contextlib
import importlib
import os
import tempfile
from pathlib import Path


def _client_for(td: Path, users: str = "admin:pw1,dev:pw2"):
    from fastapi.testclient import TestClient

    tmp_db = td / "meetings.db"
    os.environ["JWT_SECRET"] = "test-secret-intake"
    os.environ["WEB_AUTH_USERS"] = users
    os.environ["WEB_AUTH_ADMINS"] = "admin"
    os.environ["WEB_AUTH_TOKEN_TTL"] = "3600"
    os.environ.pop("CRED_ENC_KEY", None)
    import src.web.store as storemod

    store_orig = storemod.DEFAULT_DB_PATH
    try:
        storemod.DEFAULT_DB_PATH = tmp_db
        import src.web.auth as auth
        importlib.reload(auth)
        auth.DEFAULT_DB_PATH = tmp_db
        import src.web.audio_store as audio_store
        importlib.reload(audio_store)
        import src.web.app as appmod
        importlib.reload(appmod)
        with TestClient(appmod.app) as client:
            yield auth, appmod, client
    finally:
        storemod.DEFAULT_DB_PATH = store_orig


@contextlib.contextmanager
def _tmp(users: str = "admin:pw1,dev:pw2"):
    with tempfile.TemporaryDirectory() as td:
        yield from _client_for(Path(td), users)


def _headers(auth, appmod, username: str) -> dict:
    appmod.users.set_password(username, "newpassword123")
    return {"Authorization": f"Bearer {auth.make_token(username)}"}


def test_intake_scoped_token_can_create():
    with _tmp() as (auth, _appmod, client):
        tok = auth.make_token("slack-bot", ttl=60, scope=auth.REQUIREMENT_INTAKE_SCOPE)
        r = client.post(
            "/api/requirements",
            json={"text": "화자분리 기능", "source": "slack", "reporter": "rep@x.com"},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body.get("id") and body["source"] == "slack" and body["reporter"] == "rep@x.com"


def test_admin_session_can_create():
    with _tmp() as (auth, appmod, client):
        r = client.post(
            "/api/requirements",
            json={"text": "관리자 등록"},
            headers=_headers(auth, appmod, "admin"),
        )
        assert r.status_code == 201, r.text


def test_non_admin_session_forbidden():
    with _tmp() as (auth, appmod, client):
        r = client.post(
            "/api/requirements",
            json={"text": "개발자 시도"},
            headers=_headers(auth, appmod, "dev"),
        )
        assert r.status_code == 403, r.text


def test_no_token_unauthorized():
    with _tmp() as (_auth, _appmod, client):
        r = client.post("/api/requirements", json={"text": "익명"})
        assert r.status_code == 401, r.text


def test_other_scope_token_rejected():
    with _tmp() as (auth, _appmod, client):
        tok = auth.make_token("slack-bot", ttl=60, scope="audio")
        r = client.post(
            "/api/requirements",
            json={"text": "잘못된 스코프"},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 401, r.text


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_requirement_intake_auth ({len(fns)} cases)")
