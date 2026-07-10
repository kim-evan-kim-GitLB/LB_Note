"""LB Note FastAPI 호출 클라이언트 — stdlib urllib 만 사용(신규 HTTP 의존 없음).

관리자 권한이 필요한 호출은 매번 단명(60초) admin JWT 를 JWT_SECRET 으로 직접 서명해 쓴다.
서명 규약: sub='admin', scope 클레임 없음 → user_from_token(scope=None) 통과(세션 토큰 취급).
"""
from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.request

import jwt

from src.slack_bot import config

_TIMEOUT = 10  # 초


class LBNoteError(Exception):
    """LB Note API 비정상 응답(비 2xx). status/body 를 함께 담는다."""

    def __init__(self, status: int, body: str = "") -> None:
        super().__init__(f"LB Note API 오류 (HTTP {status}): {body}")
        self.status = status
        self.body = body


class UserNotFound(LBNoteError):
    """대상 사용자 미존재(reset-password 404). 계정 열거 방지 위해 상위에서 모호 처리."""


def _admin_token() -> str:
    """단명(60초) admin JWT 서명. scope 클레임 미포함(세션 토큰 규약)."""
    now = dt.datetime.now(dt.timezone.utc)
    payload = {"sub": "admin", "iat": now, "exp": now + dt.timedelta(seconds=60)}
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def _request(
    method: str, path: str, *, body: dict | None = None, admin: bool = False
) -> dict:
    """LB Note API 호출 → JSON dict. 비 2xx 는 LBNoteError(404 는 UserNotFound)."""
    url = config.LBNOTE_API_BASE.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if admin:
        headers["Authorization"] = f"Bearer {_admin_token()}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            detail = ""
        if e.code == 404:
            raise UserNotFound(e.code, detail) from e
        raise LBNoteError(e.code, detail) from e


def reset_password(username: str, new_password: str) -> None:
    """관리자 비번 초기화(must_change_password=1). 404 → UserNotFound."""
    _request(
        "POST",
        f"/api/admin/users/{username}/reset-password",
        body={"newPassword": new_password},
        admin=True,
    )


def health() -> dict:
    """공개 health 엔드포인트(인증 불필요)."""
    return _request("GET", "/api/health")


def metrics() -> dict:
    """관리자 운영 메트릭 스냅샷."""
    return _request("GET", "/api/admin/metrics", admin=True)


def create_requirement(text: str, reporter: str | None) -> dict:
    """요구사항 적재(source='slack'). 생성 행(id 포함) 반환."""
    return _request(
        "POST",
        "/api/requirements",
        body={"text": text, "source": "slack", "reporter": reporter},
        admin=True,
    )
