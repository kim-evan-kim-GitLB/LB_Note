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
import threading
import traceback
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.postprocess.backends.agent_cli import (
    AgentCLIAuthError,
    claude_auth_status,
    use_credential,
)
from src.web.service import extract_action_items, process_audio_to_contract
from src.web.store import MeetingStore
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
    return dt.datetime.now().isoformat(timespec="seconds")


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


# 셀프 비번 변경 시 새 비밀번호 최소 길이.
MIN_PASSWORD_LEN = 8


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
    if not meeting.get("id"):
        meeting["id"] = uuid.uuid4().hex
    meeting["ownerId"] = user["id"]  # 소유자는 토큰에서 강제(클라이언트 위조 방지)
    meeting.setdefault("createdAt", _now_iso())
    meeting["updatedAt"] = _now_iso()
    return store.create(meeting)


@app.patch("/api/meetings/{meeting_id}")
def patch_meeting(meeting_id: str, patch: dict, user: dict = Depends(require_user_active)) -> dict:
    _owned_or_404(meeting_id, user)  # 소유 확인 후에만 수정
    patch.pop("ownerId", None)  # 소유자 변경 불가
    patch["updatedAt"] = _now_iso()
    return store.update(meeting_id, patch)


@app.delete("/api/meetings/{meeting_id}")
def delete_meeting(meeting_id: str, user: dict = Depends(require_user_active)) -> dict:
    _owned_or_404(meeting_id, user)  # 소유 확인 후에만 삭제
    store.delete(meeting_id)
    return {"ok": True}


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
