"""장시간 파이프라인의 진행 보고 채널(ContextVar).

웹 잡 스레드가 `use_progress(reporter)` 로 콜백을 심으면, 파이프라인(core)이 단계 경계에서
`report()` 를 부른다. 취소 이벤트(src/cancellation.py)와 **같은 규약**이다 — 진행 보고라는
부수 관심사를 enrich_to_contract → run_meeting_core 스택 전체에 파라미터로 꿰지 않아도 되고,
콜백을 심지 않은 실행(도구·테스트·CLI)에서는 조용히 무동작이다.

여기서 **문구는 만들지 않는다.** 단계 이름과 개수만 나르고 사용자 문구는 서버(app.py)가
확정한다 — 게이트 label·reasonHint 와 같은 규약(화면이 문구를 다시 만들면 단계를 추가할 때
화면이 서버를 못 따라온다).

보고는 **절대 파이프라인을 죽이지 않는다.** 콜백이 던지면 삼키고 계속한다 — 관측 때문에
회의가 실패하면 안 된다.

병렬 워커에서도 보이는 이유: _run_parallel 이 제출 스레드의 ContextVar 값을 워커에 다시
심는다(orchestrator._run_parallel). 그 전파가 깨지면 진행 표시가 조용히 멈춘다.
"""
from __future__ import annotations

import contextlib
import contextvars
import traceback
from typing import Callable

# 진행 이벤트 dict 를 받는 콜백. 없으면(기본) 보고는 무동작.
_active_reporter: contextvars.ContextVar[Callable[[dict], None] | None] = contextvars.ContextVar(
    "active_progress_reporter", default=None
)


@contextlib.contextmanager
def use_progress(reporter: Callable[[dict], None] | None):
    """with 블록 동안만 진행 보고를 활성화하고, 빠져나오면 원복(누수 방지)."""
    token = _active_reporter.set(reporter)
    try:
        yield
    finally:
        _active_reporter.reset(token)


def report(stage: str, *, done: int | None = None, total: int | None = None) -> None:
    """현재 단계를 보고한다. 콜백 미설정이면 무동작.

    stage: 파이프라인이 정한 단계 키(예: analyze/reduce/critic/localize/finalize).
    done/total: 세부 진행이 있는 단계만(예: 창 3/8). 없으면 키를 싣지 않는다.
    """
    reporter = _active_reporter.get()
    if reporter is None:
        return
    event: dict = {"stage": stage}
    if done is not None:
        event["done"] = done
    if total is not None:
        event["total"] = total
    try:
        reporter(event)
    except Exception:  # noqa: BLE001 — 진행 보고 실패가 파이프라인을 죽이지 않는다
        traceback.print_exc()
