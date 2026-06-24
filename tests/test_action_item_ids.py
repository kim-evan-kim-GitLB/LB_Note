"""actionItems item_id 무결성 회귀 테스트 (계획 v4 트랙 C·재요약 P8 선결).

검증 불변식 (web_contract.ensure_action_item_ids + app.py create/patch 적용):
  - actionItems 는 구조를 잠그지 않는다(UI 자유 추가/삭제/편집) — 거부(422) 없음.
  - 각 항목에 고유 item_id 보장: 기존 값 보존(불변), 부재/중복(신규·위조·복제)은 uuid 부여.
  - 순서·개수·text·기타 필드 비파괴, 멱등.
  - create_meeting(POST)·patch_meeting(PATCH) 양 경로에서 적용.

실 DB(output/web/meetings.db)는 절대 건드리지 않는다 — tempfile + DEFAULT_DB_PATH 패치 격리.

실행: sudo uv run --frozen pytest tests/test_action_item_ids.py -q
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.postprocess.web_contract import ensure_action_item_ids  # noqa: E402


# ---------- 단위: ensure_action_item_ids ----------
def test_assigns_id_when_missing():
    out = ensure_action_item_ids([{"text": "a"}, {"text": "b"}])
    assert all(len(it["item_id"]) == 32 for it in out)
    assert out[0]["item_id"] != out[1]["item_id"]
    assert [it["text"] for it in out] == ["a", "b"], "순서·text 보존"


def test_preserves_existing_id():
    out = ensure_action_item_ids([{"item_id": "keep-1", "text": "a"}])
    assert out[0]["item_id"] == "keep-1", "기존 item_id 불변"


def test_dedupes_duplicate_ids():
    out = ensure_action_item_ids([
        {"item_id": "dup", "text": "a"},
        {"item_id": "dup", "text": "b"},  # 복제 → 새 uuid
    ])
    assert out[0]["item_id"] == "dup"
    assert out[1]["item_id"] != "dup" and len(out[1]["item_id"]) == 32


def test_non_string_id_reassigned():
    out = ensure_action_item_ids([{"item_id": 123, "text": "a"}])
    assert isinstance(out[0]["item_id"], str) and len(out[0]["item_id"]) == 32


def test_preserves_other_fields_and_is_nondestructive():
    src = [{"item_id": "x", "text": "a", "confirmed": False, "dueDate": "내일", "evidenceSegIds": [1, 2]}]
    out = ensure_action_item_ids(src)
    assert out[0] == {"item_id": "x", "text": "a", "confirmed": False, "dueDate": "내일", "evidenceSegIds": [1, 2]}
    assert src[0].get("item_id") == "x", "원본 비파괴(입력 dict 변형 없음)"


def test_empty_and_none():
    assert ensure_action_item_ids([]) == []
    assert ensure_action_item_ids(None) == []


def test_idempotent():
    once = ensure_action_item_ids([{"text": "a"}, {"text": "b"}])
    twice = ensure_action_item_ids([dict(it) for it in once])
    assert [it["item_id"] for it in once] == [it["item_id"] for it in twice], "멱등"


def test_non_dict_passthrough():
    out = ensure_action_item_ids([{"text": "a"}, "weird", 5])
    assert out[0]["item_id"] and out[1] == "weird" and out[2] == 5


# ---------- HTTP 통합 ----------
@contextlib.contextmanager
def _client_for(td: Path, users: str):
    from fastapi.testclient import TestClient
    import importlib

    tmp_db = td / "meetings.db"
    os.environ["JWT_SECRET"] = "test-secret-action-itemid"
    os.environ["WEB_AUTH_USERS"] = users
    os.environ["WEB_AUTH_ADMINS"] = "admin"
    os.environ["WEB_AUTH_TOKEN_TTL"] = "3600"
    os.environ["WEB_AUTH_PRUNE"] = "1"
    import src.web.store as storemod
    import src.web.auth as auth_pre

    store_orig = storemod.DEFAULT_DB_PATH
    auth_orig = getattr(auth_pre, "DEFAULT_DB_PATH", None)
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
        import src.web.auth as auth_post
        if auth_orig is not None:
            auth_post.DEFAULT_DB_PATH = auth_orig


def _auth_headers(auth, appmod, username: str) -> dict:
    appmod.users.set_password(username, "newpassword123")
    return {"Authorization": f"Bearer {auth.make_token(username)}"}


def test_http_create_assigns_action_item_ids():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        body = {"title": "회의", "status": "review", "actionItems": [{"text": "할일1"}, {"text": "할일2"}]}
        r = client.post("/api/meetings", json=body, headers=h)
        assert r.status_code == 200, r.text
        items = r.json()["actionItems"]
        assert all(len(it["item_id"]) == 32 for it in items)
        assert items[0]["item_id"] != items[1]["item_id"]


def test_http_patch_assigns_new_and_preserves_existing():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        m = client.post(
            "/api/meetings",
            json={"title": "회의", "status": "review", "actionItems": [{"text": "기존"}]},
            headers=h,
        ).json()
        kept_id = m["actionItems"][0]["item_id"]
        # 기존 항목 유지(item_id 동반) + 신규 항목(item_id 없음) 추가 + 확정토글
        patch_items = [
            {**m["actionItems"][0], "confirmed": False},  # 기존(item_id 보존돼야)
            {"text": "UI 신규 추가"},                      # 신규(서버가 uuid 부여)
        ]
        r = client.patch(f"/api/meetings/{m['id']}", json={"actionItems": patch_items}, headers=h)
        assert r.status_code == 200, r.text
        items = r.json()["actionItems"]
        assert len(items) == 2
        assert items[0]["item_id"] == kept_id, "기존 item_id 보존"
        assert items[0]["confirmed"] is False, "확정토글 등 자유 편집 허용"
        assert len(items[1]["item_id"]) == 32 and items[1]["item_id"] != kept_id, "신규 항목 uuid 부여"


def test_http_patch_dedupes_forged_duplicate():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        m = client.post(
            "/api/meetings",
            json={"title": "회의", "status": "review", "actionItems": [{"text": "a"}]},
            headers=h,
        ).json()
        dup = m["actionItems"][0]["item_id"]
        # 두 항목이 같은 item_id 를 주장(위조·복제) → 서버가 둘째를 새 uuid 로 분리
        r = client.patch(
            f"/api/meetings/{m['id']}",
            json={"actionItems": [{"item_id": dup, "text": "a"}, {"item_id": dup, "text": "b"}]},
            headers=h,
        )
        assert r.status_code == 200, r.text
        items = r.json()["actionItems"]
        assert items[0]["item_id"] == dup
        assert items[1]["item_id"] != dup, "복제 item_id 분리"
