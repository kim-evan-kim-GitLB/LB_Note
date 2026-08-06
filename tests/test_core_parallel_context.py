"""core 병렬 워커의 ContextVar 전달 계약 — _run_parallel.

실사고(2026-08-06, 온프레미스 배포): 회의록에 전사만 있고 요약·액션이 전부 비었다. 잡은
status='done' 으로 끝나고 에러도 사유도 없었다. 원인은 인증 만료가 아니라 `_run_parallel` 이
**워커 스레드 안에서** contextvars.copy_context() 를 부른 것이었다 — 스레드는 컨텍스트를
상속하지 않으므로 빈 컨텍스트를 복사해 사용자 자격증명이 사라지고, 워커는 전역 폴백으로
떨어져 실패했다(배포 컨테이너에는 전역 claude 로그인이 없다).

이 버그가 오래 살아남은 이유가 이 파일의 존재 이유다:
  - dev 에는 전역 claude 로그인이 있어 폴백이 성공한다 → 로컬에서 절대 재현되지 않는다.
  - 실패를 세려고 만든 _FAILURES 도 같은 ContextVar 라 함께 소실됐다 → 감사로그에
    failures=none 이 남아, 관측 장치가 하필 이 버그에서만 무력화됐다.
  - 태스크가 1개면 현재 스레드에서 돌아 통과한다 → 단일 태스크 테스트로는 절대 안 잡힌다.
따라서 아래 테스트는 **반드시 2개 이상**의 태스크로 검증한다.

실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_core_parallel_context.py -q
"""
from __future__ import annotations

import threading

import pytest

from src.cancellation import OperationCancelled, cancel_requested, use_cancel_event
from src.postprocess.backends.agent_cli import (
    AgentCLIAuthError,
    _active_credential,
    use_credential,
)
from src.postprocess.orchestrator import _FAILURES, _count_failure, _run_parallel

# 실제 토큰이 아니다(테스트는 claude 를 호출하지 않는다). 값이 워커까지 도달하는지만 본다.
CRED = {"type": "oauth_token", "secret": "SENTINEL-NOT-A-REAL-TOKEN"}


def _seen_credential():
    """워커가 보는 자격증명(없으면 None)."""
    return _active_credential.get()


@pytest.mark.parametrize("n", [1, 2, 4, 8])
def test_자격증명이_모든_워커에_전달된다(n):
    """n>=2 가 핵심 — 1개는 현재 스레드에서 돌아 버그를 통과시킨다."""
    with use_credential(CRED):
        assert _run_parallel([_seen_credential] * n) == [CRED] * n


def test_자격증명_없으면_워커도_없다():
    """전역 폴백 경로를 없애지 않는다 — 부모가 안 심었으면 워커도 None 이어야 한다."""
    assert _run_parallel([_seen_credential, _seen_credential]) == [None, None]


def test_풀_스레드에_자격증명이_남지_않는다():
    """풀 스레드는 재사용된다 → 앞 회의 자격증명이 남으면 다음 회의가 남의 토큰으로 돈다."""
    with use_credential(CRED):
        assert _run_parallel([_seen_credential] * 4) == [CRED] * 4
    # 같은 풀 스레드들이 다시 쓰이는 두 번째 실행 — 부모가 안 심었으므로 전부 None 이어야 한다.
    assert _run_parallel([_seen_credential] * 4) == [None] * 4


def test_삼킨_실패가_카운터에_남는다():
    """실패를 흡수하는 정책은 유지하되, 흔적은 남아야 한다(빈 요약의 원인 추적 근거)."""
    failures: dict = {}
    _FAILURES.set(failures)

    def boom():
        raise RuntimeError("창 1개 실패")

    assert _run_parallel([boom, boom, _seen_credential]) == [None, None, None]
    assert failures == {"worker": 2}


def test_취소_이벤트가_워커에_전달된다():
    """취소가 워커에 닿지 않으면 [취소] 를 눌러도 claude 서브프로세스가 계속 돈다."""
    ev = threading.Event()
    ev.set()
    with use_cancel_event(ev):
        assert _run_parallel([cancel_requested] * 3) == [True, True, True]


def test_인증만료는_삼키지_않고_전파한다():
    """빈 요약으로 묻으면 사용자가 재인증해야 한다는 신호가 사라진다."""

    def auth_fail():
        raise AgentCLIAuthError("만료")

    with pytest.raises(AgentCLIAuthError):
        _run_parallel([auth_fail, auth_fail])


def test_취소는_삼키지_않고_전파한다():
    def cancelled():
        raise OperationCancelled("취소")

    with pytest.raises(OperationCancelled):
        _run_parallel([cancelled, cancelled])


def test_결과는_입력_순서를_지킨다():
    """윈도우 순서가 뒤섞이면 요약 본문 순서와 evidence 매핑이 어긋난다."""
    tasks = [(lambda i=i: i) for i in range(6)]
    assert _run_parallel(tasks) == [0, 1, 2, 3, 4, 5]


def test_빈_태스크는_빈_결과():
    assert _run_parallel([]) == []


def test_카운터_없이_호출해도_죽지_않는다():
    """도구·테스트가 run_meeting_core 밖에서 호출하는 경로(LookupError 흡수)."""
    _FAILURES.set({})  # 이 테스트 컨텍스트에서만 초기화
    _count_failure("worker")  # 예외 없이 지나가야 한다
