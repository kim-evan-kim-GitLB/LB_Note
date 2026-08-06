"""장시간 요약의 진행 표시 계약 — src.progress 채널 + 잡 폴링 노출 + 사용자 문구.

실사고(2026-08-06): 50분 회의 재요약이 12.7분 걸리는 동안 phase 는 'summarizing' 하나로 고정,
세부 진행은 전혀 없었다. 화면은 스피너만 돌아 "멈춤"과 "오래 걸림"을 구분할 수 없었고, 프론트는
고정 30분 벽시계로 폴링을 끊어 **서버는 성공하는데 화면만 실패**할 수 있는 상태였다.

검증 불변식:
  - 진행 보고는 파이프라인을 **절대 죽이지 않는다**(콜백이 던져도 회의는 끝난다).
  - 보고하지 않은 실행(도구·CLI·테스트)에서도 core 는 그대로 동작한다.
  - 단계가 바뀌면 이전 단계의 세부 진행(창 k/n)은 무효다 — 옛 숫자를 남기면 거짓 진행이 된다.
  - **문구는 서버가 확정한다**(게이트 label 과 같은 규약). 단계를 추가하고 문구를 빠뜨려도
    빈 문구는 나가지 않는다.
  - 폴링 응답의 progress 는 프론트가 "잡이 살아있다"를 판단하는 근거다 → 진행 중에만 싣는다.

실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_job_progress.py -q
"""
from __future__ import annotations

import contextlib
import importlib
import os
import tempfile
import time
from pathlib import Path

import pytest

from src import progress
from src.postprocess import orchestrator as orch

from tests.test_agent_core import _cfg, _FakeBackend, _patched, _segs, _summary_fixture


@contextlib.contextmanager
def _collect():
    """진행 이벤트를 모으는 리포터를 심는다."""
    events: list[dict] = []
    with progress.use_progress(events.append):
        yield events


# ------------------------------------------------------------------ 채널 자체
def test_리포터_없으면_무동작():
    """도구·CLI·테스트가 core 를 그냥 호출하는 경로 — 보고가 없어도 예외가 없어야 한다."""
    progress.report("analyze", done=1, total=2)  # 예외 없이 지나가야 한다


def test_블록을_벗어나면_원복된다():
    """리포터가 누수되면 다음 회의의 진행이 앞 회의 잡에 기록된다."""
    with _collect() as events:
        progress.report("analyze", done=1, total=2)
    progress.report("analyze", done=2, total=2)  # 블록 밖 — 기록되지 않아야 한다
    assert events == [{"stage": "analyze", "done": 1, "total": 2}]


def test_done_total_없으면_키를_싣지_않는다():
    with _collect() as events:
        progress.report("critic")
    assert events == [{"stage": "critic"}]


def test_콜백이_던져도_파이프라인은_계속된다():
    """관측 때문에 회의가 실패하면 안 된다."""

    def boom(_event):
        raise RuntimeError("리포터 고장")

    with progress.use_progress(boom):
        progress.report("analyze", done=1, total=1)  # 예외가 새어나오면 실패


# ------------------------------------------------------------------ core 연동
def test_core가_단계를_순서대로_보고한다():
    backend = _FakeBackend(summary=_summary_fixture(), actions={"action_items": []})
    with _patched(backend), _collect() as events:
        orch.run_meeting_core(_segs(4), summarize_backend="fake", extract_backend="fake")
    stages = [e["stage"] for e in events]
    assert stages[0] == "analyze"                      # 가장 오래 걸리는 단계부터
    assert stages[-1] == "finalize"                    # 마지막은 결정적 마무리
    for later in ("localize", "finalize"):
        assert later in stages
    # analyze 는 창이 끝날 때마다 누적 보고 → done 이 단조 증가하고 total 과 같아야 한다.
    analyze = [e for e in events if e["stage"] == "analyze"]
    assert analyze[0]["done"] == 0                     # 시작 시 0 을 먼저 알린다(즉시 표시용)
    assert [e["done"] for e in analyze] == sorted(e["done"] for e in analyze)
    assert analyze[-1]["done"] == analyze[-1]["total"] == 2   # 요약 1창 + 추출 1창


def test_긴_회의는_창_수만큼_보고한다():
    """창이 여러 개면 그 수만큼 진행이 올라가야 한다 — 이게 사용자가 보는 움직이는 숫자다.

    긴 회의일수록 이 표시가 중요하다(창 수 x LLM 콜 = 대기 시간).
    """
    backend = _FakeBackend(summary=_summary_fixture(), actions={"action_items": []})
    with (
        _cfg(CORE_WINDOW_SEGMENTS=5, CORE_WINDOW_OVERLAP=1, CORE_MULTI_TOPIC_SEGMENTS=1000),
        _patched(backend),
        _collect() as events,
    ):
        orch.run_meeting_core(_segs(20), summarize_backend="fake", extract_backend="fake")
    analyze = [e for e in events if e["stage"] == "analyze"]
    assert analyze[-1]["total"] > 2                    # 창 분할이 일어났다(요약창 + 추출창)
    assert analyze[-1]["done"] == analyze[-1]["total"]
    assert len(analyze) == analyze[-1]["total"] + 1    # 시작(0) + 창마다 1건


def _without_item_ids(summary: dict) -> dict:
    """비교용 정규화 — item_id 는 실행마다 새로 발급되는 uuid 라 그대로 비교할 수 없다."""
    import copy

    out = copy.deepcopy(summary)
    for block in out.get("agenda") or []:
        for section in ("points", "decisions", "issues"):
            for item in block.get(section) or []:
                item.pop("item_id", None)
    return out


def test_보고하지_않아도_core_산출은_같다():
    """진행 보고는 부가 기능 — 심지 않은 실행에서 산출이 달라지면 안 된다."""
    backend = _FakeBackend(summary=_summary_fixture(), actions={"action_items": []})
    with _patched(backend):
        plain = orch.run_meeting_core(_segs(4), summarize_backend="fake", extract_backend="fake")
    backend2 = _FakeBackend(summary=_summary_fixture(), actions={"action_items": []})
    with _patched(backend2), _collect():
        reported = orch.run_meeting_core(_segs(4), summarize_backend="fake", extract_backend="fake")
    assert _without_item_ids(plain["summary"]) == _without_item_ids(reported["summary"])
    assert plain["coreMeta"]["callsOk"] == reported["coreMeta"]["callsOk"]


# ------------------------------------------------------------------ 웹 계약
@contextlib.contextmanager
def _client():
    from fastapi.testclient import TestClient

    saved = {k: os.environ.get(k) for k in ("JWT_SECRET", "WEB_AUTH_USERS", "WEB_AUTH_ADMINS")}
    with tempfile.TemporaryDirectory() as td:
        tmp_db = Path(td) / "meetings.db"
        os.environ["JWT_SECRET"] = "test-secret-job-progress"
        os.environ["WEB_AUTH_USERS"] = "admin:pw1"
        os.environ["WEB_AUTH_ADMINS"] = "admin"
        import src.web.store as storemod
        store_orig = storemod.DEFAULT_DB_PATH
        try:
            storemod.DEFAULT_DB_PATH = tmp_db
            import src.web.auth as auth
            importlib.reload(auth)
            auth.DEFAULT_DB_PATH = tmp_db
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


def test_리포터가_잡_메타에_기록한다():
    with _client() as (_auth, appmod, _client_):
        appmod._init_job_meta("J1", "stt")
        appmod._progress_reporter("J1")({"stage": "analyze", "done": 2, "total": 5})
        with appmod._jobs_lock:
            assert appmod._job_meta["J1"]["progress"] == {"stage": "analyze", "done": 2, "total": 5}


def test_purge된_잡_보고는_조용히_무시된다():
    """보고는 실패해선 안 된다 — 잡이 사라진 뒤 늦게 도착한 이벤트로 예외가 나면 워커가 죽는다."""
    with _client() as (_auth, appmod, _client_):
        appmod._progress_reporter("사라진잡")({"stage": "analyze"})  # 예외 없이 지나가야 한다


def test_단계가_바뀌면_세부진행은_무효화된다():
    """'창 8/8' 이 남은 채 다음 단계로 넘어가면 화면이 끝난 것처럼 보인다."""
    with _client() as (_auth, appmod, _client_):
        appmod._init_job_meta("J2", "stt")
        appmod._progress_reporter("J2")({"stage": "analyze", "done": 8, "total": 8})
        appmod._set_phase("J2", "summarizing")
        with appmod._jobs_lock:
            assert "progress" not in appmod._job_meta["J2"]


@pytest.mark.parametrize(
    "prog,expected",
    [
        ({"stage": "analyze", "done": 2, "total": 4}, "구간 2/4 완료"),
        ({"stage": "reduce"}, "병합"),
        ({"stage": "critic"}, "검증"),
        ({"stage": "localize"}, "한국어"),
        ({"stage": "finalize"}, "마무리"),
    ],
)
def test_단계별_문구가_있다(prog, expected):
    with _client() as (_auth, appmod, _client_):
        assert expected in appmod._summarizing_hint(prog)


@pytest.mark.parametrize(
    "prog",
    [None, {}, {"stage": "듣도보도못한단계"}, {"stage": "analyze"}, {"stage": "analyze", "total": 0}],
)
def test_모르는_진행에도_빈_문구는_안_나간다(prog):
    """단계를 추가하고 문구를 빠뜨리면 화면이 빈다 → 기본 문구로 떨어져야 한다."""
    with _client() as (_auth, appmod, _client_):
        assert appmod._summarizing_hint(prog).strip()


def test_폴링_응답은_진행중에만_progress를_싣는다():
    with _client() as (auth, appmod, client) :
        h = _headers(auth, appmod)
        appmod._init_job_meta("J3", "stt")
        with appmod._jobs_lock:
            appmod._jobs["J3"] = {"status": "processing"}
            appmod._job_owner["J3"] = "admin"
        appmod._set_phase("J3", "summarizing")
        appmod._progress_reporter("J3")({"stage": "analyze", "done": 2, "total": 4})
        out = client.get("/api/ai/jobs/J3", headers=h).json()
        assert out["progress"] == {"stage": "analyze", "done": 2, "total": 4}
        assert "구간 2/4 완료" in out["reasonHint"]      # 서버가 만든 문구가 그대로 내려간다
        # 완료된 잡에는 싣지 않는다 — 끝난 잡에 진행 표시가 남으면 거짓 상태가 된다.
        with appmod._jobs_lock:
            appmod._jobs["J3"] = {"status": "done", "result": {}}
        done = client.get("/api/ai/jobs/J3", headers=h).json()
        assert "progress" not in done


def test_진행이_없으면_기존_문구를_유지한다():
    """구버전 백엔드·보고 전 시점에도 문구가 비어선 안 된다(하위호환)."""
    with _client() as (auth, appmod, client):
        h = _headers(auth, appmod)
        appmod._init_job_meta("J4", "stt")
        with appmod._jobs_lock:
            appmod._jobs["J4"] = {"status": "processing"}
            appmod._job_owner["J4"] = "admin"
        appmod._set_phase("J4", "summarizing")
        out = client.get("/api/ai/jobs/J4", headers=h).json()
        assert "progress" not in out
        assert out["reasonHint"] == "요약/추출 진행 중입니다."


def test_진행_시각도_기록된다():
    """스톨 판정·관측에 쓰려면 '언제 마지막으로 움직였나'가 남아야 한다."""
    with _client() as (_auth, appmod, _client_):
        appmod._init_job_meta("J5", "stt")
        before = time.monotonic()
        appmod._progress_reporter("J5")({"stage": "critic"})
        with appmod._jobs_lock:
            assert appmod._job_meta["J5"]["progress_at"] >= before
