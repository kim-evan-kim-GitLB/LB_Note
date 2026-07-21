"""Google Docs 템플릿 적용 — 관리자 지정 템플릿 문서를 복사 + 플레이스홀더 치환.

전역(관리자) 단일 양식 기능: 관리자가 만든 Google 문서를 템플릿으로 지정하면, 회의록 Drive
동기화 시 그 문서를 사용자 Drive(회의 폴더)로 복사(files.copy)한 뒤 Docs API
batchUpdate(replaceAllText)로 `{{title}}`·`{{summary}}`·`{{action_items}}` 등
플레이스홀더를 실제 값으로 치환한다. 복사이므로 템플릿의 서식(제목/표/스타일)이 그대로 보존된다.

스코프: 템플릿 복사는 사용자가 접근 가능한(관리자가 공유한) 문서를 읽어야 하므로 drive.readonly 가
필요하다(앱 생성 파일만 보는 drive.file 로는 관리자 템플릿을 읽을 수 없다). 복사본은 앱이 만든
파일이라 Docs batchUpdate 는 drive.file 로 커버된다. google 라이브러리는 함수 안에서 지연
import(미설치/미설정 환경에서도 모듈 로드 가능 — 옵트인 기능).
"""
from __future__ import annotations

import contextlib
import re

# Google 문서 URL 에서 문서 id 추출(…/document/d/{id}/…). raw id 도 허용.
_DOC_URL_RE = re.compile(r"/document/d/([a-zA-Z0-9_-]+)")
_RAW_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{20,}$")


class GoogleDocsTemplateError(RuntimeError):
    """템플릿 복사/치환 실패(스코프 부족·미공유·삭제 등). 호출부가 기본 HTML 회의록으로 폴백해 흡수."""


def extract_doc_id(url_or_id: str) -> str | None:
    """Google 문서 URL 또는 raw 문서 id 에서 문서 id 추출. 형식 불명이면 None."""
    s = (url_or_id or "").strip()
    if not s:
        return None
    m = _DOC_URL_RE.search(s)
    if m:
        return m.group(1)
    if _RAW_ID_RE.match(s):
        return s
    return None


def _status(exc: object) -> int | None:
    """HttpError → HTTP status. 아니면 None."""
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    try:
        return int(status) if status is not None else None
    except (ValueError, TypeError):
        return None


def _services(access_token: str):
    """access_token 으로 Drive v3 + Docs v1 서비스 생성(지연 import)."""
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as e:
        raise GoogleDocsTemplateError(f"google-api-python-client 미설치: {e}") from e
    creds = Credentials(token=access_token)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    docs = build("docs", "v1", credentials=creds, cache_discovery=False)
    return drive, docs


def _safe_delete(drive, file_id: str) -> None:
    """파일 삭제(best-effort). 실패는 무시(정리용)."""
    with contextlib.suppress(Exception):
        drive.files().delete(fileId=file_id).execute()


def apply_template(
    access_token: str,
    template_id: str,
    folder_id: str,
    title: str,
    replacements: dict[str, str],
    prev_doc_id: str | None,
) -> str:
    """템플릿 문서 복사 → 플레이스홀더 치환 → 새 문서 id 반환.

    prev_doc_id 가 있으면(재동기화) 새 문서 생성 성공 뒤 best-effort 삭제한다 — 템플릿 모드는
    복사 기반이라 매 동기화마다 새 문서를 만든다(같은 fileId 갱신이 아님). 실패 시 반쪽 문서가
    남지 않도록 복사본을 정리한 뒤 GoogleDocsTemplateError 를 던진다(호출부가 HTML 폴백).
    """
    drive, docs = _services(access_token)
    from googleapiclient.errors import HttpError

    # 1) 템플릿 복사(사용자 Drive 의 회의 폴더로). 복사본은 앱 생성 파일 → 이후 Docs 편집은 drive.file 로 가능.
    try:
        copied = drive.files().copy(
            fileId=template_id,
            body={"name": title, "parents": [folder_id]},
            fields="id",
        ).execute()
    except HttpError as e:
        raise GoogleDocsTemplateError(f"템플릿 복사 실패({_status(e)}): {e}") from e
    new_id = copied["id"]

    # 2) 플레이스홀더 치환 — {{key}} → value. 빈 값도 치환해 잔여 플레이스홀더를 제거한다.
    requests = [
        {
            "replaceAllText": {
                "containsText": {"text": "{{" + key + "}}", "matchCase": True},
                "replaceText": value or "",
            }
        }
        for key, value in replacements.items()
    ]
    if requests:
        try:
            docs.documents().batchUpdate(
                documentId=new_id, body={"requests": requests}
            ).execute()
        except HttpError as e:
            _safe_delete(drive, new_id)  # 반쪽 문서 정리
            raise GoogleDocsTemplateError(f"플레이스홀더 치환 실패({_status(e)}): {e}") from e

    # 3) 이전 문서 정리(재동기화 시 중복 방지) — 새 문서 확정 후에만.
    if prev_doc_id and prev_doc_id != new_id:
        _safe_delete(drive, prev_doc_id)
    return new_id
