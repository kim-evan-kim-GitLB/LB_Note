"""slack_notify 순수 함수 단위테스트 — _request 를 모킹해 실제 HTTP 없이 검증.

검증 불변식:
  - Slack 은 실패도 HTTP 200 + {"ok": false, "error": ...} → 본문 ok 검사로 예외 분기.
  - users_not_found → SlackUserNotFound(수신자별 부분 실패), 토큰류 → SlackAuthError.
  - send_dm = lookupByEmail → conversations.open → chat.postMessage 3단계 호출.
  - 봇 토큰이 예외 메시지에 새지 않는다.
  - render_meeting_message: 공유자 명시, 전사 미포함, 3000자 상한 초과분 절단.

실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_slack_notify.py
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

from src.web import slack_notify

TOKEN = "xoxb-SECRET-TOKEN"


class _FakeResp:
    """urlopen 컨텍스트매니저 흉내 — read() 로 JSON 바이트 반환."""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen_returning(payload: dict):
    return mock.patch("urllib.request.urlopen", return_value=_FakeResp(payload))


# ---------- _request: Slack 특유의 ok:false 규약 ----------


def _capture_request(method: str, payload: dict, resp: dict) -> dict:
    """_request 1회 호출을 가로채 URL/헤더/본문을 그대로 반환."""
    captured: dict = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        # urllib 은 헤더명을 Title-Case 로 정규화한다.
        captured["content_type"] = req.headers.get("Content-type")
        captured["raw"] = req.data.decode()
        return _FakeResp(resp)

    with mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        captured["out"] = slack_notify._request(TOKEN, method, payload)
    return captured


def test_lookup_by_email_is_form_encoded_not_json():
    """회귀(2026-08-04): users.lookupByEmail 은 JSON 을 받지 않는다.

    JSON 으로 보내면 Slack 이 인자를 무시하고 invalid_arguments 를 낸다 — 실토큰 검증에서
    발견된 실제 버그. 모킹만으로는 못 잡으므로 인코딩 자체를 계약으로 고정한다.
    """
    c = _capture_request("users.lookupByEmail", {"email": "a@x.com"}, {"ok": True, "user": {"id": "U1"}})
    assert c["url"] == "https://slack.com/api/users.lookupByEmail"
    assert c["auth"] == f"Bearer {TOKEN}"
    assert c["content_type"].startswith("application/x-www-form-urlencoded")
    assert c["raw"] == "email=a%40x.com"


def test_post_message_stays_json_because_blocks_need_it():
    """반대로 chat.postMessage 는 blocks(중첩 구조)를 실어야 하므로 JSON 이어야 한다."""
    c = _capture_request(
        "chat.postMessage",
        {"channel": "D1", "text": "본문", "blocks": [{"type": "divider"}]},
        {"ok": True, "ts": "1"},
    )
    assert c["content_type"].startswith("application/json")
    assert json.loads(c["raw"])["blocks"] == [{"type": "divider"}]


def test_conversations_open_stays_json():
    c = _capture_request("conversations.open", {"users": "U1"}, {"ok": True, "channel": {"id": "D1"}})
    assert c["content_type"].startswith("application/json")
    assert json.loads(c["raw"]) == {"users": "U1"}


def test_request_ok_false_users_not_found_raises_not_found():
    with _urlopen_returning({"ok": False, "error": "users_not_found"}):
        with pytest.raises(slack_notify.SlackUserNotFound):
            slack_notify._request(TOKEN, "users.lookupByEmail", {"email": "nobody@x.com"})


@pytest.mark.parametrize("code", ["invalid_auth", "token_revoked", "account_inactive"])
def test_request_ok_false_token_errors_raise_auth_error(code):
    with _urlopen_returning({"ok": False, "error": code}):
        with pytest.raises(slack_notify.SlackAuthError):
            slack_notify._request(TOKEN, "chat.postMessage", {"channel": "D1", "text": "x"})


def test_request_ok_false_other_error_raises_generic():
    with _urlopen_returning({"ok": False, "error": "invalid_blocks"}):
        with pytest.raises(slack_notify.SlackError) as ei:
            slack_notify._request(TOKEN, "chat.postMessage", {"channel": "D1", "text": "x"})
    assert "invalid_blocks" in str(ei.value)
    assert not isinstance(ei.value, slack_notify.SlackAuthError)


def test_request_never_leaks_token_in_error_message():
    with _urlopen_returning({"ok": False, "error": "invalid_blocks"}):
        with pytest.raises(slack_notify.SlackError) as ei:
            slack_notify._request(TOKEN, "chat.postMessage", {"channel": "D1", "text": "x"})
    assert TOKEN not in str(ei.value)
    assert "SECRET" not in str(ei.value)


def test_request_without_token_raises_before_http():
    with mock.patch("urllib.request.urlopen") as m:
        with pytest.raises(slack_notify.SlackError):
            slack_notify._request("", "chat.postMessage", {})
    m.assert_not_called()


def test_request_http_429_reports_retry_after():
    import urllib.error

    err = urllib.error.HTTPError(
        "https://slack.com/api/chat.postMessage", 429, "Too Many Requests", {"Retry-After": "30"}, None
    )
    with mock.patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(slack_notify.SlackError) as ei:
            slack_notify._request(TOKEN, "chat.postMessage", {"channel": "D1", "text": "x"})
    assert "30" in str(ei.value)


# ---------- 3단계 DM 흐름 ----------


def test_lookup_user_id_returns_id():
    with mock.patch.object(slack_notify, "_request", return_value={"ok": True, "user": {"id": "U42"}}) as m:
        assert slack_notify.lookup_user_id(TOKEN, "a@x.com") == "U42"
    m.assert_called_once_with(TOKEN, "users.lookupByEmail", {"email": "a@x.com"})


def test_lookup_user_id_missing_id_raises_not_found():
    with mock.patch.object(slack_notify, "_request", return_value={"ok": True, "user": {}}):
        with pytest.raises(slack_notify.SlackUserNotFound):
            slack_notify.lookup_user_id(TOKEN, "a@x.com")


def test_open_dm_returns_channel_id():
    with mock.patch.object(
        slack_notify, "_request", return_value={"ok": True, "channel": {"id": "D9"}}
    ) as m:
        assert slack_notify.open_dm(TOKEN, "U42") == "D9"
    m.assert_called_once_with(TOKEN, "conversations.open", {"users": "U42"})


def test_send_dm_calls_three_apis_in_order():
    calls: list[str] = []

    def _fake_request(token, method, payload):
        calls.append(method)
        return {
            "users.lookupByEmail": {"ok": True, "user": {"id": "U42"}},
            "conversations.open": {"ok": True, "channel": {"id": "D9"}},
            "chat.postMessage": {"ok": True, "ts": "1712.0001"},
        }[method]

    with mock.patch.object(slack_notify, "_request", side_effect=_fake_request):
        ts = slack_notify.send_dm(TOKEN, "a@x.com", text="본문", blocks=[{"type": "divider"}])
    assert calls == ["users.lookupByEmail", "conversations.open", "chat.postMessage"]
    assert ts == "1712.0001"


def test_post_message_always_sends_fallback_text():
    """blocks 만 보내면 알림 목록에 빈 메시지로 뜬다 → text 는 항상 동반돼야 한다."""
    with mock.patch.object(slack_notify, "_request", return_value={"ok": True, "ts": "1"}) as m:
        slack_notify.post_message(TOKEN, "D9", text="폴백", blocks=[{"type": "divider"}])
    payload = m.call_args[0][2]
    assert payload["text"] == "폴백"
    assert payload["blocks"] == [{"type": "divider"}]


# ---------- 설정 감지 ----------


def test_is_configured_reflects_env(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    assert slack_notify.is_configured() is False
    monkeypatch.setenv("SLACK_BOT_TOKEN", "  xoxb-abc  ")
    assert slack_notify.is_configured() is True
    assert slack_notify.bot_token() == "xoxb-abc"  # 공백 제거


# ---------- 메시지 렌더링 ----------

_MEETING = {
    "title": "주간 회의",
    "date": "2026-08-04",
    "summary": {"agenda": [{"no": 1, "title": "안건A", "decisions": ["결정1"], "issues": []}]},
    "actionItems": [{"text": "보고서 작성", "owner": "개발팀", "due": "금요일"}],
    "transcript": [{"segmentId": 0, "timestamp": "00:01", "speakerId": "화자1", "text": "비밀대화"}],
}


def test_render_names_the_sharer_because_dm_comes_from_bot():
    """워크스페이스 공용 봇 명의로 나가므로 '누가 공유했는지'가 본문에 반드시 있어야 한다."""
    text, blocks = slack_notify.render_meeting_message(_MEETING, sender="김윤희")
    dumped = json.dumps(blocks, ensure_ascii=False)
    assert "김윤희님이 공유한 회의록" in dumped
    assert "김윤희" in text


def test_render_excludes_transcript():
    _, blocks = slack_notify.render_meeting_message(_MEETING, sender="김윤희")
    assert "비밀대화" not in json.dumps(blocks, ensure_ascii=False)


def test_render_includes_summary_and_action_items():
    _, blocks = slack_notify.render_meeting_message(_MEETING, sender="김윤희")
    dumped = json.dumps(blocks, ensure_ascii=False)
    assert "안건A" in dumped
    assert "결정1" in dumped
    assert "보고서 작성" in dumped
    assert "개발팀" in dumped


def test_render_note_and_url_are_optional():
    _, without = slack_notify.render_meeting_message(_MEETING, sender="김윤희")
    _, with_extra = slack_notify.render_meeting_message(
        _MEETING, sender="김윤희", note="검토 부탁드립니다", meeting_url="http://lb.example"
    )
    assert "검토 부탁드립니다" not in json.dumps(without, ensure_ascii=False)
    dumped = json.dumps(with_extra, ensure_ascii=False)
    assert "검토 부탁드립니다" in dumped
    assert "http://lb.example" in dumped


def test_render_truncates_over_slack_section_limit():
    """section 텍스트 3000자 초과는 invalid_blocks 로 발송이 통째로 실패한다."""
    huge = {
        "title": "긴 회의",
        "summary": {"agenda": [{"no": 1, "title": "안건", "points": ["가" * 5000]}]},
        "actionItems": [],
    }
    _, blocks = slack_notify.render_meeting_message(huge, sender="김윤희")
    for b in blocks:
        if b.get("type") == "section":
            assert len(b["text"]["text"]) <= 3000
    assert "생략" in json.dumps(blocks, ensure_ascii=False)
