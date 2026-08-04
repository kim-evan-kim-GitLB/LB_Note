"""회의록 Slack 참석자 DM HTTP 통합 테스트 — 실 Slack API 미호출(전부 mock).

검증 불변식:
  - 토큰 미설정 400(slack_not_configured), 수신자 없음 422, 남의 회의 404.
  - happy: 수신자별 결과(sent/not_found/error)를 **부분 실패로** 돌려준다 — 한 명 실패가
    나머지를 막지 않는다. 이메일 정리(중복·비이메일 제거)는 Gmail 발송과 동일 규칙.
  - 봇 토큰 무효(SlackAuthError)는 즉시 502 slack_auth_failed — 남은 수신자를 시도하지 않는다.
  - GET /api/settings/slack/status 는 configured 만 노출(토큰 값 비노출).

실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_meeting_slack_dm.py
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
def _client_for(td: Path, users: str, *, slack_token: str | None = "xoxb-test"):
    from fastapi.testclient import TestClient

    tmp_db = td / "meetings.db"
    os.environ["JWT_SECRET"] = "test-secret-slack"
    os.environ["WEB_AUTH_USERS"] = users
    os.environ["WEB_AUTH_ADMINS"] = "admin"
    os.environ["WEB_AUTH_TOKEN_TTL"] = "3600"
    os.environ["WEB_AUTH_PRUNE"] = "1"
    os.environ.pop("CRED_ENC_KEY", None)
    # ⚠️ pop 이 아니라 빈 문자열이어야 한다 — src/config.py 가 import 시점에 load_dotenv 를
    # 부르므로, 키를 지우면 개발 머신 /app/.env 의 실제 토큰이 흘러들어와 '미설정' 테스트가
    # 로컬에서만 깨진다(load_dotenv 는 override=False 라 이미 있는 키는 건드리지 않는다).
    os.environ["SLACK_BOT_TOKEN"] = "" if slack_token is None else slack_token
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
        os.environ.pop("SLACK_BOT_TOKEN", None)


@contextlib.contextmanager
def _tmp(users: str = "admin:pw1", *, slack_token: str | None = "xoxb-test"):
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), users, slack_token=slack_token) as ctx:
        yield ctx


def _headers(auth, appmod, username: str) -> dict:
    appmod.users.set_password(username, "newpassword123")
    return {"Authorization": f"Bearer {auth.make_token(username)}"}


def _make_meeting(client, headers) -> str:
    mid = uuid.uuid4().hex
    body = {
        "id": mid,
        "title": "주간 회의",
        "summary": {"agenda": [{"no": 1, "title": "안건A", "decisions": ["결정1"]}]},
        "actionItems": [{"text": "보고서 작성", "owner": "개발팀"}],
        "transcript": [{"segmentId": 0, "timestamp": "00:01", "speakerId": "화자1", "text": "비밀대화"}],
    }
    r = client.post("/api/meetings", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return mid


# ---------- 게이트 ----------


def test_not_configured_returns_400():
    with _tmp(slack_token=None) as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        mid = _make_meeting(client, h)
        r = client.post(f"/api/meetings/{mid}/slack-dm", json={"recipients": ["a@x.com"]}, headers=h)
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "slack_not_configured"


def test_no_recipients_returns_422():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        mid = _make_meeting(client, h)
        r = client.post(f"/api/meetings/{mid}/slack-dm", json={"recipients": []}, headers=h)
    assert r.status_code == 422


def test_non_owner_gets_404():
    with _tmp(users="admin:pw1,other:pw2") as (auth, appmod, client):
        owner_h = _headers(auth, appmod, "admin")
        mid = _make_meeting(client, owner_h)
        other_h = _headers(auth, appmod, "other")
        r = client.post(
            f"/api/meetings/{mid}/slack-dm", json={"recipients": ["a@x.com"]}, headers=other_h
        )
    assert r.status_code == 404


def test_bad_meeting_id_returns_400():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        r = client.post("/api/meetings/not-hex/slack-dm", json={"recipients": ["a@x.com"]}, headers=h)
    assert r.status_code == 400


# ---------- 발송 ----------


def test_happy_path_sends_to_each_recipient():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        mid = _make_meeting(client, h)
        with mock.patch.object(appmod.slack_notify, "send_dm", return_value="1712.1") as m:
            r = client.post(
                f"/api/meetings/{mid}/slack-dm",
                json={"recipients": ["a@x.com", "b@x.com"], "note": "검토 부탁드립니다"},
                headers=h,
            )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["sentCount"] == 2
    assert [x["status"] for x in data["results"]] == ["sent", "sent"]
    assert m.call_count == 2
    # 머리말이 본문에 실렸는지(blocks 로 전달).
    dumped = str(m.call_args.kwargs.get("blocks"))
    assert "검토 부탁드립니다" in dumped


def test_partial_failure_does_not_block_others():
    """Slack 계정 없는 수신자(not_found)가 있어도 나머지는 발송된다 — 조용한 실패 금지."""
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        mid = _make_meeting(client, h)

        def _side_effect(token, email, **kw):
            if email == "ghost@x.com":
                raise appmod.slack_notify.SlackUserNotFound("users_not_found")
            if email == "boom@x.com":
                raise appmod.slack_notify.SlackError("invalid_blocks")
            return "1712.1"

        with mock.patch.object(appmod.slack_notify, "send_dm", side_effect=_side_effect):
            r = client.post(
                f"/api/meetings/{mid}/slack-dm",
                json={"recipients": ["ok@x.com", "ghost@x.com", "boom@x.com"]},
                headers=h,
            )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["sentCount"] == 1
    by_email = {x["email"]: x for x in data["results"]}
    assert by_email["ok@x.com"]["status"] == "sent"
    assert by_email["ghost@x.com"]["status"] == "not_found"
    assert by_email["boom@x.com"]["status"] == "error"
    assert "invalid_blocks" in by_email["boom@x.com"]["error"]


def test_auth_error_aborts_immediately_with_502():
    """토큰이 죽었으면 남은 수신자를 시도해봐야 전부 같은 실패 → 즉시 중단."""
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        mid = _make_meeting(client, h)
        with mock.patch.object(
            appmod.slack_notify,
            "send_dm",
            side_effect=appmod.slack_notify.SlackAuthError("invalid_auth"),
        ) as m:
            r = client.post(
                f"/api/meetings/{mid}/slack-dm",
                json={"recipients": ["a@x.com", "b@x.com", "c@x.com"]},
                headers=h,
            )
    assert r.status_code == 502
    assert r.json()["detail"]["error_code"] == "slack_auth_failed"
    assert m.call_count == 1  # 첫 실패에서 중단


def test_recipients_are_deduped_and_non_emails_dropped():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        mid = _make_meeting(client, h)
        with mock.patch.object(appmod.slack_notify, "send_dm", return_value="1") as m:
            r = client.post(
                f"/api/meetings/{mid}/slack-dm",
                json={"recipients": ["a@x.com", "A@X.COM", "not-an-email", "b@x.com"]},
                headers=h,
            )
    assert r.status_code == 200, r.text
    assert [c.args[1] for c in m.call_args_list] == ["a@x.com", "b@x.com"]


def test_message_body_excludes_transcript():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        mid = _make_meeting(client, h)
        with mock.patch.object(appmod.slack_notify, "send_dm", return_value="1") as m:
            client.post(
                f"/api/meetings/{mid}/slack-dm", json={"recipients": ["a@x.com"]}, headers=h
            )
    payload = str(m.call_args.kwargs)
    assert "비밀대화" not in payload
    assert "안건A" in payload


# ---------- 상태 ----------


def test_status_reports_configured_true():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        r = client.get("/api/settings/slack/status", headers=h)
    assert r.status_code == 200
    assert r.json() == {"configured": True}


def test_status_reports_configured_false_and_never_leaks_token():
    with _tmp(slack_token=None) as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        r = client.get("/api/settings/slack/status", headers=h)
    assert r.status_code == 200
    assert r.json() == {"configured": False}
    assert "xoxb" not in r.text


def test_status_requires_auth():
    with _tmp() as (auth, appmod, client):
        r = client.get("/api/settings/slack/status")
    assert r.status_code == 401
