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


def _intake_token() -> str:
    """단명(60초) 요구사항 인테이크 스코프 토큰. admin 위조 없이 요구사항만 쓸 수 있다.

    백엔드 require_requirement_writer 가 scope='requirement_intake' 를 서명만 검증(DB 조회 없음)해
    통과시킨다. 사용자별 role 이나 'admin' 계정 존재 여부에 의존하지 않는다.
    """
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": "slack-bot",
        "iat": now,
        "exp": now + dt.timedelta(seconds=60),
        "scope": "requirement_intake",
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def _request(
    method: str, path: str, *, body: dict | None = None, admin: bool = False, intake: bool = False
) -> dict:
    """LB Note API 호출 → JSON dict. 비 2xx 는 LBNoteError(404 는 UserNotFound).

    admin=True → admin 세션 토큰, intake=True → 요구사항 인테이크 스코프 토큰. 둘 다 지정 시 admin 우선.
    연결 자체 실패(URLError: 서버 다운·타임아웃)도 LBNoteError 로 감싸 상위 핸들러가 일관 처리한다.
    """
    url = config.LBNOTE_API_BASE.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if admin:
        headers["Authorization"] = f"Bearer {_admin_token()}"
    elif intake:
        headers["Authorization"] = f"Bearer {_intake_token()}"
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
    except urllib.error.URLError as e:  # 연결 거부·DNS·타임아웃 등(HTTPError 아님)
        raise LBNoteError(0, str(getattr(e, "reason", e))) from e


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


def get_user_role(username: str) -> str | None:
    """LB Note 계정 role 조회(admin 권한). 매칭 계정 없으면 None.

    공지 권한 게이트용 — 요청자 Slack 이메일(==username 가정)로 관리자 명부에서 role 을 찾는다.
    `GET /api/admin/users` 재사용(신규 엔드포인트 없이). username 정확 매칭.
    """
    data = _request("GET", "/api/admin/users", admin=True)
    for u in data.get("users", []):
        if u.get("username") == username:
            return u.get("role")
    return None


def get_latest_notice() -> dict | None:
    """가장 최근 활성 공지 조회(admin). 없으면 None. 봇 `공지` 가 읽어 배포한다."""
    return _request("GET", "/api/notices/latest", admin=True).get("notice")


def create_requirement(text: str, reporter: str | None) -> dict:
    """요구사항 적재(source='slack'). 생성 행(id 포함) 반환.

    admin 이 아니라 요구사항 인테이크 스코프 토큰으로 쓴다 → 'admin' 계정 존재/권한에 의존하지 않음.
    """
    return _request(
        "POST",
        "/api/requirements",
        body={"text": text, "source": "slack", "reporter": reporter},
        intake=True,
    )
