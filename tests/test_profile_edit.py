"""사용자 표시명 self-edit 회귀 테스트 — PATCH /api/settings/profile + name_source 보호.

검증 불변식(계획 v4 트랙 A·Phase 1):
  - profile PATCH: displayName/englishName/jobTitle 보낸 것만 갱신, name_source='user' 설정.
  - 검증 422: displayName 빈/공백/64초과/제어문자, english/job 64초과·제어문자(빈 허용).
  - name_source='user' 면 seed 재실행 시 display_name 보존, role 은 동기화 유지.
  - env 유지→보존 / env 제거+PRUNE→행 삭제(name_source 가 삭제를 막지 않음).
  - must_change_password=1 사용자도 profile PATCH 가능(게이트 우회, change-password 동급).

가짜 토큰/임시 DB 라 외부 호출 없음. 실행: sudo PYTHONPATH=/app .venv/bin/python tests/test_profile_edit.py
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _fresh_auth(tmp: Path, users: str, admins: str = "admin", prune: str = "1"):
    """env 세팅 후 임시 DB 로 auth.init() 재실행 → 독립된 UserStore."""
    os.environ["JWT_SECRET"] = "test-secret-profile"
    os.environ["WEB_AUTH_USERS"] = users
    os.environ["WEB_AUTH_ADMINS"] = admins
    os.environ["WEB_AUTH_TOKEN_TTL"] = "3600"
    os.environ["WEB_AUTH_PRUNE"] = prune
    import importlib

    import src.web.auth as auth
    importlib.reload(auth)
    return auth, auth.init(tmp / "users.db")


def test_update_profile_sets_fields_and_name_source():
    with tempfile.TemporaryDirectory() as td:
        auth, store = _fresh_auth(Path(td), "admin:pw1,dev:pw2")
        # 초기: display_name 은 username 폴백, name_source='seed'
        assert store.get("dev")["display_name"] == "dev"
        upd = store.update_profile(
            "dev", display_name="홍길동", english_name="gildong", job_title="프로"
        )
        assert upd["displayName"] == "홍길동"
        assert upd["englishName"] == "gildong"
        assert upd["jobTitle"] == "프로"
        row = store.get("dev")
        assert row["display_name"] == "홍길동" and row["english_name"] == "gildong"
        # name_source 가 'user' 로 표시됐는지(컬럼 직접 조회)
        ns = store._conn.execute(
            "SELECT name_source FROM users WHERE username=?", ("dev",)
        ).fetchone()["name_source"]
        assert ns == "user", ns
        # 없는 사용자 → None
        assert store.update_profile("ghost", display_name="x") is None


def _client_for(td: Path, users: str):
    """임시 DB 로 격리된 app + TestClient. **실제 DB(output/web/meetings.db)는 절대 건드리지
    않는다** — store/auth 의 DEFAULT_DB_PATH 를 임시경로로 패치한 뒤 auth·app 을 reload 해
    모듈 레벨 init()/MeetingStore() 가 임시 DB 만 쓰게 한다. 반환: (auth, appmod, TestClient)."""
    from fastapi.testclient import TestClient
    import importlib
    tmp_db = td / "users.db"
    os.environ["JWT_SECRET"] = "test-secret-profile"
    os.environ["WEB_AUTH_USERS"] = users
    os.environ["WEB_AUTH_ADMINS"] = "admin"
    os.environ["WEB_AUTH_TOKEN_TTL"] = "3600"
    os.environ["WEB_AUTH_PRUNE"] = "1"
    import src.web.store as storemod
    storemod.DEFAULT_DB_PATH = tmp_db  # MeetingStore() 임시 경로
    import src.web.auth as auth
    importlib.reload(auth)  # auth 가 패치된 DEFAULT_DB_PATH 를 다시 import
    auth.DEFAULT_DB_PATH = tmp_db  # 폴백 인자(init() no-arg)도 임시 경로 보장
    import src.web.app as appmod
    importlib.reload(appmod)  # 모듈 레벨 store=MeetingStore()/users=auth.init() → 임시 DB
    return auth, appmod, TestClient(appmod.app)


def test_profile_validation_422():
    with tempfile.TemporaryDirectory() as td:
        auth, appmod, client = _client_for(Path(td), "admin:pw1")
        tok = auth.make_token("admin")
        h = {"Authorization": f"Bearer {tok}"}

        # displayName 빈/공백 → 422
        assert client.patch("/api/settings/profile", json={"displayName": ""}, headers=h).status_code == 422
        assert client.patch("/api/settings/profile", json={"displayName": "   "}, headers=h).status_code == 422
        # 64 초과 → 422
        assert client.patch(
            "/api/settings/profile", json={"displayName": "가" * 65}, headers=h
        ).status_code == 422
        # 제어문자/줄바꿈 → 422 (display 및 english 양쪽)
        assert client.patch(
            "/api/settings/profile", json={"displayName": "ab\ncd"}, headers=h
        ).status_code == 422
        assert client.patch(
            "/api/settings/profile", json={"englishName": "x\ty"}, headers=h
        ).status_code == 422
        assert client.patch(
            "/api/settings/profile", json={"jobTitle": "z" * 65}, headers=h
        ).status_code == 422
        # english/job 빈값은 허용(200)
        r = client.patch("/api/settings/profile", json={"englishName": ""}, headers=h)
        assert r.status_code == 200, r.text
        # 정상 갱신 200 + 응답 public_user
        r = client.patch(
            "/api/settings/profile", json={"displayName": "관리자", "jobTitle": "대표"}, headers=h
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["displayName"] == "관리자" and body["jobTitle"] == "대표"
        assert "password_hash" not in body


def test_seed_preserves_user_display_name_but_syncs_role():
    with tempfile.TemporaryDirectory() as td:
        # 1차: dev 가 표시명을 self-edit → name_source='user'
        auth, store = _fresh_auth(Path(td), "admin:pw1,dev:pw2")
        store.update_profile("dev", display_name="내가정한이름")
        assert store.get("dev")["display_name"] == "내가정한이름"
        # 2차: 같은 DB 로 재init(seed 재실행). dev 를 admins 에 넣어 role 변경 → role 동기화 확인.
        auth, store = _fresh_auth(Path(td), "admin:pw1,dev:pw2", admins="admin,dev")
        row = store.get("dev")
        # display_name 보존(username 으로 리셋 안 됨)
        assert row["display_name"] == "내가정한이름", row["display_name"]
        # role 은 seed 동기화 유지(developer → admin)
        assert row["role"] == "admin", row["role"]
        # name_source='seed' 였던 admin 은 display_name 이 시드값(username 폴백) 유지
        assert store.get("admin")["display_name"] == "admin"


def test_prune_deletes_user_row_regardless_of_name_source():
    with tempfile.TemporaryDirectory() as td:
        # dev self-edit(name_source='user') 후 env 유지 → 보존
        auth, store = _fresh_auth(Path(td), "admin:pw1,dev:pw2")
        store.update_profile("dev", display_name="유저편집")
        auth, store = _fresh_auth(Path(td), "admin:pw1,dev:pw2")
        assert "dev" in store.usernames()
        assert store.get("dev")["display_name"] == "유저편집"  # 보존
        # env 에서 dev 제거 + PRUNE=1 → name_source='user' 라도 행 삭제(의도된 동작)
        auth, store = _fresh_auth(Path(td), "admin:pw1", prune="1")
        assert "dev" not in store.usernames(), store.usernames()


def test_must_change_password_user_can_patch_profile():
    with tempfile.TemporaryDirectory() as td:
        auth, appmod, client = _client_for(Path(td), "admin:pw1")
        # 신규 시드 사용자는 must_change_password=1
        assert appmod.users.get("admin")["must_change_password"] == 1
        tok = auth.make_token("admin")
        h = {"Authorization": f"Bearer {tok}"}
        # require_user_active 게이트가 막는 엔드포인트는 403(대조용)
        gated = client.get("/api/settings/claude-credential", headers=h)
        assert gated.status_code == 403, gated.text
        # profile PATCH 는 게이트 우회 → 200
        r = client.patch("/api/settings/profile", json={"displayName": "변경전이름"}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["displayName"] == "변경전이름"


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_profile_edit ({len(fns)} cases)")


if __name__ == "__main__":
    _run()
