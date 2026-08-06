"""업로드(STT) 경로가 사용자 자격증명을 **LLM 백엔드까지** 전달하는지 — 종단 회귀 테스트.

실사고(2026-08-06): 배포에서 회의록에 전사만 남고 요약·액션이 전부 비었다. 원인은 core 병렬
워커가 ContextVar 자격증명을 잃고 전역 폴백으로 떨어진 것이었다(배포 컨테이너에는 전역 claude
로그인이 없다). 잡은 status='done' 으로 끝나 화면은 실패를 알 수 없었다.

그때 있던 테스트가 못 잡은 이유가 이 파일의 존재 이유다:
  - 단위 테스트는 core 를 직접 불렀다 → 잡 스레드 -> 워커 스레드 경계를 건너뛴다.
  - dev 는 전역 claude 로그인이 있어 폴백이 성공한다 → 로컬에서 증상이 나오지 않는다.
그래서 여기서는 **HTTP 업로드부터** 돌리고, LLM 백엔드가 호출된 순간에 사용자 자격증명이
보였는지를 백엔드가 직접 기록해 검사한다(전역 폴백으로 떨어졌다면 None 이 기록된다).

STT 와 claude 는 호출하지 않는다(전사·LLM 모두 가짜). 임시 DB.
실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_upload_job_credential.py -q
"""
from __future__ import annotations

import base64
import contextlib
import importlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from src.postprocess import orchestrator as orch
from src.postprocess.backends.agent_cli import _active_credential
from src.postprocess.backends.base import LLMBackend, LLMCapabilities

from tests.test_agent_core import _summary_fixture

SECRET = "SENTINEL-NOT-A-REAL-TOKEN"


class _CredentialWatchingBackend(LLMBackend):
    """호출 시점에 보이는 자격증명을 기록하는 가짜 백엔드.

    secret 자체는 남기지 않는다 — 보였는지(type)만 기록한다(로그·assert 에 비밀 금지 규약).
    """

    name = "fake-credwatch"

    def __init__(self):
        self.seen: list[str | None] = []
        self._lock = threading.Lock()

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(json_mode=True, ctx_window=200000, determinism="reproducible")

    def generate(self, messages, schema=None, temperature=0.0, max_tokens=4096, seed=0) -> str:
        cred = _active_credential.get()
        with self._lock:
            self.seen.append(cred["type"] if cred else None)
        system = messages[0]["content"]
        if "실행 과제" in system:
            return json.dumps({"action_items": []}, ensure_ascii=False)
        if "부분 요약" in system or "근거와 대조해" in system or "한국어로 옮기는" in system:
            return json.dumps({}, ensure_ascii=False)
        return json.dumps(_summary_fixture(), ensure_ascii=False)


@contextlib.contextmanager
def _client(*, summarize_backend="agent_cli", extract_backend="agent_cli"):
    from fastapi.testclient import TestClient

    keys = ("JWT_SECRET", "WEB_AUTH_USERS", "WEB_AUTH_ADMINS",
            "WEB_SUMMARIZE_BACKEND", "WEB_EXTRACT_BACKEND", "WEB_CLEAN_BACKEND")
    saved = {k: os.environ.get(k) for k in keys}
    with tempfile.TemporaryDirectory() as td:
        tmp_db = Path(td) / "meetings.db"
        os.environ["JWT_SECRET"] = "test-secret-upload-cred"
        os.environ["WEB_AUTH_USERS"] = "admin:pw1"
        os.environ["WEB_AUTH_ADMINS"] = "admin"
        os.environ["WEB_SUMMARIZE_BACKEND"] = summarize_backend
        os.environ["WEB_EXTRACT_BACKEND"] = extract_backend
        os.environ["WEB_CLEAN_BACKEND"] = "passthrough"
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
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def _headers(auth, appmod, user="admin"):
    appmod.users.set_password(user, "newpassword123")
    return {"Authorization": f"Bearer {auth.make_token(user)}"}


@contextlib.contextmanager
def _fake_stt(appmod, n_segments=6):
    """전사를 가짜로 — GPU·모델 없이 업로드 경로 전체를 돌린다."""
    segs = [
        {"id": i, "start": i * 20.0, "end": (i + 1) * 20.0,
         "text": f"그래서 이번 배포 일정을 다음 주로 확정하기로 했습니다 {i}"}
        for i in range(n_segments)
    ]
    orig = appmod.transcribe_to_segments
    appmod.transcribe_to_segments = lambda *a, **k: (segs, n_segments * 20.0, {"chunkCount": 1})
    try:
        yield segs
    finally:
        appmod.transcribe_to_segments = orig


@contextlib.contextmanager
def _fake_llm(backend):
    orig = orch.get_llm_backend
    orch.get_llm_backend = lambda name=None: backend  # type: ignore[assignment]
    try:
        yield
    finally:
        orch.get_llm_backend = orig  # type: ignore[assignment]


def _run_upload(auth, appmod, client, backend, *, with_credential=True) -> dict:
    """업로드 제출 → 잡 완료까지 폴링 → 최종 폴링 응답 반환."""
    h = _headers(auth, appmod)
    if with_credential:
        auth.set_credential("admin", "oauth_token", SECRET)
    payload = {
        "audioBase64": base64.b64encode(b"fake-audio-bytes").decode(),
        "mimeType": "audio/webm",
        "participants": [],
        "promptTemplate": "t",
    }
    with _fake_stt(appmod), _fake_llm(backend):
        res = client.post("/api/ai/process", json=payload, headers=h)
        assert res.status_code == 200, res.text
        job_id = res.json()["jobId"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            out = client.get(f"/api/ai/jobs/{job_id}", headers=h).json()
            if out["status"] in ("done", "error", "cancelled"):
                return out
            time.sleep(0.05)
    raise AssertionError("잡이 30초 안에 끝나지 않았습니다(가짜 백엔드인데 멈춤)")


# ------------------------------------------------------------------ 핵심 회귀
def test_업로드_경로가_자격증명을_LLM까지_전달한다():
    """실사고의 정확한 재현 지점 — 워커에서 자격증명이 사라지면 여기서 None 이 기록된다."""
    backend = _CredentialWatchingBackend()
    with _client() as (auth, appmod, client):
        out = _run_upload(auth, appmod, client, backend)
    assert out["status"] == "done", out
    assert backend.seen, "LLM 백엔드가 아예 호출되지 않았습니다(요약 단계 미실행)"
    assert all(t == "oauth_token" for t in backend.seen), (
        f"워커에서 자격증명이 소실됐습니다: {backend.seen}"
    )


def test_업로드_결과에_요약이_채워진다():
    """자격증명이 닿아도 산출이 비면 사용자에겐 같은 사고다 — 결과까지 확인한다."""
    backend = _CredentialWatchingBackend()
    with _client() as (auth, appmod, client):
        out = _run_upload(auth, appmod, client, backend)
    summary = out["result"]["summary"]
    assert summary["backend"], "summary.backend 가 비었다 = 요약 단계가 결과를 못 냈다"
    assert summary["agenda"], "안건이 비었다"
    assert out["result"]["transcript"], "전사는 항상 있어야 한다"


def test_요약이_비면_진단이_함께_내려간다():
    """빈 산출 자체는 정상일 수 있다(근거 부족). 다만 원인을 판별할 수 있어야 한다."""
    backend = _CredentialWatchingBackend()
    with _client() as (auth, appmod, client):
        out = _run_upload(auth, appmod, client, backend)
    assert "diag" in out and set(out["diag"]) >= {"summaryEmpty", "callsOk", "failures"}
    assert out["diag"]["callsOk"].get("summarize"), "요약 콜이 성공으로 집계되지 않았다"
    assert not out["diag"]["failures"], f"삼켜진 실패가 있다: {out['diag']['failures']}"


def test_자격증명_미등록이면_전역폴백으로_내려간다():
    """미등록 사용자는 None(전역 폴백)이어야 한다 — 남의 자격증명이 새면 안 된다."""
    backend = _CredentialWatchingBackend()
    with _client() as (auth, appmod, client):
        out = _run_upload(auth, appmod, client, backend, with_credential=False)
    assert out["status"] == "done", out
    assert backend.seen and all(t is None for t in backend.seen), backend.seen


def test_요약백엔드가_꺼져_있으면_전사만_남는다():
    """설정으로 끈 경우는 사고가 아니다 — 전사는 살고 요약은 빈 구조체로 내려간다."""
    backend = _CredentialWatchingBackend()
    with _client(summarize_backend="passthrough", extract_backend="passthrough") as (
        auth, appmod, client
    ):
        out = _run_upload(auth, appmod, client, backend)
    assert out["status"] == "done"
    assert backend.seen == []                       # LLM 미호출
    assert out["result"]["transcript"]
    assert not out["result"]["summary"]["agenda"]


def test_업로드_중_진행이_보고된다():
    """긴 회의의 무정보 대기를 막는 진행 표시가 업로드 경로에도 붙어 있어야 한다."""
    backend = _CredentialWatchingBackend()
    with _client() as (auth, appmod, client):
        auth.set_credential("admin", "oauth_token", SECRET)
        seen_progress: list[dict] = []
        real_reporter = appmod._progress_reporter

        def _spy(job_id):
            inner = real_reporter(job_id)

            def _r(event):
                seen_progress.append(event)
                inner(event)

            return _r

        appmod._progress_reporter = _spy
        try:
            _run_upload(auth, appmod, client, backend)
        finally:
            appmod._progress_reporter = real_reporter
        stages = [e["stage"] for e in seen_progress]
        assert "analyze" in stages and "finalize" in stages, stages
