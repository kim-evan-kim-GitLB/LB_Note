"""Jira Phase 1 — 설정 저장(auth) + 조회 엔드포인트(app) 회귀 테스트.

검증 불변식:
  - jira_config 저장 왕복: set→get, api_token 은 어떤 status/응답 문자열에도 없음, clear 동작.
  - CRED_ENC_KEY 미설정(평문 폴백) 환경에서 동작(하니스가 pop).
  - 엔드포인트 게이팅: /api/admin/jira-config 개발자 403·비인증 401.
  - /api/jira/projects 미설정 시 400 error_code=jira_not_configured.
  - PUT config: verify 성공 → 200(토큰 미노출), JiraAuthError → 400 jira_auth_failed.
  - createmeta 파싱(엔드포인트 경유, _request 모킹).

jira_client 는 모킹 — 실제 라이브 Jira 미호출. 임시 DB.
실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_jira_config.py
"""
from __future__ import annotations

import contextlib
import importlib
import os
import tempfile
from pathlib import Path
from unittest import mock


def _client_for(td: Path, users: str = "admin:pw1,dev:pw2"):
    from fastapi.testclient import TestClient

    tmp_db = td / "meetings.db"
    os.environ["JWT_SECRET"] = "test-secret-jira"
    os.environ["WEB_AUTH_USERS"] = users
    os.environ["WEB_AUTH_ADMINS"] = "admin"
    os.environ["WEB_AUTH_TOKEN_TTL"] = "3600"
    os.environ.pop("CRED_ENC_KEY", None)
    # env Jira 폴백이 다른 테스트/환경에서 새지 않도록 기본은 미설정.
    for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_DEFAULT_PROJECT"):
        os.environ.pop(k, None)
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


# ---------- 저장 계층 ----------
def test_jira_config_roundtrip_and_token_hidden():
    with _tmp() as (auth, _appmod, _client):
        store = auth.store()
        assert store.get_jira_config() is None
        assert store.jira_config_status() == {
            "configured": False, "base_url": None, "email": None, "default_project": None,
        }
        store.set_jira_config(
            "https://litbig.atlassian.net/", "svc@litbig.com", "SECRET-TOKEN", "AAA"
        )
        cfg = store.get_jira_config()
        assert cfg["base_url"] == "https://litbig.atlassian.net"  # 후행 슬래시 제거
        assert cfg["email"] == "svc@litbig.com"
        assert cfg["api_token"] == "SECRET-TOKEN"
        assert cfg["default_project"] == "AAA"
        # status 에는 토큰이 절대 없다
        st = store.jira_config_status()
        assert st == {
            "configured": True,
            "base_url": "https://litbig.atlassian.net",
            "email": "svc@litbig.com",
            "default_project": "AAA",
        }
        assert "SECRET-TOKEN" not in str(st)
        # clear
        assert store.clear_jira_config() is True
        assert store.get_jira_config() is None


def test_jira_config_requires_all_fields():
    import pytest

    with _tmp() as (auth, _appmod, _client):
        store = auth.store()
        with pytest.raises(ValueError):
            store.set_jira_config("", "e@x.com", "tok")
        with pytest.raises(ValueError):
            store.set_jira_config("https://x", "", "tok")
        with pytest.raises(ValueError):
            store.set_jira_config("https://x", "e@x.com", "")


def test_migrate_encrypts_jira_token():
    """CRED_ENC_KEY 설정 시 평문 api_token 이 재암호화된다(멱등)."""
    from cryptography.fernet import Fernet

    with _tmp() as (auth, _appmod, _client):
        store = auth.store()
        store.set_jira_config("https://x", "e@x.com", "PLAINTOKEN")  # 키 없음 → 평문 저장
        os.environ["CRED_ENC_KEY"] = Fernet.generate_key().decode()
        try:
            n = store.migrate_encrypt_credentials()
            assert n >= 1
            # 디스크 원본이 접두사(암호문)로 바뀌었는지 확인
            row = store._conn.execute("SELECT api_token FROM jira_config WHERE id=1").fetchone()
            assert row["api_token"].startswith("enc:fernet:")
            # 복호는 여전히 평문 반환
            assert store.get_jira_config()["api_token"] == "PLAINTOKEN"
            # 멱등: 재실행은 이 행을 다시 암호화하지 않음
            assert store.migrate_encrypt_credentials() == 0
        finally:
            os.environ.pop("CRED_ENC_KEY", None)


# ---------- 엔드포인트 게이팅 ----------
def test_admin_config_gate():
    with _tmp() as (auth, appmod, client):
        ha = _headers(auth, appmod, "admin")
        hd = _headers(auth, appmod, "dev")
        assert client.get("/api/admin/jira-config").status_code == 401  # 비인증
        assert client.get("/api/admin/jira-config", headers=hd).status_code == 403  # 개발자
        r = client.get("/api/admin/jira-config", headers=ha)
        assert r.status_code == 200
        assert r.json()["configured"] is False


def test_put_config_verifies_and_hides_token():
    with _tmp() as (auth, appmod, client):
        ha = _headers(auth, appmod, "admin")
        account = {"accountId": "5b10", "displayName": "서비스", "emailAddress": "svc@litbig.com"}
        with mock.patch.object(appmod.jira_client, "verify", return_value=account) as m:
            r = client.put(
                "/api/admin/jira-config",
                json={
                    "baseUrl": "https://litbig.atlassian.net",
                    "email": "svc@litbig.com",
                    "apiToken": "SECRET-TOKEN",
                    "defaultProject": "AAA",
                },
                headers=ha,
            )
        m.assert_called_once()
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["account"] == {"displayName": "서비스", "emailAddress": "svc@litbig.com"}
        assert body["status"]["configured"] is True
        assert "SECRET-TOKEN" not in r.text  # 토큰 미노출
        # 저장 확인(GET status 도 토큰 없음)
        r2 = client.get("/api/admin/jira-config", headers=ha)
        assert r2.json()["email"] == "svc@litbig.com"
        assert "SECRET-TOKEN" not in r2.text


def test_put_config_auth_failure_400():
    with _tmp() as (auth, appmod, client):
        ha = _headers(auth, appmod, "admin")
        with mock.patch.object(
            appmod.jira_client, "verify", side_effect=appmod.jira_client.JiraAuthError("bad")
        ):
            r = client.put(
                "/api/admin/jira-config",
                json={"baseUrl": "https://x", "email": "e@x.com", "apiToken": "bad"},
                headers=ha,
            )
        assert r.status_code == 400
        assert r.json()["detail"]["error_code"] == "jira_auth_failed"


def test_delete_config():
    with _tmp() as (auth, appmod, client):
        ha = _headers(auth, appmod, "admin")
        with mock.patch.object(appmod.jira_client, "verify", return_value={"displayName": "s"}):
            client.put(
                "/api/admin/jira-config",
                json={"baseUrl": "https://x", "email": "e@x.com", "apiToken": "tok"},
                headers=ha,
            )
        r = client.delete("/api/admin/jira-config", headers=ha)
        assert r.status_code == 200 and r.json()["cleared"] is True
        assert client.get("/api/admin/jira-config", headers=ha).json()["configured"] is False


# ---------- 조회 엔드포인트 ----------
def test_projects_not_configured_400():
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        r = client.get("/api/jira/projects", headers=hd)
        assert r.status_code == 400
        assert r.json()["detail"]["error_code"] == "jira_not_configured"


def test_projects_after_config():
    with _tmp() as (auth, appmod, client):
        ha = _headers(auth, appmod, "admin")
        hd = _headers(auth, appmod, "dev")
        auth.set_jira_config("https://x", "e@x.com", "tok", "AAA")
        projects = [{"key": "AAA", "name": "A", "style": "classic"}]
        with mock.patch.object(appmod.jira_client, "get_projects", return_value=projects):
            r = client.get("/api/jira/projects", headers=hd)
        assert r.status_code == 200
        assert r.json()["projects"] == projects
        # 관리자 status 도 configured=True(DB 설정)
        assert client.get("/api/admin/jira-config", headers=ha).json()["configured"] is True


def test_env_fallback_configures_projects():
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        os.environ["JIRA_BASE_URL"] = "https://env.atlassian.net"
        os.environ["JIRA_EMAIL"] = "env@x.com"
        os.environ["JIRA_API_TOKEN"] = "envtok"
        try:
            with mock.patch.object(appmod.jira_client, "get_projects", return_value=[]) as m:
                r = client.get("/api/jira/projects", headers=hd)
            assert r.status_code == 200
            # cfg 가 env 값으로 전달됐는지
            cfg = m.call_args.args[0]
            assert cfg["base_url"] == "https://env.atlassian.net"
            assert cfg["api_token"] == "envtok"
        finally:
            for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
                os.environ.pop(k, None)


def test_createmeta_endpoint_parses():
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        auth.set_jira_config("https://x", "e@x.com", "tok")
        raw = {
            "fields": [
                {"fieldId": "summary", "name": "Summary", "required": True, "schema": {"type": "string"}},
                {"fieldId": "duedate", "name": "Due", "required": True, "schema": {"type": "date"}},
            ]
        }
        with mock.patch.object(appmod.jira_client, "_request", return_value=raw):
            r = client.get("/api/jira/createmeta?project=AAA&issuetype=10000", headers=hd)
        assert r.status_code == 200
        fields = r.json()["fields"]
        assert {f["fieldId"] for f in fields} == {"summary", "duedate"}
        assert all(f["required"] for f in fields)


def test_issue_types_endpoint():
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        auth.set_jira_config("https://x", "e@x.com", "tok")
        types = [{"id": "10000", "name": "에픽"}, {"id": "10009", "name": "작업"}]
        with mock.patch.object(appmod.jira_client, "get_issue_types", return_value=types):
            r = client.get("/api/jira/issue-types?project=AAA", headers=hd)
        assert r.status_code == 200 and r.json()["issueTypes"] == types


def test_user_lookup_endpoint_none():
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        auth.set_jira_config("https://x", "e@x.com", "tok")
        with mock.patch.object(appmod.jira_client, "lookup_account_id", return_value=None):
            r = client.get("/api/jira/user-lookup?email=none@x.com", headers=hd)
        assert r.status_code == 200 and r.json() == {"accountId": None}
        with mock.patch.object(
            appmod.jira_client, "lookup_account_id",
            return_value={"accountId": "5b10", "displayName": "홍"},
        ):
            r2 = client.get("/api/jira/user-lookup?email=hong@x.com", headers=hd)
        assert r2.json()["accountId"] == "5b10"


def test_jira_auth_error_on_read_maps_401():
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        auth.set_jira_config("https://x", "e@x.com", "tok")
        with mock.patch.object(
            appmod.jira_client, "get_projects",
            side_effect=appmod.jira_client.JiraAuthError("nope"),
        ):
            r = client.get("/api/jira/projects", headers=hd)
        assert r.status_code == 401
        assert r.json()["detail"]["error_code"] == "jira_auth_failed"


if __name__ == "__main__":
    import sys

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_jira_config ({len(fns)} cases)")
    sys.exit(0)
