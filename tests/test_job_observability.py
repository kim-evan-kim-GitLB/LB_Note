"""STT/요약 잡 관측성(무한루프 원인 구분) 회귀 테스트.

검증 불변식:
  - phase 전환(waiting_stt→transcribing→waiting_llm→summarizing)이 큐 스냅샷에 반영된다.
  - _queue_snapshot_locked: 진행 중 잡만 슬롯 점유/대기로 집계.
  - _scan_stt_stalls: phase=transcribing 이 임계 초과 시 warning=stt_stalled(소프트),
    STT_STALL_MARK_ERROR=1 이면 error(stt_stalled)로 마감.
  - _job_reason_hint: 상태/phase/error_code → 사람이 읽는 원인 힌트(경합/전사/요약/스톨/엔진/인증).
  - GET /api/ai/jobs/{id}: phase/elapsedSec/queue/reasonHint 노출, 소유격리 유지.
  - GET /api/admin/ai-jobs: admin 전용(403/401), queue+active 스냅샷.

실제 STT/claude 미호출 — _jobs/_job_meta 를 직접 시드해 상태만 검증. 임시 DB.
실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_job_observability.py
"""
from __future__ import annotations

import contextlib
import importlib
import os
import tempfile
from pathlib import Path


def _client_for(td: Path, users: str = "admin:pw1,dev:pw2"):
    from fastapi.testclient import TestClient

    tmp_db = td / "meetings.db"
    os.environ["JWT_SECRET"] = "test-secret-jobobs"
    os.environ["WEB_AUTH_USERS"] = users
    os.environ["WEB_AUTH_ADMINS"] = "admin"
    os.environ["WEB_AUTH_TOKEN_TTL"] = "3600"
    os.environ.pop("CRED_ENC_KEY", None)
    import src.web.store as storemod

    store_orig = storemod.DEFAULT_DB_PATH
    try:
        storemod.DEFAULT_DB_PATH = tmp_db
        import src.web.auth as auth
        importlib.reload(auth)
        auth.DEFAULT_DB_PATH = tmp_db
        import src.web.audio_store as audio_store
        importlib.reload(audio_store)
        import src.web.app as appmod
        importlib.reload(appmod)
        with TestClient(appmod.app) as client:
            yield auth, appmod, client
    finally:
        storemod.DEFAULT_DB_PATH = store_orig


@contextlib.contextmanager
def _tmp(users: str = "admin:pw1,dev:pw2"):
    with tempfile.TemporaryDirectory() as td:
        yield from _client_for(Path(td), users)


def _headers(auth, appmod, username: str) -> dict:
    appmod.users.set_password(username, "newpassword123")
    return {"Authorization": f"Bearer {auth.make_token(username)}"}


def _seed(appmod, job_id: str, owner: str, status: str, kind: str, phase: str, *, age: float = 0.0):
    """잡을 직접 시드(_jobs/_job_owner/_job_meta). age=현 phase 경과 초(스톨 테스트용)."""
    now = appmod.time.monotonic()
    with appmod._jobs_lock:
        appmod._jobs[job_id] = {"status": status}
        appmod._job_owner[job_id] = owner
        appmod._job_meta[job_id] = {
            "kind": kind, "created_at": now - age, "started_at": now - age if phase != "waiting_stt" else None,
            "phase": phase, "phase_at": now - age, "warning": None,
        }


# ---------- 큐 스냅샷 / phase ----------
def test_queue_snapshot_counts_by_phase():
    with _tmp() as (_auth, appmod, _client):
        # active(점유)는 실제 슬롯 카운터(_inflight)로, 대기(queued)는 phase 로 집계.
        appmod._inflight["stt"] = 1
        appmod._inflight["llm"] = 1
        _seed(appmod, "b", "dev", "queued", "stt", "waiting_stt")
        _seed(appmod, "d", "dev", "queued", "stt", "waiting_llm")
        with appmod._jobs_lock:
            snap = appmod._queue_snapshot_locked()
        assert snap["sttActive"] == 1 and snap["sttQueued"] == 1
        assert snap["llmActive"] == 1 and snap["llmQueued"] == 1
        assert snap["sttSlots"] == appmod.STT_CONCURRENCY


def test_stalled_error_job_still_counts_slot():
    """CR#4: 스톨로 error 마감돼도 워커가 슬롯을 쥐고 있으면 sttActive 에 그대로 잡힌다."""
    with _tmp() as (_auth, appmod, _client):
        appmod._inflight["stt"] = 1  # 워커가 STT 임계구역 점유 중(아직 transcribe 반환 전)
        _seed(appmod, "stuck", "dev", "error", "stt", "transcribing")  # 스톨 error 마감된 잡
        appmod._job_meta["stuck"]["warning"] = "stt_stalled"
        with appmod._jobs_lock:
            snap = appmod._queue_snapshot_locked()
        assert snap["sttActive"] == 1  # 상태가 error 여도 점유 슬롯이 누락되지 않음


# ---------- 스톨 스캔 ----------
def test_scan_stt_stall_soft_warns():
    with _tmp() as (_auth, appmod, _client):
        appmod.STT_STALL_MARK_ERROR = False
        appmod.STT_STALL_SEC = 100.0
        _seed(appmod, "slow", "dev", "processing", "stt", "transcribing", age=999.0)
        _seed(appmod, "fresh", "dev", "processing", "stt", "transcribing", age=5.0)
        newly = appmod._scan_stt_stalls()
        assert [n["job"] for n in newly] == ["slow"]
        assert appmod._job_meta["slow"]["warning"] == "stt_stalled"
        assert appmod._jobs["slow"]["status"] == "processing"  # 소프트 — 마감 안 함
        assert appmod._job_meta["fresh"]["warning"] is None
        # 재스캔은 중복 보고 안 함(최초만)
        assert appmod._scan_stt_stalls() == []


def test_scan_stt_stall_mark_error():
    with _tmp() as (_auth, appmod, _client):
        appmod.STT_STALL_MARK_ERROR = True
        appmod.STT_STALL_SEC = 100.0
        _seed(appmod, "stuck", "dev", "processing", "stt", "transcribing", age=999.0)
        appmod._scan_stt_stalls()
        assert appmod._jobs["stuck"]["status"] == "error"
        assert appmod._jobs["stuck"]["error_code"] == "stt_stalled"


# ---------- 원인 힌트 ----------
def test_reason_hint_mapping():
    with _tmp() as (_auth, appmod, _client):
        h = appmod._job_reason_hint
        assert "인증" in h({"status": "error", "error_code": "claude_auth_expired"})
        assert "엔진" in h({"status": "error", "error_code": "stt_engine_error"})
        assert "스톨" in h({"status": "processing", "phase": "transcribing", "warning": "stt_stalled"})
        assert "전사" in h({"status": "processing", "phase": "transcribing"})
        assert "요약" in h({"status": "processing", "phase": "summarizing"})
        assert "2건" in h({"status": "queued", "phase": "waiting_stt", "ahead": 2})
        assert h({"status": "done"}) is None


# ---------- 폴링 엔드포인트 ----------
def test_ai_job_endpoint_exposes_phase_and_queue():
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        dev_id = "dev"  # user["id"] == username
        appmod._inflight["stt"] = 1  # 워커가 STT 슬롯 점유 중
        _seed(appmod, "j1", dev_id, "processing", "stt", "transcribing")
        r = client.get("/api/ai/jobs/j1", headers=hd)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["phase"] == "transcribing"
        assert body["queue"]["sttActive"] == 1
        assert "elapsedSec" in body and "전사" in body["reasonHint"]
        # 소유격리 — 타인(admin)은 404
        ha = _headers(auth, appmod, "admin")
        assert client.get("/api/ai/jobs/j1", headers=ha).status_code == 404


def test_ai_job_endpoint_ahead_position():
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        appmod._inflight["stt"] = 1  # 활성 STT 슬롯 1 점유 중
        _seed(appmod, "active", "dev", "processing", "stt", "transcribing")
        # dev 의 대기 잡 앞에 더 먼저 생성된 대기 잡 하나
        _seed(appmod, "earlier", "admin", "queued", "stt", "waiting_stt", age=50.0)
        _seed(appmod, "mine", "dev", "queued", "stt", "waiting_stt", age=1.0)
        body = client.get("/api/ai/jobs/mine", headers=hd).json()
        assert body["phase"] == "waiting_stt"
        # 앞: 활성 1(transcribing) + 더 먼저 대기 1 = 2
        assert body["ahead"] == 2 and "2건" in body["reasonHint"]


def test_admin_ai_jobs_gate_and_snapshot():
    with _tmp() as (auth, appmod, client):
        ha = _headers(auth, appmod, "admin")
        hd = _headers(auth, appmod, "dev")
        assert client.get("/api/admin/ai-jobs", headers=hd).status_code == 403
        assert client.get("/api/admin/ai-jobs").status_code == 401
        appmod._inflight["stt"] = 1  # 활성 STT 슬롯 점유 중
        _seed(appmod, "x", "dev", "processing", "stt", "transcribing")
        r = client.get("/api/admin/ai-jobs", headers=ha)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["activeCount"] == 1 and body["queue"]["sttActive"] == 1
        assert body["active"][0]["jobId"] == "x" and "sttStallSec" in body


def test_reason_hint_no_false_stall_on_terminal():
    """CR#1: 스톨 후 회복돼 완료/취소된 잡엔 스톨 힌트가 붙지 않는다(오경보 방지)."""
    with _tmp() as (_auth, appmod, _client):
        h = appmod._job_reason_hint
        assert h({"status": "done", "phase": "transcribing", "warning": "stt_stalled"}) is None
        assert h({"status": "cancelled", "warning": "stt_stalled"}) is None
        # 진행 중이면 여전히 스톨 힌트 노출
        assert "스톨" in h({"status": "processing", "phase": "transcribing", "warning": "stt_stalled"})


def test_ai_job_done_drops_warning():
    """CR#1: done 잡 응답은 warning=None·reasonHint=None(성공 회의에 거짓 경보 없음)."""
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        _seed(appmod, "d1", "dev", "done", "stt", "transcribing")
        appmod._job_meta["d1"]["warning"] = "stt_stalled"  # 스톨 후 회복·완료된 잡
        body = client.get("/api/ai/jobs/d1", headers=hd).json()
        assert body["warning"] is None and body["reasonHint"] is None


def test_scan_stt_stall_mark_error_sets_cancel():
    """CR#3: MARK_ERROR 스톨 마감 시 cancel 이벤트도 set → 워커의 done 덮어쓰기 차단."""
    with _tmp() as (_auth, appmod, _client):
        appmod.STT_STALL_MARK_ERROR = True
        appmod.STT_STALL_SEC = 100.0
        ev = appmod.threading.Event()
        _seed(appmod, "stuck", "dev", "processing", "stt", "transcribing", age=999.0)
        appmod._job_cancels["stuck"] = ev
        appmod._scan_stt_stalls()
        assert appmod._jobs["stuck"]["error_code"] == "stt_stalled"
        assert ev.is_set()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_job_observability ({len(fns)} cases)")
