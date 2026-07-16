"""2026-07-16 UX 리뷰 P0/P1 백엔드 수정 회귀 테스트.

검증 불변식:
  - date 폴백(_fill_display_date): date 없는 회의도 응답(list/get/create/patch)엔 date=createdAt.
    date 가 이미 있으면 불변. 저장 문서에는 영속되지 않는다(응답 전용).
  - email-preview: 소유자만 200(html), 남의 회의 404. Google 연동 없이 동작.
  - render_email_body(note=): 머리말이 제목 아래 삽입, HTML 이스케이프+줄바꿈(<br>) 보존.
  - send-email payload.note: 2000자 상한 절삭 후 본문에 반영.
  - GET /api/notices: noticeChannel 키 노출(env 미설정 시 null).

실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_ux_p0p1_fixes.py
"""
from __future__ import annotations

import contextlib
import importlib
import os
import tempfile
import uuid
from pathlib import Path


@contextlib.contextmanager
def _client_for(td: Path, users: str):
    from fastapi.testclient import TestClient

    tmp_db = td / "meetings.db"
    os.environ["JWT_SECRET"] = "test-secret-uxfix"
    os.environ["WEB_AUTH_USERS"] = users
    os.environ["WEB_AUTH_ADMINS"] = "admin"
    os.environ["WEB_AUTH_TOKEN_TTL"] = "3600"
    os.environ["WEB_AUTH_PRUNE"] = "1"
    os.environ.pop("CRED_ENC_KEY", None)
    os.environ.pop("SLACK_NOTICE_CHANNEL", None)
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
def _tmp(users: str = "admin:pw1,alice:pw2"):
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), users) as ctx:
        yield ctx


def _headers(auth, appmod, username: str) -> dict:
    appmod.users.set_password(username, "newpassword123")
    return {"Authorization": f"Bearer {auth.make_token(username)}"}


def _meeting_body(**extra) -> dict:
    body = {
        "id": uuid.uuid4().hex,
        "title": "주간 회의",
        "summary": {},
        "actionItems": [],
        "transcript": [],
        "status": "review",
    }
    body.update(extra)
    return body


# ---------- date 폴백 ----------
def test_create_without_date_fills_created_at():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        r = client.post("/api/meetings", json=_meeting_body(), headers=h)
        assert r.status_code == 200
        m = r.json()
        assert m["createdAt"]
        assert m["date"] == m["createdAt"]  # 응답 폴백


def test_list_and_get_fill_date():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        mid = client.post("/api/meetings", json=_meeting_body(), headers=h).json()["id"]
        lst = client.get("/api/meetings", headers=h).json()
        assert all(m.get("date") for m in lst)
        got = client.get(f"/api/meetings/{mid}", headers=h).json()
        assert got["date"] == got["createdAt"]


def test_explicit_date_is_preserved():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        r = client.post(
            "/api/meetings", json=_meeting_body(date="2026-07-01T09:00:00+09:00"), headers=h
        )
        assert r.json()["date"] == "2026-07-01T09:00:00+09:00"  # 폴백이 기존 값을 덮지 않음


def test_date_fallback_not_persisted():
    """폴백은 응답 전용 — 저장 문서엔 date 를 쓰지 않는다(원본 불변)."""
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        mid = client.post("/api/meetings", json=_meeting_body(), headers=h).json()["id"]
        stored = appmod.store.get(mid)
        assert "date" not in stored or not stored.get("date")


def test_patch_response_fills_date():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        mid = client.post("/api/meetings", json=_meeting_body(), headers=h).json()["id"]
        r = client.patch(f"/api/meetings/{mid}", json={"title": "수정됨"}, headers=h)
        assert r.status_code == 200
        assert r.json()["date"] == r.json()["createdAt"]


# ---------- email-preview ----------
def test_email_preview_returns_html_without_google():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        mid = client.post("/api/meetings", json=_meeting_body(), headers=h).json()["id"]
        r = client.get(f"/api/meetings/{mid}/email-preview", headers=h)
        assert r.status_code == 200
        assert "<h2>" in r.json()["html"]  # 발송 렌더러와 동일 HTML


def test_email_preview_hides_others_meeting():
    with _tmp() as (auth, appmod, client):
        h_admin = _headers(auth, appmod, "admin")
        h_alice = _headers(auth, appmod, "alice")
        mid = client.post("/api/meetings", json=_meeting_body(), headers=h_admin).json()["id"]
        assert client.get(f"/api/meetings/{mid}/email-preview", headers=h_alice).status_code == 404


# ---------- 이메일 머리말(note) ----------
def test_render_email_body_note_escaped_and_multiline():
    from src.web import meeting_doc

    m = {"title": "회의", "createdAt": "2026-07-16T09:00:00+09:00"}
    html = meeting_doc.render_email_body(m, note="안녕하세요.\n<b>주입</b> 확인 부탁드립니다.")
    assert "안녕하세요.<br>&lt;b&gt;주입&lt;/b&gt; 확인 부탁드립니다." in html
    # note 없으면 삽입 블록 자체가 없다
    assert "안녕하세요" not in meeting_doc.render_email_body(m)


def test_send_email_note_truncated_to_2000():
    """payload.note 는 2000자 상한 — 그 이상은 절삭되어 렌더에 전달된다."""
    from unittest import mock

    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        mid = client.post("/api/meetings", json=_meeting_body(), headers=h).json()["id"]
        appmod.auth.set_google_credential("admin", "rt", email="admin@example.com")
        captured: dict = {}

        def fake_render(m, note=None):
            captured["note"] = note
            return "<html></html>"

        with mock.patch.object(appmod.google_oauth, "refresh_access_token", return_value="at"), \
             mock.patch.object(appmod, "_ensure_drive_doc", return_value="doc1"), \
             mock.patch.object(appmod.google_drive, "export_doc", return_value=b"%PDF"), \
             mock.patch.object(appmod.meeting_doc, "render_email_body", side_effect=fake_render), \
             mock.patch.object(appmod.google_gmail, "send_message", return_value="msg1"), \
             mock.patch.object(appmod, "_start_drive_sync"):
            r = client.post(
                f"/api/meetings/{mid}/send-email",
                json={"to": ["a@b.com"], "cc": [], "subject": "제목", "note": "가" * 3000},
                headers=h,
            )
        assert r.status_code == 200
        assert captured["note"] == "가" * 2000  # 절삭 확인


# ---------- 공지 배포 채널 노출 ----------
def test_notices_expose_notice_channel_null_when_unset():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        r = client.get("/api/notices", headers=h)
        assert r.status_code == 200
        data = r.json()
        assert "noticeChannel" in data and data["noticeChannel"] is None


def test_notices_expose_notice_channel_when_set():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        os.environ["SLACK_NOTICE_CHANNEL"] = "#전사공지"
        try:
            assert client.get("/api/notices", headers=h).json()["noticeChannel"] == "#전사공지"
        finally:
            os.environ.pop("SLACK_NOTICE_CHANNEL", None)
