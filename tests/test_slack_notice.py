"""Slack `공지` 조회/배포 동작 회귀 테스트.

검증 불변식:
  - 모든 사용자: 현재 공지를 읽어서 반환(브로드캐스트 없음).
  - 관리자(role=admin): 공지 채널(없으면 명령 채널)에 브로드캐스트 + 게시 안내 반환.
  - 등록된 공지 없음: 안내 문구.
  - 권한 확인 실패(이메일 없음 등): 안전하게 '읽기'로 처리(브로드캐스트 안 함).

slack_bolt 미의존. 실제 LB Note 미호출(get_latest_notice/get_user_role 모킹).
실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_slack_notice.py
"""
from __future__ import annotations

from unittest import mock

from src.slack_bot import handlers


class FakeSlack:
    """slack_client 대역 — users_info/chat_postMessage."""

    def __init__(self, email: str | None = "u@x.com") -> None:
        self.email = email
        self.posted: list[tuple[str, str]] = []

    def users_info(self, user: str) -> dict:
        return {"user": {"profile": {"email": self.email}, "name": "n"}}

    def chat_postMessage(self, channel: str, text: str) -> None:
        self.posted.append((channel, text))


def test_regular_user_reads_in_place_no_broadcast():
    with mock.patch.object(handlers.lbnote_client, "get_latest_notice",
                           return_value={"title": "점검 안내", "body": "오늘 21시 점검"}), \
         mock.patch.object(handlers.lbnote_client, "get_user_role", return_value="user"):
        s = FakeSlack()
        out = handlers.handle_notice(s, "C1", "U1")
    assert "점검 안내" in out and "오늘 21시 점검" in out
    assert s.posted == []  # 일반 사용자는 브로드캐스트하지 않음


def test_admin_broadcasts_to_channel():
    with mock.patch.object(handlers.lbnote_client, "get_latest_notice",
                           return_value={"title": "T", "body": "B"}), \
         mock.patch.object(handlers.lbnote_client, "get_user_role", return_value="admin"):
        handlers.config.SLACK_NOTICE_CHANNEL = None  # 미설정 → 명령 채널로 브로드캐스트
        s = FakeSlack()
        out = handlers.handle_notice(s, "C9", "Uadmin")
    assert s.posted and s.posted[0][0] == "C9"
    assert "게시" in out


def test_no_notice_registered():
    with mock.patch.object(handlers.lbnote_client, "get_latest_notice", return_value=None):
        out = handlers.handle_notice(FakeSlack(), "C1", "U1")
    assert "등록된 공지가 없습니다" in out


def test_role_lookup_failure_defaults_to_read():
    # 이메일 없음 → get_user_role 미도달 → role None → 읽기(브로드캐스트 안 함).
    with mock.patch.object(handlers.lbnote_client, "get_latest_notice",
                           return_value={"title": "T", "body": "B"}):
        s = FakeSlack(email=None)
        out = handlers.handle_notice(s, "C1", "U1")
    assert "B" in out and s.posted == []


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_slack_notice ({len(fns)} cases)")
