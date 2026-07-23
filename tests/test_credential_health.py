"""claude 자격증명 헬스(만료/폐기 사전 감지) 회귀 테스트.

검증 불변식:
  - list_credential_owners: 자격증명 행이 있는 사용자만, secret 절대 미노출.
  - _evaluate_credential_health 상태 분기:
      not_configured(행 없음) / api_key(호출 없이 valid) / oauth_token(실제 ping) /
      decrypt_failed(행은 있으나 복호 실패 → valid=False, reason=decrypt_failed).
  - api_key 는 _verify_credential 을 호출하지 않는다(만료 개념 없음 → 비용 0).
  - GET .../claude-credential/verify: 즉시 재검증 + 캐시 반영.
  - GET .../claude-credential: 캐시된 health 노출(실시간 호출 없음).
  - GET /api/admin/claude-credential-health: admin 전용(403), counts/users 집계, secret 없음.

_verify_credential 을 모킹해 실제 claude 미호출. 임시 DB.
실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_credential_health.py
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
    os.environ["JWT_SECRET"] = "test-secret-credhealth"
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


# ---------- 저장 계층: list_credential_owners ----------
def test_list_credential_owners_no_secret():
    with _tmp() as (auth, appmod, _client):
        store = auth.store()
        assert store.list_credential_owners() == []  # 초기 없음
        store.set_credential("admin", "api_key", "sk-secret-xyz")
        store.set_credential("dev", "oauth_token", "tok-secret-abc")
        owners = {o["username"]: o for o in store.list_credential_owners()}
        assert set(owners) == {"admin", "dev"}
        assert owners["admin"]["type"] == "api_key"
        assert owners["dev"]["type"] == "oauth_token"
        # secret 은 어떤 필드에도 없다
        blob = str(store.list_credential_owners())
        assert "sk-secret-xyz" not in blob and "tok-secret-abc" not in blob
        for o in owners.values():
            assert set(o) == {"username", "type", "updated_at"}


# ---------- 평가 로직: 상태 분기 ----------
def test_evaluate_not_configured():
    with _tmp() as (_auth, appmod, _client):
        h = appmod._evaluate_credential_health("admin")
        assert h["reason"] == "not_configured" and h["valid"] is None and h["type"] is None


def test_evaluate_api_key_valid_without_call():
    with _tmp() as (_auth, appmod, _client):
        appmod.auth.set_credential("admin", "api_key", "sk-xxx")
        with mock.patch.object(appmod, "_verify_credential") as m:
            h = appmod._evaluate_credential_health("admin")
        m.assert_not_called()  # api_key 는 만료 없음 → 호출 0
        assert h["valid"] is True and h["reason"] == "api_key" and h["type"] == "api_key"


def test_evaluate_oauth_token_pings_backend():
    with _tmp() as (_auth, appmod, _client):
        appmod.auth.set_credential("dev", "oauth_token", "tok-xxx")
        with mock.patch.object(appmod, "_verify_credential", return_value={"ok": True, "detail": "검증 호출 성공"}) as m:
            h_ok = appmod._evaluate_credential_health("dev")
        m.assert_called_once()
        assert h_ok["valid"] is True and h_ok["reason"] == "ok" and h_ok["type"] == "oauth_token"
        # 만료/폐기 → ok False → verify_failed
        with mock.patch.object(appmod, "_verify_credential", return_value={"ok": False, "detail": "인증 실패"}):
            h_bad = appmod._evaluate_credential_health("dev")
        assert h_bad["valid"] is False and h_bad["reason"] == "verify_failed"
        assert "tok-xxx" not in str(h_bad)  # secret 미노출


def test_evaluate_decrypt_failed():
    """행은 있으나 복호 실패(CRED_ENC_KEY 손상) → decrypt_failed(valid=False)."""
    with _tmp() as (_auth, appmod, _client):
        appmod.auth.set_credential("dev", "oauth_token", "tok-xxx")
        # get_credential 이 None(복호 실패) 반환하도록 강제
        with mock.patch.object(appmod.auth, "get_credential", return_value=None):
            h = appmod._evaluate_credential_health("dev")
        assert h["valid"] is False and h["reason"] == "decrypt_failed" and h["type"] == "oauth_token"


# ---------- 엔드포인트 ----------
def test_verify_endpoint_updates_cache():
    with _tmp() as (auth, appmod, client):
        appmod.auth.set_credential("dev", "oauth_token", "tok-xxx")
        hd = _headers(auth, appmod, "dev")
        with mock.patch.object(appmod, "_verify_credential", return_value={"ok": True, "detail": "ok"}):
            r = client.get("/api/settings/claude-credential/verify", headers=hd)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["configured"] is True and body["health"]["valid"] is True
        # 이후 상태 조회는 캐시된 health 를 실시간 호출 없이 노출
        r2 = client.get("/api/settings/claude-credential", headers=hd)
        assert r2.json()["health"]["valid"] is True
        assert "tok-xxx" not in r.text and "tok-xxx" not in r2.text


def test_status_endpoint_health_none_before_check():
    with _tmp() as (auth, appmod, client):
        appmod.auth.set_credential("dev", "api_key", "sk-xxx")
        hd = _headers(auth, appmod, "dev")
        r = client.get("/api/settings/claude-credential", headers=hd)
        assert r.status_code == 200
        assert r.json()["configured"] is True and r.json()["health"] is None  # 아직 미검증


def test_admin_health_endpoint_counts_and_gate():
    with _tmp() as (auth, appmod, client):
        appmod.auth.set_credential("admin", "api_key", "sk-a")
        appmod.auth.set_credential("dev", "oauth_token", "tok-d")
        ha = _headers(auth, appmod, "admin")
        hd = _headers(auth, appmod, "dev")
        # 개발자 접근 403
        assert client.get("/api/admin/claude-credential-health", headers=hd).status_code == 403
        assert client.get("/api/admin/claude-credential-health").status_code == 401
        # admin: 아직 스윕 전 → 둘 다 unchecked
        r = client.get("/api/admin/claude-credential-health", headers=ha)
        assert r.status_code == 200, r.text
        c = r.json()["counts"]
        assert c["configured"] == 2 and c["api_key"] == 1 and c["oauth_token"] == 1
        assert c["unchecked"] == 2
        # dev(oauth) 만료로 판정 → invalid 1
        with mock.patch.object(appmod, "_verify_credential", return_value={"ok": False, "detail": "인증 실패"}):
            appmod._evaluate_credential_health("dev")
        appmod._evaluate_credential_health("admin")  # api_key → valid
        c2 = client.get("/api/admin/claude-credential-health", headers=ha).json()["counts"]
        assert c2["valid"] == 1 and c2["invalid"] == 1 and c2["unchecked"] == 0
        assert "tok-d" not in client.get("/api/admin/claude-credential-health", headers=ha).text


if __name__ == "__main__":
    import sys

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_credential_health ({len(fns)} cases)")
    sys.exit(0)
