"""FastAPI 서비스 — meetscript-ai 프론트엔드의 온프렘 백엔드.

프론트 계약(`/api/ai/*`) + 영속(`/api/meetings*`, SQLite)을 노출한다. Gemini/Firebase 대체.

STT는 장시간이라 `/api/ai/process`는 **비동기**다: meeting을 status=processing으로 만들고
백그라운드 스레드에서 STT+정제를 돌린 뒤 status=review로 갱신한다. 프론트는 GET 폴링.

컨테이너 대비: 빌드된 프론트(`dist/`)가 있으면 같은 앱이 정적 서빙도 한다(단일 컨테이너).
dev에서는 dist가 없어 Vite가 프론트를 서빙하고 이 앱은 /api 만 담당한다.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import os
import re
import threading
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel

from src.web import (
    audio_store,
    google_calendar,
    google_docs,
    google_drive,
    google_gmail,
    google_oauth,
    maintenance,
    meeting_doc,
    observability,
)

from src.postprocess.backends.agent_cli import (
    AgentCLIAuthError,
    AgentCLICancelled,
    claude_auth_status,
    use_cancel_event,
    use_credential,
)
from src.postprocess.web_contract import (
    SummaryStructureError,
    TranscriptStructureError,
    ensure_action_item_ids,
    merge_preserve_edited,
    validate_summary_edit,
    validate_transcript_edit,
)
from src.web.service import (
    _summary_action_hints,
    enrich_to_contract,
    extract_action_items,
    summarize_meeting,
    transcribe_to_segments,
)
from src.web.store import (
    MeetingStore,
    NoticeStore,
    PreconditionFailedError,
    RequirementStore,
)
# service import 가 config(load_dotenv)를 끌어와 .env 가 로드된 뒤 auth 를 가져온다(순서 주의).
from src.web import auth

# 정제 백엔드(plan D1=passthrough). 환경변수로 클라우드(agent_cli)·로컬 LLM(ollama 등) 교체 가능.
CLEAN_BACKEND = os.environ.get("WEB_CLEAN_BACKEND", "passthrough")
# 추출 백엔드. 정제와 분리(미지정 시 정제 백엔드를 따름). 추출은 회의당 1콜이라 클라우드도
# 저비용 → "정제=passthrough, 추출=agent_cli" 구성 가능(WEB_EXTRACT_BACKEND=agent_cli).
EXTRACT_BACKEND = os.environ.get("WEB_EXTRACT_BACKEND", CLEAN_BACKEND)
# 요약 백엔드. 미지정 시 off(passthrough → 빈 요약). 회의당 1콜이라 클라우드도 저비용 →
# "정제=passthrough, 요약=agent_cli"만 켜기 가능(WEB_SUMMARIZE_BACKEND=agent_cli). 설계 §6 폴백.
SUMMARIZE_BACKEND = os.environ.get("WEB_SUMMARIZE_BACKEND", "")
# 정제·추출·요약 중 하나라도 agent_cli(=claude 서브프로세스 인증)면 자격증명 헬스체크가 의미 있다.
USES_AGENT_CLI = "agent_cli" in (CLEAN_BACKEND, EXTRACT_BACKEND, SUMMARIZE_BACKEND)
# 빌드된 프론트 정적 경로(컨테이너). 없으면 정적 서빙 비활성(dev=Vite).
FRONTEND_DIST = os.environ.get("WEB_FRONTEND_DIST", "")

# ---------- 정리 배치 스케줄(P10, NFR-저장공간) ----------
# 마스터 스위치(기본 ON). 정리 주기·보존 기간은 env 로 조정(미지정 시 maintenance 기본값).
CLEANUP_ENABLED = os.environ.get("WEB_CLEANUP_ENABLED", "1") != "0"
CLEANUP_INTERVAL_SEC = float(
    os.environ.get("WEB_CLEANUP_INTERVAL_SEC", str(maintenance.DEFAULT_CLEANUP_INTERVAL))
)
STAGING_MAX_AGE_SEC = float(
    os.environ.get("WEB_STAGING_MAX_AGE_SEC", str(maintenance.DEFAULT_STAGING_MAX_AGE))
)
BACKUP_MAX_AGE_SEC = float(
    os.environ.get("WEB_BACKUP_MAX_AGE_SEC", str(maintenance.DEFAULT_BACKUP_MAX_AGE))
)

# DB 스냅샷 백업(무백업 prune 사고 재발 방지). 기본 ON·1일 주기·최근 7개 보존.
DB_BACKUP_ENABLED = os.environ.get("WEB_DB_BACKUP_ENABLED", "1") != "0"
DB_BACKUP_INTERVAL_SEC = float(
    os.environ.get("WEB_DB_BACKUP_INTERVAL_SEC", str(maintenance.DEFAULT_DB_BACKUP_INTERVAL))
)
DB_BACKUP_KEEP = int(os.environ.get("WEB_DB_BACKUP_KEEP", str(maintenance.DEFAULT_DB_BACKUP_KEEP)))

# ---------- claude 자격증명 헬스체크(요약/추출 백엔드 인증 만료 사전 감지) ----------
# 요약/추출이 agent_cli 일 때만 의미가 있다. oauth_token(구독 토큰)은 비번 변경·폐기로
# 조용히 만료되므로, 주기적으로 사용자별 자격증명을 실제 ping 해 유효성을 캐시한다. api_key 는
# 만료 개념이 없어 호출 없이 valid 처리, 복호 실패(CRED_ENC_KEY 손상) 행은 별도로 표시한다.
CRED_HEALTH_CHECK_ENABLED = os.environ.get("WEB_CRED_HEALTH_CHECK_ENABLED", "1") != "0"
# 기본 6시간. 구독 토큰 만료 감지 목적이라 촘촘할 필요가 없다(비용/부하 절약).
CRED_HEALTH_INTERVAL_SEC = float(os.environ.get("WEB_CRED_HEALTH_INTERVAL_SEC", "21600"))
# 부팅 직후 즉시 훑지 않고 잠깐 늦춘다(기동 트래픽·모델 로드와 겹치지 않게). 기본 60초.
CRED_HEALTH_INITIAL_DELAY_SEC = float(os.environ.get("WEB_CRED_HEALTH_INITIAL_DELAY_SEC", "60"))
# 사용자 간 ping 간 최소 간격(초) — 순차 실행이지만 폭주 방지용 소량 텀. 기본 0.5초.
CRED_HEALTH_PER_USER_DELAY_SEC = float(os.environ.get("WEB_CRED_HEALTH_PER_USER_DELAY_SEC", "0.5"))

# ---------- Google Drive 회의록 동기화(사용자별 OAuth) ----------
# 콜백 state(신원+CSRF) 토큰 TTL(초, 기본 10분) — 동의 왕복은 짧다.
GOOGLE_STATE_TTL = int(os.environ.get("WEB_GOOGLE_STATE_TTL", "600"))
# 콜백 완료 후 프론트로 302 리다이렉트할 오리진. 미설정이면 상대경로(/settings)로 이동(동일출처).
FRONTEND_ORIGIN = os.environ.get("WEB_FRONTEND_ORIGIN", "").rstrip("/")
# Docs 변환 import 한도(~10MB) 방어 — transcript 세그먼트 상한(기본 무제한=None).
_max_seg_env = os.environ.get("WEB_DRIVE_MAX_TRANSCRIPT_SEGMENTS", "").strip()
DRIVE_MAX_TRANSCRIPT_SEGMENTS = int(_max_seg_env) if _max_seg_env.isdigit() else None
# 회의 삭제 시 드라이브 파일 동반 삭제 여부(기본 0=유지 — 사용자 본인 드라이브 자산 보존).
DRIVE_DELETE_ON_MEETING_DELETE = os.environ.get("WEB_DRIVE_DELETE_ON_MEETING_DELETE", "0") == "1"
# 캘린더 양방향 연동: 대상 캘린더(기본 primary=본인 기본 캘린더). 일정 읽기 기본 조회 창(일).
DEFAULT_CALENDAR_ID = os.environ.get("WEB_CALENDAR_ID", "primary")
CALENDAR_WINDOW_DAYS = int(os.environ.get("WEB_CALENDAR_WINDOW_DAYS", "30"))
# 앱 회의를 캘린더 이벤트로 쓸 때 시간대(dateTime 에 오프셋 없을 때 Google 에 넘길 timeZone).
CALENDAR_TIMEZONE = os.environ.get("WEB_CALENDAR_TIMEZONE", "Asia/Seoul")
# 회의 삭제 시 캘린더 이벤트 동반 삭제 여부(기본 0=유지).
CALENDAR_DELETE_ON_MEETING_DELETE = os.environ.get("WEB_CALENDAR_DELETE_ON_MEETING_DELETE", "0") == "1"


async def _cleanup_loop() -> None:
    """부팅 직후 1회 + 이후 CLEANUP_INTERVAL_SEC 주기로 정리 배치 실행.

    두 가지를 돈다: (1) 디스크 정리(staging/backup) — CLEANUP_ENABLED 일 때만. (2) 종료된 AI 잡의
    인메모리 GC — 디스크 정리 스위치와 무관하게 항상 실행한다(WEB_CLEANUP_ENABLED=0 로 디스크 정리만
    꺼도 잡 누적은 계속 방지). 블로킹 작업(디스크 IO·락 보유 스캔)은 asyncio.to_thread 로 워커스레드에
    위임해 이벤트 루프를 막지 않는다. 개별 사이클 예외는 로깅 후 삼켜 루프를 지킨다.
    """
    while True:
        if CLEANUP_ENABLED:
            try:
                await asyncio.to_thread(
                    maintenance.run_cleanup_once,
                    store,
                    staging_max_age=STAGING_MAX_AGE_SEC,
                    backup_max_age=BACKUP_MAX_AGE_SEC,
                )
            except Exception:  # noqa: BLE001 — 한 사이클 실패가 스케줄러를 죽이지 않게 격리
                traceback.print_exc()
        try:
            # 종료된 AI 잡 인메모리 항목 GC — _jobs_lock 보유 스캔이라 to_thread 로 이벤트 루프 비점유.
            purged = await asyncio.to_thread(_purge_finished_jobs)
            if purged:
                observability.audit("ai_job.purge", removed=purged)
        except Exception:  # noqa: BLE001 — 정리 실패가 스케줄러를 죽이지 않게 격리
            traceback.print_exc()
        await asyncio.sleep(CLEANUP_INTERVAL_SEC)


async def _db_backup_loop() -> None:
    """부팅 직후 1회 + 이후 DB_BACKUP_INTERVAL_SEC 주기로 DB 스냅샷 백업(무백업 사고 재발 방지).

    블로킹 sqlite backup API 는 asyncio.to_thread 로 워커스레드에 위임한다(store._lock 으로 백업 중
    동시 쓰기 차단·일관 스냅샷 보장). 사이클 예외는 로깅 후 삼켜 루프가 죽지 않게 한다.
    """
    while True:
        try:
            await asyncio.to_thread(maintenance.run_db_backup, store, keep=DB_BACKUP_KEEP)
        except Exception:  # noqa: BLE001 — 한 사이클 실패가 스케줄러를 죽이지 않게 격리
            traceback.print_exc()
        await asyncio.sleep(DB_BACKUP_INTERVAL_SEC)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 수명 — startup 시 정리·DB백업 스케줄러 기동, shutdown 시 취소.

    테스트(MEETSCRIPT_BLOCK_DEFAULT_DB=1)·비활성(WEB_CLEANUP_ENABLED/WEB_DB_BACKUP_ENABLED=0)에서는
    해당 스케줄러를 띄우지 않는다 → 테스트는 maintenance.run_cleanup_once/run_db_backup 을 직접
    호출해 로직을 단언한다(실 DB·스케줄러 미접촉).
    """
    in_test = os.environ.get("MEETSCRIPT_BLOCK_DEFAULT_DB") == "1"
    tasks: list[asyncio.Task] = []
    # _cleanup_loop 은 (CLEANUP_ENABLED 시)디스크 정리 + (항상)종료 잡 인메모리 GC 를 돈다 →
    # 디스크 정리를 꺼도(WEB_CLEANUP_ENABLED=0) 잡 GC 를 위해 루프는 띄운다(테스트 제외).
    if not in_test:
        tasks.append(asyncio.create_task(_cleanup_loop()))
        observability.audit(
            "scheduler.start", kind="cleanup", interval=CLEANUP_INTERVAL_SEC, disk_cleanup=CLEANUP_ENABLED
        )
    if not in_test and DB_BACKUP_ENABLED:
        tasks.append(asyncio.create_task(_db_backup_loop()))
        observability.audit("scheduler.start", kind="db_backup", interval=DB_BACKUP_INTERVAL_SEC)
    # 자격증명 헬스 스윕 — 요약/추출이 agent_cli 일 때만(그 외엔 인증 자체가 무의미).
    if not in_test and CRED_HEALTH_CHECK_ENABLED and USES_AGENT_CLI:
        tasks.append(asyncio.create_task(_claude_cred_health_loop()))
        observability.audit("scheduler.start", kind="cred_health", interval=CRED_HEALTH_INTERVAL_SEC)
    # STT 스톨 워치독 — 멈춘 전사 잡을 탐지해 프론트/관리자에 보고(슬롯 무한 점유 조기 관측).
    if not in_test:
        tasks.append(asyncio.create_task(_stt_stall_watchdog_loop()))
        observability.audit("scheduler.start", kind="stt_stall", interval=STT_STALL_SCAN_SEC)
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t


app = FastAPI(title="meetscript-ai on-prem backend", version="1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # dev: Vite(:3000)에서 호출. 컨테이너 동일출처면 무관.
    allow_methods=["*"],
    allow_headers=["*"],
)

store = MeetingStore()
req_store = RequirementStore()  # Slack 봇 요구사항 적재(같은 DB, 독립 연결)
notice_store = NoticeStore()  # 공지사항(웹 콘솔 작성 → Slack 봇 배포, 같은 DB)
users = auth.init()  # users 테이블 준비 + WEB_AUTH_USERS 시드/동기화


def _now_iso() -> str:
    """create/update 타임스탬프. UTC·마이크로초로 store 의 ETag 포맷과 통일(M1).

    create 직후 동일 마이크로초에 PATCH 가 와도 store._next_etag 가 단조 증가를 보장하므로,
    여기서는 마이크로초 정밀도만 맞춰 두면 충분하다(초 단위 → 마이크로초 통일)."""
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


# KST(고정 +9, DST 없음) — Drive 하위 폴더 이름의 날짜/시간 스탬프용.
_KST = dt.timezone(dt.timedelta(hours=9))


def _kst_stamp(iso: str) -> str:
    """ISO 타임스탬프(UTC) → KST `YYYY-MM-DD_HHMM`. 파싱 실패 시 빈 문자열."""
    try:
        d = dt.datetime.fromisoformat(iso.strip())
    except (ValueError, AttributeError):
        return ""
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(_KST).strftime("%Y-%m-%d_%H%M")


def _sanitize_drive_name(name: str) -> str:
    """Drive 폴더/파일 이름으로 안전하게 정리. 경로 구분자·제어문자 제거, 공백 정규화, 길이 제한."""
    cleaned = "".join(("-" if c in "/\\" else c) for c in name if ord(c) >= 32)
    cleaned = " ".join(cleaned.split()).strip()
    return cleaned[:100] or "회의록"


def _drive_subfolder_name(m: dict) -> str:
    """회의별 Drive 하위 폴더 이름 `{회의명}_{YYYY-MM-DD}_{HHMM}`(KST). createdAt 기준으로 안정적."""
    title = _sanitize_drive_name(str(m.get("title") or "").strip() or "회의록")
    stamp = _kst_stamp(str(m.get("createdAt") or ""))
    return f"{title}_{stamp}" if stamp else title


# If-Match `*`(존재하면 일치) 표식. 비교를 생략하되, 락 안에서 "존재" 자체는 보장된다.
_IF_MATCH_ANY = object()


def _parse_if_match(if_match: str | None) -> object | str | None:
    """If-Match 헤더 파싱(M4, RFC 7232 견고화).

    반환:
      - None            : 헤더 없음 → 비교 생략(last-write-wins, 후방호환).
      - _IF_MATCH_ANY   : `*` → "리소스가 존재하면 일치"(값 비교 생략). 호출부는 존재만 요구.
      - str             : 비교할 강(strong) ETag 값(따옴표·W/ 접두 제거).

    규칙:
      - 약(weak) ETag 접두 `W/` 는 제거하고 값만 사용(편집 동시성엔 강·약 구분 불필요).
      - 다중값(콤마 구분)은 첫 유효 토큰을 사용한다.
      - 양끝 큰따옴표는 제거한다.
      - 빈 문자열·따옴표만 등 파싱 불가 입력은 400(클라이언트 오류로 명시)으로 거부한다.
    """
    if if_match is None:
        return None
    raw = if_match.strip()
    if raw == "*":
        return _IF_MATCH_ANY
    # 다중값 중 첫 토큰만 사용(편집은 단일 리소스 대상).
    first = raw.split(",", 1)[0].strip()
    if first.startswith("W/"):  # 약 ETag 접두 제거
        first = first[2:].strip()
    value = first.strip('"')
    if not value:
        raise HTTPException(status_code=400, detail="If-Match 헤더 형식이 올바르지 않습니다.")
    return value


# meetingId/stagingToken 화이트리스트(경로조립 traversal 차단). uuid4.hex == 32 소문자 hex.
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")


def _require_hex32(value: str, *, what: str) -> str:
    """meetingId/token 이 ^[0-9a-f]{32}$ 인지 검증(경로조립 전 화이트리스트). 위반 시 400."""
    if not isinstance(value, str) or not _HEX32_RE.match(value):
        raise HTTPException(status_code=400, detail=f"{what} 형식이 올바르지 않습니다.")
    return value


def _fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "0:00"
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


# ---------- 요청 스키마 ----------
class ProcessRequest(BaseModel):
    audioBase64: str
    mimeType: str | None = "audio/webm"
    participants: list[dict] = []
    promptTemplate: str | None = None
    title: str | None = None


class ExtractRequest(BaseModel):
    text: str


class LoginRequest(BaseModel):
    username: str
    password: str


class CredentialRequest(BaseModel):
    cred_type: str  # "api_key" | "oauth_token"
    secret: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ProfileRequest(BaseModel):
    """본인 표시명 self-edit. 셋 다 선택 — 보낸 필드만 갱신. username/role/password 불가."""
    displayName: str | None = None
    englishName: str | None = None
    jobTitle: str | None = None


class AdminUserUpdateRequest(BaseModel):
    """관리자 사용자 정보 수정. 모두 선택 — 보낸 필드만 갱신."""
    displayName: str | None = None
    englishName: str | None = None
    jobTitle: str | None = None
    role: str | None = None
    email: str | None = None


class AdminResetPasswordRequest(BaseModel):
    """관리자 비번 초기화. newPassword 필수(초기화 후 must_change_password=1)."""
    newPassword: str


class AdminUserCreateRequest(BaseModel):
    """관리자 신규 사용자 생성. username 필수, role 기본 'user'. 초기 비번은 서버 기본값."""
    username: str
    displayName: str | None = None
    role: str = "user"


class RequirementCreateRequest(BaseModel):
    """Slack 봇/웹 요구사항 적재. text 필수, source/reporter 선택."""
    text: str
    source: str | None = None
    reporter: str | None = None


class NoticeCreateRequest(BaseModel):
    """공지 작성(관리자 콘솔). body 필수, title 선택."""
    body: str
    title: str | None = None


class NoticeUpdateRequest(BaseModel):
    """공지 수정 — 지정 필드만 갱신(모두 선택). active 로 노출/숨김 토글."""
    body: str | None = None
    title: str | None = None
    active: bool | None = None


class GoogleOAuthConfigRequest(BaseModel):
    """관리자 앱 OAuth 클라이언트 설정(client_id/secret/redirect_uri). 셋 다 필수."""
    clientId: str
    clientSecret: str
    redirectUri: str


class DocTemplateRequest(BaseModel):
    """관리자 전역 Docs 양식(템플릿) 설정 — 구글 문서 URL 또는 문서 id."""
    templateUrl: str


# 셀프 비번 변경 시 새 비밀번호 최소 길이.
MIN_PASSWORD_LEN = 8

# 관리자 신규 생성 계정의 초기 비밀번호(첫 로그인 시 강제 변경). env 로 교체 가능.
# 공용 기본 비번은 강제 변경 게이트(must_change_password=1)로 위험을 완화한다.
NEW_USER_INITIAL_PASSWORD = os.environ.get("WEB_NEW_USER_INITIAL_PASSWORD", "litbig1234")

# 표시명 필드 최대 길이.
MAX_NAME_LEN = 64


# ---------- 인증 (프론트 src/lib/firebase.ts 계약) ----------
@app.post("/api/auth/login")
def auth_login(req: LoginRequest) -> dict:
    """ID/PW 검증 → {token, user}. 실패는 401(+detail) — 프론트 로그인 폼이 detail 표시."""
    user = users.verify(req.username.strip(), req.password)
    if user is None:
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 올바르지 않습니다.")
    return {"token": auth.make_token(user["id"]), "user": user}


@app.get("/api/auth/me")
def auth_me(user: dict = Depends(auth.require_user)) -> dict:
    """Bearer 토큰으로 세션 복원. 무효/만료 토큰은 require_user 가 401."""
    return user


@app.post("/api/auth/change-password")
def change_password(
    req: ChangePasswordRequest, user: dict = Depends(auth.require_user)
) -> dict:
    """관리자에게 부여받은 비밀번호를 본인이 변경. 현재 비번 검증 후 새 비번으로 교체.

    기존 토큰은 username 기반이라 변경 후에도 유효(재로그인 불필요). 부팅 시드는 seed_user 라
    변경된 비번이 재기동에 보존된다(auth.py 참조).
    """
    username = user["username"]
    if users.verify(username, req.current_password) is None:
        raise HTTPException(status_code=400, detail="현재 비밀번호가 올바르지 않습니다.")
    new_pw = req.new_password or ""
    if len(new_pw) < MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=400, detail=f"새 비밀번호는 {MIN_PASSWORD_LEN}자 이상이어야 합니다."
        )
    if new_pw == req.current_password:
        raise HTTPException(status_code=400, detail="새 비밀번호가 현재 비밀번호와 같습니다.")
    if not users.set_password(username, new_pw):
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
    return {"ok": True, "detail": "비밀번호가 변경되었습니다."}


def _clean_name(value: str, *, field: str, required: bool) -> str:
    """표시명 검증·정규화. trim 후 제어문자/줄바꿈 금지, 길이 제한. 위반 시 422.

    required=True(displayName): trim 후 1..MAX_NAME_LEN(빈값 거부).
    required=False(englishName/jobTitle): 0..MAX_NAME_LEN(빈 허용).
    """
    v = value.strip()
    # 길이 상한을 먼저 검사 — 긴 입력을 제어문자 전수 스캔하기 전에 빠르게 거부.
    if len(v) > MAX_NAME_LEN:
        raise HTTPException(status_code=422, detail=f"{field}: 최대 {MAX_NAME_LEN}자입니다.")
    if required and not v:
        raise HTTPException(status_code=422, detail=f"{field}: 빈 값은 저장할 수 없습니다.")
    # 제어문자(줄바꿈/탭 포함) 금지 — 표시명에 부적합.
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
        raise HTTPException(status_code=422, detail=f"{field}: 제어문자/줄바꿈은 사용할 수 없습니다.")
    return v


def _clean_email(value: str) -> str | None:
    """이메일 간단 검증·정규화. trim 후 빈 문자열이면 None(미설정), 값 있으면 '@' 포함 요구.
    길이 상한은 표시명과 동일(MAX_NAME_LEN). 위반 시 422."""
    v = value.strip()
    if not v:
        return None  # 빈 문자열 → 미설정(None 저장)
    if len(v) > MAX_NAME_LEN:
        raise HTTPException(status_code=422, detail=f"email: 최대 {MAX_NAME_LEN}자입니다.")
    if "@" not in v:
        raise HTTPException(status_code=422, detail="email: 올바른 이메일 형식이 아닙니다.")
    return v


@app.patch("/api/settings/profile")
def patch_profile(req: ProfileRequest, user: dict = Depends(auth.require_user)) -> dict:
    """본인 표시명(display/english/job) self-edit → 공개 user(갱신본). 보낸 필드만 갱신.

    인증은 요구하되 must_change_password 게이트는 우회한다(change-password 와 동급) — 초기
    비번 변경 전에도 이름 정정이 가능해야 한다(FR-A7). username/role/password 는 변경 불가.
    갱신 시 name_source='user' 로 표시해 seed 재실행이 display_name 을 덮어쓰지 않게 한다.
    """
    if req.displayName is None and req.englishName is None and req.jobTitle is None:
        raise HTTPException(status_code=422, detail="변경할 필드가 없습니다.")
    fields: dict[str, str] = {}
    if req.displayName is not None:
        fields["display_name"] = _clean_name(req.displayName, field="displayName", required=True)
    if req.englishName is not None:
        fields["english_name"] = _clean_name(req.englishName, field="englishName", required=False)
    if req.jobTitle is not None:
        fields["job_title"] = _clean_name(req.jobTitle, field="jobTitle", required=False)
    updated = auth.update_profile(user["username"], **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
    observability.audit("profile.edit", user=user["username"], fields=",".join(fields))
    return updated


def require_user_active(user: dict = Depends(auth.require_user)) -> dict:
    """비번 강제변경 대상(mustChangePassword)이면 403 — 데이터/AI/설정 엔드포인트 차단.

    로그인·/me·비번변경·health 는 열어둬 '잠김'을 막는다: 토큰은 유효하므로 로그인 상태를
    유지한 채 비번만 한 번 바꾸면(set_password→플래그 해제) 즉시 모든 기능이 풀린다. 프론트는
    응답의 error_code='must_change_password' 를 보고 강제 변경 화면으로 보낸다.
    """
    if user.get("mustChangePassword"):
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "must_change_password",
                "message": "초기 비밀번호를 먼저 변경해야 합니다.",
            },
        )
    return user


def require_admin(user: dict = Depends(require_user_active)) -> dict:
    """관리자(role=admin) 전용 게이트 — 운영 메트릭 등 민감 조회용. 그 외 403."""
    if (user.get("role") or "user") != "admin":
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    return user


# ---------- 사용자별 claude 자격증명 설정 ----------
def _verify_credential(credential: dict) -> dict:
    """저장한 자격증명으로 claude 가벼운 "ping" 1콜 → {"ok": bool, "detail": str}.

    use_credential 로 자격증명을 주입한 채 agent_cli 백엔드로 짧은 호출을 돌려 실제 인증
    유효성을 확인한다. 실패해도 예외를 던지지 않고 ok=False 로 돌려준다(저장은 유지).
    secret 은 어떤 detail/로그에도 싣지 않는다.
    """
    from src.postprocess.backends import get_llm_backend

    backend = get_llm_backend("agent_cli")
    messages = [
        {"role": "system", "content": "Reply with the single word: pong."},
        {"role": "user", "content": "ping"},
    ]
    try:
        with use_credential(credential):
            out = backend.generate(messages, max_tokens=16)
    except AgentCLIAuthError:
        # 예외 메시지(stderr 조각 등)에 토큰이 반사될 여지를 원천 차단 — 고정 사유만 노출.
        return {"ok": False, "detail": "인증 실패 — 토큰이 만료되었거나 무효합니다."}
    except Exception as e:  # noqa: BLE001
        # 메시지 본문({e})은 싣지 않는다(secret 반사 방지). 유형명만 진단용으로 남긴다.
        return {"ok": False, "detail": f"검증 호출 실패: {type(e).__name__}"}
    if not (out or "").strip():
        return {"ok": False, "detail": "빈 응답(인증/모델 응답 확인 필요)"}
    return {"ok": True, "detail": "검증 호출 성공"}


# ---------- claude 자격증명 헬스(만료/폐기 사전 감지) 캐시 ----------
# username → {type, valid(bool|None), reason, detail(secret 없음), checked_at(iso)}.
# 배경 스윕과 온디맨드 verify 가 갱신한다. 프로세스 재기동 시 비고, 다음 스윕이 복원.
_claude_cred_health: dict[str, dict] = {}
_cred_health_lock = threading.Lock()
_cred_health_meta: dict[str, str | None] = {"last_sweep_at": None}
# 온디맨드 verify 남용 방지 — 사용자별 최근 실제 검증(ping) 시각(monotonic). 쿨다운 내 재호출은
# 캐시를 돌려주고 실제 ping 은 _llm_semaphore 하에서만 수행(무제한 서브프로세스 생성 차단).
_cred_verify_last: dict[str, float] = {}
CRED_VERIFY_COOLDOWN_SEC = float(os.environ.get("WEB_CRED_VERIFY_COOLDOWN_SEC", "15"))


def _credential_owner_type(username: str) -> str | None:
    """자격증명 행이 있으면 종류(api_key|oauth_token), 없으면 None. 복호 미수행(행 존재만)."""
    for o in auth.list_credential_owners():
        if o["username"] == username:
            return o["type"]
    return None


def _evaluate_credential_health(username: str) -> dict:
    """사용자 자격증명의 실제 유효성을 판정하고 캐시를 갱신 → health dict 반환.

    상태 구분(운영자가 '만료'와 '키 손상'과 '미설정'을 구별할 수 있게):
      - not_configured : 자격증명 행 없음(valid=None).
      - decrypt_failed : 행은 있으나 복호 실패(CRED_ENC_KEY 불일치 → 재등록 필요, valid=False).
      - api_key        : 만료 개념 없음 → 호출 없이 valid=True.
      - oauth_token    : 실제 claude ping 1콜로 판정(valid=ok, reason=ok|verify_failed).
    secret 은 어떤 필드/로그에도 싣지 않는다.
    """
    now_iso = dt.datetime.now().isoformat(timespec="seconds")
    owner_type = _credential_owner_type(username)
    if owner_type is None:
        with _cred_health_lock:
            _claude_cred_health.pop(username, None)  # 미설정은 캐시에 남기지 않음
        return {
            "type": None, "valid": None, "reason": "not_configured",
            "detail": "자격증명 미설정", "checked_at": now_iso,
        }
    cred = auth.get_credential(username)
    if cred is None:
        # 복호 실패 vs 동시 삭제(TOCTOU) 구분: owner_type 관측 직후 사용자가 clear 했다면 행이
        # 사라져 None 이 된다 → 행이 실제로 없으면 '미설정'으로, 여전히 있으면 진짜 '복호 실패'로.
        if _credential_owner_type(username) is None:
            with _cred_health_lock:
                _claude_cred_health.pop(username, None)
            return {
                "type": None, "valid": None, "reason": "not_configured",
                "detail": "자격증명 미설정", "checked_at": now_iso,
            }
        health = {
            "type": owner_type, "valid": False, "reason": "decrypt_failed",
            "detail": "저장된 자격증명을 복호화하지 못했습니다(CRED_ENC_KEY 불일치 가능) — 재등록이 필요합니다.",
            "checked_at": now_iso,
        }
    elif cred["type"] == "api_key":
        health = {
            "type": "api_key", "valid": True, "reason": "api_key",
            "detail": "API 키는 만료 개념이 없습니다.", "checked_at": now_iso,
        }
    else:  # oauth_token — 실제 호출로만 만료/폐기를 알 수 있다
        v = _verify_credential(cred)
        health = {
            "type": "oauth_token", "valid": bool(v.get("ok")),
            "reason": "ok" if v.get("ok") else "verify_failed",
            "detail": str(v.get("detail", "")), "checked_at": now_iso,
        }
    with _cred_health_lock:
        _claude_cred_health[username] = health
    return health


def _cache_cred_health_from_verification(username: str, cred_type: str, verification: dict) -> None:
    """PUT(재등록) 직후의 verification 결과로 헬스 캐시를 즉시 갱신 — 재인증 후에도 옛 헬스가
    남아 '만료'로 오표시되는 문제를 막는다. verification 은 이미 실제 ping 한 결과라 재-ping 없음."""
    ok = bool(verification.get("ok"))
    reason = "api_key" if (cred_type == "api_key" and ok) else ("ok" if ok else "verify_failed")
    with _cred_health_lock:
        _claude_cred_health[username] = {
            "type": cred_type, "valid": ok, "reason": reason,
            "detail": str(verification.get("detail", "")),
            "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
        }


def _mark_cred_health_invalid(username: str | None) -> None:
    """요약/추출 잡이 claude 인증 실패로 종료됐을 때, 해당 사용자 헬스 캐시를 즉시 만료로 내린다.
    배경 스윕(최대 CRED_HEALTH_INTERVAL_SEC) 전이라도 잡 실패라는 실시간 신호로 설정 배지가
    '재인증 필요'를 반영하게 한다(폐기된 자격증명이 valid 로 계속 표시되는 창을 닫는다)."""
    if not username:
        return
    with _cred_health_lock:
        cur = _claude_cred_health.get(username)
        _claude_cred_health[username] = {
            "type": cur.get("type") if cur else None,
            "valid": False, "reason": "verify_failed",
            "detail": "요약/추출 잡이 claude 인증 실패로 종료되었습니다 — 재인증이 필요합니다.",
            "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
        }


def _sweep_credential_health() -> dict:
    """자격증명 보유 사용자 전체를 순차 판정(블로킹). to_thread 로 워커스레드에서 실행.

    api_key 는 호출 없이 통과, oauth_token 만 실제 ping 한다. 사용자 간 소량 텀으로 폭주 방지.
    """
    owners = auth.list_credential_owners()
    checked = 0
    for o in owners:
        try:
            _evaluate_credential_health(o["username"])
            checked += 1
        except Exception:  # noqa: BLE001 — 한 사용자 실패가 스윕 전체를 죽이지 않게 격리
            traceback.print_exc()
        if CRED_HEALTH_PER_USER_DELAY_SEC > 0:
            time.sleep(CRED_HEALTH_PER_USER_DELAY_SEC)
    _cred_health_meta["last_sweep_at"] = dt.datetime.now().isoformat(timespec="seconds")
    return {"owners": len(owners), "checked": checked}


async def _claude_cred_health_loop() -> None:
    """부팅 후 INITIAL_DELAY → 이후 INTERVAL 주기로 자격증명 헬스 스윕.

    블로킹 ping(_sweep_credential_health)은 asyncio.to_thread 로 이벤트 루프 비점유. 개별 사이클
    예외는 로깅 후 삼켜 루프를 지킨다. agent_cli 미사용이면 lifespan 에서 애초에 띄우지 않는다.
    """
    await asyncio.sleep(CRED_HEALTH_INITIAL_DELAY_SEC)
    while True:
        try:
            summary = await asyncio.to_thread(_sweep_credential_health)
            observability.audit("cred_health.sweep", **summary)
        except Exception:  # noqa: BLE001 — 스윕 실패가 스케줄러를 죽이지 않게 격리
            traceback.print_exc()
        await asyncio.sleep(CRED_HEALTH_INTERVAL_SEC)


@app.get("/api/settings/claude-credential")
def get_claude_credential(user: dict = Depends(require_user_active)) -> dict:
    """현재 사용자 자격증명 상태(secret 비노출) + 최근 캐시 헬스.

    반환: {configured, type, updated_at, health}. health 는 마지막 스윕/verify 의 캐시값
    (없으면 None) — 실시간 재검증은 GET .../claude-credential/verify.
    """
    status = auth.credential_status(user["username"])
    with _cred_health_lock:
        status["health"] = _claude_cred_health.get(user["username"])
    return status


@app.get("/api/settings/claude-credential/verify")
def verify_claude_credential_now(user: dict = Depends(require_user_active)) -> dict:
    """현재 사용자 자격증명을 실제 claude 호출로 즉시 재검증 → 캐시 갱신 후 상태 반환.

    반환: {configured, type, updated_at, health:{valid,reason,detail,checked_at}}.
    oauth_token 만료·폐기를 사용자가 설정 화면에서 능동적으로 확인할 때 쓴다.

    남용 방지: 쿨다운(WEB_CRED_VERIFY_COOLDOWN_SEC) 내 재호출은 캐시를 돌려주고, 실제 claude ping
    은 _llm_semaphore 하에서만 수행한다 → 반복 호출로 서브프로세스가 무제한 생성돼 스레드풀을
    고갈시키는 자원고갈(DoS) 표면을 닫는다.
    """
    username = user["username"]
    now = time.monotonic()
    with _cred_health_lock:
        last = _cred_verify_last.get(username, 0.0)
        cached = _claude_cred_health.get(username)
    if cached is not None and (now - last) < CRED_VERIFY_COOLDOWN_SEC:
        status = auth.credential_status(username)
        status["health"] = cached  # 쿨다운 내 — 재-ping 없이 최근 캐시 반환
        return status
    with _llm_semaphore:  # 실제 ping 은 LLM 슬롯 하에서만(동시 폭주 시 무제한 스폰 차단)
        health = _evaluate_credential_health(username)
    with _cred_health_lock:
        _cred_verify_last[username] = time.monotonic()
    status = auth.credential_status(username)
    status["health"] = health
    return status


@app.get("/api/admin/claude-credential-health")
def admin_claude_credential_health(user: dict = Depends(require_admin)) -> dict:
    """관리자용 자격증명 헬스 집계(secret 없음) — '1명 vs 전체' 영향 범위 판단용.

    counts: configured/valid/invalid/decrypt_failed/unchecked + 종류별(api_key/oauth_token).
    users: 문제 우선 정렬(invalid/decrypt_failed 상단). 최근 스윕 시각 포함.
    """
    owners = auth.list_credential_owners()
    with _cred_health_lock:
        cache = dict(_claude_cred_health)
    counts = {
        "configured": len(owners), "valid": 0, "invalid": 0,
        "decrypt_failed": 0, "unchecked": 0, "api_key": 0, "oauth_token": 0,
    }
    rows: list[dict] = []
    for o in owners:
        counts[o["type"]] = counts.get(o["type"], 0) + 1
        h = cache.get(o["username"])
        if h is None:
            counts["unchecked"] += 1
            valid, reason, checked_at = None, "unchecked", None
        else:
            valid, reason, checked_at = h.get("valid"), h.get("reason"), h.get("checked_at")
            if reason == "decrypt_failed":
                counts["decrypt_failed"] += 1
            elif valid is True:
                counts["valid"] += 1
            elif valid is False:
                counts["invalid"] += 1
        rows.append({
            "username": o["username"], "type": o["type"], "updated_at": o["updated_at"],
            "valid": valid, "reason": reason, "checked_at": checked_at,
        })
    # 문제(valid=False/None)를 상단으로 — 운영자가 먼저 보게.
    rows.sort(key=lambda r: (r["valid"] is True, r["username"]))
    return {
        "usesAgentCli": USES_AGENT_CLI,
        "checkEnabled": CRED_HEALTH_CHECK_ENABLED,
        "intervalSec": CRED_HEALTH_INTERVAL_SEC,
        "lastSweepAt": _cred_health_meta.get("last_sweep_at"),
        "counts": counts,
        "users": rows,
    }


@app.put("/api/settings/claude-credential")
def put_claude_credential(
    req: CredentialRequest, user: dict = Depends(require_user_active)
) -> dict:
    """자격증명 저장 + 가벼운 검증 호출. 검증 실패해도 저장은 유지(ok=false).

    응답: {status(=credential_status), verification:{ok, detail}}. secret 은 절대 미반환.
    """
    try:
        auth.set_credential(user["username"], req.cred_type, req.secret)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 저장 직후 그 자격증명으로 검증(실패해도 저장 유지).
    credential = auth.get_credential(user["username"])
    verification = _verify_credential(credential) if credential else {"ok": False, "detail": "저장 실패"}
    # 재등록 결과를 헬스 캐시에 즉시 반영 — 옛 캐시(만료 등)가 재인증 후에도 남지 않게 한다.
    if credential:
        _cache_cred_health_from_verification(user["username"], credential["type"], verification)
    else:
        with _cred_health_lock:
            _claude_cred_health.pop(user["username"], None)
    return {"status": auth.credential_status(user["username"]), "verification": verification}


@app.delete("/api/settings/claude-credential")
def delete_claude_credential(user: dict = Depends(require_user_active)) -> dict:
    """현재 사용자 자격증명 삭제 → 전역 폴백으로 복귀."""
    cleared = auth.clear_credential(user["username"])
    with _cred_health_lock:
        _claude_cred_health.pop(user["username"], None)  # 삭제 시 옛 헬스 캐시도 함께 제거
        _cred_verify_last.pop(user["username"], None)
    return {"ok": True, "cleared": cleared, "status": auth.credential_status(user["username"])}


# ---------- 비동기 AI 잡 (STT는 장시간 → 잡 + 폴링) ----------
# 메모리 잡 테이블. 영속(meeting 저장)은 프론트가 결과를 받아 /api/meetings 로 한다
# (프론트의 기존 process→save 흐름 보존 → 프론트 변경 최소화).
_jobs: dict[str, dict] = {}
# job_id → ownerId. 잡 결과(STT contract·재요약 미리보기)는 회의 파생 데이터이므로 소유격리한다
# (jobId 유출 시 타 사용자 폴링 차단). 잡 워커는 상태 dict 를 통째 교체하므로 소유자는 별도 맵에 둔다.
_job_owner: dict[str, str] = {}
# meeting_id → 진행 중 drive-sync job_id. 저장 시 자동 동기화 + 수동 버튼이 같은 회의에 동시에
# 걸릴 때 중복 잡(→ 하위폴더/Doc 중복 생성 레이스)을 막는다. 잡 종료 시 해제. _jobs_lock 로 보호.
_drive_sync_inflight: dict[str, str] = {}
_jobs_lock = threading.Lock()
# 실제 슬롯 점유 카운터 — 워커가 세마포어 임계구역 진입/이탈 시 증감한다. 잡 상태(processing 등)에서
# 추론하지 않으므로, 스톨로 error 마감된 잡이 워커에서 아직 슬롯을 쥐고 있으면 그 점유가 그대로
# 집계된다(상태 기반 집계가 놓치던 CR#4). _jobs_lock 로 보호.
_inflight = {"stt": 0, "llm": 0}


def _inflight_delta(kind: str, delta: int) -> None:
    with _jobs_lock:
        _inflight[kind] += delta

# 동시 STT 추론 제한(백프레셔). GPU 1장에 요청마다 모델을 load 하므로, 동시에 N개만 돌리고
# 나머지는 대기시킨다 → OOM/연산 경합 방지. 대기 중 잡 status='queued'(프론트가 "처리 대기 중…" 표시).
# 기본 1(직렬). WEB_STT_CONCURRENCY 로 조정(예: VRAM 여유 시 2). (모델 상주/유휴 언로드는 v2.)
STT_CONCURRENCY = max(1, int(os.environ.get("WEB_STT_CONCURRENCY", "1")))
_stt_semaphore = threading.Semaphore(STT_CONCURRENCY)

# LLM(요약·추출) 동시성 제한 — GPU 와 무관한 agent_cli/클라우드 호출이라 _stt_semaphore 와
# 분리한다: A 가 요약(수 분) 중이어도 B 의 STT 가 시작될 수 있다. 기본 2(사용자별 자격증명이라
# rate limit 도 사용자별). WEB_LLM_CONCURRENCY 로 조정.
LLM_CONCURRENCY = max(1, int(os.environ.get("WEB_LLM_CONCURRENCY", "2")))
_llm_semaphore = threading.Semaphore(LLM_CONCURRENCY)

# STT 스톨(무한루프) 탐지 — STT 추론엔 내부 취소지점이 없어, 한 잡이 엔진에서 멈추면 슬롯 1개가
# 영영 안 풀려 뒤 사용자가 무한 대기한다. phase='transcribing' 이 STT_STALL_SEC 를 넘으면 스톨로
# 보고(warning=stt_stalled, audit·로그, 관리자 진단 노출). 소프트: 슬롯 자체는 재기동으로만 회수.
# STT_STALL_MARK_ERROR=1 이면 대기 사용자에게 error(stt_stalled)로 마감까지 한다(정품 장시간 STT
# 오탐 위험 있어 기본 OFF). 임계값은 넉넉히(기본 900s=15분; RTFx≈232라 정상은 분 단위 이하).
STT_STALL_SEC = float(os.environ.get("WEB_STT_STALL_SEC", "900"))
STT_STALL_SCAN_SEC = float(os.environ.get("WEB_STT_STALL_SCAN_SEC", "30"))
STT_STALL_MARK_ERROR = os.environ.get("WEB_STT_STALL_MARK_ERROR", "0") != "0"

# Drive 동기화 동시성 제한(네트워크 I/O — GPU 와 무관해 _stt_semaphore 와 분리). 기본 2.
_drive_semaphore = threading.Semaphore(max(1, int(os.environ.get("WEB_DRIVE_CONCURRENCY", "2"))))

# 잡별 취소 신호("분석 취소"). 취소 엔드포인트가 set → 잡 스레드가 단계 경계에서 확인해 이탈,
# agent_cli 는 진행 중 claude 서브프로세스를 kill(use_cancel_event 채널). 잡 종료 시 정리.
_job_cancels: dict[str, threading.Event] = {}

# 종료(done/error/cancelled) 잡의 인메모리 항목 정리(TTL). 잡은 종료 후에도 _jobs/_job_owner 에
# 남아 프로세스 수명 동안 누적된다 → 정리 루프가 '종료 상태를 처음 관측한 시각'부터 TTL 초과 시
# 제거한다(모든 종료 경로가 시각을 남길 필요 없이 관측 기준 GC — 종료 경로가 여러 곳이라 견고).
# TTL 은 폴링 창(수 초)보다 충분히 길어 진행 중이던 클라이언트가 결과를 못 받는 일이 없다.
# 잡 결과(done 의 summary/transcript)는 스토어가 아닌 인메모리에만 있으므로, 클라이언트가 완료
# 직후 폴링을 멈췄다(모바일 탭 서스펜드·노트북 절전) 나중에 복귀하는 경우까지 결과를 살리려 기본
# TTL 을 2시간으로 둔다(CR#5 완화). 근본적으로는 완료 결과를 스토어에 영속화하는 것이 정답이며,
# 그건 잡-GC 설계와 함께 별도로 다룬다.
_JOB_TERMINAL_STATUSES = ("done", "error", "cancelled")
_job_finished_at: dict[str, float] = {}
JOB_RETENTION_SEC = float(os.environ.get("WEB_JOB_RETENTION_SEC", "7200"))


def _purge_finished_jobs(ttl: float = JOB_RETENTION_SEC) -> int:
    """종료 후 TTL 초과한 잡의 인메모리 항목(_jobs/_job_owner/_job_finished_at) 제거 → 제거 개수.

    진행 중(queued/processing) 잡은 절대 건드리지 않는다. 종료 잡은 최초 관측 시각을 기록하고,
    now - 관측시각 > ttl 이면 제거한다. _jobs_lock 하에 원자적으로 수행.
    """
    now = time.monotonic()
    removed = 0
    with _jobs_lock:
        for jid, j in list(_jobs.items()):
            if j.get("status") in _JOB_TERMINAL_STATUSES:
                _job_finished_at.setdefault(jid, now)  # 종료 최초 관측 시각
            else:
                _job_finished_at.pop(jid, None)  # 진행 중은 대상 아님(방어)
        for jid, ts in list(_job_finished_at.items()):
            if jid not in _jobs or now - ts > ttl:
                if _jobs.pop(jid, None) is not None:
                    removed += 1  # 실제 잡 항목 제거만 카운트(고아 시각 정리는 제외)
                _job_owner.pop(jid, None)
                _job_finished_at.pop(jid, None)
                _job_meta.pop(jid, None)  # 관측 메타도 함께 회수(잡 수명과 동일)
    return removed


def _mark_job(job_id: str, payload: dict) -> None:
    with _jobs_lock:
        _jobs[job_id] = payload


# ---- 잡 진행/큐 관측(무한루프 원인 구분: 대기 경합 vs 엔진 스톨 vs 인증) ----
# job_id → {kind, created_at(mono), started_at(mono|None), phase, phase_at(mono), warning}.
# phase: waiting_stt → transcribing → waiting_llm → summarizing (STT 잡 기준). 이 phase 로
# "무엇을 기다리는지/무엇을 처리 중인지"를 프론트가 구분 표시하고, 스톨 스캔이 엔진 무응답을 잡는다.
_job_meta: dict[str, dict] = {}


def _init_job_meta(job_id: str, kind: str) -> None:
    """잡 생성 시 메타 초기화. kind='stt'(STT+요약) | 'llm'(재요약 등 LLM 전용)."""
    now = time.monotonic()
    with _jobs_lock:
        _job_meta[job_id] = {
            "kind": kind,
            "created_at": now,
            "started_at": None,
            "phase": "waiting_stt" if kind == "stt" else "waiting_llm",
            "phase_at": now,
            "warning": None,
        }


def _set_phase(job_id: str, phase: str) -> None:
    """잡 단계 전환 기록(전환 시각 갱신). 첫 실제 처리(transcribing/summarizing)에서 started_at 확정."""
    now = time.monotonic()
    with _jobs_lock:
        m = _job_meta.get(job_id)
        if m is None:  # 메타 미초기화 잡 방어(관측만; 없으면 만들어 둔다)
            m = _job_meta[job_id] = {
                "kind": "stt", "created_at": now, "started_at": None,
                "phase": phase, "phase_at": now, "warning": None,
            }
        m["phase"] = phase
        m["phase_at"] = now
        if phase in ("transcribing", "summarizing") and m["started_at"] is None:
            m["started_at"] = now


def _job_phase(job_id: str) -> str | None:
    with _jobs_lock:
        m = _job_meta.get(job_id)
        return m["phase"] if m else None


def _queue_snapshot_locked() -> dict:
    """현재 STT/LLM 슬롯 점유·대기 집계(_jobs_lock 보유 상태에서 호출).

    active(점유)는 실제 세마포어 점유 카운터(_inflight)로 — 스톨로 error 마감돼도 워커가 슬롯을
    쥐고 있으면 그대로 집계된다(상태 추론이 놓치던 CR#4). 대기(queued)는 phase 로 집계한다.
    """
    stt_wait = llm_wait = 0
    for jid, m in _job_meta.items():
        if _jobs.get(jid, {}).get("status") not in ("queued", "processing"):
            continue
        ph = m.get("phase")
        if ph == "waiting_stt":
            stt_wait += 1
        elif ph == "waiting_llm":
            llm_wait += 1
    return {
        "sttSlots": STT_CONCURRENCY, "sttActive": _inflight["stt"], "sttQueued": stt_wait,
        "llmSlots": LLM_CONCURRENCY, "llmActive": _inflight["llm"], "llmQueued": llm_wait,
    }


def _scan_stt_stalls() -> list[dict]:
    """phase='transcribing' 이 STT_STALL_SEC 초과한 잡을 스톨로 표시 → 표시한 목록 반환.

    소프트: 워커 스레드/토치 추론을 강제 종료하지 않는다(안전). warning 을 달아 프론트·관리자
    진단에 노출하고, STT_STALL_MARK_ERROR 면 대기 사용자에게 error(stt_stalled)로 마감한다.
    슬롯 자체의 회수는 프로세스 재기동으로만 가능(로그로 운영자에게 알린다).
    """
    now = time.monotonic()
    newly: list[dict] = []
    with _jobs_lock:
        for jid, m in list(_job_meta.items()):
            if m.get("phase") != "transcribing":
                continue
            if _jobs.get(jid, {}).get("status") != "processing":
                continue
            if now - m["phase_at"] <= STT_STALL_SEC:
                continue
            already = m.get("warning") == "stt_stalled"
            m["warning"] = "stt_stalled"
            elapsed = round(now - m["phase_at"], 1)
            if STT_STALL_MARK_ERROR:
                _jobs[jid] = {
                    "status": "error",
                    "error": f"STT 엔진이 응답하지 않습니다(경과 {elapsed:.0f}s). 잠시 후 다시 시도하세요.",
                    "error_code": "stt_stalled",
                }
                # 취소 이벤트도 set — 멈췄던 워커가 나중에 STT 를 반환하면 단계 경계에서 이탈해,
                # 이미 사용자에게 통보한 error 를 done 으로 되덮는 것을 막는다(잘못된 실패 판정 유지).
                ev = _job_cancels.get(jid)
                if ev is not None:
                    ev.set()
            if not already:  # 최초 탐지만 보고(중복 로그 억제)
                newly.append({"job": jid, "owner": _job_owner.get(jid), "elapsed_sec": elapsed})
    for s in newly:
        observability.audit("stt.stall", marked_error=STT_STALL_MARK_ERROR, **s)
        print(
            f"[stt-stall] job={s['job']} owner={s['owner']} elapsed={s['elapsed_sec']}s "
            "— STT 슬롯이 장시간 점유됨(엔진 스톨 의심). 지속되면 서버 재기동으로 슬롯 회수 필요.",
            flush=True,
        )
    return newly


async def _stt_stall_watchdog_loop() -> None:
    """STT_STALL_SCAN_SEC 주기로 스톨 스캔(블로킹 없음 — 짧은 인메모리 스캔). 예외는 삼켜 루프 유지."""
    while True:
        await asyncio.sleep(STT_STALL_SCAN_SEC)
        try:
            _scan_stt_stalls()
        except Exception:  # noqa: BLE001 — 스캔 실패가 워치독을 죽이지 않게 격리
            traceback.print_exc()


def _job_cancelled(job_id: str, cancel: threading.Event) -> bool:
    """취소 여부 확인 — set 이면 status='cancelled' 로 마감하고 True."""
    if not cancel.is_set():
        return False
    _mark_job(job_id, {"status": "cancelled"})
    return True


def _run_ai_job(
    job_id: str,
    audio_bytes: bytes,
    mime_type: str | None,
    credential: dict | None,
    cancel: threading.Event,
) -> None:
    """STT+정제(GPU 슬롯) → 요약·추출(LLM 슬롯) → 잡 결과 저장. 실패해도 status=error(폴백 원칙).

    credential(현재 사용자 자격증명, secret 포함)은 use_credential 로 이 스레드 컨텍스트에만
    심어 agent_cli 백엔드가 사용자별 인증으로 호출하게 한다(스레드별 ContextVar 격리). None 이면
    전역 폴백. 새 Thread 는 부모 ContextVar 를 자동 상속하지 않으므로 여기서 명시 설정한다.

    취소: cancel(Event)를 세마포어 획득 직후/단계 경계에서 확인해 이탈하고, agent_cli 진행 중에는
    use_cancel_event 채널로 서브프로세스를 kill(AgentCLICancelled) — 슬롯을 빨리 반납해
    뒤 사용자의 대기를 끊는다. STT 추론 자체는 비중단(짧음, RTFx≈232).
    """
    try:
        # [1단계: GPU] 슬롯 확보까지 대기(status='queued' 유지). 확보하면 'processing' 전환.
        # 락 순서 규약: 세마포어 보유 중 store._lock(update_if_match)을 잡지 않는다 —
        # 추론은 store 비접촉이고 _jobs(인메모리)만 갱신한다(데드락/장시간 점유 방지).
        with _stt_semaphore:
            if _job_cancelled(job_id, cancel):  # 대기 중 취소된 잡 — 슬롯 즉시 반납
                return
            _mark_job(job_id, {"status": "processing"})
            _set_phase(job_id, "transcribing")  # STT 슬롯 확보 → 실제 전사 시작(스톨 탐지 기준)
            _inflight_delta("stt", 1)  # 실제 슬롯 점유 시작(스톨 error 마감돼도 여기 반환까지 점유)
            try:
                with use_credential(credential), use_cancel_event(cancel):
                    seg_dicts, duration = transcribe_to_segments(
                        audio_bytes, mime_type=mime_type, backend_name=CLEAN_BACKEND
                    )
            finally:
                _inflight_delta("stt", -1)  # 슬롯 반납(세마포어 해제 직전)
        if _job_cancelled(job_id, cancel):
            return
        # [2단계: LLM] GPU 슬롯은 반납한 상태 — 다음 사용자의 STT 가 여기서 시작될 수 있다.
        _set_phase(job_id, "waiting_llm")  # STT 슬롯 반납 → LLM 슬롯 대기(요약/추출)
        with _llm_semaphore:
            if _job_cancelled(job_id, cancel):
                return
            _set_phase(job_id, "summarizing")  # LLM 슬롯 확보 → 요약/추출 진행
            _inflight_delta("llm", 1)
            try:
                with use_credential(credential), use_cancel_event(cancel):
                    contract = enrich_to_contract(
                        seg_dicts,
                        duration,
                        extract_backend_name=EXTRACT_BACKEND,
                        summarize_backend_name=SUMMARIZE_BACKEND or None,
                        clean_backend_name=CLEAN_BACKEND,
                    )
            finally:
                _inflight_delta("llm", -1)
        result = {
            "summary": contract.get("summary", {}),  # 구조체(dict) 계약 — 빈 기본값도 객체
            "actionItems": contract.get("actionItems", []),
            "transcript": contract.get("transcript", []),
            "duration": _fmt_duration(contract.get("_duration_seconds")),
        }
        _mark_job(job_id, {"status": "done", "result": result})
    except AgentCLICancelled:
        _mark_job(job_id, {"status": "cancelled"})
    except AgentCLIAuthError as e:
        # 인증 만료/미로그인: 일반 실패와 구분해 error_code 를 실어 프론트가 "재인증" 흐름을
        # 안내하게 한다(STT 는 됐어도 요약/추출 백엔드 claude 인증이 끊긴 상태).
        traceback.print_exc()
        _mark_job(
            job_id,
            {"status": "error", "error": str(e), "error_code": "claude_auth_expired"},
        )
        _mark_cred_health_invalid(_job_owner.get(job_id))  # 실시간 신호로 헬스 배지 즉시 만료 반영
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        # STT 단계(전사/슬롯 대기) 실패는 엔진 이상으로 구분 태깅 → 프론트가 "자격증명/경합"과
        # 다른 원인(백엔드 엔진)임을 안내할 수 있게 한다. LLM 단계 실패는 일반 오류로 둔다.
        payload = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        if _job_phase(job_id) in ("waiting_stt", "transcribing"):
            payload["error_code"] = "stt_engine_error"
        _mark_job(job_id, payload)
    finally:
        with _jobs_lock:
            _job_cancels.pop(job_id, None)


@app.post("/api/ai/process")
def ai_process(req: ProcessRequest, user: dict = Depends(require_user_active)) -> dict:
    """오디오 제출 → 백그라운드 STT 잡 등록 → {jobId} 즉시 반환. 프론트는 GET 잡 폴링."""
    import base64
    try:
        audio_bytes = base64.b64decode(req.audioBase64)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"audioBase64 디코딩 실패: {e}")
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="빈 오디오")

    # 현재 사용자 자격증명(secret 포함, 내부 주입용) 조회 → 잡 스레드로 전달.
    credential = auth.get_credential(user["username"])
    job_id = uuid.uuid4().hex
    # 'queued' 로 시작: 스레드가 동시성 슬롯을 확보하면 _run_ai_job 이 'processing' 으로 전환한다.
    # (슬롯이 비어 있으면 거의 즉시 processing, 혼잡하면 대기 → 프론트가 "처리 대기 중…" 표시.)
    cancel = threading.Event()
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued"}
        _job_owner[job_id] = user["id"]
        _job_cancels[job_id] = cancel
    _init_job_meta(job_id, "stt")  # 관측 메타(phase/타이밍) 초기화 — 스레드 시작 전에
    threading.Thread(
        target=_run_ai_job,
        args=(job_id, audio_bytes, req.mimeType, credential, cancel),
        daemon=True,
    ).start()
    return {"jobId": job_id, "status": "queued"}


@app.get("/api/ai/jobs/{job_id}")
def ai_job(job_id: str, user: dict = Depends(require_user_active)) -> dict:
    """잡 상태/결과 폴링. status: processing | done(result) | error(error).

    소유격리: 잡 결과는 회의 파생 데이터이므로 잡 소유자만 조회 가능(타인은 404, 존재 은닉)."""
    now = time.monotonic()
    with _jobs_lock:
        j = _jobs.get(job_id)
        owner = _job_owner.get(job_id)
        # owner 는 정상 잡이면 항상 존재 → 불일치·부재(None) 모두 404. 부재 거부는 purge 가 소유자를
        # 지운 뒤 워커가 상태를 재삽입해 만든 owner 없는 유령 잡이 소유격리를 우회하는 것을 막는다.
        if j is None or owner != user["id"]:
            raise HTTPException(status_code=404, detail="job 없음")
        out = {"jobId": job_id, **j}
        m = _job_meta.get(job_id)
        snap = _queue_snapshot_locked()
        ahead = None
        if m is not None:
            out["phase"] = m["phase"]
            # 스톨 경고는 '진행 중'일 때만 의미가 있다 — 스톨 후 엔진이 회복돼 done 으로 끝난
            # 잡에까지 warning 을 실어 보내면 성공한 회의에 거짓 '엔진 무응답' 경보가 뜬다.
            out["warning"] = m.get("warning") if j.get("status") in ("queued", "processing") else None
            if j.get("status") in ("queued", "processing"):
                out["elapsedSec"] = round(now - m["created_at"], 1)  # 접수 후 총 경과
                out["phaseElapsedSec"] = round(now - m["phase_at"], 1)  # 현 단계 경과
            # STT 슬롯 대기 중이면 '내 앞에 몇 건'인지 — 경합(누가 쓰는 중) 여부를 사용자가 알 수 있게.
            if j.get("status") == "queued" and m["phase"] == "waiting_stt":
                created = m["created_at"]
                earlier_wait = sum(
                    1
                    for jid2, m2 in _job_meta.items()
                    if jid2 != job_id
                    and m2.get("phase") == "waiting_stt"
                    and m2.get("created_at", now) < created
                    and _jobs.get(jid2, {}).get("status") in ("queued", "processing")
                )
                ahead = snap["sttActive"] + earlier_wait
    out["queue"] = snap
    if ahead is not None:
        out["ahead"] = ahead
    # 사람이 읽는 원인 힌트(프론트가 그대로 노출 가능) — 무한루프 3원인을 구분해 준다.
    out["reasonHint"] = _job_reason_hint(out)
    return out


def _job_reason_hint(out: dict) -> str | None:
    """폴링 응답 → 한 줄 원인 힌트(경합/전사중/요약중/스톨/엔진오류/인증). 없으면 None."""
    status, phase = out.get("status"), out.get("phase")
    # 종료 상태부터 판정 — done/cancelled 는 힌트 없음, error 는 코드별. 스톨 경고는 그 뒤(진행 중만)라
    # 회복돼 완료된 잡에 거짓 스톨 힌트가 붙지 않는다.
    if status == "error":
        code = out.get("error_code")
        if code == "claude_auth_expired":
            return "claude 인증이 만료되어 요약/추출이 실패했습니다 — 재인증이 필요합니다."
        if code == "stt_engine_error":
            return "STT 엔진 오류로 전사에 실패했습니다 — 백엔드 엔진 점검이 필요합니다."
        if code == "stt_stalled":
            return "STT 엔진 무응답으로 마감되었습니다 — 백엔드 엔진 점검이 필요합니다."
        return None
    if status in ("done", "cancelled"):
        return None
    if out.get("warning") == "stt_stalled":  # 여기부터 진행 중(queued/processing)만
        return "STT 엔진이 응답하지 않습니다(스톨 의심) — 관리자 확인이 필요합니다."
    if status == "queued":
        if phase == "waiting_stt":
            ahead = out.get("ahead")
            if ahead:
                return f"다른 STT 처리 {ahead}건이 앞서 진행 중입니다 — 순서 대기 중입니다."
            return "STT 처리 슬롯을 기다리는 중입니다."
        if phase == "waiting_llm":
            return "요약/추출(LLM) 슬롯을 기다리는 중입니다."
        return "처리 대기 중입니다."
    if status == "processing":
        if phase == "transcribing":
            return "전사(STT) 진행 중입니다."
        if phase == "summarizing":
            return "요약/추출 진행 중입니다."
        return "처리 중입니다."
    return None


@app.post("/api/ai/jobs/{job_id}/cancel")
def ai_job_cancel(job_id: str, user: dict = Depends(require_user_active)) -> dict:
    """잡 취소 요청 — "분석 취소"가 서버 파이프라인까지 실제로 멈추게 한다(소유자만, 존재 은닉 404).

    동작: 취소 이벤트 set → 대기(queued) 잡은 즉시 cancelled 로 마감(슬롯 미점유),
    실행 중 잡은 단계 경계/claude 서브프로세스 kill 로 이탈해 슬롯을 조기 반납한다.
    이미 끝난 잡(done/error/cancelled)은 그대로 현재 상태를 돌려준다(멱등).
    """
    with _jobs_lock:
        j = _jobs.get(job_id)
        owner = _job_owner.get(job_id)
        if j is None or owner != user["id"]:  # owner 부재(None)도 거부(purge 재삽입 유령 잡 방어)
            raise HTTPException(status_code=404, detail="job 없음")
        cancel = _job_cancels.get(job_id)
        if cancel is not None and j.get("status") in ("queued", "processing"):
            cancel.set()
            if j.get("status") == "queued":
                # 스레드는 아직 슬롯 대기 중 — 상태를 먼저 마감해 프론트가 즉시 복귀하게 한다.
                # (스레드가 슬롯을 얻으면 _job_cancelled 가 재확인 후 조용히 이탈.)
                _jobs[job_id] = {"status": "cancelled"}
        status = _jobs[job_id].get("status", "unknown")
    observability.audit("ai_job.cancel", owner=user["username"], job_id=job_id, status=status)
    return {"jobId": job_id, "status": status}


@app.get("/api/admin/ai-jobs")
def admin_ai_jobs(user: dict = Depends(require_admin)) -> dict:
    """관리자용 진행 중 잡/슬롯 스냅샷 — STT 무한루프 원인 진단(경합 vs 엔진 스톨).

    queue: 슬롯 점유·대기 집계. active: 진행 중(queued/processing) 잡을 경과시간 내림차순으로
    (오래 걸린 잡·스톨이 상단). 스톨 임계값(sttStallSec)과 마감 여부(markError)도 노출.
    """
    now = time.monotonic()
    with _jobs_lock:
        snap = _queue_snapshot_locked()
        active: list[dict] = []
        for jid, m in _job_meta.items():
            st = _jobs.get(jid, {}).get("status")
            if st not in ("queued", "processing"):
                continue
            active.append({
                "jobId": jid, "owner": _job_owner.get(jid), "kind": m.get("kind"),
                "status": st, "phase": m.get("phase"), "warning": m.get("warning"),
                "elapsedSec": round(now - m["created_at"], 1),
                "phaseElapsedSec": round(now - m["phase_at"], 1),
            })
    active.sort(key=lambda a: -a["elapsedSec"])
    stalled = [a for a in active if a.get("warning") == "stt_stalled"]
    return {
        "queue": snap,
        "sttStallSec": STT_STALL_SEC,
        "markError": STT_STALL_MARK_ERROR,
        "activeCount": len(active),
        "stalledCount": len(stalled),
        "active": active,
    }


# ---------- 재요약 regenerate (트랙 C·P8, prompt 모드: 미리보기→확정→백업/undo) ----------
def _ts_to_seconds(ts: object) -> float:
    """transcript timestamp(MM:SS | HH:MM:SS) → 초. 파싱 불가 시 0.0."""
    parts = str(ts or "").strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0.0
    if len(nums) == 2:
        return float(nums[0] * 60 + nums[1])
    if len(nums) == 3:
        return float(nums[0] * 3600 + nums[1] * 60 + nums[2])
    return 0.0


def _segments_from_transcript(transcript: list) -> list[dict]:
    """저장본 transcript → 재요약 입력 pseudo-segment([{id,start,end,text}]).

    id = segmentId(토대 PR 로 영속, 없으면 위치 인덱스 폴백). text = 교정된 transcript text(편집 반영).
    start = timestamp→초, end = 다음 항목 start(마지막은 start). 새 summary/actionItems 의
    evidence_seg_ids 가 이 id(=segmentId) 공간을 참조하므로 transcript 와 anchor/점프가 자기정합한다.
    """
    segs: list[dict] = []
    for i, e in enumerate(transcript or []):
        if not isinstance(e, dict):
            continue
        text = str(e.get("text", "")).strip()
        if not text:
            continue
        try:
            sid = int(e["segmentId"]) if e.get("segmentId") is not None else i
        except (ValueError, TypeError):
            sid = i  # 비정수 segmentId(클라 위조 저장본) → 위치 인덱스 폴백(500 방지)
        start = _ts_to_seconds(e.get("timestamp"))
        segs.append({"id": sid, "start": start, "end": start, "text": text})
    for j in range(len(segs) - 1):  # end = 다음 start(시간범위 산출용), 역순 방지
        if segs[j + 1]["start"] > segs[j]["start"]:
            segs[j]["end"] = segs[j + 1]["start"]
    return segs


def _run_regenerate_job(
    job_id: str, segments: list[dict], credential: dict | None, cancel: threading.Event
) -> None:
    """재구성 segment → 재요약(summary+actionItems) 미리보기를 잡 결과로 저장(DB 미접촉).

    _run_ai_job 과 동일한 자격증명/취소/폴백 규약. STT 없이 LLM 만 돌므로 GPU 슬롯이 아닌
    _llm_semaphore 를 쥔다(재요약이 다른 사용자의 STT 를 막지 않음).
    DB 는 건드리지 않는다 — 확정(apply)에서만 백업+교체한다(prompt 모드, 무파괴).
    """
    try:
        with _llm_semaphore:
            if _job_cancelled(job_id, cancel):
                return
            _mark_job(job_id, {"status": "processing"})
            _set_phase(job_id, "summarizing")  # LLM 슬롯 확보 → 재요약 진행
            _inflight_delta("llm", 1)
            try:
                with use_credential(credential), use_cancel_event(cancel):
                    summary = summarize_meeting(segments, backend_name=SUMMARIZE_BACKEND or "passthrough")
                    hints = _summary_action_hints(summary)
                    action_items = extract_action_items(
                        segments, backend_name=EXTRACT_BACKEND, summary_hints=hints
                    )
            finally:
                _inflight_delta("llm", -1)
        result = {"summary": summary, "actionItems": action_items}
        _mark_job(job_id, {"status": "done", "result": result})
    except AgentCLICancelled:
        _mark_job(job_id, {"status": "cancelled"})
    except AgentCLIAuthError as e:
        traceback.print_exc()
        _mark_job(
            job_id,
            {"status": "error", "error": str(e), "error_code": "claude_auth_expired"},
        )
        _mark_cred_health_invalid(_job_owner.get(job_id))  # 실시간 신호로 헬스 배지 즉시 만료 반영
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        _mark_job(job_id, {"status": "error", "error": f"{type(e).__name__}: {e}"})
    finally:
        with _jobs_lock:
            _job_cancels.pop(job_id, None)


@app.post("/api/meetings/{meeting_id}/regenerate")
def regenerate_meeting(meeting_id: str, user: dict = Depends(require_user_active)) -> dict:
    """교정 transcript 로 summary+actionItems 재생성(비동기 잡). 미리보기만 — DB 미접촉.

    소유자만. 잡 결과는 GET /api/ai/jobs/{jobId} 로 폴링(result={summary, actionItems}).
    확정은 별도 POST .../regenerate/apply(백업+교체). 미리보기는 프론트 메모리 한정(새로고침 시 폐기).
    """
    m = _owned_or_404(meeting_id, user)
    segments = _segments_from_transcript(m.get("transcript") or [])
    if not segments:
        raise HTTPException(status_code=400, detail="재요약할 transcript 가 없습니다.")
    credential = auth.get_credential(user["username"])
    job_id = uuid.uuid4().hex
    cancel = threading.Event()
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued"}
        _job_owner[job_id] = user["id"]
        _job_cancels[job_id] = cancel
    _init_job_meta(job_id, "llm")  # 재요약은 LLM 전용 잡 — 관측 메타 초기화
    threading.Thread(
        target=_run_regenerate_job, args=(job_id, segments, credential, cancel), daemon=True
    ).start()
    observability.audit("regenerate.request", meeting_id=meeting_id, owner=user["username"])
    return {"jobId": job_id, "status": "queued"}


@app.post("/api/meetings/{meeting_id}/regenerate/apply")
def regenerate_apply(
    meeting_id: str,
    payload: dict,
    response: Response,
    user: dict = Depends(require_user_active),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict:
    """재요약 미리보기 확정 — summary+actionItems 교체(적용 직전 현행을 백업). If-Match 412.

    mode(payload.mode, 기본 'prompt'):
      - 'prompt'/'overwrite': 재생성본으로 전면 교체(구조 무결성은 잡 미리보기 신뢰 전제).
      - 'preserve_edited': 재생성본 + 현행 edited=true 항목 보존 병합(merge_preserve_edited).
        summary 는 '사용자 편집 보존' 블록 추가, actionItems 는 현행 편집 항목 덧붙임(손실 0).
    구조 전면 교체이므로 summary 편집(text-only) 검증을 거치지 않는다. actionItems 는 item_id
    무결성만 정규화한다. 백업은 meeting_backup 에 기록되어 undo 로 복원 가능하다.
    """
    m = _owned_or_404(meeting_id, user)
    summary = payload.get("summary")
    # summary 내부 스키마(agenda/anchor/evidence)는 검증하지 않는다 — 정상 흐름은 잡 미리보기
    # 결과(ground_summary 보장)를 그대로 확정하는 것이며, 구조 무결성은 그 신뢰를 전제한다.
    # 자기 소유 회의에만 작용하므로 임의 dict 주입의 영향 범위는 본인뿐(타인 격리는 _owned_or_404).
    if not isinstance(summary, dict):
        raise HTTPException(status_code=400, detail="summary(객체)가 필요합니다.")
    action_items = ensure_action_item_ids(payload.get("actionItems"))
    mode = str(payload.get("mode") or "prompt")
    if mode not in ("prompt", "preserve_edited", "overwrite"):
        raise HTTPException(status_code=400, detail="mode 값이 올바르지 않습니다.")
    if mode == "preserve_edited":
        # 현행(m)의 edited 항목을 재생성본에 보존 병합. m 은 락 밖 읽기본이라 병합 입력이 최신이라는
        # 보장은 없다 — 다만 stale 입력으로 병합했더라도 store.apply_regenerate 의 If-Match 비교가
        # 락 안에서 불일치 시 412 로 거부하므로 손상 저장은 없다(If-Match 제공 시). If-Match 미제공이면
        # last-write-wins 라 그 사이 추가 편집이 덮일 수 있다(프론트는 항상 If-Match 전송).
        summary, action_items = merge_preserve_edited(
            m.get("summary"), m.get("actionItems"), summary, action_items
        )
        action_items = ensure_action_item_ids(action_items)  # 병합 후 item_id 무결성 재보장
    parsed = _parse_if_match(if_match)
    expected = None if parsed is _IF_MATCH_ANY else parsed
    try:
        updated = store.apply_regenerate(meeting_id, summary, action_items, expected)
    except PreconditionFailedError as e:
        observability.audit("regenerate.conflict_412", meeting_id=meeting_id)
        if e.current_updated_at:
            response.headers["ETag"] = f'"{e.current_updated_at}"'
        raise HTTPException(
            status_code=412,
            detail={
                "error": "precondition_failed",
                "message": "저장본이 변경되었습니다. 최신 회의를 재조회한 뒤 다시 시도하세요.",
                "currentUpdatedAt": e.current_updated_at,
            },
        )
    if updated is None:
        raise HTTPException(status_code=404, detail="meeting 없음")
    observability.audit("regenerate.apply", meeting_id=meeting_id, owner=user["username"], mode=mode)
    response.headers["ETag"] = f'"{updated["updatedAt"]}"'
    return updated


@app.post("/api/meetings/{meeting_id}/regenerate/undo")
def regenerate_undo(
    meeting_id: str,
    response: Response,
    user: dict = Depends(require_user_active),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict:
    """직전 재요약 적용을 되돌린다(가장 최근 백업 복원, 그 백업은 소비). If-Match 412."""
    _owned_or_404(meeting_id, user)
    parsed = _parse_if_match(if_match)
    expected = None if parsed is _IF_MATCH_ANY else parsed
    try:
        updated, restored = store.restore_latest_backup(meeting_id, expected)
    except PreconditionFailedError as e:
        if e.current_updated_at:
            response.headers["ETag"] = f'"{e.current_updated_at}"'
        raise HTTPException(
            status_code=412,
            detail={
                "error": "precondition_failed",
                "message": "저장본이 변경되었습니다. 최신 회의를 재조회한 뒤 다시 시도하세요.",
                "currentUpdatedAt": e.current_updated_at,
            },
        )
    if updated is None:
        raise HTTPException(status_code=404, detail="meeting 없음")
    if not restored:
        raise HTTPException(status_code=409, detail="복원할 재요약 백업이 없습니다.")
    observability.audit("regenerate.undo", meeting_id=meeting_id, owner=user["username"])
    response.headers["ETag"] = f'"{updated["updatedAt"]}"'
    return updated


@app.post("/api/meetings/{meeting_id}/revert")
def revert_meeting(
    meeting_id: str,
    response: Response,
    user: dict = Depends(require_user_active),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict:
    """회의 내용을 최초 생성본(원본)으로 원복. 원본은 소비하지 않아 반복 원복 가능. If-Match 412.

    title/participants/summary/actionItems/transcript 를 원본으로 되돌리고 status(확정 여부)·
    createdAt 등은 보존한다. 원본 스냅샷이 없는(이 기능 이전 생성) 회의는 409.
    """
    _owned_or_404(meeting_id, user)
    parsed = _parse_if_match(if_match)
    expected = None if parsed is _IF_MATCH_ANY else parsed
    try:
        updated, restored = store.restore_original(meeting_id, expected)
    except PreconditionFailedError as e:
        if e.current_updated_at:
            response.headers["ETag"] = f'"{e.current_updated_at}"'
        raise HTTPException(
            status_code=412,
            detail={
                "error": "precondition_failed",
                "message": "저장본이 변경되었습니다. 최신 회의를 재조회한 뒤 다시 시도하세요.",
                "currentUpdatedAt": e.current_updated_at,
            },
        )
    if updated is None:
        raise HTTPException(status_code=404, detail="meeting 없음")
    if not restored:
        raise HTTPException(
            status_code=409, detail="원본 스냅샷이 없습니다(이 기능 이전에 생성된 회의)."
        )
    observability.audit("meeting.revert", meeting_id=meeting_id, owner=user["username"])
    response.headers["ETag"] = f'"{updated["updatedAt"]}"'
    return _fill_display_date(updated)


@app.post("/api/ai/extract-actions")
def ai_extract_actions(req: ExtractRequest, user: dict = Depends(require_user_active)) -> list[str]:
    """텍스트 붙여넣기 → 액션아이템 string[](프론트 계약: Promise<string[]>).

    입력이 raw text 한 덩어리라 segment/timestamp 가 없다 → anchor/evidence/owner 는 만들 수 없고
    **item.text 만 평탄화**해서 반환한다(계약 결정 2026-06-09). 줄 단위로 pseudo-segment 를 만들어
    ExtractStage(EXTRACT_BACKEND)에 넣는다. passthrough/실패 → 빈 배열(graceful).
    """
    text = (req.text or "").strip()
    if not text or EXTRACT_BACKEND == "passthrough":
        return []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()] or [text]
    segs = [{"id": i, "start": 0.0, "end": 0.0, "text": ln} for i, ln in enumerate(lines)]
    # 현재 사용자 자격증명을 이 호출 컨텍스트에만 주입(agent_cli 가 사용자별 인증으로 호출).
    credential = auth.get_credential(user["username"])
    try:
        with use_credential(credential):
            items = extract_action_items(segs, backend_name=EXTRACT_BACKEND)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return []
    return [it["text"] for it in items if it.get("text")]


# ---------- 영속 엔드포인트 (meetingService 대체) ----------
def _fill_display_date(m: dict) -> dict:
    """응답용 `date` 폴백 — 없거나 비면 createdAt 을 채워 내려준다(영속 아님).

    date 필드 부재 시 프론트가 `new Date(undefined)` 로 전 화면 Invalid Date ·
    통계 0 · 정렬 무동작을 일으켰다(2026-07-16 UX 리뷰 §2). 저장 문서는 불변,
    store 가 매 호출 json.loads 사본을 반환하므로 응답 딕셔너리 변형은 안전하다.
    """
    if not m.get("date") and m.get("createdAt"):
        m["date"] = m["createdAt"]
    return m


def _owned_or_404(meeting_id: str, user: dict) -> dict:
    """meeting 조회 + 소유자 확인. 없거나 남의 것이면 404(존재 자체를 숨김)."""
    m = store.get(meeting_id)
    if m is None or m.get("ownerId") != user["id"]:
        raise HTTPException(status_code=404, detail="meeting 없음")
    return m


@app.get("/api/meetings")
def list_meetings(user: dict = Depends(require_user_active)) -> list[dict]:
    return [_fill_display_date(m) for m in store.list(owner_id=user["id"])]


@app.get("/api/meetings/{meeting_id}")
def get_meeting(meeting_id: str, user: dict = Depends(require_user_active)) -> dict:
    return _fill_display_date(_owned_or_404(meeting_id, user))


@app.post("/api/meetings")
def create_meeting(meeting: dict, user: dict = Depends(require_user_active)) -> dict:
    """회의 확정 저장. optional audioStagingToken 이 있으면 staging 오디오를 이 회의에 bind.

    D7-id 옵션B: 처리 시점에 1회 업로드한 staging 을 finalize 시 meetingId 로 이동(이중 전송 없음).
    토큰 없으면 무시(후방호환). 토큰이 있어도 staging 파일이 없으면(만료·정리됨) 회의는 정상
    저장하되 audioRef 를 남기지 않는다(graceful — 오디오는 부가 정보).
    """
    if not meeting.get("id"):
        meeting["id"] = uuid.uuid4().hex
    _require_hex32(meeting["id"], what="meetingId")  # 경로조립(audio bind) 안전 보장
    meeting["ownerId"] = user["id"]  # 소유자는 토큰에서 강제(클라이언트 위조 방지)
    meeting.setdefault("createdAt", _now_iso())
    meeting["updatedAt"] = _now_iso()
    # item_id 무결성(재요약 조인키): 신규/중복 항목에 uuid 부여. POST 는 신규 단발 생성
    # (read-compare 사이클 없음)이라 store 락 밖 정규화로 충분하다(PATCH 는 _validator 안에서 수행).
    if "actionItems" in meeting:
        meeting["actionItems"] = ensure_action_item_ids(meeting.get("actionItems"))

    # 오디오 bind(옵션B). audioStagingToken 은 meeting JSON 에 영속하지 않는다(메타는 audioRef).
    token = meeting.pop("audioStagingToken", None)
    if token:
        _require_hex32(token, what="audioStagingToken")
        audio_ref = audio_store.bind_staging(token, meeting["id"])
        if audio_ref is not None:
            meeting["audioRef"] = audio_ref
            observability.audit(
                "audio.bind", meeting_id=meeting["id"], bytes=audio_ref.get("sizeBytes")
            )
    created = store.create(meeting)
    # 최초 생성본(AI 생성 결과)을 원복용 스냅샷으로 1회 보관(idempotent — 재저장해도 원본 불변).
    store.save_original_snapshot(created)
    observability.audit(
        "meeting.create",
        meeting_id=meeting["id"],
        owner=user["username"],
        audio=bool(meeting.get("audioRef")),
    )
    return _fill_display_date(created)


@app.patch("/api/meetings/{meeting_id}")
def patch_meeting(
    meeting_id: str,
    patch: dict,
    response: Response,
    user: dict = Depends(require_user_active),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict:
    """회의 부분 업데이트.

    후방호환: **If-Match 헤더가 없으면 기존 동작(last-write-wins)** 그대로 — finalize·제목·
    액션아이템 편집 등 기존 호출부를 깨지 않는다. If-Match 가 있으면 저장본 updatedAt 과
    비교해 불일치 시 412(현재 updatedAt 을 ETag 헤더·본문 힌트로 재조회 유도).
    If-Match 파싱(M4): `*`=존재하면 일치(값 비교 생략·존재만 요구), 약 ETag `W/` 접두 제거,
    다중값(콤마)은 첫 토큰 사용, 비어있는 비표준 입력은 400.

    구조보존 편집(편집 시에만, 독립 적용):
      - transcript: 저장본에 비어있지 않은 transcript 가 있고 patch 에 transcript 가 포함되면
        개수·timestamp·speakerId 불변을 검증(위반 422)하고 text 가 바뀐 엔트리에 edited=True 를
        서버가 set 한다(위치 식별, 클라 edited 무시).
      - summary(P6): 저장본 summary 에 agenda 가 있고 patch 에 summary 가 포함되면 블록/항목
        개수·no·title·anchor·evidence·item_id 불변을 검증(위반 422)하고 SummaryItem.text 만
        편집 허용, 바뀐 항목에 edited/edited_at/original_text 를 서버가 set·evidence 스냅샷
        동결한다(item_id 식별, 레거시 회의는 lazy 부여, grounding 우회).
      - actionItems: UI 에서 자유 추가/삭제/편집되므로 구조를 잠그지 않고(개수/순서 가변), 재요약
        조인키용 item_id 무결성만 보장한다(부재/중복=uuid 부여, 기존 보존). 거부(422) 없음.
        세 처리는 독립이며 다른 필드 동시 patch 를 막지 않는다.

    비교+갱신+구조검증은 store.update_if_match() 로 store 락 내 원자 수행(M2: read+compare+
    validate+write 단일 구간). transcript 구조검증은 락 안에서 재조회한 저장본 기준으로
    수행되므로(락 밖 읽기본 cur 기준이 아님) If-Match 없는 편집의 TOCTOU 가 차단된다.
    """
    _owned_or_404(meeting_id, user)  # 소유 확인 후에만 수정(소유권/존재 게이트)
    patch.pop("ownerId", None)  # 소유자 변경 불가(store 도 한 번 더 강제)
    patch.pop("updatedAt", None)  # updatedAt(ETag)은 서버가 부여 — 클라 값 무시

    # M4: If-Match 견고 파싱. `*`=존재하면 일치(값 비교 생략), W/·다중값·따옴표 처리.
    parsed = _parse_if_match(if_match)
    expected = None if parsed is _IF_MATCH_ANY else parsed

    def _validator(stored: dict, p: dict) -> dict:
        """락 안에서 재조회한 저장본(stored) 기준 transcript·summary 구조검증(M2).

        patch 에 transcript/summary 가 있고 저장본의 해당 구조가 비어있지 않을 때만 적용한다
        (후방호환: 초기 0→N 채우기·미포함 patch 는 통과). 두 검증은 독립이며 한쪽이 다른 필드
        동시 patch 를 막지 않는다. 검증 실패(Transcript/SummaryStructureError)는 락 밖으로
        전파되어 422 로 변환된다."""
        out = dict(p)
        if "transcript" in p:
            stored_tr = stored.get("transcript") or []
            if stored_tr:  # 초기 빈 상태가 아니면 구조검증(0→N 채우기는 허용)
                out["transcript"] = validate_transcript_edit(stored_tr, p.get("transcript") or [])
        if "summary" in p:
            stored_sum = stored.get("summary") or {}
            if stored_sum.get("agenda"):  # agenda 가 있는(생성 완료) summary 만 편집 검증
                out["summary"] = validate_summary_edit(stored_sum, p.get("summary") or {})
        if "actionItems" in p:  # 구조 잠금 아님(자유 추가/삭제) — item_id 무결성만 보장
            out["actionItems"] = ensure_action_item_ids(p.get("actionItems"))
        return out

    try:
        updated = store.update_if_match(meeting_id, patch, expected, validator=_validator)
    except (TranscriptStructureError, SummaryStructureError) as e:
        # M2: 락 안 검증 실패 — 검증에 쓴 스냅샷 == write 대상 스냅샷 보장하에 422.
        raise HTTPException(status_code=422, detail=str(e))
    except PreconditionFailedError as e:
        # 412: 프론트가 현재 값 재조회·재적용하도록 현재 updatedAt(ETag)을 힌트로 제공.
        observability.audit("meeting.conflict_412", meeting_id=meeting_id)
        if e.current_updated_at:
            response.headers["ETag"] = f'"{e.current_updated_at}"'
        raise HTTPException(
            status_code=412,
            detail={
                "error": "precondition_failed",
                "message": "저장본이 변경되었습니다. 최신 회의를 재조회한 뒤 다시 시도하세요.",
                "currentUpdatedAt": e.current_updated_at,
            },
        )
    if updated is None:  # 비교 직전 삭제된 경합
        raise HTTPException(status_code=404, detail="meeting 없음")
    observability.incr("meeting.patch")
    response.headers["ETag"] = f'"{updated["updatedAt"]}"'
    return _fill_display_date(updated)


@app.delete("/api/meetings/{meeting_id}")
def delete_meeting(meeting_id: str, user: dict = Depends(require_user_active)) -> dict:
    """회의 삭제 + 원본 오디오 동반 삭제(보존=회의 수명 동일).

    meetingId 화이트리스트(^[0-9a-f]{32}$) 통과분만 오디오 디렉토리 경로를 조립한다(traversal 차단).
    형식 위반 meetingId 는 오디오 삭제를 건너뛴다(DB 삭제만; 그런 id 는 오디오가 있을 수 없음).
    """
    m = _owned_or_404(meeting_id, user)  # 소유 확인 후에만 삭제
    # 구글 자산(드라이브 파일·캘린더 이벤트) 동반 삭제(옵션, 기본 OFF=유지). DB 삭제 전에 ref 확보.
    gdrive_ref = m.get("gdriveRef") if DRIVE_DELETE_ON_MEETING_DELETE else None
    gcal_ref = m.get("gcalRef") if CALENDAR_DELETE_ON_MEETING_DELETE else None
    if gdrive_ref or gcal_ref:
        _delete_google_assets_async(user["username"], gdrive_ref, gcal_ref)
    store.delete(meeting_id)
    audio_removed = False
    if _HEX32_RE.match(meeting_id):
        audio_removed = audio_store.delete_meeting_audio(meeting_id)
    observability.audit(
        "meeting.delete", meeting_id=meeting_id, owner=user["username"], audio=audio_removed
    )
    return {"ok": True}


def _delete_google_assets_async(
    username: str, gdrive_ref: dict | None, gcal_ref: dict | None
) -> None:
    """회의 삭제 시 구글 자산(드라이브 문서/오디오·캘린더 이벤트) 삭제(best-effort, 백그라운드).

    네트워크 I/O 라 삭제 응답을 막지 않게 데몬 스레드로 던지고, 토큰 갱신 1회로 둘 다 정리한다.
    공유 드라이브 루트 폴더는 보존(다른 회의 파일). 실패는 무시(본인 자산이라 잔존해도 안전).
    연동 해제 상태면 조용히 skip."""
    doc_id = (gdrive_ref or {}).get("docId")
    audio_id = (gdrive_ref or {}).get("audioId")
    folder_id = (gdrive_ref or {}).get("folderId")
    event_id = (gcal_ref or {}).get("eventId")
    cal_id = (gcal_ref or {}).get("calendarId") or DEFAULT_CALENDAR_ID
    if not (doc_id or audio_id or folder_id or event_id):
        return

    def _run() -> None:
        cred = auth.get_google_credential(username)
        if not cred:
            return
        try:
            access_token = google_oauth.refresh_access_token(cred["refresh_token"])
            root_id = cred.get("root_folder_id")
            # 회의별 하위 폴더(LB_NOTE/{회의명_날짜})면 폴더째 삭제 → 빈 폴더 잔존 방지(내용 동반 삭제).
            # 단, folderId 가 루트와 같으면(구 flat 회의) 루트 삭제 금지 — 개별 파일만 지운다.
            if folder_id and folder_id != root_id:
                google_drive.delete_files(access_token, [folder_id])
            elif doc_id or audio_id:
                google_drive.delete_files(access_token, [doc_id, audio_id])
            if event_id:
                google_calendar.delete_event(access_token, event_id, calendar_id=cal_id)
        except Exception:  # noqa: BLE001 — best-effort 정리(실패 무시)
            traceback.print_exc()

    threading.Thread(target=_run, daemon=True).start()


# ---------- 원본 오디오 영속(플랜 v4 트랙 C·Phase 4, D7-id 옵션B) ----------
@app.post("/api/meetings/audio/staging")
def upload_audio_staging(
    file: UploadFile = File(...),
    content_length: int | None = Header(default=None, alias="Content-Length"),
    user: dict = Depends(require_user_active),
) -> dict:
    """멀티파트 오디오 업로드 → {stagingToken, format, sizeBytes}. 처리 시점 1회 업로드(옵션B).

    finalize(create_meeting)가 stagingToken 을 받으면 회의로 bind 한다. 인증 필요.
    크기 상한(MAX_AUDIO_BYTES) 초과는 413. 빈 파일은 400. 저장 실패 시 부분파일 정리(rollback).

    메모리/조기 413: 전체를 메모리에 적재하지 않고 1MB 청크로 스트리밍하며 디스크에 누적 write 한다.
    누적 크기가 상한을 넘으면 즉시 중단·부분파일 정리·413. Content-Length 헤더로 명백한 초과는
    바디를 읽기 전에 조기 거부한다(멀티파트 오버헤드만큼 헐겁지만 명백한 초과는 빠르게 걸러짐).

    이벤트 루프 블로킹 해소: save_staging_stream 은 동기 디스크 누적 write(대용량 ≤500MB)이므로
    이 엔드포인트를 동기 def 로 둔다 → Starlette 가 자동으로 threadpool 에 위임해 이벤트 루프를
    막지 않는다(인증 Depends·예외 매핑·Content-Length 선검사 동작은 sync 에서도 동일).
    """
    # Content-Length 선검사: 명백한 초과는 바디를 받기 전에 조기 거부(DoS 완화).
    if content_length is not None and content_length > audio_store.MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"오디오가 너무 큽니다(최대 {audio_store.MAX_AUDIO_BYTES} bytes).",
        )
    try:
        token, ext, size = audio_store.save_staging_stream(
            file.file.read,
            mime_type=file.content_type,
            filename=file.filename,
            max_bytes=audio_store.MAX_AUDIO_BYTES,  # 런타임 시점 상한(테스트 monkeypatch·재설정 반영)
        )
    except audio_store.AudioTooLarge:
        raise HTTPException(
            status_code=413,
            detail=f"오디오가 너무 큽니다(최대 {audio_store.MAX_AUDIO_BYTES} bytes).",
        )
    except ValueError:  # 빈 오디오(0바이트) — 부분파일 정리됨
        raise HTTPException(status_code=400, detail="빈 오디오")
    except Exception as e:  # noqa: BLE001 — 저장 실패는 부분파일 정리 후 500(부분파일 미잔존)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"오디오 저장 실패: {type(e).__name__}")
    observability.incr("audio.staging_bytes", size)
    observability.audit("audio.staging", owner=user["username"], format=ext, bytes=size)
    return {"stagingToken": token, "format": ext, "sizeBytes": size}


# Range 응답 청크 크기(부분요청 스트리밍 단위).
_RANGE_CHUNK = 1024 * 1024


def _parse_range(range_header: str | None, size: int) -> tuple[int, int] | None:
    """`Range: bytes=start-end` 단일 범위 파싱 → (start, end) inclusive. 없거나 불만족이면 None.

    멀티 레인지(콤마)는 미지원 — 단일 범위만(오디오 시킹 용도). 잘못된 범위는 None(호출부 416/200).
    """
    if not range_header:
        return None
    unit, _, spec = range_header.partition("=")
    if unit.strip().lower() != "bytes" or not spec:
        return None
    first = spec.split(",", 1)[0].strip()
    start_s, _, end_s = first.partition("-")
    try:
        if start_s == "":  # suffix: bytes=-N (마지막 N 바이트)
            n = int(end_s)
            if n <= 0:
                return None
            start = max(0, size - n)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return None
    end = min(end, size - 1)
    return start, end


_AUDIO_MIME = {
    "webm": "audio/webm", "wav": "audio/wav", "mp3": "audio/mpeg", "m4a": "audio/mp4",
    "mp4": "audio/mp4", "ogg": "audio/ogg", "oga": "audio/ogg", "flac": "audio/flac",
    "aac": "audio/aac", "opus": "audio/opus", "bin": "application/octet-stream",
}


# 오디오 스트리밍용 단기 토큰 TTL(초). 쿼리파라미터로 URL 에 실리므로 짧게(기본 1시간).
AUDIO_TOKEN_TTL = int(os.environ.get("WEB_AUDIO_TOKEN_TTL", "3600"))


def _audio_user(authorization: str | None, access_token: str | None) -> dict:
    """오디오 스트리밍 인증 — Bearer 헤더(세션 토큰) 우선, 없으면 access_token 쿼리(audio 스코프 토큰).

    네이티브 <audio src> 는 Authorization 헤더를 못 싣는다 → audio 스코프 단기 토큰을 쿼리로 받아
    검증한다. Bearer 경로는 일반 세션 토큰(scope 없음), 쿼리 경로는 scope='audio' 토큰만 통과한다
    (스코프 토큰의 다른 엔드포인트 재사용 차단). must_change_password 사용자는 require_user_active
    와 동일하게 403. 무효/만료/스코프불일치 토큰은 401.
    """
    scheme, _, header_token = (authorization or "").partition(" ")
    if scheme.lower() == "bearer" and header_token.strip():
        user = auth.user_from_token(header_token.strip())  # 세션 토큰(scope 없음)
    elif access_token:
        user = auth.user_from_token(access_token.strip(), scope="audio")  # 오디오 전용 토큰
    else:
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    if user.get("mustChangePassword"):
        raise HTTPException(
            status_code=403,
            detail={"error_code": "must_change_password", "message": "초기 비밀번호를 먼저 변경해야 합니다."},
        )
    return user


@app.get("/api/meetings/{meeting_id}/audio-token")
def get_audio_token(meeting_id: str, user: dict = Depends(require_user_active)) -> dict:
    """오디오 스트리밍용 단기 토큰 발급(네이티브 <audio> Range). 소유자 검증 후 short-lived 토큰.

    프론트는 이 토큰을 GET .../audio?access_token=... 쿼리로 실어 브라우저 네이티브 Range 스트리밍
    (206)을 쓴다. 토큰 노출 창을 줄이려 TTL 을 짧게(AUDIO_TOKEN_TTL) 둔다. 세션 토큰은 URL 에 싣지 않는다.
    """
    _require_hex32(meeting_id, what="meetingId")
    _owned_or_404(meeting_id, user)  # 소유자만 발급(존재·격리)
    # scope='audio' 제한 토큰 — 탈취돼도 오디오 스트리밍 외 엔드포인트엔 재사용 불가.
    token = auth.make_token(user["id"], ttl=AUDIO_TOKEN_TTL, scope="audio")
    return {"token": token, "expiresIn": AUDIO_TOKEN_TTL}


def _stream_audio_from_drive(m: dict, user: dict, range_header: str | None) -> Response:
    """로컬 원본이 없는 회의의 오디오를 Drive 에서 Range 프록시 스트리밍(업로드 후 정리된 원본).

    gdriveRef.audioId + 사용자 Google 연동 필요. 접근불가/미연동/만료/오류는 404 로 은닉(존재 노출
    회피, 로컬 부재 404 와 동일 UX). 토큰은 캐시판(refresh_access_token_cached)으로 Range 폭주 완화.
    """
    gref = m.get("gdriveRef") or {}
    audio_id = gref.get("audioId")
    if not audio_id:
        raise HTTPException(status_code=404, detail="오디오 없음")
    cred = auth.get_google_credential(user["username"])
    if not cred:
        raise HTTPException(status_code=404, detail="오디오 없음")  # 연동 해제 → 접근 불가
    try:
        access = google_oauth.refresh_access_token_cached(cred["refresh_token"])
        status, hdrs, reader = google_drive.stream_media(access, audio_id, range_header)
    except (
        google_oauth.GoogleAuthExpired,
        google_oauth.GoogleOAuthError,
        google_drive.GoogleDriveError,
    ) as e:
        raise HTTPException(status_code=404, detail="오디오 없음") from e
    observability.incr("audio.stream.drive")
    if status == 416:  # 범위 불만족 → 릴레이
        return Response(
            status_code=416,
            headers={
                "Content-Range": hdrs.get("Content-Range", "bytes */0"),
                "Accept-Ranges": "bytes",
            },
        )
    ext = (m.get("audioRef") or {}).get("format", "bin")
    media_type = hdrs.get("Content-Type") or _AUDIO_MIME.get(ext, "application/octet-stream")

    def _proxy():
        try:
            while chunk := reader.read(_RANGE_CHUNK):
                yield chunk
        finally:
            with contextlib.suppress(Exception):
                reader.close()

    relay = {"Accept-Ranges": "bytes"}
    if hdrs.get("Content-Range"):
        relay["Content-Range"] = hdrs["Content-Range"]
    if hdrs.get("Content-Length"):
        relay["Content-Length"] = hdrs["Content-Length"]
    return StreamingResponse(_proxy(), status_code=status, media_type=media_type, headers=relay)


@app.get("/api/meetings/{meeting_id}/audio")
def get_meeting_audio(
    meeting_id: str,
    access_token: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
    range_header: str | None = Header(default=None, alias="Range"),
) -> Response:
    """회의 원본 오디오 스트리밍. 인증(Bearer 또는 access_token 쿼리) + 소유자 검증 + Range(206).

    네이티브 <audio> 는 Bearer 를 못 싣어 access_token 쿼리(단기 토큰)로 인증한다(Range 시킹 가능).
    audioRef 없거나 파일 부재면 404. 소유자 아니면 404(_owned_or_404, 존재 자체 숨김).
    meetingId 가 ^[0-9a-f]{32}$ 아니면 400(경로조립 traversal 차단). Range 미지정 시 전체(200).
    """
    user = _audio_user(authorization, access_token)  # Bearer 또는 쿼리 토큰
    _require_hex32(meeting_id, what="meetingId")  # 경로조립 전 화이트리스트(traversal 차단)
    m = _owned_or_404(meeting_id, user)  # 소유자 격리
    path = audio_store.meeting_audio_path(meeting_id, m.get("audioRef"))
    if path is None:
        # 로컬 원본 부재 → Drive 업로드 후 정리됐으면 Drive 에서 프록시, 아니면 404.
        return _stream_audio_from_drive(m, user, range_header)
    observability.incr("audio.stream")  # Range 1요청당 1(재생 1건은 보통 다수 요청)
    size = path.stat().st_size
    ext = (m.get("audioRef") or {}).get("format", "bin")
    media_type = _AUDIO_MIME.get(ext, "application/octet-stream")

    rng = _parse_range(range_header, size)
    if rng is None:
        if range_header:  # 범위 헤더는 왔으나 불만족 → 416
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
            )

        def _full():
            with path.open("rb") as f:
                while chunk := f.read(_RANGE_CHUNK):
                    yield chunk

        return StreamingResponse(
            _full(),
            media_type=media_type,
            headers={"Accept-Ranges": "bytes", "Content-Length": str(size)},
        )

    start, end = rng
    length = end - start + 1

    def _partial():
        remaining = length
        with path.open("rb") as f:
            f.seek(start)
            while remaining > 0:
                chunk = f.read(min(_RANGE_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        _partial(),
        status_code=206,
        media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
        },
    )


# ---------- Google Drive 회의록 동기화(사용자별 OAuth) ----------
@app.get("/api/settings/google/status")
def google_status(user: dict = Depends(require_user_active)) -> dict:
    """현재 사용자 Google 연동 상태(refresh_token 비노출). configured=서버가 연동을 지원하는지."""
    st = auth.google_status(user["username"])
    st["configured"] = google_oauth.oauth_configured()  # 프론트가 '연동' 버튼 노출 판단
    return st


@app.post("/api/settings/google/connect")
def google_connect(user: dict = Depends(require_user_active)) -> dict:
    """동의 URL 발급 → {authUrl}. state=scope 'google_oauth' 단기 토큰(신원+CSRF).

    프론트가 authUrl 로 이동해 사용자가 본인 Google 계정 동의를 마치면 callback 으로 돌아온다.
    서버 미설정(env 없음)이면 503(관리자 설정 필요).
    """
    if not google_oauth.oauth_configured():
        raise HTTPException(status_code=503, detail="Google 연동이 설정되지 않았습니다(관리자 문의).")
    state = auth.make_token(user["id"], ttl=GOOGLE_STATE_TTL, scope="google_oauth")
    try:
        url = google_oauth.build_consent_url(state)
    except google_oauth.GoogleOAuthError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"authUrl": url}


def _google_redirect(status: str) -> RedirectResponse:
    """콜백 후 프론트 설정 페이지로 302(?google=connected|error). 오리진 미설정 시 상대경로."""
    base = f"{FRONTEND_ORIGIN}/settings" if FRONTEND_ORIGIN else "/settings"
    return RedirectResponse(url=f"{base}?google={status}", status_code=302)


@app.get("/api/integrations/google/callback")
def google_callback(
    state: str = Query(...),
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> Response:
    """Google OAuth 콜백 — code→refresh_token 교환·저장 후 프론트로 302.

    인증은 Bearer 가 아니라 state(scope='google_oauth' JWT)로 한다(브라우저 리다이렉트라 헤더 없음).
    state 검증(신원·CSRF)은 user_from_token 이 담당 — 무효/만료/위조/스코프불일치면 401(재사용 차단).
    동의 거부(error)·code 누락·교환 실패는 프론트로 google=error 리다이렉트(사용자 안내).
    """
    user = auth.user_from_token(state, scope="google_oauth")  # 무효 state → 401(CSRF 차단)
    if error or not code:
        observability.audit("google.connect_error", owner=user["username"], reason=error or "no_code")
        return _google_redirect("error")
    try:
        tok = google_oauth.exchange_code(code)
        auth.set_google_credential(user["username"], tok["refresh_token"], email=tok.get("email"))
    except google_oauth.GoogleOAuthError as e:
        traceback.print_exc()  # 실제 사유(토큰 교환 실패 메시지)를 서버 로그에 남긴다
        observability.audit(
            "google.connect_error", owner=user["username"], reason=f"{type(e).__name__}: {e}"
        )
        return _google_redirect("error")
    observability.audit("google.connect", owner=user["username"], email=tok.get("email"))
    return _google_redirect("connected")


@app.delete("/api/settings/google")
def google_disconnect(user: dict = Depends(require_user_active)) -> dict:
    """Google 연동 해제 — refresh_token 폐기(best-effort) + 로컬 자격증명 삭제."""
    cred = auth.get_google_credential(user["username"])
    if cred and cred.get("refresh_token"):
        google_drive.revoke(cred["refresh_token"])  # best-effort(실패 무시)
    cleared = auth.clear_google_credential(user["username"])
    observability.audit("google.disconnect", owner=user["username"], cleared=cleared)
    return {"ok": True, "cleared": cleared, "status": auth.google_status(user["username"])}


def _build_meeting_doc(
    access_token: str,
    m: dict,
    folder_id: str,
    prev_doc_id: str | None,
    username: str,
    meeting_id: str,
) -> str:
    """회의록 Docs 생성/갱신 → docId. 전역 템플릿이 설정돼 있으면 템플릿 복사+치환, 아니면 기본 HTML.

    템플릿 적용 실패(스코프 부족·미공유·삭제 등)는 기본 HTML 회의록으로 폴백해 저장을 무중단으로
    보장한다(관리자 재동의/공유 누락이 사용자 저장을 막지 않도록). 폴백은 audit 로 남긴다.
    """
    tmpl = auth.get_doc_template()
    if tmpl and tmpl.get("template_id"):
        try:
            values = meeting_doc.render_template_values(m)
            doc_id = google_docs.apply_template(
                access_token,
                tmpl["template_id"],
                folder_id,
                meeting_doc.doc_title(m),
                values,
                prev_doc_id,
            )
            observability.audit("drive.template_applied", meeting_id=meeting_id, owner=username)
            return doc_id
        except google_docs.GoogleDocsTemplateError as e:  # noqa: BLE001 — 폴백 원칙(저장 무중단)
            traceback.print_exc()
            observability.audit(
                "drive.template_fallback", meeting_id=meeting_id, owner=username, error=str(e)
            )
            # 폴백: 기본 HTML 회의록으로 저장(무중단).
    # 요약+액션 중심(전체 대화 로그 transcript 제외 — 앱/DB 에 보존).
    html = meeting_doc.render_meeting_html(m, include_transcript=False)
    return google_drive.upsert_doc(access_token, folder_id, html, "회의록", prev_doc_id)


def _run_drive_sync_job(
    job_id: str, meeting_id: str, owner_id: str, username: str, google_cred: dict
) -> None:
    """회의록(Docs)+오디오를 사용자 본인 Drive 로 업로드/재동기화 → 잡 결과에 gdriveRef·docUrl.

    네트워크 I/O 라 _drive_semaphore(STT 와 분리) 하에 돈다. refresh_token 무효면 error_code=
    google_auth_expired 로 프론트 재연동 유도. 성공 시 gdriveRef 를 meeting.data 에 영속(멱등 재동기화
    토대)한다. 잡 인메모리 규약은 _run_ai_job 과 동일(status queued→processing→done|error).
    """
    with _drive_semaphore:
        with _jobs_lock:
            _jobs[job_id] = {"status": "processing"}
        try:
            access_token = google_oauth.refresh_access_token(google_cred["refresh_token"])
            m = store.get(meeting_id)
            if m is None or m.get("ownerId") != owner_id:
                raise RuntimeError("meeting 없음")  # 잡 대기 중 삭제/이전된 경합
            # 루트 폴더 확보(없으면 생성 → 영속). drive.file 스코프로 앱이 만든 폴더만 접근.
            folder_id = google_drive.ensure_root_folder(access_token, google_cred.get("root_folder_id"))
            if folder_id != google_cred.get("root_folder_id"):
                auth.set_google_root_folder(username, folder_id)
            gref = m.get("gdriveRef") or {}
            # 회의별 하위 폴더 확보(LB_NOTE/{회의명_날짜_시간}). 재싱크 시 gref.folderId 재사용.
            subfolder_id = google_drive.ensure_subfolder(
                access_token, folder_id, _drive_subfolder_name(m), gref.get("folderId")
            )
            # 전역 Docs 템플릿(설정 시) 복사+치환, 아니면 기본 HTML. 템플릿 실패는 HTML 폴백.
            doc_id = _build_meeting_doc(
                access_token, m, subfolder_id, gref.get("docId"), username, meeting_id
            )
            audio_id = gref.get("audioId")
            audio_ref = m.get("audioRef")
            if audio_ref:
                path = audio_store.meeting_audio_path(meeting_id, audio_ref)
                if path is not None:
                    ext = (audio_ref.get("format") or "bin").strip().lower()
                    mime = _AUDIO_MIME.get(ext, "application/octet-stream")
                    audio_id = google_drive.upsert_audio(
                        access_token, subfolder_id, path, mime, f"원본.{ext}", gref.get("audioId")
                    )
            doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
            new_ref = {
                "docId": doc_id,
                "audioId": audio_id,
                "folderId": subfolder_id,
                "docUrl": doc_url,
                "syncedAt": _now_iso(),
            }
            # gdriveRef 를 meeting.data 에 영속(서버 주도라 If-Match 없이 병합). 소유격리는 위에서 확인.
            store.update_if_match(meeting_id, {"gdriveRef": new_ref}, None)
            # 원본 오디오가 Drive 에 안전히 올라갔으면(audioId 확정) 로컬 원본 삭제 → 디스크 회수.
            # audioRef 메타(format/size)는 meeting.data 에 남아 재생 프록시가 Drive 에서 스트리밍한다.
            if audio_id and audio_store.delete_meeting_audio(meeting_id):
                observability.audit("audio.local_pruned", meeting_id=meeting_id, owner=username)
            with _jobs_lock:
                _jobs[job_id] = {"status": "done", "result": {"gdriveRef": new_ref, "docUrl": doc_url}}
            observability.audit("drive.sync_done", meeting_id=meeting_id, owner=username)
        except google_oauth.GoogleAuthExpired as e:
            traceback.print_exc()
            with _jobs_lock:
                _jobs[job_id] = {
                    "status": "error",
                    "error": str(e),
                    "error_code": "google_auth_expired",
                }
        except Exception as e:  # noqa: BLE001 — 폴백 원칙(잡은 멈추지 않고 error 로 종료)
            traceback.print_exc()
            with _jobs_lock:
                _jobs[job_id] = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        finally:
            # in-flight 해제(내가 등록한 잡일 때만) → 이후 동기화 재요청 허용.
            with _jobs_lock:
                if _drive_sync_inflight.get(meeting_id) == job_id:
                    del _drive_sync_inflight[meeting_id]


def _start_drive_sync(meeting_id: str, user_id: str, username: str, google_cred: dict) -> dict:
    """drive-sync 백그라운드 잡 시작(in-flight 디둡) → {jobId, status}. 진행 중이면 기존 잡 재사용.

    중복 폴더/Doc 레이스 방지(자동+수동/연속 저장·이메일 후속 동기화가 겹칠 때). 잡은 회의록+오디오
    를 올리고 오디오 업로드 성공 시 로컬 원본을 정리한다.
    """
    with _jobs_lock:
        existing = _drive_sync_inflight.get(meeting_id)
        if existing is not None and _jobs.get(existing, {}).get("status") in ("queued", "processing"):
            return {"jobId": existing, "status": _jobs[existing]["status"]}
        job_id = uuid.uuid4().hex
        _jobs[job_id] = {"status": "queued"}
        _job_owner[job_id] = user_id
        _drive_sync_inflight[meeting_id] = job_id
    threading.Thread(
        target=_run_drive_sync_job,
        args=(job_id, meeting_id, user_id, username, google_cred),
        daemon=True,
    ).start()
    observability.audit("drive.sync_request", meeting_id=meeting_id, owner=username)
    return {"jobId": job_id, "status": "queued"}


@app.post("/api/meetings/{meeting_id}/drive-sync")
def drive_sync(meeting_id: str, user: dict = Depends(require_user_active)) -> dict:
    """회의록+오디오를 본인 Google Drive 로 내보내기/재동기화(백그라운드 잡) → {jobId}.

    소유자만. 미연동이면 400 error_code=google_not_connected(프론트 연동 유도). 진행은
    GET /api/ai/jobs/{jobId} 로 폴링(done: result={gdriveRef, docUrl} / error: error_code).
    """
    _require_hex32(meeting_id, what="meetingId")
    _owned_or_404(meeting_id, user)  # 소유·존재 게이트
    google_cred = auth.get_google_credential(user["username"])
    if not google_cred:
        raise HTTPException(
            status_code=400,
            detail={"error_code": "google_not_connected", "message": "Google Drive 연동이 필요합니다."},
        )
    return _start_drive_sync(meeting_id, user["id"], user["username"], google_cred)


def _ensure_drive_doc(access_token: str, m: dict, google_cred: dict, username: str) -> str:
    """회의의 Drive 회의록 Doc 을 보장 → docId. 없으면 폴더/하위폴더 확보 후 생성·영속(멱등).

    이메일 첨부(export)용으로 Doc 만 만든다(오디오는 후속 백그라운드 동기화가 담당). 새로 만들면
    gdriveRef(docId/folderId/docUrl) 를 meeting.data 에 병합 저장해 이후 동기화가 재사용한다.
    """
    gref = m.get("gdriveRef") or {}
    doc_id = gref.get("docId")
    if doc_id:
        return doc_id
    folder_id = google_drive.ensure_root_folder(access_token, google_cred.get("root_folder_id"))
    if folder_id != google_cred.get("root_folder_id"):
        auth.set_google_root_folder(username, folder_id)
    subfolder_id = google_drive.ensure_subfolder(
        access_token, folder_id, _drive_subfolder_name(m), gref.get("folderId")
    )
    # 전역 Docs 템플릿(설정 시) 복사+치환, 아니면 기본 HTML. 템플릿 실패는 HTML 폴백.
    doc_id = _build_meeting_doc(access_token, m, subfolder_id, None, username, m["id"])
    new_ref = {
        **gref,
        "docId": doc_id,
        "folderId": subfolder_id,
        "docUrl": f"https://docs.google.com/document/d/{doc_id}/edit",
        "syncedAt": _now_iso(),
    }
    store.update_if_match(m["id"], {"gdriveRef": new_ref}, None)
    return doc_id


def _clean_email_list(raw: object) -> list[str]:
    """이메일 리스트 정리 — '@' 포함·중복(대소문자 무시) 제거. 리스트 아니면 빈 리스트."""
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for x in raw:
        e = str(x or "").strip()
        if "@" in e and len(e) <= MAX_NAME_LEN and e.lower() not in seen:
            seen.add(e.lower())
            out.append(e)
    return out


@app.get("/api/meetings/{meeting_id}/email-preview")
def preview_meeting_email(
    meeting_id: str, user: dict = Depends(require_user_active)
) -> dict:
    """발송 이메일 본문 미리보기(HTML) — send-email 과 동일 렌더러(2026-07-16 UX 리뷰 T3).

    Google 연동 없이도 동작(렌더만). 머리말(note)은 프론트가 입력란으로 따로 보여주므로
    여기선 본문 원형만 반환한다. 프론트는 sandbox iframe(srcDoc)으로 표시할 것.
    """
    _require_hex32(meeting_id, what="meetingId")
    m = _owned_or_404(meeting_id, user)
    return {"html": meeting_doc.render_email_body(m)}


@app.post("/api/meetings/{meeting_id}/send-email")
def send_meeting_email(
    meeting_id: str, payload: dict, user: dict = Depends(require_user_active)
) -> dict:
    """회의록을 참석자에게 이메일 발송(본인 Gmail). 본문=요약+액션, 첨부=회의록 PDF.

    body: {to:[...], cc:[...], subject?, note?}. 소유자만. 미연동 400(google_not_connected),
    gmail.send 미동의 400(google_scope_missing → 재연동), 만료 400(google_auth_expired).
    note = 발송자 머리말(인사말) — 본문 최상단 삽입(2026-07-16 UX 리뷰 T3, 2000자 상한).
    발송 성공 시 회의록/오디오 Drive 영속(백그라운드) 도 함께 트리거한다.
    """
    _require_hex32(meeting_id, what="meetingId")
    m = _owned_or_404(meeting_id, user)  # 소유·존재 게이트
    google_cred = auth.get_google_credential(user["username"])
    if not google_cred:
        raise HTTPException(
            status_code=400,
            detail={"error_code": "google_not_connected", "message": "Google 연동이 필요합니다."},
        )
    to = _clean_email_list(payload.get("to"))
    cc = _clean_email_list(payload.get("cc"))
    if not to:
        raise HTTPException(status_code=422, detail="받는사람(수신자) 이메일이 최소 1명 필요합니다.")
    subject = str(payload.get("subject") or "").strip() or meeting_doc.doc_title(m)
    note = str(payload.get("note") or "").strip()[:2000] or None
    try:
        access = google_oauth.refresh_access_token(google_cred["refresh_token"])
        doc_id = _ensure_drive_doc(access, m, google_cred, user["username"])
        pdf = google_drive.export_doc(access, doc_id, "application/pdf")
        html = meeting_doc.render_email_body(m, note=note)
        sender = google_cred.get("email") or user["username"]
        pdf_name = f"{_sanitize_drive_name(meeting_doc.doc_title(m))}.pdf"
        msg_id = google_gmail.send_message(
            access, sender=sender, to=to, cc=cc, subject=subject,
            html_body=html, attachment=pdf, attachment_name=pdf_name,
        )
    except google_oauth.GoogleAuthExpired as e:
        raise HTTPException(
            status_code=400,
            detail={"error_code": "google_auth_expired", "message": "Google 재연동이 필요합니다."},
        ) from e
    except google_gmail.GmailScopeMissing as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "google_scope_missing",
                "message": "메일 발송 권한 동의가 필요합니다. Google 연동을 다시 진행해 주세요.",
            },
        ) from e
    except (google_gmail.GoogleGmailError, google_drive.GoogleDriveError, google_oauth.GoogleOAuthError) as e:
        traceback.print_exc()
        raise HTTPException(
            status_code=502,
            detail={"error_code": "email_send_failed", "message": f"이메일 발송 실패: {e}"},
        ) from e
    # 발송 성공 → 회의록/오디오 Drive 영속(백그라운드; Doc 존재하므로 재생성 없이 오디오+정리).
    _start_drive_sync(meeting_id, user["id"], user["username"], google_cred)
    observability.audit(
        "email.sent", meeting_id=meeting_id, owner=user["username"], to=len(to), cc=len(cc)
    )
    return {"ok": True, "messageId": msg_id, "sentTo": to, "cc": cc}


# ---------- Google Calendar 양방향 연동 ----------
def _google_access_token(username: str) -> str:
    """저장된 refresh_token → 단기 access_token(동기 캘린더 호출용). 미연동 400, 만료 401(재연동 유도)."""
    cred = auth.get_google_credential(username)
    if not cred:
        raise HTTPException(
            status_code=400,
            detail={"error_code": "google_not_connected", "message": "Google 연동이 필요합니다."},
        )
    try:
        return google_oauth.refresh_access_token(cred["refresh_token"])
    except google_oauth.GoogleAuthExpired:
        raise HTTPException(
            status_code=401,
            detail={"error_code": "google_auth_expired", "message": "Google 재연동이 필요합니다."},
        )
    except google_oauth.GoogleOAuthError as e:
        raise HTTPException(status_code=502, detail=f"Google 인증 실패: {e}")


def _rfc3339(offset_days: int = 0) -> str:
    """현재(UTC) 기준 offset_days 를 더한 RFC3339 문자열(캘린더 timeMin/timeMax 기본값)."""
    return (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=offset_days)
    ).isoformat(timespec="seconds")


@app.get("/api/google/calendar/events")
def google_calendar_events(
    user: dict = Depends(require_user_active),
    time_min: str | None = Query(default=None, alias="timeMin"),
    time_max: str | None = Query(default=None, alias="timeMax"),
) -> list[dict]:
    """구글 캘린더 → 앱: 본인 캘린더 일정 목록(원시 event dict). 프론트가 앱 회의와 합쳐 표시.

    timeMin/timeMax(RFC3339) 미지정 시 now ~ now+CALENDAR_WINDOW_DAYS. 미연동 400, 만료 401.
    """
    access = _google_access_token(user["username"])
    tmin = time_min or _rfc3339(0)
    tmax = time_max or _rfc3339(CALENDAR_WINDOW_DAYS)
    try:
        items = google_calendar.list_events(
            access, time_min=tmin, time_max=tmax, calendar_id=DEFAULT_CALENDAR_ID
        )
    except google_calendar.GoogleCalendarError as e:
        raise HTTPException(status_code=502, detail=f"캘린더 조회 실패: {e}")
    observability.incr("calendar.events_fetch")
    return items


def _parse_duration_minutes(duration: object) -> int:
    """'HH:MM' 형식 회의 길이 → 분. 파싱 불가/비정상이면 기본 60분."""
    parts = str(duration or "").strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 60
    if len(nums) == 2:
        minutes = nums[0] * 60 + nums[1]
    elif len(nums) == 1:
        minutes = nums[0]
    else:
        return 60
    return minutes if minutes > 0 else 60


def _date_only(value: str) -> str:
    """ISO datetime 또는 'YYYY-MM-DD' 에서 날짜 부분(YYYY-MM-DD)만 추출('T' 앞 10자)."""
    return str(value or "").strip().split("T", 1)[0][:10]


def _meeting_to_calendar_event(m: dict) -> dict:
    """앱 회의 → 구글 캘린더 이벤트 body(앱→구글 쓰기). start=date(없으면 createdAt), end=start+duration.

    종일(allDay) 회의는 date 필드(날짜만)로 매핑한다 — Google all-day 의 end 는 배타적(exclusive)
    이라 사용자 포함형(inclusive) 종료일(endDate, 없으면 date)에 +1일 한다. location 이 있으면 넣고,
    사용자 description 은 자동 파트(회의록 링크·안건 제목)보다 위에 붙인다. participants 의 email 은
    attendees 로 넣는다. timed 이벤트에서 dateTime 에 오프셋이 없으면 timeZone 을 함께 넘겨
    Google 이 로컬시각으로 해석하게 한다.
    """
    title = str(m.get("title") or "").strip() or "회의"
    if m.get("allDay"):
        # 종일: date(날짜만) → end 는 배타적이므로 (endDate 또는 date) + 1일. timeZone 불필요.
        start_date = _date_only(m.get("date") or m.get("createdAt"))
        end_inclusive = _date_only(m.get("endDate") or m.get("date") or m.get("createdAt"))
        try:
            end_exclusive = (
                dt.date.fromisoformat(end_inclusive) + dt.timedelta(days=1)
            ).isoformat()
        except ValueError:  # 파싱 불가 시 시작일 기준 하루짜리
            end_exclusive = (
                dt.date.fromisoformat(start_date) + dt.timedelta(days=1)
            ).isoformat() if start_date else start_date
        start_field: dict = {"date": start_date}
        end_field: dict = {"date": end_exclusive}
    else:
        start_iso = str(m.get("date") or m.get("createdAt") or "").strip()
        # ISO 파싱('Z' → +00:00 보정). 실패 시 지금 시각(UTC).
        try:
            start_dt = dt.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        except ValueError:
            start_dt = dt.datetime.now(dt.timezone.utc)
        end_dt = start_dt + dt.timedelta(minutes=_parse_duration_minutes(m.get("duration")))
        has_tz = start_dt.tzinfo is not None
        start_field = {"dateTime": start_dt.isoformat()}
        end_field = {"dateTime": end_dt.isoformat()}
        if not has_tz:  # 오프셋 없는 로컬시각 → timeZone 명시(Google 이 UTC 로 오해하지 않게)
            start_field["timeZone"] = CALENDAR_TIMEZONE
            end_field["timeZone"] = CALENDAR_TIMEZONE

    desc_parts: list[str] = []
    user_desc = str(m.get("description") or "").strip()
    if user_desc:  # 사용자 입력 설명을 자동 파트보다 위에 배치
        desc_parts.append(user_desc)
    gref = m.get("gdriveRef") or {}
    if gref.get("docUrl"):
        desc_parts.append(f"회의록: {gref['docUrl']}")
    for block in (m.get("summary") or {}).get("agenda") or []:
        if isinstance(block, dict) and str(block.get("title") or "").strip():
            desc_parts.append(f"- {block['title']}")

    attendees = []
    for p in m.get("participants") or []:
        email = str((p or {}).get("email") or "").strip() if isinstance(p, dict) else ""
        if "@" in email:
            attendees.append({"email": email})

    body: dict = {"summary": title, "start": start_field, "end": end_field}
    location = str(m.get("location") or "").strip()
    if location:
        body["location"] = location
    if desc_parts:
        body["description"] = "\n".join(desc_parts)
    if attendees:
        body["attendees"] = attendees
    return body


@app.post("/api/meetings/{meeting_id}/calendar-sync")
def calendar_sync(meeting_id: str, user: dict = Depends(require_user_active)) -> dict:
    """앱 → 구글 캘린더: 회의를 캘린더 이벤트로 생성/갱신(동기). gcalRef(eventId) 기반 멱등.

    소유자만. 미연동 400, 만료 401. 저장된 gcalRef.eventId 가 있으면 같은 일정을 갱신(중복 없음).
    성공 시 gcalRef 를 meeting.data 에 영속하고 {gcalRef} 반환(htmlLink 로 '캘린더에서 열기').
    """
    _require_hex32(meeting_id, what="meetingId")
    m = _owned_or_404(meeting_id, user)
    access = _google_access_token(user["username"])
    gref = m.get("gcalRef") or {}
    cal_id = gref.get("calendarId") or DEFAULT_CALENDAR_ID
    try:
        event_id, html_link = google_calendar.upsert_event(
            access,
            calendar_id=cal_id,
            event_body=_meeting_to_calendar_event(m),
            event_id=gref.get("eventId"),
        )
    except google_calendar.GoogleCalendarError as e:
        raise HTTPException(status_code=502, detail=f"캘린더 동기화 실패: {e}")
    new_ref = {
        "eventId": event_id,
        "htmlLink": html_link,
        "calendarId": cal_id,
        "syncedAt": _now_iso(),
    }
    store.update_if_match(meeting_id, {"gcalRef": new_ref}, None)
    observability.audit("calendar.sync", meeting_id=meeting_id, owner=user["username"])
    return {"gcalRef": new_ref}


# ---------- 관리자: 앱 Google OAuth 클라이언트 설정(① client_id/secret, .env/재시작 불필요) ----------
@app.get("/api/admin/google-oauth-config")
def get_google_oauth_config(user: dict = Depends(require_admin)) -> dict:
    """앱 OAuth 설정 상태(관리자, **client_secret 미노출**): {configured, source, clientId, redirectUri, updatedAt}."""
    return google_oauth.config_status()


@app.put("/api/admin/google-oauth-config")
def put_google_oauth_config(
    req: GoogleOAuthConfigRequest, user: dict = Depends(require_admin)
) -> dict:
    """관리자가 앱 OAuth 클라이언트(client_id/secret/redirect_uri) 저장. secret 은 DB Fernet 암호화.

    저장 즉시 반영(재시작 불필요) — google_oauth 가 DB 우선으로 읽는다. secret 은 응답에 미포함.
    """
    try:
        auth.set_google_oauth_config(req.clientId, req.clientSecret, req.redirectUri)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    observability.audit("google.oauth_config_set", by=user["username"])
    return google_oauth.config_status()


@app.delete("/api/admin/google-oauth-config")
def delete_google_oauth_config(user: dict = Depends(require_admin)) -> dict:
    """앱 OAuth 설정 삭제(→ env 폴백으로 복귀)."""
    cleared = auth.clear_google_oauth_config()
    observability.audit("google.oauth_config_clear", by=user["username"], cleared=cleared)
    return {"ok": True, "cleared": cleared, "status": google_oauth.config_status()}


# ---------- 관리자: 전역 Docs 양식(템플릿) ----------
def _doc_template_status() -> dict:
    """전역 Docs 템플릿 공개 상태."""
    tmpl = auth.get_doc_template()
    if not tmpl:
        return {"configured": False, "templateUrl": None, "templateId": None, "updatedAt": None}
    return {
        "configured": True,
        "templateUrl": tmpl.get("template_url"),
        "templateId": tmpl.get("template_id"),
        "updatedAt": tmpl.get("updated_at"),
    }


@app.get("/api/admin/doc-template")
def get_doc_template(user: dict = Depends(require_admin)) -> dict:
    """전역 Docs 양식(템플릿) 상태(관리자): {configured, templateUrl, templateId, updatedAt}."""
    return _doc_template_status()


@app.put("/api/admin/doc-template")
def put_doc_template(req: DocTemplateRequest, user: dict = Depends(require_admin)) -> dict:
    """전역 Docs 템플릿 지정 — 구글 문서 URL/ID 에서 문서 id 추출해 저장. 즉시 반영(재시작 불필요).

    템플릿을 쓰려면 (a) 사용자 연동 계정에 drive.readonly 재동의, (b) 템플릿 문서를 사용자들이
    읽을 수 있게 공유해야 한다(관리 UI 안내). 미충족 시 저장은 기본 HTML 회의록으로 폴백한다.
    """
    template_id = google_docs.extract_doc_id(req.templateUrl)
    if not template_id:
        raise HTTPException(
            status_code=400, detail="유효한 Google 문서 URL 또는 문서 ID 가 아닙니다."
        )
    auth.set_doc_template(template_id, req.templateUrl.strip())
    observability.audit("doc_template.set", by=user["username"], template_id=template_id)
    return _doc_template_status()


@app.delete("/api/admin/doc-template")
def delete_doc_template(user: dict = Depends(require_admin)) -> dict:
    """전역 Docs 템플릿 해제(→ 기본 HTML 회의록으로 복귀)."""
    cleared = auth.clear_doc_template()
    observability.audit("doc_template.clear", by=user["username"], cleared=cleared)
    return {"ok": True, "cleared": cleared, "status": _doc_template_status()}


# ---------- 관리자: 사용자 명부 관리 + 참석자 피커 디렉터리 ----------
@app.get("/api/admin/users")
def admin_list_users(user: dict = Depends(require_admin)) -> dict:
    """전체 사용자 명부(관리자 전용, 비번 해시 미노출)."""
    return {"users": auth.list_users()}


@app.patch("/api/admin/users/{username}")
def admin_update_user(
    username: str, req: AdminUserUpdateRequest, user: dict = Depends(require_admin)
) -> dict:
    """관리자 사용자 정보 수정. 이름 필드는 _clean_name, email 은 _clean_email 로 검증.

    가드: (a) 본인 role 을 admin→user 로 낮추기 금지, (b) 대상이 마지막 admin 이면 강등 금지.
    """
    fields: dict = {}
    if req.displayName is not None:
        fields["display_name"] = _clean_name(req.displayName, field="displayName", required=True)
    if req.englishName is not None:
        fields["english_name"] = _clean_name(req.englishName, field="englishName", required=False)
    if req.jobTitle is not None:
        fields["job_title"] = _clean_name(req.jobTitle, field="jobTitle", required=False)
    if req.email is not None:
        fields["email"] = _clean_email(req.email)
    if req.role is not None:
        if req.role not in ("user", "admin"):
            raise HTTPException(status_code=422, detail="role 은 'user'|'admin' 만 허용됩니다.")
        fields["role"] = req.role
        # 강등(admin→user) 가드 — 대상의 현재 role 을 조회.
        if req.role == "user":
            target = users.get(username)
            if target is None:
                raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
            if (target.get("role") or "user") == "admin":
                # (a) 본인 강등 금지(관리 콘솔에서 자기 권한을 실수로 잃지 않게).
                if username == user["username"]:
                    raise HTTPException(status_code=409, detail="본인 권한은 강등할 수 없습니다.")
                # (b) 마지막 관리자 강등 금지.
                if auth.count_admins() <= 1:
                    raise HTTPException(status_code=409, detail="마지막 관리자는 강등할 수 없습니다.")
    updated = auth.admin_update_user(username, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
    observability.audit(
        "admin.user.update", user=user["username"], target=username, fields=",".join(fields)
    )
    return updated


@app.post("/api/admin/users/{username}/reset-password")
def admin_reset_password(
    username: str, req: AdminResetPasswordRequest, user: dict = Depends(require_admin)
) -> dict:
    """관리자 비번 초기화 → must_change_password=1(대상은 다음 로그인 시 강제변경). 비번 미반환."""
    new_pw = req.newPassword or ""
    if len(new_pw) < MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=400, detail=f"새 비밀번호는 {MIN_PASSWORD_LEN}자 이상이어야 합니다."
        )
    if not auth.admin_reset_password(username, new_pw):
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
    observability.audit("admin.user.reset_password", user=user["username"], target=username)
    return {"ok": True}


@app.post("/api/requirements", status_code=201)
def create_requirement(
    req: RequirementCreateRequest, user: dict = Depends(require_admin)
) -> dict:
    """요구사항/건의 적재(Slack 봇·웹). text 필수, source 기본 'slack'. 생성 행(id 포함) 반환."""
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="요구사항 내용이 비어 있습니다.")
    source = req.source or "slack"
    created = req_store.add(text, source=source, reporter=req.reporter)
    observability.audit(
        "requirement.create",
        user=user["username"],
        reporter=(req.reporter or ""),
        source=source,
    )
    return created


@app.get("/api/requirements")
def list_requirements(
    status: str | None = Query(default=None), user: dict = Depends(require_admin)
) -> dict:
    """요구사항 목록(관리자·웹 조회용). status 지정 시 그 상태만 필터."""
    return {"requirements": req_store.list(status)}


# ---------- 공지사항(웹 콘솔 작성 → Slack 봇 배포) ----------
# 라우트 순서 주의: 정적 경로 /api/notices/latest 를 동적 /{notice_id} 보다 먼저 정의한다.
@app.post("/api/notices", status_code=201)
def create_notice(
    req: NoticeCreateRequest, user: dict = Depends(require_admin)
) -> dict:
    """공지 작성(관리자). body 필수. 생성 행(id 포함) 반환."""
    body = (req.body or "").strip()
    if not body:
        raise HTTPException(status_code=422, detail="공지 내용이 비어 있습니다.")
    title = (req.title or "").strip() or None
    created = notice_store.add(body, title=title, created_by=user["username"])
    observability.audit("notice.create", user=user["username"], notice_id=created["id"])
    return created


@app.get("/api/notices")
def list_notices(user: dict = Depends(require_admin)) -> dict:
    """전체 공지 목록(관리자 콘솔용, 최신 우선).

    noticeChannel: 봇이 공지를 쏘는 대상 채널(SLACK_NOTICE_CHANNEL). 웹·슬랙봇이
    같은 .env.deploy 를 공유하므로 웹 env 로 판별 가능. 미설정(None)이면 봇은
    명령을 실행한 채널로 전송한다(slack_bot/handlers.py) — 프론트가 경고 배지 노출.
    """
    return {
        "notices": notice_store.list(),
        "noticeChannel": os.environ.get("SLACK_NOTICE_CHANNEL") or None,
    }


@app.get("/api/notices/latest")
def latest_notice(user: dict = Depends(require_admin)) -> dict:
    """가장 최근 활성 공지(Slack 봇 `공지` 가 읽음). 없으면 {"notice": null}."""
    return {"notice": notice_store.latest()}


@app.patch("/api/notices/{notice_id}")
def update_notice(
    notice_id: int, req: NoticeUpdateRequest, user: dict = Depends(require_admin)
) -> dict:
    """공지 수정(관리자). 지정 필드만 갱신. body 를 빈 문자열로 지우는 것은 거부(422)."""
    body = req.body.strip() if req.body is not None else None
    if body is not None and not body:
        raise HTTPException(status_code=422, detail="공지 내용은 비울 수 없습니다.")
    title = req.title.strip() or None if req.title is not None else None
    updated = notice_store.update(notice_id, body=body, title=title, active=req.active)
    if updated is None:
        raise HTTPException(status_code=404, detail="공지를 찾을 수 없습니다.")
    observability.audit("notice.update", user=user["username"], notice_id=notice_id)
    return updated


@app.delete("/api/notices/{notice_id}")
def delete_notice(notice_id: int, user: dict = Depends(require_admin)) -> dict:
    """공지 삭제(관리자)."""
    if not notice_store.delete(notice_id):
        raise HTTPException(status_code=404, detail="공지를 찾을 수 없습니다.")
    observability.audit("notice.delete", user=user["username"], notice_id=notice_id)
    return {"ok": True}


@app.post("/api/admin/users", status_code=201)
def admin_create_user(
    req: AdminUserCreateRequest, user: dict = Depends(require_admin)
) -> dict:
    """관리자 신규 사용자 생성. 초기 비번은 서버 기본값(NEW_USER_INITIAL_PASSWORD)이며 대상은
    첫 로그인 시 강제 변경한다(must_change_password=1). username 중복이면 409, 생성된 사용자
    (비번 미포함)를 반환한다.
    """
    username = (req.username or "").strip()
    if not username or any(c.isspace() for c in username):
        raise HTTPException(status_code=422, detail="username 은 공백 없이 입력해야 합니다.")
    if req.role not in ("user", "admin"):
        raise HTTPException(status_code=422, detail="role 은 'user'|'admin' 만 허용됩니다.")
    display_name = username
    if req.displayName is not None:
        display_name = _clean_name(req.displayName, field="displayName", required=False) or username
    if not auth.create_user(
        username, NEW_USER_INITIAL_PASSWORD, display_name=display_name, role=req.role
    ):
        raise HTTPException(status_code=409, detail="이미 존재하는 사용자입니다.")
    observability.audit(
        "admin.user.create", user=user["username"], target=username, role=req.role
    )
    created = next((u for u in auth.list_users() if u["username"] == username), None)
    result = created or {"username": username, "displayName": display_name, "role": req.role}
    # 관리자가 신규 사용자에게 전달할 초기 비번을 1회 반환한다(첫 로그인 시 강제 변경 대상).
    return {**result, "initialPassword": NEW_USER_INITIAL_PASSWORD}


@app.delete("/api/admin/users/{username}")
def admin_delete_user(username: str, user: dict = Depends(require_admin)) -> dict:
    """관리자 사용자 삭제. 계정만 제거하고 소유 회의록은 보존한다(소유자 없는 상태로 잔존).

    가드: (a) 본인 삭제 금지, (b) 마지막 관리자 삭제 금지.
    """
    if username == user["username"]:
        raise HTTPException(status_code=409, detail="본인 계정은 삭제할 수 없습니다.")
    target = users.get(username)
    if target is None:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
    if (target.get("role") or "user") == "admin" and auth.count_admins() <= 1:
        raise HTTPException(status_code=409, detail="마지막 관리자는 삭제할 수 없습니다.")
    if not auth.delete_user(username):
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
    observability.audit("admin.user.delete", user=user["username"], target=username)
    return {"ok": True}


# ---------- 참석자 피커용 디렉터리(인증 사용자 전체) ----------
@app.get("/api/directory")
def get_directory(user: dict = Depends(require_user_active)) -> dict:
    """참석자 피커용 경량 디렉터리([{username, displayName, email}]). 민감필드 미포함."""
    return {"users": auth.list_directory()}


@app.get("/api/admin/metrics")
def admin_metrics(user: dict = Depends(require_admin)) -> dict:
    """운영 관측 스냅샷(관리자 전용) — 카운터·저장량·디스크·정리 설정.

    무거운 모니터링 의존성 없이(P10) 인프로세스 카운터(observability.snapshot)와 디스크 집계를
    한 번에 노출한다. 비밀은 포함하지 않는다. 단일 워커라 카운터는 프로세스 수명 동안 누적된다.
    """
    return {
        "counters": observability.snapshot(),
        "meetings": store.count_meetings(),
        "backups": store.count_backups(),
        "audioBytes": maintenance.audio_storage_bytes(),
        "disk": maintenance.disk_usage(),
        "cleanup": {
            "enabled": CLEANUP_ENABLED,
            "intervalSec": CLEANUP_INTERVAL_SEC,
            "stagingMaxAgeSec": STAGING_MAX_AGE_SEC,
            "backupMaxAgeSec": BACKUP_MAX_AGE_SEC,
        },
        "dbBackup": {
            "enabled": DB_BACKUP_ENABLED,
            "intervalSec": DB_BACKUP_INTERVAL_SEC,
            "keep": DB_BACKUP_KEEP,
            "count": maintenance.db_backup_count(store),
        },
    }


@app.get("/api/health")
def health() -> dict:
    # claude 구독 인증 상태(요약/추출 백엔드가 agent_cli 일 때만 의미 있음). 만료/미로그인
    # 이면 프론트·운영자가 미리 재인증할 수 있게 노출(토큰 값은 절대 포함하지 않음).
    # agent_cli 사용 여부는 호출 시점에 재계산 — 백엔드를 런타임에 재지정해도 실제 값과 일치.
    uses_agent_cli = "agent_cli" in (CLEAN_BACKEND, EXTRACT_BACKEND, SUMMARIZE_BACKEND)
    return {
        "ok": True,
        "clean_backend": CLEAN_BACKEND,
        "extract_backend": EXTRACT_BACKEND,
        "summarize_backend": SUMMARIZE_BACKEND or "off",
        "stt_model": "Cohere transcribe-03-2026",
        "auth_users": users.count(),
        "claude_auth": claude_auth_status() if uses_agent_cli else {"ok": True, "reason": "not_used"},
    }


# ---------- 정적 프론트 서빙(컨테이너; dev에서는 비활성) ----------
if FRONTEND_DIST and Path(FRONTEND_DIST).is_dir():
    from fastapi.staticfiles import StaticFiles
    from starlette.exceptions import HTTPException as _StarletteHTTPException
    from starlette.responses import FileResponse

    _SPA_INDEX = str(Path(FRONTEND_DIST) / "index.html")

    class _SPAStaticFiles(StaticFiles):
        """SPA 폴백 — 존재하지 않는 경로(예: /settings, /settings?google=connected)는 404 대신
        index.html 을 돌려 클라이언트 라우터가 처리하게 한다. OAuth 콜백이 302 로 보내는 /settings
        착지 지점이 백엔드 라우트가 아니어서 FastAPI 404({"detail":"Not Found"})가 뜨던 문제를 해결.
        실제 정적 자산(assets/*)은 그대로 서빙되고, /api/* 오타 등은 404 를 유지한다."""

        async def get_response(self, path, scope):  # type: ignore[override]
            try:
                return await super().get_response(path, scope)
            except _StarletteHTTPException as exc:
                if exc.status_code == 404 and not path.startswith("api"):
                    return FileResponse(_SPA_INDEX)
                raise

    # API 라우트 뒤에 mount → /api/* 가 우선, 나머지는 SPA index.html(폴백 포함).
    app.mount("/", _SPAStaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
