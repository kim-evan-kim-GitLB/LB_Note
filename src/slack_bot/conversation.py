"""요구사항 되묻기 대화 상태 — (channel, thread_ts) 키의 인메모리 상태머신.

봇 1프로세스 전제. 스레드별로 한 사용자와 '입력받기 → 저장 → 추가 입력?' 을 반복한다.
방치된 대화는 TTL 로 자동 만료. slack_bolt 가 이벤트를 여러 스레드로 디스패치할 수 있어
전역 Lock 으로 보호한다. slack_bolt 를 import 하지 않는다(순수 상태 로직 → 단위 테스트 가능).
"""
from __future__ import annotations

import threading
import time

# 상태값.
STATE_AWAITING_TEXT = "awaiting_text"  # 요구사항 내용을 기다리는 중
STATE_AWAITING_MORE = "awaiting_more"  # 저장 후 '추가로 입력?' 응답을 기다리는 중

TTL_SEC = 300.0  # 5분 방치 시 대화 자동 종료

_lock = threading.Lock()
# (channel, thread_ts) -> {"user": str, "state": str, "at": float(monotonic)}
_convos: dict[tuple[str, str], dict] = {}


def _sweep_locked(now: float) -> None:
    """만료(TTL 초과) 대화 제거. _lock 보유 상태에서 호출."""
    for key in [k for k, v in _convos.items() if now - v["at"] > TTL_SEC]:
        _convos.pop(key, None)


def start(channel: str, thread_ts: str, user: str, state: str) -> None:
    """대화 시작/재설정. 같은 스레드의 기존 대화는 덮어쓴다."""
    now = time.monotonic()
    with _lock:
        _sweep_locked(now)
        _convos[(channel, thread_ts)] = {"user": user, "state": state, "at": now}


def get(channel: str, thread_ts: str, user: str) -> str | None:
    """이 (채널,스레드)에서 이 사용자의 진행 중 상태. 없음/만료/타인이면 None(활성 시각 갱신)."""
    now = time.monotonic()
    with _lock:
        _sweep_locked(now)
        c = _convos.get((channel, thread_ts))
        if not c or c["user"] != user:
            return None
        c["at"] = now
        return c["state"]


def set_state(channel: str, thread_ts: str, user: str, state: str) -> None:
    """진행 중 대화의 상태 전이(소유자 일치 시에만)."""
    now = time.monotonic()
    with _lock:
        c = _convos.get((channel, thread_ts))
        if c and c["user"] == user:
            c["state"] = state
            c["at"] = now


def clear(channel: str, thread_ts: str) -> None:
    """대화 종료(제거)."""
    with _lock:
        _convos.pop((channel, thread_ts), None)


def _reset_for_test() -> None:
    """테스트 격리용 — 전역 상태 비움."""
    with _lock:
        _convos.clear()
