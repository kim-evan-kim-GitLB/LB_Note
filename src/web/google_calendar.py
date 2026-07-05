"""Google Calendar 양방향 연동 — 일정 읽기(events.list) + 회의 쓰기(events.insert/update).

calendar.events 스코프로 사용자 본인 캘린더의 일정을 읽어 앱 캘린더에 표시하고(→앱), 앱 회의를
구글 캘린더 이벤트로 생성/갱신한다(앱→). 재동기화는 저장된 eventId 를 events.update 해 같은
일정을 갱신한다(중복 생성 없음). eventId 가 404/410(삭제)면 재생성(자가치유).

google 라이브러리는 함수 안에서 지연 import(미설치 환경에서도 모듈 로드 — 옵트인 기능).
"""
from __future__ import annotations


class GoogleCalendarError(RuntimeError):
    """Calendar API 호출 실패(라이브러리 미설치·요청 오류 등)."""


def _service(access_token: str):
    """access_token 으로 Calendar v3 서비스 생성(지연 import)."""
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as e:
        raise GoogleCalendarError(f"google-api-python-client 미설치: {e}") from e
    creds = Credentials(token=access_token)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _status(exc: object) -> int | None:
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    try:
        return int(status) if status is not None else None
    except (ValueError, TypeError):
        return None


def list_events(
    access_token: str,
    *,
    time_min: str | None = None,
    time_max: str | None = None,
    calendar_id: str = "primary",
    max_results: int = 250,
) -> list[dict]:
    """캘린더 일정 목록(events.list) → 원시 event dict 리스트(프론트가 start/end/attendees 매핑).

    time_min/time_max 는 RFC3339(예: 2026-07-01T00:00:00Z). 시작시각 오름차순·단일 인스턴스 전개
    (singleEvents=True)로 반환한다. 미지정이면 API 기본 범위.
    """
    service = _service(access_token)
    from googleapiclient.errors import HttpError

    params: dict = {
        "calendarId": calendar_id,
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": max(1, min(int(max_results), 2500)),
    }
    if time_min:
        params["timeMin"] = time_min
    if time_max:
        params["timeMax"] = time_max
    try:
        resp = service.events().list(**params).execute()
    except HttpError as e:
        raise GoogleCalendarError(f"일정 조회 실패: {e}") from e
    return resp.get("items", [])


def upsert_event(
    access_token: str,
    *,
    calendar_id: str = "primary",
    event_body: dict,
    event_id: str | None = None,
) -> tuple[str, str | None]:
    """이벤트 생성/갱신 → (eventId, htmlLink).

    event_id 가 있으면 events.update 로 같은 일정 갱신(중복 없음). 404/410(삭제됨)이면 재생성
    (자가치유). 없으면 events.insert 로 새 일정 생성.
    """
    service = _service(access_token)
    from googleapiclient.errors import HttpError

    if event_id:
        try:
            ev = service.events().update(
                calendarId=calendar_id, eventId=event_id, body=event_body
            ).execute()
            return ev["id"], ev.get("htmlLink")
        except HttpError as e:
            if _status(e) not in (403, 404, 410):
                raise GoogleCalendarError(f"일정 갱신 실패: {e}") from e
            # 삭제/접근불가 → 아래에서 재생성(자가치유)
    try:
        ev = service.events().insert(calendarId=calendar_id, body=event_body).execute()
    except HttpError as e:
        raise GoogleCalendarError(f"일정 생성 실패: {e}") from e
    return ev["id"], ev.get("htmlLink")


def delete_event(access_token: str, event_id: str, *, calendar_id: str = "primary") -> bool:
    """이벤트 삭제(회의 삭제 동반, best-effort). 이미 없으면(404/410) 성공으로 간주(멱등)."""
    if not event_id:
        return False
    service = _service(access_token)
    from googleapiclient.errors import HttpError

    try:
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        return True
    except HttpError as e:
        return _status(e) in (404, 410)  # 이미 없음 → 멱등 성공
    except Exception:  # noqa: BLE001
        return False
