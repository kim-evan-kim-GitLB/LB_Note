"""온프렘 인증 회귀 테스트 — UserStore + JWT + require_user (src/web/auth.py).

프론트 src/lib/firebase.ts 계약을 잠그는 불변식:
  - 비밀번호는 해시 저장(평문 미저장), 검증은 verify().
  - 계정 = 개발자·어드민만. WEB_AUTH_ADMINS 만 role=admin, 나머지 listed=developer.
  - 자가가입 없음(가입 엔드포인트 미존재) → 발급은 env(WEB_AUTH_USERS)로만.
  - env 가 단일 진실원천: 목록에 없는 계정은 prune(WEB_AUTH_PRUNE=0 로만 해제).
  - 유효 Bearer 토큰만 통과, 없음/위조/만료 토큰은 401.

가짜 토큰/임시 DB 라 외부 호출 없음. 실행: sudo .venv/bin/python tests/test_auth.py
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials


def _fresh_init(tmp: Path, users: str, admins: str = "admin", ttl: str = "3600", prune: str = "1"):
    """env 세팅 후 임시 DB 로 auth.init() 재실행 → 독립된 UserStore."""
    os.environ["JWT_SECRET"] = "test-secret-xyz"
    os.environ["WEB_AUTH_USERS"] = users
    os.environ["WEB_AUTH_ADMINS"] = admins
    os.environ["WEB_AUTH_TOKEN_TTL"] = ttl
    os.environ["WEB_AUTH_PRUNE"] = prune
    import importlib

    import src.web.auth as auth
    importlib.reload(auth)
    return auth, auth.init(tmp / "users.db")


def _cred(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_password_verify_and_roles():
    with tempfile.TemporaryDirectory() as td:
        auth, store = _fresh_init(Path(td), "admin:pw1,dev:pw2")
        # 올바른 비번 → 공개 user, 역할 매핑
        admin = store.verify("admin", "pw1")
        assert admin == {"id": "admin", "username": "admin", "displayName": "admin", "role": "admin"}
        dev = store.verify("dev", "pw2")
        assert dev["role"] == "developer"
        # 잘못된 비번 / 없는 사용자 → None
        assert store.verify("admin", "WRONG") is None
        assert store.verify("ghost", "x") is None
        # 평문 미저장(해시)
        row = store.get("admin")
        assert row["password_hash"] != "pw1" and len(row["password_hash"]) > 20


def test_token_roundtrip_and_require_user():
    with tempfile.TemporaryDirectory() as td:
        auth, store = _fresh_init(Path(td), "admin:pw1")
        tok = auth.make_token("admin")
        user = auth.require_user(_cred(tok))
        assert user["id"] == "admin" and user["role"] == "admin"


def test_require_user_rejects_bad_tokens():
    with tempfile.TemporaryDirectory() as td:
        auth, store = _fresh_init(Path(td), "admin:pw1")
        for bad in (None, _cred(""), _cred("garbage.token.value")):
            try:
                auth.require_user(bad)
                assert False, f"401 이어야 함: {bad}"
            except HTTPException as e:
                assert e.status_code == 401


def test_expired_token_rejected():
    with tempfile.TemporaryDirectory() as td:
        auth, store = _fresh_init(Path(td), "admin:pw1", ttl="-1")  # 즉시 만료
        tok = auth.make_token("admin")
        try:
            auth.require_user(_cred(tok))
            assert False, "만료 토큰은 401"
        except HTTPException as e:
            assert e.status_code == 401


def test_prune_keeps_only_listed_accounts():
    with tempfile.TemporaryDirectory() as td:
        # 1차: admin,dev 등록 + 잔존 계정 stray 수동 삽입
        auth, store = _fresh_init(Path(td), "admin:pw1,dev:pw2")
        store.upsert("stray", "x", role="developer")
        assert "stray" in store.usernames()
        # 2차: 같은 DB 로 재init → env 목록에 없는 stray 제거
        auth, store = _fresh_init(Path(td), "admin:pw1,dev:pw2")
        assert set(store.usernames()) == {"admin", "dev"}
        # prune 해제 시 보존
        auth, store = _fresh_init(Path(td), "admin:pw1", prune="0")
        store.upsert("keep", "x")
        auth, store = _fresh_init(Path(td), "admin:pw1", prune="0")
        assert "keep" in store.usernames()


if __name__ == "__main__":
    test_password_verify_and_roles()
    test_token_roundtrip_and_require_user()
    test_require_user_rejects_bad_tokens()
    test_expired_token_rejected()
    test_prune_keeps_only_listed_accounts()
    print("[test_auth] 5개 테스트 통과")
