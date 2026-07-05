"""사용자별 Google 자격증명(google_credentials) 저장/복호/비노출 회귀 테스트.

검증 불변식:
  - set/get round-trip: refresh_token·email 저장·복호.
  - google_status 는 refresh_token 을 절대 노출하지 않는다(connected/email/updatedAt 만).
  - clear 멱등(있으면 True, 없으면 False).
  - CRED_ENC_KEY 설정 시 refresh_token 이 접두사(enc:fernet:) 암호문으로 저장된다(평문 미노출).
  - set_google_root_folder 로 루트 폴더 id 영속. 재연동 시 root_folder_id 보존.
  - migrate_encrypt_credentials 가 google_credentials 평문도 암호화(멱등).

실 DB 미접촉 — tempfile + auth.init(임시경로). google 라이브러리 불필요(순수 저장 계층).
실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_google_credential.py
"""
from __future__ import annotations

import importlib
import os
import tempfile
from pathlib import Path


def _fresh_auth(td: Path, *, enc_key: str | None = None):
    """임시 DB 로 auth.init. enc_key 지정 시 CRED_ENC_KEY 설정(암호화 경로)."""
    os.environ["JWT_SECRET"] = "test-secret-gcred"
    os.environ["WEB_AUTH_USERS"] = "admin:pw1"
    if enc_key is not None:
        os.environ["CRED_ENC_KEY"] = enc_key
    else:
        os.environ.pop("CRED_ENC_KEY", None)
    import src.web.auth as auth
    importlib.reload(auth)
    store = auth.init(td / "users.db")
    return auth, store


def test_google_credential_roundtrip_and_status_hides_token():
    with tempfile.TemporaryDirectory() as td:
        _auth, store = _fresh_auth(Path(td))
        # 미설정
        assert store.google_status("admin") == {"connected": False, "email": None, "updatedAt": None}
        assert store.get_google_credential("admin") is None
        # 저장
        store.set_google_credential("admin", "1//refresh-secret-xyz", email="me@corp.com")
        cred = store.get_google_credential("admin")  # 내부용: refresh_token 포함
        assert cred["refresh_token"] == "1//refresh-secret-xyz"
        assert cred["email"] == "me@corp.com"
        # 공개 상태: refresh_token 절대 비노출
        st = store.google_status("admin")
        assert st["connected"] is True and st["email"] == "me@corp.com" and st["updatedAt"]
        assert "1//refresh-secret-xyz" not in str(st) and "refresh_token" not in st


def test_clear_google_credential_idempotent():
    with tempfile.TemporaryDirectory() as td:
        _auth, store = _fresh_auth(Path(td))
        store.set_google_credential("admin", "tok", email=None)
        assert store.clear_google_credential("admin") is True
        assert store.google_status("admin")["connected"] is False
        assert store.clear_google_credential("admin") is False  # 이미 없음


def test_empty_refresh_token_rejected():
    with tempfile.TemporaryDirectory() as td:
        _auth, store = _fresh_auth(Path(td))
        try:
            store.set_google_credential("admin", "   ", email=None)
            raise AssertionError("ValueError 여야 함")
        except ValueError:
            pass


def test_root_folder_persist_and_preserved_on_reconnect():
    with tempfile.TemporaryDirectory() as td:
        _auth, store = _fresh_auth(Path(td))
        store.set_google_credential("admin", "tok1", email="a@b.com")
        store.set_google_root_folder("admin", "folder-abc")
        assert store.get_google_credential("admin")["root_folder_id"] == "folder-abc"
        # 재연동(새 refresh_token) 시 root_folder_id 는 보존(같은 계정이면 폴더 유효)
        store.set_google_credential("admin", "tok2", email="a@b.com")
        cred = store.get_google_credential("admin")
        assert cred["refresh_token"] == "tok2"
        assert cred["root_folder_id"] == "folder-abc"


def test_encryption_at_rest_prefix():
    """CRED_ENC_KEY 설정 시 refresh_token 이 enc:fernet: 접두사 암호문으로 저장(평문 미노출)."""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    with tempfile.TemporaryDirectory() as td:
        _auth, store = _fresh_auth(Path(td), enc_key=key)
        store.set_google_credential("admin", "1//plaintext-secret", email=None)
        # 원시 저장값을 직접 조회 → 접두사 암호문, 평문 미포함
        row = store._conn.execute(
            "SELECT refresh_token FROM google_credentials WHERE username='admin'"
        ).fetchone()
        raw = row["refresh_token"]
        assert raw.startswith("enc:fernet:")
        assert "1//plaintext-secret" not in raw
        # 복호는 정상(내부 주입 경로)
        assert store.get_google_credential("admin")["refresh_token"] == "1//plaintext-secret"


def test_migrate_encrypts_legacy_plaintext_google():
    """평문으로 저장된 google refresh_token 을 migrate 가 암호화(멱등)."""
    from cryptography.fernet import Fernet

    with tempfile.TemporaryDirectory() as td:
        # 먼저 키 없이 평문 저장
        _auth, store = _fresh_auth(Path(td))
        store.set_google_credential("admin", "1//legacy-plain", email=None)
        raw0 = store._conn.execute(
            "SELECT refresh_token FROM google_credentials WHERE username='admin'"
        ).fetchone()["refresh_token"]
        assert raw0 == "1//legacy-plain"  # 평문
        # 키 설정 후 migrate → 암호화
        os.environ["CRED_ENC_KEY"] = Fernet.generate_key().decode()
        n = store.migrate_encrypt_credentials()
        assert n >= 1
        raw1 = store._conn.execute(
            "SELECT refresh_token FROM google_credentials WHERE username='admin'"
        ).fetchone()["refresh_token"]
        assert raw1.startswith("enc:fernet:")
        # 재실행은 멱등(추가 암호화 0건, 이미 암호문)
        assert store.migrate_encrypt_credentials() == 0
        os.environ.pop("CRED_ENC_KEY", None)


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_google_credential ({len(fns)} cases)")


if __name__ == "__main__":
    _run()
