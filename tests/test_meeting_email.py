"""회의록 이메일 발송(Gmail) HTTP 통합 테스트 — 실 API 미호출(전부 mock).

검증 불변식:
  - 미연동 400(google_not_connected), 수신자 없음 422, 소유자 아니면 404.
  - happy: Doc export→PDF 첨부 + 본문(요약+액션) + 본인 Gmail 발송. to/cc 정리(중복·비이메일 제거).
  - gmail.send 미동의 → 400 google_scope_missing(재연동 유도).
  - render_email_body 는 전사(transcript) 미포함(요약/액션만).

실 DB·google 라이브러리 불필요(함수 mock). 실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_meeting_email.py
"""
from __future__ import annotations

import contextlib
import importlib
import os
import tempfile
import uuid
from pathlib import Path
from unittest import mock


@contextlib.contextmanager
def _client_for(td: Path, users: str):
    from fastapi.testclient import TestClient

    tmp_db = td / "meetings.db"
    os.environ["JWT_SECRET"] = "test-secret-email"
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
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), users) as ctx:
        yield ctx


def _headers(auth, appmod, username: str) -> dict:
    appmod.users.set_password(username, "newpassword123")
    return {"Authorization": f"Bearer {auth.make_token(username)}"}


def _make_meeting(client, headers) -> str:
    mid = uuid.uuid4().hex
    body = {
        "id": mid,
        "title": "주간 회의",
        "summary": {"agenda": [{"no": 1, "title": "안건A", "decisions": ["결정1"], "issues": []}]},
        "actionItems": [{"text": "보고서 작성", "owner": "홍길동", "due": "금요일"}],
        "transcript": [{"segmentId": 0, "timestamp": "00:01", "speakerId": "화자1", "text": "비밀대화"}],
    }
    r = client.post("/api/meetings", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return mid


def _set_doc(appmod, mid: str) -> None:
    """gdriveRef.docId 를 미리 심어 _ensure_drive_doc 이 폴더 생성 없이 바로 export 하도록."""
    appmod.store.update_if_match(
        mid, {"gdriveRef": {"docId": "doc1", "folderId": "sub1", "docUrl": "u", "syncedAt": "t"}}, None
    )


def test_send_email_not_connected():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        mid = _make_meeting(client, h)
        r = client.post(f"/api/meetings/{mid}/send-email", json={"to": ["a@x.com"]}, headers=h)
        assert r.status_code == 400
        assert r.json()["detail"]["error_code"] == "google_not_connected"


def test_send_email_requires_recipient():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        appmod.auth.set_google_credential("admin", "rt", email="me@corp.com")
        mid = _make_meeting(client, h)
        r = client.post(f"/api/meetings/{mid}/send-email", json={"to": ["없음"], "cc": []}, headers=h)
        assert r.status_code == 422  # '@' 없는 값만 → 유효 수신자 0


def test_send_email_happy_path():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        appmod.auth.set_google_credential("admin", "rt", email="me@corp.com")
        mid = _make_meeting(client, h)
        _set_doc(appmod, mid)
        sent = {}

        def _fake_send(access, *, sender, to, cc, subject, html_body,
                       attachment=None, attachment_name="회의록.pdf", attachment_mime="application/pdf"):
            sent.update(sender=sender, to=to, cc=cc, subject=subject,
                        attachment=attachment, name=attachment_name, body=html_body)
            return "msg1"

        with mock.patch.object(appmod.google_oauth, "refresh_access_token", return_value="AT"), \
             mock.patch.object(appmod.google_drive, "export_doc", return_value=b"PDFBYTES"), \
             mock.patch.object(appmod.google_gmail, "send_message", side_effect=_fake_send), \
             mock.patch.object(appmod, "_start_drive_sync", return_value={"jobId": "j", "status": "queued"}):
            r = client.post(
                f"/api/meetings/{mid}/send-email",
                json={"to": ["a@x.com", "a@x.com", "bad"], "cc": ["c@y.com"], "subject": "제목"},
                headers=h,
            )
        assert r.status_code == 200, r.text
        assert r.json()["sentTo"] == ["a@x.com"]  # 중복·비이메일 제거
        assert r.json()["cc"] == ["c@y.com"]
        assert sent["sender"] == "me@corp.com"  # 본인 Gmail 발신
        assert sent["to"] == ["a@x.com"] and sent["subject"] == "제목"
        assert sent["attachment"] == b"PDFBYTES"  # PDF 첨부
        assert "주간 회의" in sent["body"] and "비밀대화" not in sent["body"]  # 본문=요약/액션(전사 제외)


def test_send_email_scope_missing():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        appmod.auth.set_google_credential("admin", "rt", email="me@corp.com")
        mid = _make_meeting(client, h)
        _set_doc(appmod, mid)
        with mock.patch.object(appmod.google_oauth, "refresh_access_token", return_value="AT"), \
             mock.patch.object(appmod.google_drive, "export_doc", return_value=b"PDF"), \
             mock.patch.object(
                 appmod.google_gmail, "send_message",
                 side_effect=appmod.google_gmail.GmailScopeMissing("no scope")), \
             mock.patch.object(appmod, "_start_drive_sync", return_value={}):
            r = client.post(f"/api/meetings/{mid}/send-email", json={"to": ["a@x.com"]}, headers=h)
        assert r.status_code == 400
        assert r.json()["detail"]["error_code"] == "google_scope_missing"


def test_send_email_ownership():
    with _tmp("admin:pw1,bob:pw2") as (auth, appmod, client):
        ha = _headers(auth, appmod, "admin")
        hb = _headers(auth, appmod, "bob")
        appmod.auth.set_google_credential("bob", "rt", email="bob@corp.com")
        mid = _make_meeting(client, ha)  # admin 소유
        r = client.post(f"/api/meetings/{mid}/send-email", json={"to": ["a@x.com"]}, headers=hb)
        assert r.status_code == 404  # 타인 회의 발송 차단(존재 은닉)


def test_render_email_body_excludes_transcript():
    import src.web.meeting_doc as meeting_doc

    m = {
        "title": "주간 회의",
        "summary": {"agenda": [{"no": 1, "title": "안건A", "decisions": ["결정1"]}]},
        "actionItems": [{"text": "보고서 작성"}],
        "transcript": [{"segmentId": 0, "text": "비밀대화"}],
    }
    html = meeting_doc.render_email_body(m)
    assert "안건A" in html and "보고서 작성" in html
    assert "비밀대화" not in html  # 전사는 본문에 넣지 않는다(첨부로만)


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_meeting_email ({len(fns)} cases)")


if __name__ == "__main__":
    _run()
