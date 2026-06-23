"""FastAPI 서비스 — meetscript-ai 프론트엔드의 온프렘 백엔드.

프론트 계약(`/api/ai/*`) + 영속(`/api/meetings*`, SQLite)을 노출한다. Gemini/Firebase 대체.

STT는 장시간이라 `/api/ai/process`는 **비동기**다: meeting을 status=processing으로 만들고
백그라운드 스레드에서 STT+정제를 돌린 뒤 status=review로 갱신한다. 프론트는 GET 폴링.

컨테이너 대비: 빌드된 프론트(`dist/`)가 있으면 같은 앱이 정적 서빙도 한다(단일 컨테이너).
dev에서는 dist가 없어 Vite가 프론트를 서빙하고 이 앱은 /api 만 담당한다.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import threading
import traceback
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.web import audio_store

from src.postprocess.backends.agent_cli import (
    AgentCLIAuthError,
    claude_auth_status,
    use_credential,
)
from src.postprocess.web_contract import (
    TranscriptStructureError,
    validate_transcript_edit,
)
from src.web.service import extract_action_items, process_audio_to_contract
from src.web.store import MeetingStore, PreconditionFailedError
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
# 빌드된 프론트 정적 경로(컨테이너). 없으면 정적 서빙 비활성(dev=Vite).
FRONTEND_DIST = os.environ.get("WEB_FRONTEND_DIST", "")

app = FastAPI(title="meetscript-ai on-prem backend", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # dev: Vite(:3000)에서 호출. 컨테이너 동일출처면 무관.
    allow_methods=["*"],
    allow_headers=["*"],
)

store = MeetingStore()
users = auth.init()  # users 테이블 준비 + WEB_AUTH_USERS 시드/동기화


def _now_iso() -> str:
    """create/update 타임스탬프. UTC·마이크로초로 store 의 ETag 포맷과 통일(M1).

    create 직후 동일 마이크로초에 PATCH 가 와도 store._next_etag 가 단조 증가를 보장하므로,
    여기서는 마이크로초 정밀도만 맞춰 두면 충분하다(초 단위 → 마이크로초 통일)."""
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


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


# 셀프 비번 변경 시 새 비밀번호 최소 길이.
MIN_PASSWORD_LEN = 8

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
    except AgentCLIAuthError as e:
        return {"ok": False, "detail": f"인증 실패: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"검증 호출 실패: {type(e).__name__}: {e}"}
    if not (out or "").strip():
        return {"ok": False, "detail": "빈 응답(인증/모델 응답 확인 필요)"}
    return {"ok": True, "detail": "검증 호출 성공"}


@app.get("/api/settings/claude-credential")
def get_claude_credential(user: dict = Depends(require_user_active)) -> dict:
    """현재 사용자 자격증명 상태(secret 비노출): {configured, type, updated_at}."""
    return auth.credential_status(user["username"])


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
    return {"status": auth.credential_status(user["username"]), "verification": verification}


@app.delete("/api/settings/claude-credential")
def delete_claude_credential(user: dict = Depends(require_user_active)) -> dict:
    """현재 사용자 자격증명 삭제 → 전역 폴백으로 복귀."""
    cleared = auth.clear_credential(user["username"])
    return {"ok": True, "cleared": cleared, "status": auth.credential_status(user["username"])}


# ---------- 비동기 AI 잡 (STT는 장시간 → 잡 + 폴링) ----------
# 메모리 잡 테이블. 영속(meeting 저장)은 프론트가 결과를 받아 /api/meetings 로 한다
# (프론트의 기존 process→save 흐름 보존 → 프론트 변경 최소화).
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

# 동시 STT 추론 제한(백프레셔). GPU 1장에 요청마다 모델을 load 하므로, 동시에 N개만 돌리고
# 나머지는 대기시킨다 → OOM/연산 경합 방지. 대기 중 잡 status='queued'(프론트가 "처리 대기 중…" 표시).
# 기본 1(직렬). WEB_STT_CONCURRENCY 로 조정(예: VRAM 여유 시 2). (모델 상주/유휴 언로드는 v2.)
_stt_semaphore = threading.Semaphore(max(1, int(os.environ.get("WEB_STT_CONCURRENCY", "1"))))


def _run_ai_job(
    job_id: str, audio_bytes: bytes, mime_type: str | None, credential: dict | None
) -> None:
    """STT+정제 → 잡 결과(contract) 저장. 실패해도 멈추지 않고 status=error(설계 폴백 원칙).

    credential(현재 사용자 자격증명, secret 포함)은 use_credential 로 이 스레드 컨텍스트에만
    심어 agent_cli 백엔드가 사용자별 인증으로 호출하게 한다(스레드별 ContextVar 격리). None 이면
    전역 폴백. 새 Thread 는 부모 ContextVar 를 자동 상속하지 않으므로 여기서 명시 설정한다.
    """
    # 동시성 슬롯 확보까지 대기(잡 status 는 'queued' 유지). 확보하면 'processing' 으로 전환.
    # 한 번에 _stt_semaphore 한도(기본 1)만 실제 추론, 나머지는 여기서 블록되어 큐처럼 동작한다.
    # 락 순서 규약: 이 잡 스레드는 _stt_semaphore 보유 중 store._lock(update_if_match)을 잡지
    # 않는다 — STT 추론은 store 비접촉이고 _jobs(인메모리)만 갱신한다(데드락/장시간 점유 방지).
    with _stt_semaphore:
        with _jobs_lock:
            _jobs[job_id] = {"status": "processing"}
        try:
            with use_credential(credential):
                contract = process_audio_to_contract(
                    audio_bytes,
                    mime_type=mime_type,
                    backend_name=CLEAN_BACKEND,
                    extract_backend_name=EXTRACT_BACKEND,
                    summarize_backend_name=SUMMARIZE_BACKEND or None,
                )
            result = {
                "summary": contract.get("summary", {}),  # 구조체(dict) 계약 — 빈 기본값도 객체
                "actionItems": contract.get("actionItems", []),
                "transcript": contract.get("transcript", []),
                "duration": _fmt_duration(contract.get("_duration_seconds")),
            }
            with _jobs_lock:
                _jobs[job_id] = {"status": "done", "result": result}
        except AgentCLIAuthError as e:
            # 인증 만료/미로그인: 일반 실패와 구분해 error_code 를 실어 프론트가 "재인증" 흐름을
            # 안내하게 한다(STT 는 됐어도 요약/추출 백엔드 claude 인증이 끊긴 상태).
            traceback.print_exc()
            with _jobs_lock:
                _jobs[job_id] = {
                    "status": "error",
                    "error": str(e),
                    "error_code": "claude_auth_expired",
                }
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            with _jobs_lock:
                _jobs[job_id] = {"status": "error", "error": f"{type(e).__name__}: {e}"}


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
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued"}
    threading.Thread(
        target=_run_ai_job,
        args=(job_id, audio_bytes, req.mimeType, credential),
        daemon=True,
    ).start()
    return {"jobId": job_id, "status": "queued"}


@app.get("/api/ai/jobs/{job_id}")
def ai_job(job_id: str, user: dict = Depends(require_user_active)) -> dict:
    """잡 상태/결과 폴링. status: processing | done(result) | error(error)."""
    with _jobs_lock:
        j = _jobs.get(job_id)
    if j is None:
        raise HTTPException(status_code=404, detail="job 없음")
    return {"jobId": job_id, **j}


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
def _owned_or_404(meeting_id: str, user: dict) -> dict:
    """meeting 조회 + 소유자 확인. 없거나 남의 것이면 404(존재 자체를 숨김)."""
    m = store.get(meeting_id)
    if m is None or m.get("ownerId") != user["id"]:
        raise HTTPException(status_code=404, detail="meeting 없음")
    return m


@app.get("/api/meetings")
def list_meetings(user: dict = Depends(require_user_active)) -> list[dict]:
    return store.list(owner_id=user["id"])


@app.get("/api/meetings/{meeting_id}")
def get_meeting(meeting_id: str, user: dict = Depends(require_user_active)) -> dict:
    return _owned_or_404(meeting_id, user)


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

    # 오디오 bind(옵션B). audioStagingToken 은 meeting JSON 에 영속하지 않는다(메타는 audioRef).
    token = meeting.pop("audioStagingToken", None)
    if token:
        _require_hex32(token, what="audioStagingToken")
        audio_ref = audio_store.bind_staging(token, meeting["id"])
        if audio_ref is not None:
            meeting["audioRef"] = audio_ref
    return store.create(meeting)


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

    transcript 구조보존(편집 시에만): 저장본에 비어있지 않은 transcript 가 있고 patch 에
    transcript 가 포함되면 개수·timestamp·speakerId 불변을 검증(위반 시 422)하고 text 가
    바뀐 엔트리에 edited=True 를 서버가 set 한다(클라 제공 edited 무시). summary/actionItems
    구조검증은 이 Phase 비대상이며, transcript 검증이 다른 필드 동시 patch 를 막지 않는다.

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
        """락 안에서 재조회한 저장본(stored) 기준 transcript 구조검증(M2).

        patch 에 transcript 가 있고 저장본 transcript 가 비어있지 않을 때만 적용한다(후방호환).
        검증 실패(TranscriptStructureError)는 락 밖으로 전파되어 422 로 변환된다."""
        if "transcript" not in p:
            return p
        stored_tr = stored.get("transcript") or []
        if not stored_tr:  # 초기 빈 상태 → 구조검증 미적용(0→N 채우기 허용)
            return p
        normalized = validate_transcript_edit(stored_tr, p.get("transcript") or [])
        out = dict(p)
        out["transcript"] = normalized
        return out

    try:
        updated = store.update_if_match(meeting_id, patch, expected, validator=_validator)
    except TranscriptStructureError as e:
        # M2: 락 안 검증 실패 — 검증에 쓴 스냅샷 == write 대상 스냅샷 보장하에 422.
        raise HTTPException(status_code=422, detail=str(e))
    except PreconditionFailedError as e:
        # 412: 프론트가 현재 값 재조회·재적용하도록 현재 updatedAt(ETag)을 힌트로 제공.
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
    response.headers["ETag"] = f'"{updated["updatedAt"]}"'
    return updated


@app.delete("/api/meetings/{meeting_id}")
def delete_meeting(meeting_id: str, user: dict = Depends(require_user_active)) -> dict:
    """회의 삭제 + 원본 오디오 동반 삭제(보존=회의 수명 동일).

    meetingId 화이트리스트(^[0-9a-f]{32}$) 통과분만 오디오 디렉토리 경로를 조립한다(traversal 차단).
    형식 위반 meetingId 는 오디오 삭제를 건너뛴다(DB 삭제만; 그런 id 는 오디오가 있을 수 없음).
    """
    _owned_or_404(meeting_id, user)  # 소유 확인 후에만 삭제
    store.delete(meeting_id)
    if _HEX32_RE.match(meeting_id):
        audio_store.delete_meeting_audio(meeting_id)
    return {"ok": True}


# ---------- 원본 오디오 영속(플랜 v4 트랙 C·Phase 4, D7-id 옵션B) ----------
@app.post("/api/meetings/audio/staging")
async def upload_audio_staging(
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
    """
    # Content-Length 선검사: 명백한 초과는 바디를 받기 전에 조기 거부(DoS 완화).
    if content_length is not None and content_length > audio_store.MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"오디오가 너무 큽니다(최대 {audio_store.MAX_AUDIO_BYTES} bytes).",
        )
    try:
        token, ext, size = audio_store.save_staging_stream(
            file.file.read, mime_type=file.content_type, filename=file.filename
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


@app.get("/api/meetings/{meeting_id}/audio")
def get_meeting_audio(
    meeting_id: str,
    user: dict = Depends(require_user_active),
    range_header: str | None = Header(default=None, alias="Range"),
) -> Response:
    """회의 원본 오디오 스트리밍. 인증 + 소유자 검증 + meetingId 화이트리스트 + HTTP Range(206).

    audioRef 없거나 파일 부재면 404. 소유자 아니면 404(_owned_or_404, 존재 자체 숨김).
    meetingId 가 ^[0-9a-f]{32}$ 아니면 400(경로조립 traversal 차단). Range 미지정 시 전체(200).
    """
    _require_hex32(meeting_id, what="meetingId")  # 경로조립 전 화이트리스트(traversal 차단)
    m = _owned_or_404(meeting_id, user)  # 인증 + 소유자 격리
    path = audio_store.meeting_audio_path(meeting_id, m.get("audioRef"))
    if path is None:
        raise HTTPException(status_code=404, detail="오디오 없음")
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


@app.get("/api/health")
def health() -> dict:
    # claude 구독 인증 상태(요약/추출 백엔드가 agent_cli 일 때만 의미 있음). 만료/미로그인
    # 이면 프론트·운영자가 미리 재인증할 수 있게 노출(토큰 값은 절대 포함하지 않음).
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
    # API 라우트 뒤에 mount → /api/* 가 우선, 나머지는 SPA index.html.
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
