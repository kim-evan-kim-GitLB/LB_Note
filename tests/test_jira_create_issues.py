"""Jira Phase 2 — 회의 액션아이템 → 에픽/작업 생성 엔드포인트 회귀 테스트.

검증 불변식(POST /api/meetings/{id}/jira-issues):
  - 에픽 신규 + 작업 2건 생성: 응답 구조·에픽 key 를 각 작업 parent 로 전달·멱등 되쓰기.
  - 기존 epicKey 사용: 에픽 생성 안 함(작업만 parent 로 붙임).
  - 작업 한 건 실패(예외)여도 나머지 성공 + per-item error(200 유지).
  - reporterAccountId best-effort: 첫 시도 실패 → reporter 없이 재시도 성공, reporterApplied=false.
  - watcherAccountIds best-effort: 일부 실패는 collect(전체 실패시키지 않음).
  - 멱등성: 이미 jiraKey 있는 액션아이템은 재생성 스킵(기존 키 반환).
  - 소유권: 타인 회의 404 / admin 은 타인 회의 허용.
  - 미설정 400 jira_not_configured.
  - description 평문 → ADF 변환(create_issue 에 dict 로 전달).

jira_client.create_issue/add_watchers 는 모킹 — 실제 라이브 Jira 미호출. 임시 DB.
실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_jira_create_issues.py
"""
from __future__ import annotations

import contextlib
import importlib
import os
import tempfile
import uuid
from pathlib import Path
from unittest import mock


def _client_for(td: Path, users: str = "admin:pw1,dev:pw2,other:pw3"):
    from fastapi.testclient import TestClient

    tmp_db = td / "meetings.db"
    os.environ["JWT_SECRET"] = "test-secret-jira-create"
    os.environ["WEB_AUTH_USERS"] = users
    os.environ["WEB_AUTH_ADMINS"] = "admin"
    os.environ["WEB_AUTH_TOKEN_TTL"] = "3600"
    os.environ.pop("CRED_ENC_KEY", None)
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
def _tmp(users: str = "admin:pw1,dev:pw2,other:pw3"):
    with tempfile.TemporaryDirectory() as td:
        yield from _client_for(Path(td), users)


def _headers(auth, appmod, username: str) -> dict:
    appmod.users.set_password(username, "newpassword123")
    return {"Authorization": f"Bearer {auth.make_token(username)}"}


def _make_meeting(appmod, owner: str, action_items: list[dict]) -> str:
    """owner 소유 회의를 생성하고 id 반환(id=hex32)."""
    mid = uuid.uuid4().hex
    appmod.store.create({
        "id": mid, "ownerId": owner, "title": "회의", "status": "done",
        "actionItems": action_items,
    })
    return mid


def _fake_create():
    """create_issue 모킹 — 호출 kwargs 를 기록하고 순번 key 를 반환."""
    def fn(cfg, **kw):
        fn.calls.append(kw)
        n = len(fn.calls)
        key = f"K-{n}"
        return {"key": key, "id": str(n), "url": f"https://x/browse/{key}"}
    fn.calls = []
    return fn


# ---------- (1) 에픽 신규 + 작업 2건 ----------
def test_epic_and_two_tasks():
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        auth.set_jira_config("https://x", "e@x.com", "tok")
        mid = _make_meeting(appmod, "dev", [
            {"item_id": "a1", "text": "작업1"},
            {"item_id": "a2", "text": "작업2"},
        ])
        fake = _fake_create()
        with mock.patch.object(appmod.jira_client, "create_issue", side_effect=fake):
            r = client.post(
                f"/api/meetings/{mid}/jira-issues",
                json={
                    "projectKey": "AAA",
                    "epic": {"issuetypeId": "10000", "fields": {"summary": "에픽"}},
                    "tasks": [
                        {"issuetypeId": "10001", "fields": {"summary": "작업1"}, "sourceActionId": "a1"},
                        {"issuetypeId": "10001", "fields": {"summary": "작업2"}, "sourceActionId": "a2"},
                    ],
                },
                headers=hd,
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["epic"] == {"key": "K-1", "url": "https://x/browse/K-1", "created": True}
        assert [t["key"] for t in body["tasks"]] == ["K-2", "K-3"]
        assert all(t["ok"] for t in body["tasks"])
        # 에픽 생성은 parent 없음, 각 작업은 에픽 key 를 parent 로.
        assert fake.calls[0]["parent_key"] is None
        assert fake.calls[1]["parent_key"] == "K-1"
        assert fake.calls[2]["parent_key"] == "K-1"
        # projectKey/issuetypeId 백엔드 주입.
        assert fake.calls[0]["project_key"] == "AAA"
        assert fake.calls[0]["issuetype_id"] == "10000"
        # 멱등 되쓰기: 액션아이템에 jiraKey/jiraUrl 기록.
        stored = {it["item_id"]: it for it in appmod.store.get(mid)["actionItems"]}
        assert stored["a1"]["jiraKey"] == "K-2"
        assert stored["a1"]["jiraUrl"] == "https://x/browse/K-2"
        assert stored["a2"]["jiraKey"] == "K-3"


# ---------- (2) 기존 epicKey 사용(에픽 생성 안 함) ----------
def test_existing_epic_key_no_epic_create():
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        auth.set_jira_config("https://x", "e@x.com", "tok")
        mid = _make_meeting(appmod, "dev", [{"item_id": "a1", "text": "작업1"}])
        fake = _fake_create()
        with mock.patch.object(appmod.jira_client, "create_issue", side_effect=fake):
            r = client.post(
                f"/api/meetings/{mid}/jira-issues",
                json={
                    "projectKey": "AAA",
                    "epicKey": "EPIC-99",
                    "tasks": [{"issuetypeId": "10001", "fields": {"summary": "작업1"}, "sourceActionId": "a1"}],
                },
                headers=hd,
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["epic"] == {"key": "EPIC-99", "url": "https://x/browse/EPIC-99", "created": False}
        # create_issue 는 작업 1회만(에픽 생성 없음), parent=기존 epicKey.
        assert len(fake.calls) == 1
        assert fake.calls[0]["parent_key"] == "EPIC-99"


# ---------- (3) 작업 한 건 실패여도 나머지 성공 ----------
def test_one_task_fails_others_succeed():
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        auth.set_jira_config("https://x", "e@x.com", "tok")
        mid = _make_meeting(appmod, "dev", [
            {"item_id": "a1", "text": "작업1"},
            {"item_id": "a2", "text": "작업2"},
        ])

        def fn(cfg, **kw):
            if kw.get("summary") == "작업2":
                raise appmod.jira_client.JiraError("boom")
            return {"key": "K-OK", "id": "1", "url": "https://x/browse/K-OK"}

        with mock.patch.object(appmod.jira_client, "create_issue", side_effect=fn):
            r = client.post(
                f"/api/meetings/{mid}/jira-issues",
                json={
                    "projectKey": "AAA",
                    "tasks": [
                        {"issuetypeId": "10001", "fields": {"summary": "작업1"}, "sourceActionId": "a1"},
                        {"issuetypeId": "10001", "fields": {"summary": "작업2"}, "sourceActionId": "a2"},
                    ],
                },
                headers=hd,
            )
        assert r.status_code == 200, r.text
        tasks = r.json()["tasks"]
        assert tasks[0]["ok"] is True and tasks[0]["key"] == "K-OK"
        assert tasks[1]["ok"] is False and "boom" in tasks[1]["error"]
        # 실패한 작업(a2)은 되쓰기 안 됨.
        stored = {it["item_id"]: it for it in appmod.store.get(mid)["actionItems"]}
        assert stored["a1"].get("jiraKey") == "K-OK"
        assert "jiraKey" not in stored["a2"]


# ---------- (4) reporter best-effort: 첫 시도 실패 → 재시도, reporterApplied=false ----------
def test_reporter_best_effort_retry_without_reporter():
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        auth.set_jira_config("https://x", "e@x.com", "tok")
        mid = _make_meeting(appmod, "dev", [{"item_id": "a1", "text": "작업1"}])
        calls = []

        def fn(cfg, **kw):
            calls.append(kw)
            if kw.get("reporter_id"):
                raise appmod.jira_client.JiraError("reporter not permitted")
            return {"key": "K-1", "id": "1", "url": "https://x/browse/K-1"}

        with mock.patch.object(appmod.jira_client, "create_issue", side_effect=fn):
            r = client.post(
                f"/api/meetings/{mid}/jira-issues",
                json={
                    "projectKey": "AAA",
                    "reporterAccountId": "acc-123",
                    "tasks": [{"issuetypeId": "10001", "fields": {"summary": "작업1"}, "sourceActionId": "a1"}],
                },
                headers=hd,
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reporterApplied"] is False
        assert body["tasks"][0]["ok"] is True and body["tasks"][0]["key"] == "K-1"
        # reporter 로 1회 시도 → 실패 → reporter 없이 재시도(총 2회).
        assert len(calls) == 2
        assert calls[0]["reporter_id"] == "acc-123"
        assert calls[1].get("reporter_id") is None


def test_reporter_applied_true_when_accepted():
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        auth.set_jira_config("https://x", "e@x.com", "tok")
        mid = _make_meeting(appmod, "dev", [{"item_id": "a1", "text": "작업1"}])
        fake = _fake_create()
        with mock.patch.object(appmod.jira_client, "create_issue", side_effect=fake):
            r = client.post(
                f"/api/meetings/{mid}/jira-issues",
                json={
                    "projectKey": "AAA",
                    "reporterAccountId": "acc-ok",
                    "tasks": [{"issuetypeId": "10001", "fields": {"summary": "작업1"}, "sourceActionId": "a1"}],
                },
                headers=hd,
            )
        assert r.status_code == 200 and r.json()["reporterApplied"] is True
        assert fake.calls[0]["reporter_id"] == "acc-ok"


# ---------- (5) watchers best-effort(일부 실패 collect) ----------
def test_watchers_best_effort_partial_failure():
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        auth.set_jira_config("https://x", "e@x.com", "tok")
        mid = _make_meeting(appmod, "dev", [
            {"item_id": "a1", "text": "작업1"},
            {"item_id": "a2", "text": "작업2"},
        ])
        fake = _fake_create()

        def watch(cfg, key, ids):
            if key == "K-2":
                raise appmod.jira_client.JiraError("watch fail")

        with mock.patch.object(appmod.jira_client, "create_issue", side_effect=fake), \
             mock.patch.object(appmod.jira_client, "add_watchers", side_effect=watch) as mw:
            r = client.post(
                f"/api/meetings/{mid}/jira-issues",
                json={
                    "projectKey": "AAA",
                    "watcherAccountIds": ["w1", "w2"],
                    "tasks": [
                        {"issuetypeId": "10001", "fields": {"summary": "작업1"}, "sourceActionId": "a1"},
                        {"issuetypeId": "10001", "fields": {"summary": "작업2"}, "sourceActionId": "a2"},
                    ],
                },
                headers=hd,
            )
        assert r.status_code == 200, r.text
        w = r.json()["watchers"]
        assert w["attempted"] == 2  # 생성된 작업 2건
        assert w["failed"] == ["K-2"]
        assert mw.call_count == 2


# ---------- (6) 멱등성: 이미 jiraKey 있는 액션아이템 재생성 스킵 ----------
def test_idempotent_skip_existing_jira_key():
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        auth.set_jira_config("https://x", "e@x.com", "tok")
        mid = _make_meeting(appmod, "dev", [
            {"item_id": "a1", "text": "이미생성", "jiraKey": "OLD-1", "jiraUrl": "https://x/browse/OLD-1"},
            {"item_id": "a2", "text": "신규"},
        ])
        fake = _fake_create()
        with mock.patch.object(appmod.jira_client, "create_issue", side_effect=fake):
            r = client.post(
                f"/api/meetings/{mid}/jira-issues",
                json={
                    "projectKey": "AAA",
                    "tasks": [
                        {"issuetypeId": "10001", "fields": {"summary": "이미생성"}, "sourceActionId": "a1"},
                        {"issuetypeId": "10001", "fields": {"summary": "신규"}, "sourceActionId": "a2"},
                    ],
                },
                headers=hd,
            )
        assert r.status_code == 200, r.text
        tasks = r.json()["tasks"]
        # a1 은 기존 키 반환(create_issue 미호출), a2 만 신규 생성.
        assert tasks[0]["key"] == "OLD-1" and tasks[0]["ok"] is True
        assert tasks[1]["key"] == "K-1" and tasks[1]["ok"] is True
        assert len(fake.calls) == 1
        assert fake.calls[0]["summary"] == "신규"


# ---------- (7) 소유권 ----------
def test_ownership_other_meeting_404():
    with _tmp() as (auth, appmod, client):
        hd_dev = _headers(auth, appmod, "dev")
        ha = _headers(auth, appmod, "admin")
        auth.set_jira_config("https://x", "e@x.com", "tok")
        mid = _make_meeting(appmod, "other", [{"item_id": "a1", "text": "작업1"}])
        payload = {"projectKey": "AAA", "tasks": []}
        # 타인(dev) → 404
        r = client.post(f"/api/meetings/{mid}/jira-issues", json=payload, headers=hd_dev)
        assert r.status_code == 404
        # admin → 허용(200)
        fake = _fake_create()
        with mock.patch.object(appmod.jira_client, "create_issue", side_effect=fake):
            r2 = client.post(f"/api/meetings/{mid}/jira-issues", json=payload, headers=ha)
        assert r2.status_code == 200, r2.text


# ---------- (8) 미설정 400 ----------
def test_not_configured_400():
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        mid = _make_meeting(appmod, "dev", [{"item_id": "a1", "text": "작업1"}])
        r = client.post(
            f"/api/meetings/{mid}/jira-issues",
            json={"projectKey": "AAA", "tasks": []},
            headers=hd,
        )
        assert r.status_code == 400
        assert r.json()["detail"]["error_code"] == "jira_not_configured"


# ---------- (9) description 평문 → ADF 변환 ----------
def test_plain_description_wrapped_as_adf():
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        auth.set_jira_config("https://x", "e@x.com", "tok")
        mid = _make_meeting(appmod, "dev", [{"item_id": "a1", "text": "작업1"}])
        fake = _fake_create()
        with mock.patch.object(appmod.jira_client, "create_issue", side_effect=fake):
            r = client.post(
                f"/api/meetings/{mid}/jira-issues",
                json={
                    "projectKey": "AAA",
                    "tasks": [{
                        "issuetypeId": "10001",
                        "fields": {"summary": "작업1", "description": "평문 설명입니다"},
                        "sourceActionId": "a1",
                    }],
                },
                headers=hd,
            )
        assert r.status_code == 200, r.text
        desc = fake.calls[0]["description"]
        assert isinstance(desc, dict) and desc["type"] == "doc"
        # 평문이 ADF paragraph text 로 들어갔는지.
        text = desc["content"][0]["content"][0]["text"]
        assert text == "평문 설명입니다"


def test_dict_description_passthrough():
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        auth.set_jira_config("https://x", "e@x.com", "tok")
        mid = _make_meeting(appmod, "dev", [{"item_id": "a1", "text": "작업1"}])
        adf = {"type": "doc", "version": 1, "content": []}
        fake = _fake_create()
        with mock.patch.object(appmod.jira_client, "create_issue", side_effect=fake):
            r = client.post(
                f"/api/meetings/{mid}/jira-issues",
                json={
                    "projectKey": "AAA",
                    "tasks": [{
                        "issuetypeId": "10001",
                        "fields": {"summary": "작업1", "description": adf},
                        "sourceActionId": "a1",
                    }],
                },
                headers=hd,
            )
        assert r.status_code == 200, r.text
        assert fake.calls[0]["description"] == adf  # dict 는 그대로 통과


# ---------- extra_fields 통과(커스텀필드/duedate) ----------
def test_extra_fields_passthrough():
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        auth.set_jira_config("https://x", "e@x.com", "tok")
        mid = _make_meeting(appmod, "dev", [{"item_id": "a1", "text": "작업1"}])
        fake = _fake_create()
        with mock.patch.object(appmod.jira_client, "create_issue", side_effect=fake):
            r = client.post(
                f"/api/meetings/{mid}/jira-issues",
                json={
                    "projectKey": "AAA",
                    "tasks": [{
                        "issuetypeId": "10001",
                        "fields": {"summary": "작업1", "duedate": "2026-08-01", "customfield_10192": {"id": "5"}},
                        "sourceActionId": "a1",
                    }],
                },
                headers=hd,
            )
        assert r.status_code == 200, r.text
        extra = fake.calls[0]["extra_fields"]
        assert extra == {"duedate": "2026-08-01", "customfield_10192": {"id": "5"}}
        assert fake.calls[0]["summary"] == "작업1"


if __name__ == "__main__":
    import sys

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_jira_create_issues ({len(fns)} cases)")
    sys.exit(0)
