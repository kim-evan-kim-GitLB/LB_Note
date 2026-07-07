"""Google Drive 업로드/갱신 — 회의록 Docs 변환 + 원본 오디오(google-api-python-client).

drive.file 스코프(앱이 만든 파일만)로 앱 전용 루트 폴더 아래에 회의록 문서/오디오를 만든다.
재동기화는 저장된 fileId 를 files.update 해 같은 파일을 갱신한다(중복 생성 없음). fileId 가 404/403
(사용자가 드라이브에서 삭제)면 재생성한다(자가치유).

google 라이브러리는 함수 안에서 지연 import(미설치 환경에서도 모듈 로드 가능 — 옵트인 기능).
"""
from __future__ import annotations

from pathlib import Path

# 앱 전용 루트 폴더 이름(drive.file 스코프로 생성). 사용자 드라이브에 이 폴더가 보인다.
# 구조: LB_NOTE/{회의명_날짜_시간}/{회의록(Docs), 원본.{ext}}
ROOT_FOLDER_NAME = "LB_NOTE"
_FOLDER_MIME = "application/vnd.google-apps.folder"
_DOC_MIME = "application/vnd.google-apps.document"


class GoogleDriveError(RuntimeError):
    """Drive API 호출 실패(라이브러리 미설치·업로드 오류 등)."""


def _drive(access_token: str):
    """access_token 으로 Drive v3 서비스 생성(지연 import)."""
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as e:
        raise GoogleDriveError(f"google-api-python-client 미설치: {e}") from e
    creds = Credentials(token=access_token)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _status(exc: object) -> int | None:
    """HttpError → HTTP status. 아니면 None."""
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    try:
        return int(status) if status is not None else None
    except (ValueError, TypeError):
        return None


def ensure_root_folder(access_token: str, folder_id: str | None) -> str:
    """앱 루트 폴더 id 확보. folder_id 가 유효하면 그대로, 없거나 404/삭제면 새로 생성한다."""
    service = _drive(access_token)
    from googleapiclient.errors import HttpError

    if folder_id:
        try:
            meta = service.files().get(fileId=folder_id, fields="id,trashed").execute()
            if not meta.get("trashed"):
                return meta["id"]
        except HttpError as e:
            if _status(e) not in (403, 404):
                raise GoogleDriveError(f"루트 폴더 조회 실패: {e}") from e
            # 403/404 → 폴더가 사라졌거나 접근 불가 → 아래에서 재생성
    try:
        created = service.files().create(
            body={"name": ROOT_FOLDER_NAME, "mimeType": _FOLDER_MIME}, fields="id"
        ).execute()
    except HttpError as e:
        raise GoogleDriveError(f"루트 폴더 생성 실패: {e}") from e
    return created["id"]


def ensure_subfolder(
    access_token: str, parent_id: str, name: str, folder_id: str | None
) -> str:
    """parent_id 아래 회의별 하위 폴더 확보 → 하위 폴더 id.

    folder_id 가 유효하면 그대로 재사용(멱등 재동기화). 없거나 404/삭제면 parent 아래에서
    name 으로 검색해 있으면 그 폴더, 없으면 새로 생성한다(중복 폴더 생성 방지).
    """
    service = _drive(access_token)
    from googleapiclient.errors import HttpError

    if folder_id:
        try:
            meta = service.files().get(fileId=folder_id, fields="id,trashed").execute()
            if not meta.get("trashed"):
                return meta["id"]
        except HttpError as e:
            if _status(e) not in (403, 404):
                raise GoogleDriveError(f"하위 폴더 조회 실패: {e}") from e
            # 403/404 → 폴더가 사라짐 → 아래에서 검색/재생성
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")  # Drive q 문자열 이스케이프
    query = (
        f"name = '{escaped}' and '{parent_id}' in parents "
        f"and mimeType = '{_FOLDER_MIME}' and trashed = false"
    )
    try:
        res = service.files().list(
            q=query, fields="files(id)", pageSize=1, spaces="drive"
        ).execute()
        found = res.get("files") or []
        if found:
            return found[0]["id"]
        created = service.files().create(
            body={"name": name, "mimeType": _FOLDER_MIME, "parents": [parent_id]},
            fields="id",
        ).execute()
    except HttpError as e:
        raise GoogleDriveError(f"하위 폴더 생성 실패: {e}") from e
    return created["id"]


def upsert_doc(
    access_token: str, folder_id: str, html: str, title: str, doc_id: str | None
) -> str:
    """회의록 HTML → Google Docs 네이티브 문서 생성/갱신 → docId.

    doc_id 가 있으면 files.update(media=html)로 같은 문서 내용을 교체(fileId 유지·중복 없음).
    404/403 이면 재생성(자가치유). 없으면 files.create(convert)로 새 Docs 문서 생성.
    """
    service = _drive(access_token)
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaInMemoryUpload

    media = MediaInMemoryUpload(html.encode("utf-8"), mimetype="text/html", resumable=False)
    if doc_id:
        try:
            updated = service.files().update(
                fileId=doc_id, media_body=media, body={"name": title}, fields="id"
            ).execute()
            return updated["id"]
        except HttpError as e:
            if _status(e) not in (403, 404):
                raise GoogleDriveError(f"문서 갱신 실패: {e}") from e
            # 삭제/접근불가 → 아래에서 재생성(자가치유). media 는 재사용 가능.
    try:
        created = service.files().create(
            body={"name": title, "mimeType": _DOC_MIME, "parents": [folder_id]},
            media_body=media,
            fields="id",
        ).execute()
    except HttpError as e:
        raise GoogleDriveError(f"문서 생성 실패: {e}") from e
    return created["id"]


def upsert_audio(
    access_token: str,
    folder_id: str,
    audio_path: Path,
    mime: str,
    name: str,
    audio_id: str | None,
) -> str:
    """원본 오디오 업로드 → audioId. audio_id 가 이미 있으면 재업로드 skip(오디오 불변, 대역폭 절약).

    무변환 업로드(resumable — 대용량 ≤500MB). 신규만 생성한다(오디오는 편집으로 바뀌지 않음).
    """
    if audio_id:
        return audio_id  # 오디오는 불변 → 이미 업로드됨, 재업로드 skip
    service = _drive(access_token)
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(str(audio_path), mimetype=mime, resumable=True)
    try:
        request = service.files().create(
            body={"name": name, "parents": [folder_id]}, media_body=media, fields="id"
        )
        response = None
        while response is None:
            _status_chunk, response = request.next_chunk()  # resumable 청크 업로드 루프
    except HttpError as e:
        raise GoogleDriveError(f"오디오 업로드 실패: {e}") from e
    return response["id"]


def export_doc(access_token: str, doc_id: str, mime: str = "application/pdf") -> bytes:
    """Google Docs 문서를 지정 포맷(기본 PDF)으로 export → bytes. 이메일 첨부용.

    files.export(mimeType=...) — Docs 네이티브 문서를 PDF/DOCX 등으로 변환해 받는다.
    404/403 이면 GoogleDriveError(호출부가 매핑). 대용량은 export 한도(10MB) 내 가정.
    """
    service = _drive(access_token)
    from googleapiclient.errors import HttpError

    try:
        return service.files().export(fileId=doc_id, mimeType=mime).execute()
    except HttpError as e:
        raise GoogleDriveError(f"문서 export 실패({_status(e)}): {e}") from e


def stream_media(access_token: str, file_id: str, range_header: str | None):
    """Drive 파일을 alt=media 로 GET → (status_code, headers: dict, reader) 릴레이용.

    로컬 원본을 Drive 업로드 후 삭제한 회의의 오디오 재생 프록시. Range 헤더를 그대로 전달해
    부분 응답(206)을 받고, 호출부가 status/헤더(Content-Range·Content-Length·Content-Type)를
    클라이언트에 그대로 중계한다. reader 는 read(n) 가능한 file-like(스트리밍·close 는 호출부).
    401/403/404 는 접근 불가 → GoogleDriveError(호출부가 404 로 은닉). 416 은 그대로 릴레이.
    """
    import urllib.error
    import urllib.request

    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    headers = {"Authorization": f"Bearer {access_token}"}
    if range_header:
        headers["Range"] = range_header
    req = urllib.request.Request(url, headers=headers)  # 기본 GET
    try:
        resp = urllib.request.urlopen(req, timeout=30)  # noqa: S310 — 고정 https 엔드포인트
    except urllib.error.HTTPError as e:
        if e.code == 416:  # 범위 불만족 → 헤더만 릴레이(본문 없음). fd 는 즉시 닫는다.
            hdrs = e.headers
            e.close()
            return 416, hdrs, None
        raise GoogleDriveError(f"Drive 오디오 GET 실패({e.code})") from e
    except urllib.error.URLError as e:
        raise GoogleDriveError(f"Drive 오디오 스트리밍 실패: {e}") from e
    # resp.headers 는 email.message.Message → 케이스 무시 .get 지원(상류 헤더 케이스 변화에 강건).
    return getattr(resp, "status", 200), resp.headers, resp


def delete_files(access_token: str, file_ids: list[str]) -> int:
    """주어진 fileId 들을 삭제(회의 삭제 동반, best-effort) → 삭제 성공 개수.

    이미 없는(404) 파일은 성공으로 간주(멱등). 공유 루트 폴더는 삭제하지 않는다(다른 회의 파일
    보존) — 호출부는 docId/audioId 만 넘긴다.
    """
    ids = [f for f in file_ids if f]
    if not ids:
        return 0
    service = _drive(access_token)
    from googleapiclient.errors import HttpError

    removed = 0
    for fid in ids:
        try:
            service.files().delete(fileId=fid).execute()
            removed += 1
        except HttpError as e:
            if _status(e) == 404:
                removed += 1  # 이미 없음 → 멱등 성공
            # 그 외 오류는 무시(best-effort)
        except Exception:  # noqa: BLE001
            pass
    return removed


def revoke(refresh_token: str) -> None:
    """refresh_token 폐기(연동 해제 시 best-effort). 실패는 무시(로컬 삭제가 본질)."""
    import contextlib
    import urllib.parse
    import urllib.request

    with contextlib.suppress(Exception):
        data = urllib.parse.urlencode({"token": refresh_token}).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/revoke",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        urllib.request.urlopen(req, timeout=10).close()  # noqa: S310 — 고정 https 엔드포인트
