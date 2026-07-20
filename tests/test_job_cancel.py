"""잡 취소(분석 취소) 회귀 테스트 — agent_cli 취소 채널의 불변식.

검증 불변식:
  - 취소 이벤트가 set 된 컨텍스트에서 generate() 는 CLI 를 호출하지 않고 즉시 AgentCLICancelled.
  - _run_cancellable 은 실행 중 프로세스를 취소 시 kill 하고 AgentCLICancelled 로 이탈(수 초 내).
  - _run_cancellable 타임아웃은 기존 subprocess.run(timeout=) 계약(TimeoutExpired) 유지.

실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_job_cancel.py -q
"""
from __future__ import annotations

import subprocess
import threading
import time
from unittest import mock

import pytest

import src.postprocess.backends.agent_cli as ac


def test_generate_cancelled_before_call_skips_cli():
    ev = threading.Event()
    ev.set()
    backend = ac.AgentCLIBackend()
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]

    def _must_not_run(argv, sub_env, timeout):  # noqa: ARG001
        raise AssertionError("취소 상태에서 CLI 가 호출되면 안 된다")

    with mock.patch.object(ac.shutil, "which", return_value="/usr/bin/claude"), \
         mock.patch.object(ac.AgentCLIBackend, "_run_cancellable", staticmethod(_must_not_run)):
        with ac.use_cancel_event(ev):
            with pytest.raises(ac.AgentCLICancelled):
                backend.generate(msgs)


def test_run_cancellable_kills_on_cancel():
    ev = threading.Event()
    threading.Timer(0.3, ev.set).start()
    started = time.monotonic()
    with ac.use_cancel_event(ev):
        with pytest.raises(ac.AgentCLICancelled):
            ac.AgentCLIBackend._run_cancellable(["sleep", "30"], None, timeout=60)
    # sleep 30 을 기다리지 않고 취소 직후(폴링 1s 간격) 이탈해야 한다.
    assert time.monotonic() - started < 5.0


def test_run_cancellable_timeout_contract():
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        ac.AgentCLIBackend._run_cancellable(["sleep", "30"], None, timeout=1)
    assert time.monotonic() - started < 5.0


def test_run_cancellable_normal_completion():
    proc = ac.AgentCLIBackend._run_cancellable(["echo", "hello"], None, timeout=10)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "hello"
