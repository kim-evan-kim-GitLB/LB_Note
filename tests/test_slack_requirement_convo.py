"""Slack 요구사항 되묻기(스레드 대화) 상태머신 회귀 테스트.

검증 불변식:
  - `요구사항`(내용 없음) → 대화 시작·입력 요청, 저장 없음.
  - 스레드 답글 → 저장 후 '추가?' 안내(awaiting_more).
  - 정확히 '예' 일 때만 다음 입력을 더 받음. 그 외(네/yes/예요/아무거나) → 종료.
  - 인라인 `요구사항 <내용>` → 즉시 저장 후 '추가?'.
  - 추적하지 않는 스레드/타인 답글 → None(무시).
  - 저장 실패 → 오류 문구 + 상태 유지(재입력 가능).
  - TTL 만료 → 대화 없음 취급.
  - 슬래시(thread_ts="") → 되묻기 없이 한 방 저장(하위호환).

slack_bolt 미의존(순수 상태 로직). 실제 LB Note 미호출(FakeClient).
실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_slack_requirement_convo.py
"""
from __future__ import annotations

import time

from src.slack_bot import conversation, handlers

CH = "C1"
TS = "1700000000.0001"
USER = "U1"


class FakeClient:
    """lbnote_client 대역 — create_requirement 호출 기록/실패 주입."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.fail = fail

    def create_requirement(self, text: str, reporter: str | None) -> dict:
        self.calls.append((text, reporter))
        if self.fail:
            raise RuntimeError("boom")
        return {"id": len(self.calls)}


def _setup() -> None:
    conversation._reset_for_test()


def test_full_conversation_happy_path():
    _setup()
    c = FakeClient()
    # 1) `요구사항` → 입력 요청, 저장 없음.
    r = handlers.requirement_start(c, CH, TS, USER, "", "rep@x.com")
    assert r == handlers._MSG_PROMPT
    assert c.calls == []
    assert conversation.get(CH, TS, USER) == conversation.STATE_AWAITING_TEXT
    # 2) 내용 답글 → 저장 + '추가?'.
    r = handlers.requirement_reply(c, CH, TS, USER, "화자분리 기능", "rep@x.com")
    assert r == handlers._MSG_CONFIRM_MORE
    assert c.calls == [("화자분리 기능", "rep@x.com")]
    assert conversation.get(CH, TS, USER) == conversation.STATE_AWAITING_MORE
    # 3) 정확히 '예' → 다시 입력 요청.
    r = handlers.requirement_reply(c, CH, TS, USER, "예", "rep@x.com")
    assert r == handlers._MSG_PROMPT
    assert conversation.get(CH, TS, USER) == conversation.STATE_AWAITING_TEXT
    # 4) 두 번째 내용 → 저장 + '추가?'.
    r = handlers.requirement_reply(c, CH, TS, USER, "요약 길이 옵션", "rep@x.com")
    assert r == handlers._MSG_CONFIRM_MORE
    assert len(c.calls) == 2
    # 5) '아니오' → 종료(대화 제거).
    r = handlers.requirement_reply(c, CH, TS, USER, "아니오", "rep@x.com")
    assert r == handlers._MSG_END
    assert conversation.get(CH, TS, USER) is None


def test_yes_must_be_exact():
    _setup()
    c = FakeClient()
    for wrong in ["네", "yes", "예요", "예!", "ㅇㅇ", ""]:
        conversation.start(CH, TS, USER, conversation.STATE_AWAITING_MORE)
        r = handlers.requirement_reply(c, CH, TS, USER, wrong, None)
        assert r == handlers._MSG_END, wrong
        assert conversation.get(CH, TS, USER) is None
    # 앞뒤 공백만 있는 '예'는 정확 매칭으로 인정.
    conversation.start(CH, TS, USER, conversation.STATE_AWAITING_MORE)
    assert handlers.requirement_reply(c, CH, TS, USER, "  예  ", None) == handlers._MSG_PROMPT


def test_inline_content_saves_immediately():
    _setup()
    c = FakeClient()
    r = handlers.requirement_start(c, CH, TS, USER, "바로 저장할 내용", "rep@x.com")
    assert r == handlers._MSG_CONFIRM_MORE
    assert c.calls == [("바로 저장할 내용", "rep@x.com")]
    assert conversation.get(CH, TS, USER) == conversation.STATE_AWAITING_MORE


def test_reply_on_untracked_thread_is_ignored():
    _setup()
    c = FakeClient()
    # 시작하지 않은 스레드 → None.
    assert handlers.requirement_reply(c, CH, "9999.1", USER, "내용", None) is None
    assert c.calls == []


def test_reply_from_other_user_ignored():
    _setup()
    c = FakeClient()
    handlers.requirement_start(c, CH, TS, USER, "", None)
    # 다른 사용자가 같은 스레드에 답글 → None(대화 소유자만 진행).
    assert handlers.requirement_reply(c, CH, TS, "U2", "가로채기", None) is None
    assert c.calls == []


def test_save_failure_keeps_awaiting_text():
    _setup()
    c = FakeClient(fail=True)
    handlers.requirement_start(c, CH, TS, USER, "", None)
    r = handlers.requirement_reply(c, CH, TS, USER, "저장 실패할 내용", None)
    assert r == handlers._MSG_SAVE_ERR
    # 상태 유지 → 같은 스레드에서 재입력 가능.
    assert conversation.get(CH, TS, USER) == conversation.STATE_AWAITING_TEXT


def test_ttl_expiry_drops_conversation():
    _setup()
    c = FakeClient()
    handlers.requirement_start(c, CH, TS, USER, "", None)
    # 저장된 대화의 활성 시각을 TTL 이전으로 되돌린다.
    conversation._convos[(CH, TS)]["at"] = time.monotonic() - conversation.TTL_SEC - 1
    assert conversation.get(CH, TS, USER) is None
    assert handlers.requirement_reply(c, CH, TS, USER, "만료된 대화", None) is None


def test_slash_no_thread_one_shot():
    _setup()
    c = FakeClient()
    # thread_ts="" (슬래시) + 내용 → 한 방 저장(접수번호 안내), 대화 미생성.
    r = handlers.requirement_start(c, "", "", USER, "슬래시 요구사항", "rep")
    assert "접수" in r and c.calls == [("슬래시 요구사항", "rep")]
    # thread_ts="" + 내용 없음 → 사용법 안내.
    r2 = handlers.requirement_start(c, "", "", USER, "", "rep")
    assert "입력해 주세요" in r2


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_slack_requirement_convo ({len(fns)} cases)")
