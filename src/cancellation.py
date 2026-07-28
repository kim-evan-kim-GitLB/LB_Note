"""사용자 취소 신호 채널 — 장시간 작업(STT 디코딩, LLM 호출)의 공통 중단 지점.

웹 잡 스레드가 `use_cancel_event(event)` 로 threading.Event 를 심으면, 작업 코드가 단계 경계에서
`raise_if_cancelled()` 로 확인해 즉시 이탈한다. 자격증명과 같은 이유로 ContextVar(스레드별 격리)를
쓴다 — 잡 스레드마다 다른 취소 이벤트가 필요하고 전역 변수는 서로를 덮어쓴다.

이 모듈이 STT 백엔드(src/backends)와 LLM 백엔드(src/postprocess/backends) 어느 쪽에도 속하지 않는
이유: 양쪽이 같은 채널을 공유해야 하는데, 한쪽에 두면 다른 쪽이 그것을 임포트하면서 계층이 역전된다.
(원래 agent_cli.py 에 있던 것을 여기로 올렸고, agent_cli 는 하위호환을 위해 재노출한다.)
"""
from __future__ import annotations

import contextlib
import contextvars
import threading


class OperationCancelled(RuntimeError):
    """사용자 취소로 작업이 중단됨. 실패가 아니므로 재시도하지 않고 즉시 전파한다.

    호출부(웹 잡 스레드)가 이 예외를 status='cancelled' 로 매핑한다.
    """


_active_cancel: contextvars.ContextVar["threading.Event | None"] = contextvars.ContextVar(
    "active_cancel", default=None
)


@contextlib.contextmanager
def use_cancel_event(event: "threading.Event | None"):
    """with 블록 동안만 취소 이벤트를 활성화하고, 빠져나오면 원복(누수 방지)."""
    token = _active_cancel.set(event)
    try:
        yield
    finally:
        _active_cancel.reset(token)


def cancel_requested() -> bool:
    """현재 컨텍스트에 취소가 걸려 있는가(예외 없이 확인만)."""
    ev = _active_cancel.get()
    return ev is not None and ev.is_set()


def raise_if_cancelled() -> None:
    """취소됐으면 OperationCancelled 를 던진다. 단계/배치 경계에서 호출한다."""
    if cancel_requested():
        raise OperationCancelled("사용자 취소로 중단되었습니다.")
