"""관리자 앱 Google OAuth 설정(app_oauth_config) — 저장/암호화/DB우선/권한 회귀 테스트.

검증 불변식:
  - set/get round-trip, client_secret 은 Fernet 암호화(CRED_ENC_KEY) 저장·비노출.
  - 세 값 중 하나라도 비면 ValueError. clear → env 폴백.
  - google_oauth 가 DB 우선 → env 폴백. config_status 는 secret 미노출, source 정확.
  - 관리자 엔드포인트: role=admin 만(개발자 403, 미인증 401). PUT 후 connect 가 authUrl 발급(재시작 없이).

실 DB·실 API 미접촉(tempfile + google_oauth 는 URL만 구성, 네트워크 없음).
실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_google_oauth_config.py
"""
from __future__ import annotations

import contextlib
import importlib
import os
import tempfile
from pathlib import Path


def _fresh_auth(td: Path, *, enc_key: str | None = None):
    os.environ["JWT_SECRET"] = "test-secret-oauthcfg"
    os.environ["WEB_AUTH_USERS"] = "admin:pw1"
    if enc_key is not None:
        os.environ["CRED_ENC_KEY"] = enc_key
    else:
        os.environ.pop("CRED_ENC_KEY", None)
    import src.web.auth as auth
    importlib.reload(auth)
    store = auth.init(td / "users.db")
    return auth, store


# ---------- 저장 계층(UserStore) ----------
def test_config_roundtrip_and_secret_encrypted():
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    with tempfile.TemporaryDirectory() as td:
        _auth, store = _fresh_auth(Path(td), enc_key=key)
        assert store.get_google_oauth_config() is None
        store.set_google_oauth_config("cid-123", "csecret-xyz", "https://x/cb")
        cfg = store.get_google_oauth_config()
        assert cfg["client_id"] == "cid-123"
        assert cfg["client_secret"] == "csecret-xyz"
        assert cfg["redirect_uri"] == "https://x/cb"
        # 원시 저장값: 암호문 접두사, 평문 미포함
        raw = store._conn.execute(
            "SELECT client_secret FROM app_oauth_config WHERE provider='google'"
        ).fetchone()["client_secret"]
        assert raw.startswith("enc:fernet:") and "csecret-xyz" not in raw
        # clear
        assert store.clear_google_oauth_config() is True
        assert store.get_google_oauth_config() is None
        assert store.clear_google_oauth_config() is False


def test_set_requires_all_fields():
    with tempfile.TemporaryDirectory() as td:
        _auth, store = _fresh_auth(Path(td))
        for a, b, c in [("", "s", "r"), ("i", "", "r"), ("i", "s", "")]:
            try:
                store.set_google_oauth_config(a, b, c)
                raise AssertionError("ValueError 여야 함")
            except ValueError:
                pass


# ---------- google_oauth: DB 우선 → env 폴백 ----------
def test_db_first_then_env_fallback():
    from src.web import google_oauth

    with tempfile.TemporaryDirectory() as td:
        _auth, store = _fresh_auth(Path(td))
        os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "env-id"
        os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "env-sec"
        os.environ["GOOGLE_OAUTH_REDIRECT_URI"] = "https://env/cb"
        try:
            # DB 없음 → env 사용
            assert google_oauth._client_id() == "env-id"
            assert google_oauth.config_status()["source"] == "env"
            assert google_oauth.oauth_configured() is True
            # DB 설정 → DB 우선
            store.set_google_oauth_config("db-id", "db-sec", "https://db/cb")
            assert google_oauth._client_id() == "db-id"
            assert google_oauth._redirect_uri() == "https://db/cb"
            st = google_oauth.config_status()
            assert st["source"] == "db" and st["clientId"] == "db-id"
            assert "db-sec" not in str(st) and "env-sec" not in str(st)  # secret 미노출
            # clear → env 폴백 복귀
            store.clear_google_oauth_config()
            assert google_oauth._client_id() == "env-id"
            assert google_oauth.config_status()["source"] == "env"
        finally:
            for k in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REDIRECT_URI"):
                os.environ.pop(k, None)


def test_none_when_unset():
    from src.web import google_oauth

    with tempfile.TemporaryDirectory() as td:
        _fresh_auth(Path(td))
        for k in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REDIRECT_URI"):
            os.environ.pop(k, None)
        assert google_oauth.oauth_configured() is False
        assert google_oauth.config_status()["source"] == "none"


# ---------- HTTP: 관리자 엔드포인트 ----------
@contextlib.contextmanager
def _client_for(td: Path, users: str):
    from fastapi.testclient import TestClient

    tmp_db = td / "meetings.db"
    os.environ["JWT_SECRET"] = "test-secret-oauthcfg-http"
    os.environ["WEB_AUTH_USERS"] = users
    os.environ["WEB_AUTH_ADMINS"] = "admin"
    os.environ["WEB_AUTH_TOKEN_TTL"] = "3600"
    os.environ["WEB_AUTH_PRUNE"] = "1"
    os.environ.pop("CRED_ENC_KEY", None)
    for k in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REDIRECT_URI"):
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
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), users) as ctx:
        yield ctx


def _headers(auth, appmod, username: str) -> dict:
    appmod.users.set_password(username, "newpassword123")
    return {"Authorization": f"Bearer {auth.make_token(username)}"}


def test_admin_only_and_unauth():
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")  # role=developer
        body = {"clientId": "c", "clientSecret": "s", "redirectUri": "https://x/cb"}
        assert client.put("/api/admin/google-oauth-config", json=body, headers=hd).status_code == 403
        assert client.get("/api/admin/google-oauth-config", headers=hd).status_code == 403
        assert client.get("/api/admin/google-oauth-config").status_code == 401  # 미인증


def test_admin_put_get_delete_and_connect():
    with _tmp() as (auth, appmod, client):
        ha = _headers(auth, appmod, "admin")
        # 미설정: connect 503
        assert client.post("/api/settings/google/connect", headers=ha).status_code == 503
        # PUT 설정
        body = {"clientId": "cid-abc", "clientSecret": "sec-xyz", "redirectUri": "https://x/callback"}
        r = client.put("/api/admin/google-oauth-config", json=body, headers=ha)
        assert r.status_code == 200
        assert r.json()["configured"] is True and r.json()["source"] == "db"
        assert "sec-xyz" not in r.text  # secret 절대 미노출
        # GET 상태
        g = client.get("/api/admin/google-oauth-config", headers=ha).json()
        assert g["clientId"] == "cid-abc" and g["redirectUri"] == "https://x/callback"
        assert "sec-xyz" not in str(g)
        # 이제 connect 가 authUrl 발급(재시작 없이, DB 설정 즉시 반영)
        c = client.post("/api/settings/google/connect", headers=ha)
        assert c.status_code == 200 and c.json()["authUrl"].startswith("https://accounts.google.com/")
        # DELETE → env 폴백(env 없음) → connect 503
        d = client.delete("/api/admin/google-oauth-config", headers=ha)
        assert d.status_code == 200 and d.json()["cleared"] is True
        assert client.post("/api/settings/google/connect", headers=ha).status_code == 503


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_google_oauth_config ({len(fns)} cases)")


if __name__ == "__main__":
    _run()
