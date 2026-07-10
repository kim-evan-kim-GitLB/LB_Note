"""slack_bolt App 구성 — app_mention + /lbnote 슬래시를 공통 디스패처로 라우팅.

slack_bolt 는 여기(및 __main__)에서만 import 한다(선택 의존성 slack 그룹).
"""
from __future__ import annotations

import re
import sys
import traceback

from slack_bolt import App

from src.slack_bot import config, handlers, lbnote_client

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


def dispatch(text: str, user_id: str, channel_id: str, say, slack_client) -> None:
    """공통 디스패처 — 첫 토큰을 서브명령으로 파싱해 핸들러 라우팅. 예외는 일반 안내로 흡수."""
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
            if not rest:
                say("요구사항 내용을 입력해 주세요. 예) `요구사항 화자분리 기능`")
            else:
                reporter = _reporter_of(slack_client, user_id)
                say(handlers.handle_requirement(lbnote_client, rest, reporter))
        else:
            say(handlers.help_text())
    except Exception:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        say("요청 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.")


def build_app() -> App:
    """slack_bolt App 생성 + 핸들러 등록."""
    app = App(token=config.SLACK_BOT_TOKEN)

    @app.event("app_mention")
    def _on_mention(event, say, client):
        dispatch(
            event.get("text", ""),
            event.get("user", ""),
            event.get("channel", ""),
            say,
            client,
        )

    @app.command("/lbnote")
    def _on_command(ack, command, say, client):
        ack()
        dispatch(
            command.get("text", ""),
            command.get("user_id", ""),
            command.get("channel_id", ""),
            say,
            client,
        )

    return app
