"""관측성 — 경량 audit 로깅 + 인프로세스 카운터 (플랜 v4 P10, 무거운 의존성 없음).

온프레미스 단일 uvicorn 워커(__main__.py:22)·사내 LAN(~57명) 전제다. Prometheus/OpenTelemetry
같은 외부 의존성을 들이지 않고 stdlib logging + 스레드안전 카운터만으로 핵심 이벤트(audit)와
누적 집계(metrics)를 남긴다. 단일 프로세스라 in-process 카운터로 충분하다(워커 간 합산 불요).

설계:
  - audit(event, **fields): 구조적 한 줄 로그(`event=... k=v ...`). 비밀(secret/비번/토큰)은 절대
    싣지 않는다 — 호출부가 owner/메타만 넘긴다. logging.getLogger("meetscript.audit") 로 흘려
    uvicorn 로깅(stdout)·옵션 파일(WEB_AUDIT_LOG)에 함께 기록된다.
  - incr(metric, n): 스레드안전 누적 카운터(잡 스레드/요청 스레드 공용). snapshot() 으로 조회.
  - audit 는 incr 도 자동 호출한다(event 명을 카운터 키로) → 이벤트 1건 = 로그 1줄 + 카운트 1.

테스트: reset() 로 카운터·로깅 핸들러 초기화(격리). snapshot() 으로 단언.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

# audit 전용 로거. propagate=True 라 root(=uvicorn)로 흘러 stdout 에 보인다. 추가로 WEB_AUDIT_LOG
# 가 지정되면 파일에도 남긴다(운영 audit 보존). 핸들러 중복 부착은 _ensure_handler 가 막는다.
_LOGGER = logging.getLogger("meetscript.audit")
_LOGGER.setLevel(logging.INFO)

_counters: dict[str, int] = {}
_lock = threading.Lock()
_handler_ready = False


def _ensure_handler() -> None:
    """WEB_AUDIT_LOG 지정 시 FileHandler 1회 부착(중복 방지). 미지정이면 propagate 만으로 충분."""
    global _handler_ready
    if _handler_ready:
        return
    path = os.environ.get("WEB_AUDIT_LOG", "").strip()
    if path:
        # reload/재초기화로 _handler_ready 가 리셋돼도 동일 경로 FileHandler 중복 부착(로그 중복)을
        # 막는다 — 이미 같은 파일을 보는 핸들러가 있으면 부착하지 않는다.
        target = str(Path(path).resolve())
        already = any(
            isinstance(h, logging.FileHandler)
            and getattr(h, "baseFilename", None) == target
            for h in _LOGGER.handlers
        )
        try:
            if not already:
                h = logging.FileHandler(path, encoding="utf-8")
                h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
                _LOGGER.addHandler(h)
        except OSError:
            # 파일 핸들러 부착 실패(권한/경로)는 치명적이지 않다 — propagate(stdout)만으로 진행.
            pass
    _handler_ready = True


def incr(metric: str, n: int = 1) -> None:
    """스레드안전 누적 카운터 증가. metric 은 점 표기(예: 'meeting.create')."""
    with _lock:
        _counters[metric] = _counters.get(metric, 0) + n


def _fmt_fields(fields: dict) -> str:
    """audit 필드를 `k=v` 로 직렬화. 값의 공백은 한 줄 유지를 위해 `_` 로 치환(파싱 단순화)."""
    parts = []
    for k, v in fields.items():
        if v is None:
            continue
        # 개행·공백은 한 줄 유지를 위해, `=` 는 k=v 파서 경계 모호성을 막기 위해 치환.
        s = str(v).replace("\n", " ").replace(" ", "_").replace("=", "_")
        parts.append(f"{k}={s}")
    return " ".join(parts)


def audit(event: str, **fields: object) -> None:
    """audit 이벤트 1건 기록 — 구조적 로그 1줄 + 동명 카운터 +1.

    비밀(secret/password/token 값)은 절대 넘기지 않는다(호출부 책임). owner/meeting_id/메타만.
    """
    _ensure_handler()
    incr(event)
    suffix = _fmt_fields(fields)
    _LOGGER.info("event=%s %s", event, suffix) if suffix else _LOGGER.info("event=%s", event)


def snapshot() -> dict[str, int]:
    """현재 카운터 스냅샷(복사본). 메트릭 엔드포인트·테스트가 사용한다."""
    with _lock:
        return dict(_counters)


def reset() -> None:
    """카운터 초기화(테스트 격리용). 로깅 핸들러 상태는 건드리지 않는다."""
    with _lock:
        _counters.clear()
