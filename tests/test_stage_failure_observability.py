"""삼켜진 실패의 관측성 회귀 테스트 (설계 docs/2026-08-05-회의록-품질-개선-설계.md §3 1순위).

배경: 요약·추출·critic 은 LLM 응답을 JSON 으로 파싱하지 못하면 **예외도 로그도 없이** 빈 결과를
돌려줬다. 그래서 "요약이 왜 비었는지"(백엔드 off / PATH / 타임아웃 / 파싱 실패)를 구분할 수
없었고, 실제 사고에서 원인 판별에 시간이 걸렸다.

검증 불변식:
  - 실백엔드에서 파싱이 깨지면 `parse_failed` 가 서고 raw 앞부분이 남는다(전문 저장 금지).
  - passthrough 의 빈 결과는 **정상**이라 parse_failed 가 서지 않는다(오탐 방지).
  - 삼켜진 예외는 coreMeta.failures 로 올라온다.
  - `calls`(계획)와 `callsOk`(성공)가 분리된다 — 예전에는 calls=3 을 "실행됐다"로 오독했다.
  - done + 빈 산출이면 reasonHint 가 원인을 구분해 알려준다(예전엔 무조건 None).

실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_stage_failure_observability.py -q
"""
from __future__ import annotations

import contextlib
import importlib
import os
import tempfile
from pathlib import Path

from src.postprocess.backends.base import LLMBackend, LLMCapabilities
from src.postprocess.orchestrator import run_meeting_core
from src.postprocess.stages.extract import ExtractStage
from src.postprocess.stages.summarize import SummarizeStage


@contextlib.contextmanager
def _appmod():
    """src.web.app 은 import 시 실 DB 를 연다 → 임시 DB 로 격리해 로드(conftest 규약)."""
    with tempfile.TemporaryDirectory() as td:
        tmp_db = Path(td) / "meetings.db"
        os.environ["JWT_SECRET"] = "test-secret-stagefail"
        os.environ["WEB_AUTH_USERS"] = "admin:pw1"
        os.environ["WEB_AUTH_ADMINS"] = "admin"
        import src.web.store as storemod
        orig = storemod.DEFAULT_DB_PATH
        try:
            storemod.DEFAULT_DB_PATH = tmp_db
            import src.web.auth as auth
            importlib.reload(auth)
            auth.DEFAULT_DB_PATH = tmp_db
            import src.web.audio_store as audio_store
            importlib.reload(audio_store)
            import src.web.app as appmod
            importlib.reload(appmod)
            yield appmod
        finally:
            storemod.DEFAULT_DB_PATH = orig

SEGMENTS = [
    {"id": i, "start": i * 10.0, "end": i * 10.0 + 9.0,
     "text": "회의록 저장 기능과 캘린더 동기화 연동 상태를 공유하고 다음 작업을 정했습니다."}
    for i in range(6)
]


class _BrokenJSONBackend(LLMBackend):
    """JSON 이 잘려 돌아오는 실백엔드 흉내 — 실제로 관측된 실패 형태."""

    name = "agent_cli"          # passthrough 가 아니어야 '비정상'으로 잡힌다

    def generate(self, messages, *, schema=None, temperature=0.0, max_tokens=2048, seed=0) -> str:
        return '{"agenda": [{"no": 1, "title": "잘린 응답'

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(json_mode=True, determinism="none")


class _PassthroughLike(_BrokenJSONBackend):
    name = "passthrough"


# --------------------------------------------------------------------- 스테이지 신호
def test_실백엔드_파싱실패는_표시된다():
    out = SummarizeStage().run(SEGMENTS, _BrokenJSONBackend())
    assert out.agenda == []
    assert out.parse_failed is True
    assert out.raw_head.startswith('{"agenda"')
    assert len(out.raw_head) <= 200          # 전문 저장 금지(PII)


def test_passthrough_빈결과는_정상이라_표시하지_않는다():
    """오탐 방지 — passthrough 는 원래 JSON 을 못 낸다. 이걸 실패로 세면 로그가 무의미해진다."""
    out = SummarizeStage().run(SEGMENTS, _PassthroughLike())
    assert out.agenda == []
    assert out.parse_failed is False


def test_추출도_같은_규약():
    out = ExtractStage().run(SEGMENTS, _BrokenJSONBackend())
    assert out.items == []
    assert out.parse_failed is True
    assert out.raw_head


def test_추출_passthrough는_표시하지_않는다():
    assert ExtractStage().run(SEGMENTS, _PassthroughLike()).parse_failed is False


# --------------------------------------------------------------------- coreMeta 집계
def test_coreMeta_에_파싱실패가_올라온다(monkeypatch):
    monkeypatch.setattr(
        "src.postprocess.orchestrator.get_llm_backend", lambda name: _BrokenJSONBackend()
    )
    core = run_meeting_core(SEGMENTS, summarize_backend="agent_cli", extract_backend="agent_cli")
    meta = core["coreMeta"]
    assert meta["failures"].get("summarizeParse", 0) >= 1
    assert meta["failures"].get("extractParse", 0) >= 1


def test_calls_와_callsOk_가_분리된다(monkeypatch):
    """calls 는 계획 수다 — 성공 수와 같은 값으로 두면 '실행됐다'고 오독한다."""
    monkeypatch.setattr(
        "src.postprocess.orchestrator.get_llm_backend", lambda name: _BrokenJSONBackend()
    )
    meta = run_meeting_core(
        SEGMENTS, summarize_backend="agent_cli", extract_backend="agent_cli"
    )["coreMeta"]
    assert meta["calls"]["summarize"] >= 1          # 계획됨
    assert "callsOk" in meta                        # 성공 수가 별도로 있다


def test_정상_실행에는_failures_필드가_없다():
    """평시 로그를 깨끗하게 둔다 — 실패가 없으면 필드 자체를 싣지 않는다."""
    meta = run_meeting_core(
        SEGMENTS, summarize_backend="passthrough", extract_backend="passthrough"
    )["coreMeta"]
    assert "failures" not in meta


# --------------------------------------------------------------------- 사용자 힌트
def _diag(**kw):
    base = {"summaryEmpty": True, "actionsEmpty": True, "failures": {}, "callsOk": {}, "cases": []}
    base.update(kw)
    return {"status": "done", "diag": base}


def test_힌트_분기_전체():
    """done + 빈 산출의 원인을 구분한다. 예전에는 이 분기가 무조건 None 이었다."""
    with _appmod() as app:
        f = app._empty_output_hint
        # 산출이 정상이면 힌트 없음
        assert f(_diag(summaryEmpty=False, actionsEmpty=False)) is None
        # 파싱 실패(비정상)
        h = f(_diag(failures={"summarizeParse": 1}))
        assert h and "해석하지 못해" in h
        # 단계 예외(비정상)
        h = f(_diag(failures={"worker": 2}))
        assert h and "서버 로그" in h
        # 보수 모드가 스스로 비운 것(정상) — 위 둘과 구분되어야 한다
        h = f(_diag(cases=["low_quality"]))
        assert h and "정상 동작" in h
        # 요약 백엔드가 안 돌았음
        h = f(_diag(callsOk={"summarize": 0}))
        assert h and "설정" in h


def test_job_diag_는_계약이_아니라_진단용():
    with _appmod() as app:
        contract = {"_core_meta": {"failures": {"worker": 1}, "callsOk": {"summarize": 0},
                                   "plan": {"cases": ["low_quality"]}}}
        result = {"summary": {"agenda": []}, "actionItems": []}
        d = app._job_diag(contract, result)
        assert d["summaryEmpty"] is True and d["actionsEmpty"] is True
        assert d["failures"] == {"worker": 1}
        assert d["cases"] == ["low_quality"]
