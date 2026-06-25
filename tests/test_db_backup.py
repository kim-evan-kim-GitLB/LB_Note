"""DB 스냅샷 백업 회귀 테스트 (무백업 prune 사고 재발 방지 안전장치).

검증 불변식:
  - store.backup_to: sqlite backup API 로 일관 스냅샷 — 열어서 meetings 행이 보존됨.
  - maintenance.run_db_backup: output/web/backup/meetings-*.db 생성 + audit + 보존개수 정리.
  - _prune_old_db_backups: 최신 keep 개만 남기고 오래된 스냅샷 삭제(이름=시간순).
  - GET /api/admin/metrics: dbBackup 설정·개수 노출.

실 DB 미접촉(tempfile + DEFAULT_DB_PATH 패치). 스케줄러는 MEETSCRIPT_BLOCK_DEFAULT_DB=1 에서
미가동 → 백업 로직은 run_db_backup 직접 호출로 검증.

실행: sudo uv run --frozen pytest tests/test_db_backup.py -q
"""
from __future__ import annotations

import contextlib
import importlib
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_backup_to_creates_consistent_snapshot():
    import src.web.store as storemod

    with tempfile.TemporaryDirectory() as td:
        orig = storemod.DEFAULT_DB_PATH
        storemod.DEFAULT_DB_PATH = Path(td) / "meetings.db"
        try:
            store = storemod.MeetingStore(Path(td) / "meetings.db")
            mid = "c" * 32
            store.create({"id": mid, "ownerId": "u1", "title": "백업회의",
                          "updatedAt": "2026-01-01T00:00:00.000000+00:00"})
            dest = Path(td) / "snap" / "meetings-snap.db"
            out = store.backup_to(dest)
            assert out == dest and dest.is_file()
            # 스냅샷을 독립 연결로 열어 행 보존 확인.
            conn = sqlite3.connect(str(dest))
            try:
                row = conn.execute("SELECT title FROM meetings WHERE id=?", (mid,)).fetchone()
            finally:
                conn.close()
            assert row is not None and row[0] == "백업회의"
        finally:
            storemod.DEFAULT_DB_PATH = orig


def test_prune_old_db_backups_keeps_newest():
    from src.web import maintenance

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # 이름=시간순 5개 생성.
        names = [
            "meetings-20260101-000000.db", "meetings-20260102-000000.db",
            "meetings-20260103-000000.db", "meetings-20260104-000000.db",
            "meetings-20260105-000000.db",
        ]
        for n in names:
            (d / n).write_bytes(b"x")
        # 무관 파일은 건드리지 않음.
        (d / "other.db").write_bytes(b"y")
        removed = maintenance._prune_old_db_backups(d, keep=2)
        assert removed == 3
        kept = sorted(p.name for p in d.glob("meetings-*.db"))
        assert kept == ["meetings-20260104-000000.db", "meetings-20260105-000000.db"]
        assert (d / "other.db").is_file()  # 무관 파일 보존


def test_run_db_backup_creates_file_and_audits():
    import src.web.store as storemod
    from src.web import maintenance, observability

    with tempfile.TemporaryDirectory() as td:
        orig = storemod.DEFAULT_DB_PATH
        storemod.DEFAULT_DB_PATH = Path(td) / "meetings.db"
        try:
            store = storemod.MeetingStore(Path(td) / "meetings.db")
            store.create({"id": "d" * 32, "ownerId": "u1",
                          "updatedAt": "2026-01-01T00:00:00.000000+00:00"})
            observability.reset()
            res = maintenance.run_db_backup(store, keep=7)
            assert res["file"] and res["file"].startswith("meetings-")
            d = maintenance.db_backup_dir(store)
            assert d.is_dir()
            assert maintenance.db_backup_count(store) == 1
            assert observability.snapshot()["db_backup.run"] == 1
            # 비번 해시 포함 사본 → 파일 0600(소유자만).
            snap_file = next(d.glob("meetings-*.db"))
            assert (snap_file.stat().st_mode & 0o777) == 0o600
        finally:
            storemod.DEFAULT_DB_PATH = orig


def test_run_db_backup_keep_zero_still_retains_fresh():
    """keep=0 이라도 방금 만든 스냅샷은 보존(하한 1 강제) — 백업 무력화 방지."""
    import src.web.store as storemod
    from src.web import maintenance, observability

    with tempfile.TemporaryDirectory() as td:
        orig = storemod.DEFAULT_DB_PATH
        storemod.DEFAULT_DB_PATH = Path(td) / "meetings.db"
        try:
            store = storemod.MeetingStore(Path(td) / "meetings.db")
            store.create({"id": "e" * 32, "ownerId": "u1",
                          "updatedAt": "2026-01-01T00:00:00.000000+00:00"})
            observability.reset()
            res = maintenance.run_db_backup(store, keep=0)
            assert res["file"] is not None
            assert maintenance.db_backup_count(store) == 1  # 하한 1로 보존
        finally:
            storemod.DEFAULT_DB_PATH = orig


# ---------- HTTP: 메트릭에 dbBackup 노출 ----------
@contextlib.contextmanager
def _client_for(td: Path, users: str):
    from fastapi.testclient import TestClient

    tmp_db = td / "meetings.db"
    os.environ["JWT_SECRET"] = "test-secret-db-backup"
    os.environ["WEB_AUTH_USERS"] = users
    os.environ["WEB_AUTH_ADMINS"] = "admin"
    os.environ["WEB_AUTH_TOKEN_TTL"] = "3600"
    os.environ["WEB_AUTH_PRUNE"] = "1"
    import src.web.store as storemod

    store_orig = storemod.DEFAULT_DB_PATH
    try:
        storemod.DEFAULT_DB_PATH = tmp_db
        import src.web.auth as auth
        importlib.reload(auth)
        auth.DEFAULT_DB_PATH = tmp_db
        import src.web.audio_store as audio_store
        importlib.reload(audio_store)
        import src.web.app as appmod
        importlib.reload(appmod)
        with TestClient(appmod.app) as client:
            yield auth, appmod, client
    finally:
        storemod.DEFAULT_DB_PATH = store_orig


def test_metrics_exposes_db_backup():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        appmod.users.set_password("admin", "newpassword123")
        h = {"Authorization": f"Bearer {auth.make_token('admin')}"}
        r = client.get("/api/admin/metrics", headers=h)
        assert r.status_code == 200, r.text
        db = r.json()["dbBackup"]
        assert db["enabled"] in (True, False)
        assert "keep" in db and "count" in db and "intervalSec" in db
