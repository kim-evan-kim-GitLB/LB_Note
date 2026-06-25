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

import datetime as _dt
import shutil
from pathlib import Path

from src.web import audio_store, observability

# 기본 보존 기간(env 미지정 시). staging=1일, 백업=7일. 운영 env(WEB_*_MAX_AGE_SEC)로 조정.
DEFAULT_STAGING_MAX_AGE = 24 * 3600
DEFAULT_BACKUP_MAX_AGE = 7 * 24 * 3600
DEFAULT_CLEANUP_INTERVAL = 3600  # 주기 실행 간격(초)

# DB 스냅샷 백업: 기본 1일 주기·최근 7개 보존. 과거 무백업 prune 사고 재발 방지.
DEFAULT_DB_BACKUP_INTERVAL = 24 * 3600
DEFAULT_DB_BACKUP_KEEP = 7


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


def db_backup_dir(store) -> Path:
    """DB 스냅샷 저장 디렉토리(<db_path>/../backup). store 인스턴스의 db_path 기준.

    모듈 전역(DEFAULT_DB_PATH)이 아니라 store.db_path 를 따르므로 비표준 경로 store 라도 백업이
    실제 DB 와 같은 위치에 남는다(인스턴스-디렉토리 일관). 테스트 격리도 store 가 임시 DB 라 성립.
    """
    return Path(store.db_path).parent / "backup"


def _prune_old_db_backups(dir_: Path, keep: int) -> int:
    """meetings-*.db 스냅샷 중 최신 keep 개만 남기고 오래된 것 삭제 → 삭제 개수.

    파일명 타임스탬프(meetings-YYYYMMDD-HHMMSS-ffffff.db)가 사전식==시간순이라 이름 정렬로 충분하다.
    """
    if not dir_.is_dir():
        return 0
    snaps = sorted(p for p in dir_.glob("meetings-*.db") if p.is_file())
    removed = 0
    for p in snaps[: max(0, len(snaps) - keep)]:
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def db_backup_count(store) -> int:
    """현재 보존 중인 DB 스냅샷 개수(메트릭/관측성용)."""
    d = db_backup_dir(store)
    return sum(1 for p in d.glob("meetings-*.db") if p.is_file()) if d.is_dir() else 0


def run_db_backup(store, *, keep: int = DEFAULT_DB_BACKUP_KEEP) -> dict:
    """DB 일관 스냅샷 1개 생성 + 오래된 스냅샷 정리 → {file, removed}. audit 기록.

    <db_path>/../backup/meetings-{타임스탬프}.db 로 store.backup_to(sqlite backup API) 한다. 실패는
    audit 후 격리(서비스·다른 정리에 영향 없음). 무백업 prune 사고 재발 방지의 핵심 안전장치다.
    keep 은 최소 1 로 강제한다 — keep=0 이면 방금 만든 스냅샷까지 지워 백업이 무력화되므로
    (비활성화는 WEB_DB_BACKUP_ENABLED=0 로 일원화). 타임스탬프는 마이크로초까지 포함해 충돌 방지.
    """
    keep = max(1, keep)
    dir_ = db_backup_dir(store)
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    dest = dir_ / f"meetings-{ts}.db"
    try:
        store.backup_to(dest)
    except Exception as e:  # noqa: BLE001 — 백업 실패가 서비스를 막지 않게 격리
        observability.audit("db_backup.error", error=type(e).__name__)
        return {"file": None, "removed": 0}
    removed = _prune_old_db_backups(dir_, keep)
    # audit 가 동명 카운터(db_backup.run)를 +1 한다 — 별도 incr 금지(이중 카운트 방지).
    observability.audit("db_backup.run", file=dest.name, removed=removed)
    return {"file": dest.name, "removed": removed}


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
