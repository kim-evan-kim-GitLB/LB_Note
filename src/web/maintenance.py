"""정리 배치 — staging·만료 백업 정리 + 디스크 집계 (플랜 v4 P10, NFR-저장공간/S6).

고아 자원 정리(스케줄):
  - staging 오디오: 처리(STT) 시점에 업로드됐으나 finalize(create_meeting) 안 한 임시 파일.
    audio_store.cleanup_staging 으로 max_age 초과분 삭제(D7-id 옵션B FR-C1: 미확정 staging 비영속).
  - 재요약 백업: apply 후 undo 안 한 meeting_backup 누적. store.prune_expired_backups 로 만료분 삭제.
  - draft 회의: 이 앱은 finalize 시점에만 회의를 영속하므로(프론트 상태로만 존재) 영속 draft 고아가
    없다 → 별도 정리 대상 없음(처리중 회의 자동삭제는 인플라이트 잡 유실 위험이라 비대상).

순수 함수(run_cleanup_once)로 분리해 테스트가 스케줄러 없이 직접 호출·단언할 수 있게 한다. asyncio
주기 실행 배선은 app.py lifespan 이 담당한다(단일 워커 가정).
"""
from __future__ import annotations

import shutil
from pathlib import Path

from src.web import audio_store, observability

# 기본 보존 기간(env 미지정 시). staging=1일, 백업=7일. 운영 env(WEB_*_MAX_AGE_SEC)로 조정.
DEFAULT_STAGING_MAX_AGE = 24 * 3600
DEFAULT_BACKUP_MAX_AGE = 7 * 24 * 3600
DEFAULT_CLEANUP_INTERVAL = 3600  # 주기 실행 간격(초)


def _dir_size_bytes(path: Path) -> int:
    """디렉토리 누적 파일 크기(바이트). 부재/접근불가는 0(관측 지표라 견고하게)."""
    if not path.is_dir():
        return 0
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def audio_storage_bytes() -> int:
    """원본 오디오 저장 루트(output/web/audio) 누적 크기. 디스크 예산 관측용."""
    return _dir_size_bytes(audio_store.audio_base())


def disk_usage() -> dict:
    """오디오 저장 파티션의 디스크 사용률({total,used,free,percent}). 실패 시 빈 dict."""
    base = audio_store.audio_base()
    target = base if base.exists() else base.parent
    try:
        du = shutil.disk_usage(target)
    except OSError:
        return {}
    percent = round(du.used / du.total * 100, 1) if du.total else 0.0
    return {"total": du.total, "used": du.used, "free": du.free, "percent": percent}


def run_cleanup_once(
    store,
    *,
    staging_max_age: float = DEFAULT_STAGING_MAX_AGE,
    backup_max_age: float = DEFAULT_BACKUP_MAX_AGE,
) -> dict:
    """정리 1회 실행 → {stagingRemoved, backupsRemoved}. audit·메트릭 기록.

    staging 파일 정리(cleanup_staging)와 만료 백업 정리(prune_expired_backups)를 한 번에 수행한다.
    각 단계는 독립이며 한쪽 실패가 다른 쪽을 막지 않는다(개별 try). 스케줄러가 주기 호출하고,
    테스트는 이 함수를 직접 호출해 단언한다.
    """
    staging_removed = 0
    backups_removed = 0
    try:
        staging_removed = audio_store.cleanup_staging(staging_max_age)
    except Exception as e:  # noqa: BLE001 — 정리 실패가 서비스/다른 정리를 막지 않게 격리
        observability.audit("cleanup.staging_error", error=type(e).__name__)
    try:
        backups_removed = store.prune_expired_backups(backup_max_age)
    except Exception as e:  # noqa: BLE001
        observability.audit("cleanup.backup_error", error=type(e).__name__)
    observability.incr("cleanup.staging_removed", staging_removed)
    observability.incr("cleanup.backups_removed", backups_removed)
    observability.audit(
        "cleanup.run",
        staging_removed=staging_removed,
        backups_removed=backups_removed,
        audio_bytes=audio_storage_bytes(),
    )
    return {"stagingRemoved": staging_removed, "backupsRemoved": backups_removed}
