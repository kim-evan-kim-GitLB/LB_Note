"""Google Calendar 양방향 연동 HTTP 테스트 — 읽기(events.list)·쓰기(events.insert/update) mock.

검증 불변식:
  - GET /api/google/calendar/events: 미연동 400, 만료 401, 연동 시 items 반환.
  - POST /api/meetings/{id}/calendar-sync: 첫 sync=생성(event_id=None 전달), 재sync=갱신(같은 eventId).
  - gcalRef 가 meeting.data 에 영속되고 htmlLink 반환.
  - 소유자 아니면 404.
  - _meeting_to_calendar_event: date/duration→start/end, participants email→attendees, docUrl→description.

실 DB·실 API 미접촉(tempfile + google 함수 mock).
실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_google_calendar.py
"""
from __future__ import annotations

import contextlib
import importlib
import os
import uuid
from pathlib import Path
from unittest import mock


@contextlib.contextmanager
def _client_for(td: Path, users: str):
    from fastapi.testclient import TestClient

    tmp_db = td / "meetings.db"
    os.environ["JWT_SECRET"] = "test-secret-gcal"
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
def _tmp(users: str = "admin:pw1"):
    import tempfile

    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), users) as ctx:
        yield ctx


def _headers(auth, appmod, username: str) -> dict:
    appmod.users.set_password(username, "newpassword123")
    return {"Authorization": f"Bearer {auth.make_token(username)}"}


def _make_meeting(client, headers, **extra) -> str:
    mid = uuid.uuid4().hex
    body = {"id": mid, "title": "기획 회의", **extra}
    r = client.post("/api/meetings", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return mid


# ---------- 읽기(구글→앱) ----------
def test_events_not_connected():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        r = client.get("/api/google/calendar/events", headers=h)
        assert r.status_code == 400 and r.json()["detail"]["error_code"] == "google_not_connected"


def test_events_returns_items():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        appmod.auth.set_google_credential("admin", "rt", email=None)
        fake_items = [{"id": "g1", "summary": "구글 일정", "start": {"dateTime": "2026-07-10T02:00:00Z"}}]
        with mock.patch.object(appmod.google_oauth, "refresh_access_token", return_value="AT"), \
             mock.patch.object(appmod.google_calendar, "list_events", return_value=fake_items) as le:
            r = client.get("/api/google/calendar/events?timeMin=2026-07-01T00:00:00Z", headers=h)
        assert r.status_code == 200 and r.json() == fake_items
        # timeMin 쿼리가 전달됐는지
        assert le.call_args.kwargs["time_min"] == "2026-07-01T00:00:00Z"


def test_events_auth_expired():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        appmod.auth.set_google_credential("admin", "rt-bad", email=None)
        with mock.patch.object(
            appmod.google_oauth, "refresh_access_token",
            side_effect=appmod.google_oauth.GoogleAuthExpired("invalid_grant")):
            r = client.get("/api/google/calendar/events", headers=h)
        assert r.status_code == 401 and r.json()["detail"]["error_code"] == "google_auth_expired"


# ---------- 쓰기(앱→구글) ----------
def test_calendar_sync_creates_then_updates():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        appmod.auth.set_google_credential("admin", "rt", email=None)
        mid = _make_meeting(client, h, date="2026-07-10T02:00:00Z", duration="01:30")
        ev_calls = []

        def _upsert(access, *, calendar_id, event_body, event_id):
            ev_calls.append(event_id)
            return (event_id or "ev1", "https://calendar.google.com/event?eid=ev1")

        with mock.patch.object(appmod.google_oauth, "refresh_access_token", return_value="AT"), \
             mock.patch.object(appmod.google_calendar, "upsert_event", side_effect=_upsert):
            r1 = client.post(f"/api/meetings/{mid}/calendar-sync", headers=h)
            assert r1.status_code == 200, r1.text
            assert r1.json()["gcalRef"]["eventId"] == "ev1"
            assert "calendar.google.com" in r1.json()["gcalRef"]["htmlLink"]
            # gcalRef 영속 확인
            m = client.get(f"/api/meetings/{mid}", headers=h).json()
            assert m["gcalRef"]["eventId"] == "ev1"
            # 재동기화: 같은 eventId 전달(갱신)
            client.post(f"/api/meetings/{mid}/calendar-sync", headers=h)
        assert ev_calls == [None, "ev1"], ev_calls


def test_calendar_sync_not_connected():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        mid = _make_meeting(client, h, date="2026-07-10T02:00:00Z")
        r = client.post(f"/api/meetings/{mid}/calendar-sync", headers=h)
        assert r.status_code == 400 and r.json()["detail"]["error_code"] == "google_not_connected"


def test_calendar_sync_ownership():
    with _tmp("admin:pw1,bob:pw2") as (auth, appmod, client):
        ha = _headers(auth, appmod, "admin")
        hb = _headers(auth, appmod, "bob")
        appmod.auth.set_google_credential("bob", "rt2", email=None)
        mid = _make_meeting(client, ha, date="2026-07-10T02:00:00Z")  # admin 소유
        assert client.post(f"/api/meetings/{mid}/calendar-sync", headers=hb).status_code == 404


def test_meeting_to_event_body():
    with _tmp() as (_auth, appmod, _client):
        m = {
            "title": "출시 회의",
            "date": "2026-07-10T02:00:00Z",
            "duration": "01:30",
            "participants": [{"email": "a@corp.com", "name": "A"}, {"name": "B"}],
            "summary": {"agenda": [{"title": "일정 확정"}]},
            "gdriveRef": {"docUrl": "https://docs.google.com/document/d/doc1/edit"},
        }
        body = appmod._meeting_to_calendar_event(m)
        assert body["summary"] == "출시 회의"
        assert body["start"]["dateTime"].startswith("2026-07-10T02:00:00")
        # 90분 후 = 03:30
        assert "03:30:00" in body["end"]["dateTime"]
        assert body["attendees"] == [{"email": "a@corp.com"}]  # email 있는 참석자만
        assert "docs.google.com/document/d/doc1" in body["description"]
        assert "일정 확정" in body["description"]


def test_meeting_to_event_defaults():
    with _tmp() as (_auth, appmod, _client):
        # date 없음 → createdAt 폴백, duration 없음 → 60분, 오프셋 없는 시각 → timeZone 부착
        m = {"title": "", "createdAt": "2026-07-01T05:00:00"}
        body = appmod._meeting_to_calendar_event(m)
        assert body["summary"] == "회의"  # 빈 title 폴백
        assert body["start"].get("timeZone") == appmod.CALENDAR_TIMEZONE
        assert "06:00:00" in body["end"]["dateTime"]  # +60분


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_google_calendar ({len(fns)} cases)")


if __name__ == "__main__":
    _run()
