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


# ---------- 비동기 AI 잡 (STT는 장시간 → 잡 + 폴링) ----------
# 메모리 잡 테이블. 영속(meeting 저장)은 프론트가 결과를 받아 /api/meetings 로 한다
# (프론트의 기존 process→save 흐름 보존 → 프론트 변경 최소화).
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _run_ai_job(job_id: str, audio_bytes: bytes, mime_type: str | None) -> None:
    """STT+정제 → 잡 결과(contract) 저장. 실패해도 멈추지 않고 status=error(설계 폴백 원칙)."""
    try:
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
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        with _jobs_lock:
            _jobs[job_id] = {"status": "error", "error": f"{type(e).__name__}: {e}"}


@app.post("/api/ai/process")
def ai_process(req: ProcessRequest, user: dict = Depends(auth.require_user)) -> dict:
    """오디오 제출 → 백그라운드 STT 잡 등록 → {jobId} 즉시 반환. 프론트는 GET 잡 폴링."""
    import base64
    try:
        audio_bytes = base64.b64decode(req.audioBase64)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"audioBase64 디코딩 실패: {e}")
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="빈 오디오")

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "processing"}
    threading.Thread(
        target=_run_ai_job, args=(job_id, audio_bytes, req.mimeType), daemon=True
    ).start()
    return {"jobId": job_id, "status": "processing"}


@app.get("/api/ai/jobs/{job_id}")
def ai_job(job_id: str, user: dict = Depends(auth.require_user)) -> dict:
    """잡 상태/결과 폴링. status: processing | done(result) | error(error)."""
    with _jobs_lock:
        j = _jobs.get(job_id)
    if j is None:
        raise HTTPException(status_code=404, detail="job 없음")
    return {"jobId": job_id, **j}


@app.post("/api/ai/extract-actions")
def ai_extract_actions(req: ExtractRequest, user: dict = Depends(auth.require_user)) -> list[str]:
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
    try:
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
def list_meetings(user: dict = Depends(auth.require_user)) -> list[dict]:
    return store.list(owner_id=user["id"])


@app.get("/api/meetings/{meeting_id}")
def get_meeting(meeting_id: str, user: dict = Depends(auth.require_user)) -> dict:
    return _owned_or_404(meeting_id, user)


@app.post("/api/meetings")
def create_meeting(meeting: dict, user: dict = Depends(auth.require_user)) -> dict:
    if not meeting.get("id"):
        meeting["id"] = uuid.uuid4().hex
    meeting["ownerId"] = user["id"]  # 소유자는 토큰에서 강제(클라이언트 위조 방지)
    meeting.setdefault("createdAt", _now_iso())
    meeting["updatedAt"] = _now_iso()
    return store.create(meeting)


@app.patch("/api/meetings/{meeting_id}")
def patch_meeting(meeting_id: str, patch: dict, user: dict = Depends(auth.require_user)) -> dict:
    _owned_or_404(meeting_id, user)  # 소유 확인 후에만 수정
    patch.pop("ownerId", None)  # 소유자 변경 불가
    patch["updatedAt"] = _now_iso()
    return store.update(meeting_id, patch)


@app.delete("/api/meetings/{meeting_id}")
def delete_meeting(meeting_id: str, user: dict = Depends(auth.require_user)) -> dict:
    _owned_or_404(meeting_id, user)  # 소유 확인 후에만 삭제
    store.delete(meeting_id)
    return {"ok": True}


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "clean_backend": CLEAN_BACKEND,
        "extract_backend": EXTRACT_BACKEND,
        "summarize_backend": SUMMARIZE_BACKEND or "off",
        "stt_model": "Cohere transcribe-03-2026",
        "auth_users": users.count(),
    }


# ---------- 정적 프론트 서빙(컨테이너; dev에서는 비활성) ----------
if FRONTEND_DIST and Path(FRONTEND_DIST).is_dir():
    from fastapi.staticfiles import StaticFiles
    # API 라우트 뒤에 mount → /api/* 가 우선, 나머지는 SPA index.html.
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
