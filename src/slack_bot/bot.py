"""slack_bolt App 구성 — app_mention + /lbnote 슬래시를 공통 디스패처로 라우팅.

slack_bolt 는 여기(및 __main__)에서만 import 한다(선택 의존성 slack 그룹).
"""
from __future__ import annotations

import re
import sys
import traceback

from slack_bolt import App

from src.slack_bot import config, conversation, handlers, lbnote_client

# app_mention 텍스트 선두의 `<@BOTID>` 멘션 제거용.
_MENTION_RE = re.compile(r"^\s*<@[^>]+>\s*")

# 서브명령 별칭(한글/영문) → 정규 키.
_ALIASES = {
    "비번초기화": "reset",
    "reset": "reset",
    "상태": "status",
    "status": "status",
    "공지": "notice",
    "notice": "notice",
    "요구사항": "requirement",
    "req": "requirement",
    "help": "help",
}


def _channel_allowed(channel_id: str) -> bool:
    """화이트리스트 설정 시 그 채널만 허용. 미설정(빈 집합)이면 전체 허용."""
    if not config.SLACK_ALLOWED_CHANNELS:
        return True
    return channel_id in config.SLACK_ALLOWED_CHANNELS


def _reporter_of(slack_client, user_id: str) -> str | None:
    """요구사항 reporter — 요청자 이메일 우선, 없으면 표시명."""
    try:
        info = slack_client.users_info(user=user_id)
        profile = info["user"]["profile"] or {}
        return profile.get("email") or profile.get("real_name") or info["user"].get("name")
    except Exception:
        return None


def dispatch(text: str, user_id: str, channel_id: str, thread_ts: str, say, slack_client) -> None:
    """공통 디스패처 — 첫 토큰을 서브명령으로 파싱해 핸들러 라우팅. 예외는 일반 안내로 흡수.

    thread_ts: 요구사항 되묻기 대화를 게시/추적할 스레드 루트 ts(멘션이면 그 메시지 ts, 슬래시면 "").
    """
    if not _channel_allowed(channel_id):
        return  # 허용 채널 아님 → 조용히 무시(비번초기화 포함 명령 접수 자체를 게이트).
    try:
        stripped = _MENTION_RE.sub("", text or "").strip()
        parts = stripped.split(maxsplit=1)
        raw_cmd = parts[0] if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        cmd = _ALIASES.get(raw_cmd.lower(), "help" if not raw_cmd else None)

        if cmd == "reset":
            say(handlers.handle_reset(slack_client, user_id))
        elif cmd == "status":
            say(handlers.handle_status(lbnote_client))
        elif cmd == "notice":
            # 공지는 웹 콘솔에서 작성된 최신 공지를 DB 에서 읽어 배포(관리자 전용).
            say(handlers.handle_notice(slack_client, channel_id, user_id))
        elif cmd == "requirement":
            # 요구사항은 스레드(댓글) 되묻기 대화를 시작한다 — 응답은 thread_ts 에 게시.
            reporter = _reporter_of(slack_client, user_id)
            resp = handlers.requirement_start(
                lbnote_client, channel_id, thread_ts, user_id, rest, reporter
            )
            _say_threaded(say, resp, thread_ts)
        else:
            say(handlers.help_text())
    except Exception:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        say("요청 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.")


def _say_threaded(say, text: str, thread_ts: str) -> None:
    """thread_ts 가 있으면 스레드에, 없으면 채널에 게시."""
    if thread_ts:
        say(text=text, thread_ts=thread_ts)
    else:
        say(text)


def handle_message_event(event: dict, say, slack_client) -> None:
    """진행 중 되묻기 대화의 스레드 답글(@멘션 없이 온 일반 메시지) 처리.

    봇/편집/합성 메시지는 무시하고, 우리가 추적 중인 (채널,스레드) 답글만 대화로 이어간다.
    (message.channels 이벤트 구독 + channels:history 스코프가 있어야 도달한다.)
    """
    if event.get("bot_id") or event.get("subtype"):
        return  # 봇 자신·편집·채널조인 등은 무시(무한루프 방지).
    thread_ts = event.get("thread_ts")
    if not thread_ts:
        return  # 스레드 답글만 되묻기 대화로 취급.
    channel = event.get("channel", "")
    user = event.get("user", "")
    stripped = _MENTION_RE.sub("", event.get("text", "") or "").strip()
    reporter = _reporter_of(slack_client, user)
    resp = handlers.requirement_reply(lbnote_client, channel, thread_ts, user, stripped, reporter)
    if resp is not None:  # None = 우리가 관리하는 대화 아님.
        say(text=resp, thread_ts=thread_ts)


def _route_mention(event: dict, say, slack_client) -> None:
    """app_mention — 진행 중 대화의 스레드 답글이면 대화 진행, 아니면 일반 명령 디스패치.

    (message 이벤트 미구독 상태에서도 @멘션으로는 대화를 이어갈 수 있는 폴백 경로.)
    """
    channel = event.get("channel", "")
    user = event.get("user", "")
    reply_ts = event.get("thread_ts")  # 답글이면 스레드 루트, 새 멘션이면 None.
    if reply_ts and conversation.get(channel, reply_ts, user) is not None:
        stripped = _MENTION_RE.sub("", event.get("text", "") or "").strip()
        reporter = _reporter_of(slack_client, user)
        resp = handlers.requirement_reply(lbnote_client, channel, reply_ts, user, stripped, reporter)
        if resp is not None:
            say(text=resp, thread_ts=reply_ts)
        return
    # 새 명령 — 새 멘션이면 그 메시지(ts)를 스레드 루트로 삼는다.
    thread_root = reply_ts or event.get("ts", "")
    dispatch(event.get("text", ""), user, channel, thread_root, say, slack_client)


def build_app() -> App:
    """slack_bolt App 생성 + 핸들러 등록."""
    app = App(token=config.SLACK_BOT_TOKEN)

    @app.event("app_mention")
    def _on_mention(event, say, client):
        _route_mention(event, say, client)

    @app.event("message")
    def _on_message(event, say, client):
        handle_message_event(event, say, client)

    @app.command("/lbnote")
    def _on_command(ack, command, say, client):
        ack()
        # 슬래시 명령은 스레드 루트가 없다 → thread_ts="" (요구사항은 한 방 저장으로 폴백).
        dispatch(
            command.get("text", ""),
            command.get("user_id", ""),
            command.get("channel_id", ""),
            "",
            say,
            client,
        )

    return app
