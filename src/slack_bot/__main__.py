"""Slack 봇 진입점 — `sudo .venv/bin/python -m src.slack_bot`.

Socket Mode 아웃바운드 WebSocket 으로 접속(인바운드 포트/공인 URL 개방 없음).
"""
from __future__ import annotations

import sys

from src.slack_bot import config


def main() -> None:
    config.validate()
    try:
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError:
        print(
            "slack_bolt 미설치 — Slack 봇 의존성을 먼저 설치하세요: uv sync --extra slack",
            file=sys.stderr,
        )
        raise SystemExit(1)

    from src.slack_bot.bot import build_app

    app = build_app()
    SocketModeHandler(app, config.SLACK_APP_TOKEN).start()


if __name__ == "__main__":
    main()
