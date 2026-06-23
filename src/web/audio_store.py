"""원본 오디오 영속 — 파일시스템 저장(플랜 v4 트랙 C·Phase 4, D7-id 옵션B).

회의 원본 오디오를 DB(meeting JSON)가 아니라 파일시스템에 저장한다(대용량 → BLOB 회피).
DB 에는 audioRef(메타: format/sizeBytes/durationSec?/createdAt)만 싣는다.

흐름(옵션B, 이중 전송 없음):
  1. 처리 시점에 오디오를 staging 으로 1회 업로드 → stagingToken 반환.
     파일: output/web/audio/_staging/{token}.<ext>
  2. 회의 확정(create_meeting)이 audioStagingToken 을 받으면 staging→meeting 으로 이동(rename).
     파일: output/web/audio/{meetingId}/source.<ext>, meeting.audioRef 기록.
  3. 회의 삭제 시 {meetingId}/ 디렉토리 동반 삭제(보존=회의 수명 동일).

보안/정합:
  - 저장 베이스는 store.DEFAULT_DB_PATH 와 동일 output 루트 기준(하드코딩 금지 →
    테스트가 DEFAULT_DB_PATH 패치만으로 임시경로 격리 가능). audio_base() 가 매 호출 시
    현재 DEFAULT_DB_PATH 를 읽으므로 reload 없이도 패치가 반영된다.
  - meetingId/token 은 호출부에서 정규식 화이트리스트로 검증한 값만 넘긴다(경로조립 traversal 차단).
  - 멀티파트 실패/중단 시 부분파일 정리(rollback) → audioRef 미기록(원자성은 호출부에서 보장).

무거운 의존성 없음: stdlib(pathlib/shutil/uuid/datetime) 만 사용.
"""
from __future__ import annotations

import datetime as _dt
import shutil
import uuid
from pathlib import Path

import src.web.store as _storemod

# 업로드 크기 상한(과대 거부). 기본 500MB. 회의 원본 오디오 1건 기준 넉넉.
MAX_AUDIO_BYTES = 500 * 1024 * 1024

# 허용 확장자(mime/파일명에서 추출). 알 수 없으면 .bin 으로 저장(경로조립 안전·재생은 메타로).
_ALLOWED_EXTS = {
    "webm", "wav", "mp3", "m4a", "mp4", "ogg", "oga", "flac", "aac", "opus", "3gp", "amr", "bin",
}

# mimeType → 확장자(프론트가 보내는 주요 포맷). 미지정/미상은 호출부에서 파일명·기본값 사용.
_MIME_EXT = {
    "audio/webm": "webm",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
    "audio/opus": "opus",
}


def audio_base() -> Path:
    """오디오 저장 루트(output/web/audio). store.DEFAULT_DB_PATH 와 동일 output 디렉토리 기준.

    매 호출 시 현재 DEFAULT_DB_PATH 를 읽으므로 테스트가 그 전역만 임시경로로 패치하면
    오디오도 같은 임시 디렉토리에 격리된다(하드코딩 금지)."""
    return Path(_storemod.DEFAULT_DB_PATH).parent / "audio"


def _staging_dir() -> Path:
    return audio_base() / "_staging"


def safe_ext(mime_type: str | None, filename: str | None) -> str:
    """확장자 결정(화이트리스트). mime → 파일명 → 기본 'bin' 순. 경로조립 안전한 소문자 영숫자만."""
    if mime_type:
        ext = _MIME_EXT.get(mime_type.split(";", 1)[0].strip().lower())
        if ext:
            return ext
    if filename and "." in filename:
        cand = filename.rsplit(".", 1)[-1].strip().lower()
        if cand.isalnum() and cand in _ALLOWED_EXTS:
            return cand
    return "bin"


def save_staging(data: bytes, *, mime_type: str | None, filename: str | None) -> tuple[str, str]:
    """staging 디렉토리에 1회 업로드 저장 → (stagingToken, ext). 실패 시 부분파일 정리(rollback).

    token 은 uuid4.hex(32 hex) — bind 시 호출부가 형식 검증한다. 파일은 {token}.{ext}.
    """
    token = uuid.uuid4().hex
    ext = safe_ext(mime_type, filename)
    sdir = _staging_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / f"{token}.{ext}"
    try:
        path.write_bytes(data)
    except Exception:
        # 부분파일이 남았으면 정리(rollback) 후 재전파.
        path.unlink(missing_ok=True)
        raise
    return token, ext


def _staging_path(token: str) -> Path | None:
    """token 에 해당하는 staging 파일 경로(확장자 무관 첫 매치). 없으면 None."""
    sdir = _staging_dir()
    if not sdir.is_dir():
        return None
    for p in sdir.glob(f"{token}.*"):
        if p.is_file():
            return p
    return None


def bind_staging(token: str, meeting_id: str) -> dict | None:
    """staging 파일을 {meetingId}/source.<ext> 로 이동(rename) + audioRef 메타 반환. 없으면 None.

    호출부(create_meeting)는 token(^[0-9a-f]{32}$)·meeting_id(^[0-9a-f]{32}$) 형식을 검증한
    값만 넘긴다(경로조립 traversal 차단). 동일 디렉토리 내 rename → 원자적 이동.
    audioRef: {format, sizeBytes, createdAt}. (durationSec 은 디코딩 비용상 v2 — 키 미포함.)
    """
    src = _staging_path(token)
    if src is None:
        return None
    ext = src.suffix.lstrip(".") or "bin"
    mdir = audio_base() / meeting_id
    mdir.mkdir(parents=True, exist_ok=True)
    dst = mdir / f"source.{ext}"
    size = src.stat().st_size
    shutil.move(str(src), str(dst))
    return {
        "format": ext,
        "sizeBytes": size,
        "createdAt": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="microseconds"),
    }


def meeting_audio_path(meeting_id: str, audio_ref: dict | None) -> Path | None:
    """meeting 의 오디오 파일 경로. audioRef 없거나 파일 부재면 None.

    호출부가 meeting_id(^[0-9a-f]{32}$)를 검증한 뒤에만 호출한다(경로조립 안전)."""
    if not audio_ref:
        return None
    ext = (audio_ref.get("format") or "bin").strip().lower()
    if not ext.isalnum() or ext not in _ALLOWED_EXTS:
        return None
    path = audio_base() / meeting_id / f"source.{ext}"
    return path if path.is_file() else None


def delete_meeting_audio(meeting_id: str) -> bool:
    """{meetingId}/ 오디오 디렉토리 동반 삭제(회의 삭제 시). 있었으면 True.

    호출부가 meeting_id(^[0-9a-f]{32}$)를 검증한 뒤에만 호출한다."""
    mdir = audio_base() / meeting_id
    if mdir.is_dir():
        shutil.rmtree(mdir, ignore_errors=True)
        return True
    return False


def cleanup_staging(max_age_seconds: float) -> int:
    """미bind staging 파일 중 max_age_seconds 초과 경과분 삭제 → 삭제 개수.

    finalize 되지 않은 staging(업로드만 하고 회의 확정 안 함)이 누적되지 않게 정리한다.
    Follow-up: 실제 스케줄(주기 실행)은 미구현 — 부팅/크론 훅에서 호출하도록 후속 작업.
    """
    sdir = _staging_dir()
    if not sdir.is_dir():
        return 0
    now = _dt.datetime.now(_dt.timezone.utc).timestamp()
    removed = 0
    for p in sdir.iterdir():
        if not p.is_file():
            continue
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        if age > max_age_seconds:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed
