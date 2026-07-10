"""Slack 봇 환경설정 — /app/.env 로딩 + 필수값 검증.

LB Note 웹서버와 동일한 .env 를 공유한다(JWT_SECRET 재사용). slack_bolt 를 import 하지 않으므로
config/lbnote_client/handlers 는 slack-bolt 미설치 환경에서도 임포트된다(bot.py/__main__.py 만 의존).
"""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

# 웹서버와 동일한 프로젝트 루트 .env 를 로드(JWT_SECRET 등 공유).
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(_ENV_PATH)

import os  # noqa: E402  (load_dotenv 이후 os.environ 읽기)


def _csv_set(raw: str | None) -> set[str]:
    """CSV 문자열 → 공백 제거된 값 집합. 빈/미설정이면 빈 집합(= 전체 허용 의미)."""
    if not raw:
        return set()
    return {tok.strip() for tok in raw.split(",") if tok.strip()}


SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
# 반응 허용 채널 ID CSV. 빈 집합이면 모든 채널 허용.
SLACK_ALLOWED_CHANNELS = _csv_set(os.environ.get("SLACK_ALLOWED_CHANNELS"))
# 공지 브로드캐스트 대상 채널. 미설정 시 명령이 온 채널로 공지.
SLACK_NOTICE_CHANNEL = os.environ.get("SLACK_NOTICE_CHANNEL") or None
LBNOTE_API_BASE = os.environ.get("LBNOTE_API_BASE") or "http://127.0.0.1:8088"
JWT_SECRET = os.environ.get("JWT_SECRET", "")


def validate() -> None:
    """필수 환경변수 누락 시 명확한 RuntimeError. 봇 기동 진입점에서 1회 호출."""
    missing: list[str] = []
    if not SLACK_BOT_TOKEN:
        missing.append("SLACK_BOT_TOKEN")
    if not SLACK_APP_TOKEN:
        missing.append("SLACK_APP_TOKEN")
    if not JWT_SECRET:
        missing.append("JWT_SECRET")
    if missing:
        raise RuntimeError(
            "Slack 봇 필수 환경변수 누락: "
            + ", ".join(missing)
            + f" — {_ENV_PATH} 에 설정하세요."
        )
