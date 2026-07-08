"""관리자 사용자 명부 관리 + 참석자 피커 디렉터리 회귀 테스트.

검증 불변식:
  - GET/PATCH/reset-password 는 role=admin 전용(개발자 403, 미인증 401).
  - GET /api/admin/users: 시드 사용자 목록, 비번 해시 절대 미노출.
  - PATCH: displayName/englishName/jobTitle/email/role 갱신 반영. displayName 갱신 시
    name_source='user' → 이후 seed 재실행이 덮지 않음.
  - PATCH role 가드: 본인 강등 거부, 마지막 admin 강등 거부.
  - reset-password: must_change_password=1 → 대상이 require_user_active 막힘, 비번 미노출.
  - GET /api/directory: 인증 사용자 접근, {username,displayName,email}만(민감필드 없음).

가짜 토큰/임시 DB — 외부 호출 없음.
실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_admin_users.py
"""
from __future__ import annotations

import contextlib
import importlib
import os
import tempfile
from pathlib import Path


# ---------- 저장 계층(UserStore) ----------
def _fresh_auth(td: Path):
    os.environ["JWT_SECRET"] = "test-secret-adminusers"
    os.environ["WEB_AUTH_USERS"] = "admin:pw1,dev:pw2"
    os.environ["WEB_AUTH_ADMINS"] = "admin"
    os.environ.pop("CRED_ENC_KEY", None)
    import src.web.auth as auth
    importlib.reload(auth)
    return auth, auth.init(td / "users.db")


def test_list_users_shape_no_hash():
    with tempfile.TemporaryDirectory() as td:
        _auth, store = _fresh_auth(Path(td))
        users = store.list_users()
        names = {u["username"] for u in users}
        assert {"admin", "dev"} <= names
        for u in users:
            assert set(u) == {
                "username", "displayName", "role", "englishName",
                "jobTitle", "email", "mustChangePassword",
            }
            assert "password_hash" not in u
        # display_name 기준 정렬
        assert [u["displayName"] for u in users] == sorted(u["displayName"] for u in users)


def test_admin_update_fields_and_name_source():
    with tempfile.TemporaryDirectory() as td:
        _auth, store = _fresh_auth(Path(td))
        updated = store.admin_update_user(
            "dev",
            display_name="개발자",
            english_name="Dev Kim",
            job_title="프로",
            email="dev@corp.io",
            role="admin",
        )
        assert updated["displayName"] == "개발자"
        assert updated["englishName"] == "Dev Kim"
        assert updated["jobTitle"] == "프로"
        assert updated["email"] == "dev@corp.io"
        assert updated["role"] == "admin"
        # name_source='user' → seed 재실행이 display_name 을 덮지 않음
        store.seed_user("dev", "pw2", role="developer")
        assert store.get("dev")["display_name"] == "개발자"


def test_admin_update_noop_no_name_source_promote():
    with tempfile.TemporaryDirectory() as td:
        _auth, store = _fresh_auth(Path(td))
        # 갱신 필드 0개 → 현재 값 그대로, name_source 승격 금지(seed 가 이후 덮을 수 있어야)
        cur = store.admin_update_user("dev")
        assert cur["username"] == "dev"
        store.seed_user("dev", "pw2", display_name="바뀐이름", role="developer")
        assert store.get("dev")["display_name"] == "바뀐이름"


def test_admin_update_invalid_role_and_missing():
    with tempfile.TemporaryDirectory() as td:
        _auth, store = _fresh_auth(Path(td))
        try:
            store.admin_update_user("dev", role="superuser")
            raise AssertionError("ValueError 여야 함")
        except ValueError:
            pass
        assert store.admin_update_user("ghost", display_name="x") is None


def test_admin_reset_password_sets_must_change():
    with tempfile.TemporaryDirectory() as td:
        _auth, store = _fresh_auth(Path(td))
        # 본인이 바꿔 해제된 상태로 만든 뒤 관리자 초기화 → 다시 1
        store.set_password("dev", "selfchosen123")
        assert store.get("dev")["must_change_password"] == 0
        assert store.admin_reset_password("dev", "resetpw123") is True
        assert store.get("dev")["must_change_password"] == 1
        assert store.admin_reset_password("ghost", "x") is False


def test_list_directory_lean():
    with tempfile.TemporaryDirectory() as td:
        _auth, store = _fresh_auth(Path(td))
        store.admin_update_user("dev", email="dev@corp.io")
        dir_ = store.list_directory()
        for u in dir_:
            assert set(u) == {"username", "displayName", "email"}
        got = {u["username"]: u["email"] for u in dir_}
        assert got["dev"] == "dev@corp.io"


def test_list_directory_effective_email_from_username():
    """email override 가 없고 username 이 이메일이면 username 을 effective email 로 쓴다.
    '@' 없는 계정(admin 등)은 email=None. override 가 있으면 override 우선."""
    with tempfile.TemporaryDirectory() as td:
        _auth, store = _fresh_auth(Path(td))
        store.seed_user("hong@litbig.com", "pw", display_name="홍길동")
        store.admin_update_user("dev", email="override@corp.io")  # override 우선 확인
        got = {u["username"]: u["email"] for u in store.list_directory()}
        assert got["hong@litbig.com"] == "hong@litbig.com"  # username 자동 도출
        assert got["admin"] is None                          # '@' 없음 → None
        assert got["dev"] == "override@corp.io"              # override 우선


def test_count_admins():
    with tempfile.TemporaryDirectory() as td:
        _auth, store = _fresh_auth(Path(td))
        assert store.count_admins() == 1
        store.admin_update_user("dev", role="admin")
        assert store.count_admins() == 2


def test_developer_role_normalized_to_user():
    """레거시 role='developer' 는 관리자 뷰에서 '사용자'(user)로 정규화된다.

    시드 비관리자는 developer 로 저장되지만, 편집 select(user/admin)·PATCH role 검증과
    맞지 않으면 저장이 막힌다 → shape 단계에서 admin 외 전부 user 로 노출·수렴.
    """
    with tempfile.TemporaryDirectory() as td:
        _auth, store = _fresh_auth(Path(td))
        store.seed_user("leg", "pw", role="developer")
        got = next(u for u in store.list_users() if u["username"] == "leg")
        assert got["role"] == "user"
        # 이름만 수정해도 role 은 'user' 로 노출(developer 재노출 금지)
        updated = store.admin_update_user("leg", display_name="레거시")
        assert updated["role"] == "user"


def test_create_user_and_duplicate():
    with tempfile.TemporaryDirectory() as td:
        _auth, store = _fresh_auth(Path(td))
        assert store.create_user("newbie", "litbig1234", display_name="새사람", role="user") is True
        row = store.get("newbie")
        assert row is not None
        assert row["must_change_password"] == 1  # 첫 로그인 시 강제 변경
        assert row["display_name"] == "새사람" and row["role"] == "user"
        # 중복 생성 거부 — 기존 비번/역할을 덮지 않는다(upsert 와 다름)
        assert store.create_user("newbie", "other", role="admin") is False
        assert store.get("newbie")["role"] == "user"


def test_delete_user_store():
    with tempfile.TemporaryDirectory() as td:
        _auth, store = _fresh_auth(Path(td))
        assert store.delete("dev") is True
        assert store.get("dev") is None
        assert store.delete("ghost") is False


# ---------- HTTP: 관리자 엔드포인트 ----------
@contextlib.contextmanager
def _client_for(td: Path, users: str):
    from fastapi.testclient import TestClient

    tmp_db = td / "meetings.db"
    os.environ["JWT_SECRET"] = "test-secret-adminusers-http"
    os.environ["WEB_AUTH_USERS"] = users
    os.environ["WEB_AUTH_ADMINS"] = "admin"
    os.environ["WEB_AUTH_TOKEN_TTL"] = "3600"
    os.environ["WEB_AUTH_PRUNE"] = "1"
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
def _tmp(users: str = "admin:pw1,admin2:pw2,dev:pw3"):
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), users) as ctx:
        yield ctx


def _headers(auth, appmod, username: str) -> dict:
    # 시드 사용자는 must_change_password=1 → require_user_active 막힘. 셀프 변경으로 해제.
    appmod.users.set_password(username, "newpassword123")
    return {"Authorization": f"Bearer {auth.make_token(username)}"}


def test_http_admin_only_and_unauth():
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")  # role=developer
        assert client.get("/api/admin/users", headers=hd).status_code == 403
        assert client.patch(
            "/api/admin/users/dev", json={"displayName": "x"}, headers=hd
        ).status_code == 403
        assert client.post(
            "/api/admin/users/dev/reset-password", json={"newPassword": "abcdefgh"}, headers=hd
        ).status_code == 403
        assert client.get("/api/admin/users").status_code == 401  # 미인증


def test_http_list_users_no_hash():
    with _tmp() as (auth, appmod, client):
        ha = _headers(auth, appmod, "admin")
        r = client.get("/api/admin/users", headers=ha)
        assert r.status_code == 200
        users = r.json()["users"]
        names = {u["username"] for u in users}
        assert {"admin", "admin2", "dev"} <= names
        assert "password_hash" not in r.text and "pbkdf2" not in r.text


def test_http_patch_fields_and_seed_protect():
    with _tmp() as (auth, appmod, client):
        ha = _headers(auth, appmod, "admin")
        body = {
            "displayName": "개발자",
            "englishName": "Dev Kim",
            "jobTitle": "프로",
            "email": "dev@corp.io",
            "role": "admin",
        }
        r = client.patch("/api/admin/users/dev", json=body, headers=ha)
        assert r.status_code == 200
        j = r.json()
        assert j["displayName"] == "개발자" and j["email"] == "dev@corp.io"
        assert j["role"] == "admin" and j["englishName"] == "Dev Kim" and j["jobTitle"] == "프로"
        # name_source='user' → seed 재실행이 덮지 않음
        appmod.users.seed_user("dev", "pw3", role="developer")
        assert appmod.users.get("dev")["display_name"] == "개발자"


def test_http_patch_email_validation():
    with _tmp() as (auth, appmod, client):
        ha = _headers(auth, appmod, "admin")
        # 빈 문자열 → None 저장
        r = client.patch("/api/admin/users/dev", json={"email": "  "}, headers=ha)
        assert r.status_code == 200 and r.json()["email"] is None
        # '@' 없음 → 422
        assert client.patch(
            "/api/admin/users/dev", json={"email": "noat"}, headers=ha
        ).status_code == 422


def test_http_patch_role_guards():
    with _tmp() as (auth, appmod, client):
        ha = _headers(auth, appmod, "admin")
        # (a) 본인 강등 거부
        assert client.patch(
            "/api/admin/users/admin", json={"role": "user"}, headers=ha
        ).status_code == 409
        # admin2 는 아직 admin — admin 이 admin2 를 강등하는 것은 가능(admin 이 2명 남음)
        assert client.patch(
            "/api/admin/users/admin2", json={"role": "user"}, headers=ha
        ).status_code == 200
        # (b) 이제 admin 은 본인뿐 → 다른 admin 이 없으므로 어떤 admin 강등도 마지막 admin 가드에 걸림
        # (본인 강등은 (a) 로 먼저 막힘). 새 admin 을 만든 뒤 마지막 admin 가드 확인.
        client.patch("/api/admin/users/dev", json={"role": "admin"}, headers=ha)
        # dev 를 강등(admin 2명 → 통과)
        assert client.patch(
            "/api/admin/users/dev", json={"role": "user"}, headers=ha
        ).status_code == 200
        # 없는 사용자
        assert client.patch(
            "/api/admin/users/ghost", json={"displayName": "x"}, headers=ha
        ).status_code == 404


def test_http_reset_password_blocks_target():
    with _tmp() as (auth, appmod, client):
        ha = _headers(auth, appmod, "admin")
        # dev 셀프 변경으로 활성화 → 목록 접근 가능
        hd = _headers(auth, appmod, "dev")
        assert client.get("/api/directory", headers=hd).status_code == 200
        # 관리자 초기화 → must_change_password=1
        r = client.post(
            "/api/admin/users/dev/reset-password", json={"newPassword": "resetpw123"}, headers=ha
        )
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert "resetpw123" not in r.text
        # 이제 dev(기존 토큰)는 require_user_active 막힘(403 must_change_password)
        assert client.get("/api/directory", headers=hd).status_code == 403
        # 짧은 비번 → 400
        assert client.post(
            "/api/admin/users/dev/reset-password", json={"newPassword": "short"}, headers=ha
        ).status_code == 400
        # 없는 사용자 → 404
        assert client.post(
            "/api/admin/users/ghost/reset-password", json={"newPassword": "abcdefgh"}, headers=ha
        ).status_code == 404


def test_http_directory_lean_and_authenticated():
    with _tmp() as (auth, appmod, client):
        ha = _headers(auth, appmod, "admin")
        client.patch("/api/admin/users/dev", json={"email": "dev@corp.io"}, headers=ha)
        hd = _headers(auth, appmod, "dev")
        r = client.get("/api/directory", headers=hd)
        assert r.status_code == 200
        users = r.json()["users"]
        for u in users:
            assert set(u) == {"username", "displayName", "email"}
        assert "role" not in r.text and "mustChangePassword" not in r.text
        # 미인증 401
        assert client.get("/api/directory").status_code == 401


def test_http_create_user():
    with _tmp() as (auth, appmod, client):
        ha = _headers(auth, appmod, "admin")
        # 비관리자 403
        hd = _headers(auth, appmod, "dev")
        assert client.post(
            "/api/admin/users", json={"username": "u1"}, headers=hd
        ).status_code == 403
        # 미인증 401
        assert client.post("/api/admin/users", json={"username": "u1"}).status_code == 401
        # 생성 201 + shape + mustChangePassword=True, 초기 비번 미노출
        r = client.post(
            "/api/admin/users", json={"username": "newbie", "displayName": "새사람"}, headers=ha
        )
        assert r.status_code == 201
        j = r.json()
        assert j["username"] == "newbie" and j["displayName"] == "새사람"
        assert j["role"] == "user" and j["mustChangePassword"] is True
        # 초기 비번은 1회 반환(관리자가 사용자에게 전달용) — 해시는 절대 미노출
        assert j["initialPassword"] == "litbig1234" and "password_hash" not in r.text
        # 목록 반영
        names = {u["username"] for u in client.get("/api/admin/users", headers=ha).json()["users"]}
        assert "newbie" in names
        # 중복 409
        assert client.post(
            "/api/admin/users", json={"username": "newbie"}, headers=ha
        ).status_code == 409
        # 공백/스페이스 포함 username 422
        assert client.post(
            "/api/admin/users", json={"username": "   "}, headers=ha
        ).status_code == 422
        assert client.post(
            "/api/admin/users", json={"username": "a b"}, headers=ha
        ).status_code == 422
        # 잘못된 role 422
        assert client.post(
            "/api/admin/users", json={"username": "u2", "role": "super"}, headers=ha
        ).status_code == 422


def test_http_create_user_can_login_with_initial_password():
    with _tmp() as (auth, appmod, client):
        ha = _headers(auth, appmod, "admin")
        client.post("/api/admin/users", json={"username": "flowuser", "role": "admin"}, headers=ha)
        # 서버 기본 초기 비번으로 로그인 가능(생성 시 발급된 값)
        lr = client.post(
            "/api/auth/login",
            json={"username": "flowuser", "password": appmod.NEW_USER_INITIAL_PASSWORD},
        )
        assert lr.status_code == 200
        # 생성 role(admin) 반영
        got = next(
            u for u in client.get("/api/admin/users", headers=ha).json()["users"]
            if u["username"] == "flowuser"
        )
        assert got["role"] == "admin"


def test_http_delete_user():
    with _tmp() as (auth, appmod, client):
        ha = _headers(auth, appmod, "admin")
        # 비관리자 403 / 미인증 401
        hd = _headers(auth, appmod, "dev")
        assert client.delete("/api/admin/users/dev", headers=hd).status_code == 403
        assert client.delete("/api/admin/users/dev").status_code == 401
        # 없는 사용자 404
        assert client.delete("/api/admin/users/ghost", headers=ha).status_code == 404
        # 본인 삭제 409
        assert client.delete("/api/admin/users/admin", headers=ha).status_code == 409
        # 일반 사용자 삭제 200 → 목록에서 제거
        assert client.delete("/api/admin/users/dev", headers=ha).status_code == 200
        names = {u["username"] for u in client.get("/api/admin/users", headers=ha).json()["users"]}
        assert "dev" not in names


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_admin_users ({len(fns)} cases)")


if __name__ == "__main__":
    _run()
