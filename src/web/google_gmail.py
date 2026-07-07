"""Gmail API 발송 — 회의록 이메일(사용자 본인 Gmail 로 전송, 읽기 없음).

gmail.send 스코프로 users.messages.send 호출. MIME(멀티파트: HTML 본문 + 첨부)를 base64url raw 로
싣는다. google 라이브러리는 함수 안에서 지연 import(미설치 환경에서도 모듈 로드 — 옵트인 기능).
"""
from __future__ import annotations

import base64


class GoogleGmailError(RuntimeError):
    """Gmail 발송 실패(라이브러리 미설치·API 오류 등)."""


class GmailScopeMissing(GoogleGmailError):
    """gmail.send 스코프 미동의(403) — 사용자 재연동 필요. endpoint 가 error_code 로 안내."""


def _gmail(access_token: str):
    """access_token 으로 Gmail v1 서비스 생성(지연 import)."""
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as e:
        raise GoogleGmailError(f"google-api-python-client 미설치: {e}") from e
    creds = Credentials(token=access_token)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _status(exc: object) -> int | None:
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    try:
        return int(status) if status is not None else None
    except (ValueError, TypeError):
        return None


def _build_mime(
    *,
    sender: str,
    to: list[str],
    cc: list[str],
    subject: str,
    html_body: str,
    attachment: bytes | None,
    attachment_name: str,
    attachment_mime: str,
) -> str:
    """MIME 메시지 → base64url raw 문자열. 첨부가 있으면 multipart/mixed, 없으면 단일 HTML."""
    from email.mime.application import MIMEApplication
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart("mixed")
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    if attachment is not None:
        maintype, _, subtype = attachment_mime.partition("/")
        part = MIMEApplication(attachment, _subtype=subtype or "octet-stream")
        part.add_header("Content-Disposition", "attachment", filename=attachment_name)
        msg.attach(part)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def send_message(
    access_token: str,
    *,
    sender: str,
    to: list[str],
    cc: list[str],
    subject: str,
    html_body: str,
    attachment: bytes | None = None,
    attachment_name: str = "회의록.pdf",
    attachment_mime: str = "application/pdf",
) -> str:
    """회의록 이메일 발송 → messageId. 403(스코프 미동의)이면 GmailScopeMissing.

    sender 는 인증 사용자 본인 주소(Gmail 은 From 을 인증 계정으로 강제 — 위조 방지). to/cc 는
    이메일 주소 리스트. 첨부(PDF 등)는 선택.
    """
    service = _gmail(access_token)
    from googleapiclient.errors import HttpError

    raw = _build_mime(
        sender=sender,
        to=to,
        cc=cc,
        subject=subject,
        html_body=html_body,
        attachment=attachment,
        attachment_name=attachment_name,
        attachment_mime=attachment_mime,
    )
    try:
        sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    except HttpError as e:
        if _status(e) == 403:  # ACCESS_TOKEN_SCOPE_INSUFFICIENT 등 → 재연동 유도
            raise GmailScopeMissing(f"gmail.send 미동의(재연동 필요): {e}") from e
        raise GoogleGmailError(f"이메일 발송 실패({_status(e)}): {e}") from e
    return sent.get("id", "")
