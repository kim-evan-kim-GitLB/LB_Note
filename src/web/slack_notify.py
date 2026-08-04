"""Slack DM 발송 — 워크스페이스 봇 토큰(xoxb) 기반 순수 함수 모듈.

회의록을 참석자에게 **개인 DM** 으로 보낸다(채널 브로드캐스트 아님). 인증은 Slack 컨트롤 봇
(`src/slack_bot/`)과 **같은 `SLACK_BOT_TOKEN`** 을 쓰되, 프로세스는 별개다 — 봇은 Socket Mode
인바운드 전용이라 웹앱이 호출할 HTTP 엔드포인트가 없다. 여기서는 Slack Web API 를 직접 친다.

설계(jira_client.py 미러링):
  - 토큰/설정을 인자로 받는 순수 함수 — store/전역 상태 참조 없음. 토큰은 `bot_token()` 으로
    호출 시점에 환경변수를 읽는다(모듈 상수로 굳히면 테스트/재기동에서 stale).
  - HTTP 는 **함수 내부에서 지연 import 한 stdlib urllib.request** (신규 의존성 없음).
  - Slack Web API 는 실패도 **HTTP 200 + {"ok": false, "error": "..."}** 로 준다 → 상태코드가
    아니라 본문 `ok` 를 봐야 한다. 이게 일반 REST 와 다른 유일한 함정.
  - 에러는 SlackError(일반)/SlackAuthError(토큰 무효)/SlackUserNotFound(이메일 매칭 실패)로
    구분한다. **봇 토큰은 예외 메시지·로그에 절대 싣지 않는다.**

필요 스코프는 기존 봇 앱이 모두 보유 → 앱 재설치·추가 설정 불필요:
  users:read.email(lookupByEmail) / im:write(conversations.open) / chat:write(postMessage).
파일 첨부(files_upload_v2)는 files:write 가 필요해 v1 범위 밖이다.

설계 문서: docs/2026-07-24-slack-jira-연동-설계.md §3.1
"""
from __future__ import annotations

import json
import os

_API_BASE = "https://slack.com/api/"

# Slack section 블록 텍스트 상한은 3000자 — 여유를 두고 자른다(초과 시 invalid_blocks).
_MAX_SECTION = 2800

# 토큰 무효/권한 관련 Slack 에러 코드 — 재시도해도 소용없고 관리자 조치가 필요한 부류.
_AUTH_ERRORS = frozenset(
    {"invalid_auth", "not_authed", "account_inactive", "token_revoked", "token_expired"}
)

# ⚠️ Slack 은 메서드마다 허용 content-type 이 다르다. 아래 메서드들은 JSON 본문을 받지 않고
# application/x-www-form-urlencoded 만 받는다 — JSON 으로 보내면 인자가 통째로 무시돼
# `invalid_arguments` 가 난다(2026-08-04 실토큰 검증에서 발견). blocks 를 실어야 하는
# chat.postMessage 는 반대로 JSON 이어야 하므로 인코딩을 메서드별로 나눈다.
_FORM_METHODS = frozenset({"users.lookupByEmail"})


class SlackError(RuntimeError):
    """Slack API 호출 일반 실패(ok:false·연결오류·타임아웃·JSON 파싱 실패). 토큰 미포함."""


class SlackAuthError(SlackError):
    """봇 토큰 무효/비활성 — 관리자가 토큰을 재발급해야 하는 상태."""


class SlackUserNotFound(SlackError):
    """해당 이메일의 Slack 계정 없음(users_not_found). 수신자별 부분 실패로 처리한다."""


def bot_token() -> str:
    """워크스페이스 봇 토큰(xoxb). 미설정이면 빈 문자열 — 호출부가 not configured 로 매핑."""
    return (os.environ.get("SLACK_BOT_TOKEN") or "").strip()


def is_configured() -> bool:
    """봇 토큰이 설정돼 있는지. False 면 엔드포인트가 400 slack_not_configured 로 응답."""
    return bool(bot_token())


def _request(token: str, method: str, payload: dict) -> dict:
    """Slack Web API 호출(POST, JSON) → 응답 dict.

    Slack 은 실패도 HTTP 200 + {"ok": false, "error": ...} 로 주므로 본문 ok 를 검사한다.
    users_not_found → SlackUserNotFound, 토큰류 → SlackAuthError, 나머지 → SlackError.
    **예외 메시지에 토큰/헤더를 넣지 않는다.**
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    if not token:
        raise SlackError("Slack 봇 토큰이 설정되지 않았습니다.")
    if method in _FORM_METHODS:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        content_type = "application/x-www-form-urlencoded; charset=utf-8"
    else:
        data = json.dumps(payload).encode("utf-8")
        content_type = "application/json; charset=utf-8"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": content_type,
    }
    req = urllib.request.Request(_API_BASE + method, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — 고정 상수 URL
            body = resp.read()
    except urllib.error.HTTPError as e:
        # 429(rate limited)만 별도 문구 — Retry-After 를 그대로 노출해 호출부가 안내할 수 있게.
        if e.code == 429:
            retry = e.headers.get("Retry-After") if e.headers else None
            raise SlackError(f"Slack 요청 한도 초과(잠시 후 재시도: {retry or '?'}초)") from None
        raise SlackError(f"Slack API 오류(HTTP {e.code})") from None
    except urllib.error.URLError as e:
        raise SlackError(f"Slack 연결 실패: {e.reason}") from None
    except TimeoutError:
        raise SlackError("Slack 요청 타임아웃") from None
    try:
        out = json.loads(body)
    except json.JSONDecodeError as e:
        raise SlackError(f"Slack 응답 JSON 파싱 실패: {e}") from None
    if not isinstance(out, dict):
        raise SlackError("Slack 응답 형식이 올바르지 않습니다.")
    if not out.get("ok"):
        err = str(out.get("error") or "unknown_error")
        if err == "users_not_found":
            raise SlackUserNotFound(err)
        if err in _AUTH_ERRORS:
            raise SlackAuthError(f"Slack 봇 토큰 인증 실패({err})")
        raise SlackError(f"Slack API 오류({err})")
    return out


def lookup_user_id(token: str, email: str) -> str:
    """이메일 → Slack 유저 ID. 계정 없으면 SlackUserNotFound.

    Slack 프로필 이메일과 LB Note 계정 이메일이 **정확히 일치**해야 한다(봇의 비번초기화 흐름과
    동일 전제). 불일치는 조용한 실패가 아니라 수신자별 not_found 로 사용자에게 보여준다.
    """
    out = _request(token, "users.lookupByEmail", {"email": email})
    uid = ((out.get("user") or {}).get("id")) or ""
    if not uid:
        raise SlackUserNotFound("users_not_found")
    return str(uid)


def open_dm(token: str, user_id: str) -> str:
    """유저 ID → DM 채널 ID(conversations.open). 이미 열려 있으면 기존 채널을 돌려준다."""
    out = _request(token, "conversations.open", {"users": user_id})
    cid = ((out.get("channel") or {}).get("id")) or ""
    if not cid:
        raise SlackError("DM 채널을 열지 못했습니다.")
    return str(cid)


def post_message(token: str, channel: str, *, text: str, blocks: list | None = None) -> str:
    """chat.postMessage → 메시지 ts. text 는 알림/폴백용이므로 blocks 와 함께 항상 채운다."""
    payload: dict = {"channel": channel, "text": text}
    if blocks:
        payload["blocks"] = blocks
    out = _request(token, "chat.postMessage", payload)
    return str(out.get("ts") or "")


def send_dm(token: str, email: str, *, text: str, blocks: list | None = None) -> str:
    """이메일 1건에 DM 발송 — lookupByEmail → conversations.open → postMessage. 반환 ts."""
    uid = lookup_user_id(token, email)
    channel = open_dm(token, uid)
    return post_message(token, channel, text=text, blocks=blocks)


def _truncate(text: str, limit: int = _MAX_SECTION) -> str:
    """Slack 블록 상한 초과분을 잘라내고 말줄임 표시. 상한 초과는 invalid_blocks 로 발송 실패."""
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n… (이하 생략, 웹에서 전체 보기)"


def _mrkdwn(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def render_meeting_message(
    meeting: dict,
    *,
    sender: str,
    note: str | None = None,
    meeting_url: str | None = None,
) -> tuple[str, list]:
    """회의록 → (폴백 text, Slack blocks). 전사는 제외하고 요약+액션아이템만(이메일 본문과 동일 정책).

    발송 명의가 워크스페이스 공용 봇이라 수신자에겐 'LB Note 봇' DM 으로 보인다 → 첫 줄에
    **누가 공유했는지**를 반드시 명시한다(개인 명의 발송은 per-user Slack OAuth 필요, 후속).
    """
    from src.web import meeting_doc

    title = meeting_doc.doc_title(meeting)
    head = f"*{title}*"
    blocks: list = [
        _mrkdwn(head),
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"{sender}님이 공유한 회의록입니다."}],
        },
    ]
    if note:
        blocks.append(_mrkdwn(_truncate(note)))
    summary = meeting_doc._plain_summary(meeting).strip()
    if summary:
        blocks.append({"type": "divider"})
        blocks.append(_mrkdwn("*요약*\n" + _truncate(summary)))
    actions = meeting_doc._plain_action_items(meeting).strip()
    if actions:
        blocks.append(_mrkdwn("*액션 아이템*\n" + _truncate(actions)))
    if meeting_url:
        # 회의별 딥링크가 아니라 앱 루트다(프론트에 라우팅이 없어 회의 URL 이 존재하지 않음).
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"<{meeting_url}|LB Note 에서 전체 보기>"}],
            }
        )
    return f"{sender}님이 공유한 회의록: {title}", blocks
