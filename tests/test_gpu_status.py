"""GPU 여유 경고 회귀 테스트 — 남이 GPU 를 점유해 STT 가 실패할 상황을 미리 알리는가.

배경(실측, 2026-07-28): VRAM 이 모자라면 STT 잡은 대기하지 않고 OOM 으로 죽는다. 모델 로드
실패는 int8 폴백까지 실패해 RuntimeError 가 되고, 디코딩 중 OOM 은 폴백 대상도 아니다. 어느
쪽이든 사용자에게는 'STT 엔진 오류'로만 보여서 엔진 결함과 자원 부족을 구분할 수 없었다.

검증 불변식:
  - required_mb = 모델 + 청크당 VRAM x batch_size (실측 기반 추정).
  - pressure: ok / tight / insufficient / unknown(조회 불가) 로 갈린다.
  - nvidia-smi 가 없거나 실패해도 기능이 막히지 않는다(unknown, 경고 없음).
  - TTL 캐시로 폴링마다 서브프로세스를 부르지 않는다.
  - 제출 응답에 gpuWarning 이 실리고 audit 이 남는다.
  - 대기자 힌트: 우리 STT 슬롯이 비어 있을 때만 '다른 작업이 점유' 로 단정한다.
  - _scan_gpu_pressure: 수준이 바뀔 때만 audit(정상 상태 로그 스팸 방지).

실제 nvidia-smi 미호출 — gpu_status._QUERY 를 갈아끼운다. 임시 DB.
실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_gpu_status.py
"""
from __future__ import annotations

import contextlib
import importlib
import os
import tempfile
from pathlib import Path


def _client_for(td: Path):
    from fastapi.testclient import TestClient

    tmp_db = td / "meetings.db"
    os.environ["JWT_SECRET"] = "test-secret-gpustatus"
    os.environ["WEB_AUTH_USERS"] = "admin:pw1,dev:pw2"
    os.environ["WEB_AUTH_ADMINS"] = "admin"
    os.environ["WEB_AUTH_TOKEN_TTL"] = "3600"
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
        appmod.gpu_status.reset_cache()
        appmod._gpu_pressure_last = None
        with TestClient(appmod.app) as client:
            yield auth, appmod, client
    finally:
        storemod.DEFAULT_DB_PATH = store_orig


@contextlib.contextmanager
def _tmp():
    with tempfile.TemporaryDirectory() as td:
        yield from _client_for(Path(td))


def _headers(auth, appmod, username: str) -> dict:
    appmod.users.set_password(username, "newpassword123")
    return {"Authorization": f"Bearer {auth.make_token(username)}"}


def _fake_query(free_mb: int, total_mb: int = 97887, counter: list | None = None):
    def q():
        if counter is not None:
            counter.append(1)
        return (free_mb, total_mb)
    return q


# ---------- 필요량/압박 수준 ----------
def test_required_mb_tracks_batch_size():
    import src.web.gpu_status as gs

    # 모델 + 컨텍스트 고정비 + 청크당 VRAM x batch
    assert gs.required_mb(8) == gs.MODEL_VRAM_MB + gs.CONTEXT_VRAM_MB + gs.CHUNK_VRAM_MB * 8
    assert gs.required_mb(32) > gs.required_mb(8)
    assert gs.required_mb(0) == gs.required_mb(1)  # 0 이하는 1 로 취급(0 나눗셈/과소추정 방지)
    # 실측 reserved peak(+컨텍스트 약 1GB)을 밑돌지 않아야 한다 — 과소추정하면 OOM 날 상황을
    # 'ok' 로 보고해 경고 자체가 무의미해진다. (실측: bs=8 5,834 / bs=32 7,344 / bs=64 10,326)
    for bs, reserved_peak in ((8, 5834), (32, 7344), (64, 10326)):
        assert gs.required_mb(bs) >= reserved_peak, (bs, gs.required_mb(bs), reserved_peak)


def test_pressure_levels():
    import src.web.gpu_status as gs

    req = gs.required_mb(8)
    for free, expect in ((req - 1, "insufficient"), (int(req * 1.2), "tight"), (req * 5, "ok")):
        gs.reset_cache()
        gs._QUERY = _fake_query(free)
        snap = gs.snapshot(8)
        assert snap["pressure"] == expect, (free, snap)
        assert snap["available"] is True and snap["usedMb"] == snap["totalMb"] - free
    gs._QUERY = gs._query_nvidia_smi
    gs.reset_cache()


def test_query_failure_is_unknown_not_error():
    """nvidia-smi 없음/실패 → unknown. 관측 실패가 본 기능을 막지 않는다."""
    import src.web.gpu_status as gs

    gs.reset_cache()
    gs._QUERY = lambda: None
    snap = gs.snapshot(8)
    assert snap["pressure"] == "unknown" and snap["available"] is False
    assert gs.warning_text(snap) is None  # 판단 불가면 조용히
    assert "requiredMb" in snap  # 추정치는 그대로 노출(진단용)
    gs._QUERY = gs._query_nvidia_smi
    gs.reset_cache()


def test_snapshot_uses_ttl_cache():
    """폴링마다 서브프로세스를 부르지 않는다(TTL 안에서는 1회)."""
    import src.web.gpu_status as gs

    calls: list = []
    gs.reset_cache()
    gs._QUERY = _fake_query(50_000, counter=calls)
    for _ in range(5):
        gs.snapshot(8)
    assert len(calls) == 1
    gs.snapshot(8, force=True)  # force 는 캐시를 무시
    assert len(calls) == 2
    gs._QUERY = gs._query_nvidia_smi
    gs.reset_cache()


def test_warning_text_does_not_blame_others():
    """문구가 점유자를 단정하지 않는다 — 우리 잡이 쓰는 중일 수도 있다."""
    import src.web.gpu_status as gs

    gs.reset_cache()
    gs._QUERY = _fake_query(100)
    txt = gs.warning_text(gs.snapshot(8))
    assert txt and "부족" in txt and "다른 작업" not in txt
    gs._QUERY = gs._query_nvidia_smi
    gs.reset_cache()


# ---------- 앱 배선 ----------
def test_submit_response_carries_gpu_warning_and_audit():
    import base64

    with _tmp() as (auth, appmod, client):
        from src.web import observability

        hd = _headers(auth, appmod, "dev")
        appmod.gpu_status.reset_cache()
        appmod.gpu_status._QUERY = _fake_query(100)  # 여유 거의 없음
        observability.reset()
        payload = base64.b64encode(b"fake-audio").decode()
        r = client.post(
            "/api/ai/process", json={"audioBase64": payload, "mimeType": "audio/wav"}, headers=hd
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "gpuWarning" in body and "부족" in body["gpuWarning"]
        assert body["gpu"]["pressure"] == "insufficient"
        assert observability.snapshot().get("gpu.pressure_on_submit") == 1
        # 사용자별 진단 이력에도 남아 사후 추적 가능
        assert any(e["event"] == "gpu.pressure_on_submit" for e in auth.list_user_events(owner="dev"))
        appmod.gpu_status._QUERY = appmod.gpu_status._query_nvidia_smi
        appmod.gpu_status.reset_cache()


def test_submit_response_has_no_warning_when_ok():
    import base64

    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        appmod.gpu_status.reset_cache()
        appmod.gpu_status._QUERY = _fake_query(80_000)
        payload = base64.b64encode(b"fake-audio").decode()
        body = client.post(
            "/api/ai/process", json={"audioBase64": payload, "mimeType": "audio/wav"}, headers=hd
        ).json()
        assert "gpuWarning" not in body  # 정상일 때는 조용히
        appmod.gpu_status._QUERY = appmod.gpu_status._query_nvidia_smi
        appmod.gpu_status.reset_cache()


def _seed(appmod, job_id: str, owner: str, status: str, phase: str):
    now = appmod.time.monotonic()
    with appmod._jobs_lock:
        appmod._jobs[job_id] = {"status": status}
        appmod._job_owner[job_id] = owner
        appmod._job_meta[job_id] = {
            "kind": "stt", "created_at": now, "started_at": None,
            "phase": phase, "phase_at": now, "warning": None,
        }


def test_waiter_hint_blames_others_only_when_our_slots_are_free():
    """우리 슬롯이 비었는데 여유가 없으면 외부 점유가 확실 → 그때만 '다른 작업' 으로 말한다."""
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        appmod.gpu_status.reset_cache()
        appmod.gpu_status._QUERY = _fake_query(100)
        appmod._inflight["stt"] = 0  # 우리 STT 슬롯 미점유
        _seed(appmod, "mine", "dev", "queued", "waiting_stt")
        body = client.get("/api/ai/jobs/mine", headers=hd).json()
        assert body["gpu"]["pressure"] == "insufficient"
        assert "다른 작업이 GPU" in body["reasonHint"]
        appmod.gpu_status._QUERY = appmod.gpu_status._query_nvidia_smi
        appmod.gpu_status.reset_cache()


def test_waiter_hint_stays_queue_message_when_our_job_holds_gpu():
    """우리 잡이 GPU 를 쥔 탓에 여유가 준 경우까지 '다른 작업' 이라 하면 오진이다."""
    with _tmp() as (auth, appmod, client):
        hd = _headers(auth, appmod, "dev")
        appmod.gpu_status.reset_cache()
        appmod.gpu_status._QUERY = _fake_query(100)
        appmod._inflight["stt"] = 1  # 우리 잡이 슬롯 점유 중
        _seed(appmod, "busy", "admin", "processing", "transcribing")
        _seed(appmod, "mine", "dev", "queued", "waiting_stt")
        body = client.get("/api/ai/jobs/mine", headers=hd).json()
        assert "다른 작업이 GPU" not in body["reasonHint"]
        assert "앞서 진행 중입니다" in body["reasonHint"]
        appmod.gpu_status._QUERY = appmod.gpu_status._query_nvidia_smi
        appmod.gpu_status.reset_cache()


def test_admin_ai_jobs_exposes_gpu():
    with _tmp() as (auth, appmod, client):
        ha = _headers(auth, appmod, "admin")
        appmod.gpu_status.reset_cache()
        appmod.gpu_status._QUERY = _fake_query(1000, total_mb=90_000)
        body = client.get("/api/admin/ai-jobs", headers=ha).json()
        assert body["gpu"]["pressure"] == "insufficient"
        assert body["gpu"]["freeMb"] == 1000 and body["gpu"]["usedMb"] == 89_000
        appmod.gpu_status._QUERY = appmod.gpu_status._query_nvidia_smi
        appmod.gpu_status.reset_cache()


# ---------- 압박 수준 전환 감시 ----------
def test_scan_gpu_pressure_audits_only_on_change():
    with _tmp() as (_auth, appmod, _client):
        from src.web import observability

        gs = appmod.gpu_status
        gs.reset_cache()
        appmod._gpu_pressure_last = None
        observability.reset()

        gs._QUERY = _fake_query(80_000)
        assert appmod._scan_gpu_pressure() is None  # 기동 직후 정상은 기록하지 않는다
        assert observability.snapshot().get("gpu.pressure_change") is None

        gs._QUERY = _fake_query(100)
        assert appmod._scan_gpu_pressure() == "insufficient"
        assert observability.snapshot()["gpu.pressure_change"] == 1
        assert appmod._scan_gpu_pressure() is None  # 같은 수준이 이어지면 추가 기록 없음
        assert observability.snapshot()["gpu.pressure_change"] == 1

        gs._QUERY = _fake_query(80_000)
        assert appmod._scan_gpu_pressure() == "ok"  # 회복도 전환이므로 기록
        assert observability.snapshot()["gpu.pressure_change"] == 2

        gs._QUERY = gs._query_nvidia_smi
        gs.reset_cache()


# ---------- VRAM 부족과 엔진 결함 구분 ----------
def test_is_vram_oom_detects_direct_and_chained():
    """체인까지 훑는다 — 백엔드가 int8 폴백 실패 시 OOM 을 다시 올리며 폴백 오류를 매단다."""
    import torch

    import src.web.app as appmod

    oom = torch.cuda.OutOfMemoryError("CUDA out of memory. Tried to allocate 3.85 GiB")
    assert appmod._is_vram_oom(oom) is True
    # 메시지만으로도 판정(타입이 감싸여 바뀐 경우)
    assert appmod._is_vram_oom(RuntimeError("CUDA out of memory. Tried to allocate 1 GiB")) is True
    # 체인: 표면 예외는 다른 타입이지만 원인이 OOM
    wrapped = RuntimeError("weights 변환 실패")
    wrapped.__cause__ = oom
    assert appmod._is_vram_oom(wrapped) is True
    # 무관한 오류는 False — 엔진 결함까지 VRAM 탓으로 돌리면 진단이 반대로 샌다
    assert appmod._is_vram_oom(ValueError("모델 파일 손상")) is False


def test_is_vram_oom_survives_cyclic_chain():
    """순환 체인이 있어도 무한루프에 빠지지 않는다."""
    import src.web.app as appmod

    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert appmod._is_vram_oom(a) is False


def test_vram_exhausted_hint_differs_from_engine_error():
    """VRAM 부족은 '엔진 점검' 이 아니라 '잠시 후 재시도' 로 안내해야 한다(조치가 다르다)."""
    with _tmp() as (_auth, appmod, _client):
        h = appmod._job_reason_hint
        vram = h({
            "status": "error", "error_code": "stt_vram_exhausted",
            "gpu": {"freeMb": 1200, "requiredMb": 8700},
        })
        assert "GPU 메모리가 부족" in vram and "1200MB" in vram and "8700MB" in vram
        assert "엔진 점검" not in vram
        engine = h({"status": "error", "error_code": "stt_engine_error"})
        assert "엔진 점검" in engine
        # gpu 정보가 없어도 문구는 나온다(수치만 생략)
        assert "GPU 메모리가 부족" in h({"status": "error", "error_code": "stt_vram_exhausted"})


def test_startup_records_effective_stt_config():
    """기동 시 실효 설정을 남긴다 — 배포 환경파일이 코드 기본값을 덮어도 로그로 드러나게."""
    with _tmp() as (auth, appmod, _client):
        rows = auth.list_user_events(event_prefix="stt.config")
        # owner 없는 이벤트라 사용자별 이력에는 안 쌓인다 → 카운터로 확인
        from src.web import observability

        assert observability.snapshot().get("stt.config", 0) >= 1
        assert rows == []  # owner 없는 audit 은 사용자 이력에 적재되지 않는다(기존 규약)
        assert appmod.STT_CONCURRENCY >= 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_gpu_status ({len(fns)} cases)")
