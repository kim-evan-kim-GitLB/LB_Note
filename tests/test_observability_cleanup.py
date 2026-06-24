"""관측성·정리 배치 회귀 테스트 (계획 v4 P10).

검증 불변식:
  - observability: incr/snapshot 누적, audit 가 동명 카운터 +1, reset 격리.
  - store.prune_expired_backups: 만료(created_at < now-max_age) 백업만 삭제·개수 반환, 최신 유지.
  - store.count_backups/count_meetings: 행 수 집계.
  - maintenance.run_cleanup_once: staging 파일·만료 백업 정리 + 카운터 기록(스케줄러 없이 직접 호출).
  - GET /api/admin/metrics: admin 200(카운터·디스크·정리설정), 비admin 403, 카운터가 이벤트 반영.

실 DB 미접촉(tempfile + DEFAULT_DB_PATH 패치 격리). 스케줄러는 MEETSCRIPT_BLOCK_DEFAULT_DB=1 에서
미가동(conftest 가 설정) → 정리 로직은 run_cleanup_once 직접 호출로 검증한다.

실행: sudo uv run --frozen pytest tests/test_observability_cleanup.py -q
"""
from __future__ import annotations

import contextlib
import importlib
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------- 단위: observability ----------
def test_incr_snapshot_and_reset():
    from src.web import observability

    observability.reset()
    observability.incr("foo")
    observability.incr("foo", 4)
    observability.incr("bar")
    snap = observability.snapshot()
    assert snap["foo"] == 5 and snap["bar"] == 1
    # snapshot 은 복사본 — 외부 변형이 내부에 새지 않음.
    snap["foo"] = 999
    assert observability.snapshot()["foo"] == 5
    observability.reset()
    assert observability.snapshot() == {}


def test_audit_increments_named_counter():
    from src.web import observability

    observability.reset()
    observability.audit("meeting.create", meeting_id="abc", owner="kim")
    observability.audit("meeting.create", meeting_id="def", owner="lee")
    assert observability.snapshot()["meeting.create"] == 2


# ---------- 단위: store 백업 정리·집계 ----------
def test_prune_expired_backups_and_counts():
    import src.web.store as storemod

    with tempfile.TemporaryDirectory() as td:
        orig = storemod.DEFAULT_DB_PATH
        storemod.DEFAULT_DB_PATH = Path(td) / "meetings.db"
        try:
            store = storemod.MeetingStore(Path(td) / "meetings.db")
            mid = "a" * 32
            store.create({"id": mid, "ownerId": "u1", "summary": {"x": 1}, "actionItems": [],
                          "updatedAt": "2026-01-01T00:00:00.000000+00:00"})
            assert store.count_meetings() == 1
            # apply_regenerate 2회 → 백업 2건 적재(현행 스냅샷).
            cur = store.get(mid)
            store.apply_regenerate(mid, {"y": 2}, [], cur["updatedAt"])
            cur = store.get(mid)
            store.apply_regenerate(mid, {"y": 3}, [], cur["updatedAt"])
            assert store.count_backups() == 2
            # max_age 큼 → 아무것도 삭제 안 됨(방금 생성).
            assert store.prune_expired_backups(3600) == 0
            assert store.count_backups() == 2
            # max_age 0 → cutoff=now > 생성시각 → 전부 만료 삭제.
            time.sleep(0.01)
            assert store.prune_expired_backups(0) == 2
            assert store.count_backups() == 0
        finally:
            storemod.DEFAULT_DB_PATH = orig


# ---------- 단위: maintenance.run_cleanup_once ----------
def test_run_cleanup_once_removes_staging_and_backups():
    import src.web.store as storemod
    import src.web.audio_store as audio_store
    from src.web import maintenance, observability

    with tempfile.TemporaryDirectory() as td:
        orig = storemod.DEFAULT_DB_PATH
        storemod.DEFAULT_DB_PATH = Path(td) / "meetings.db"
        try:
            store = storemod.MeetingStore(Path(td) / "meetings.db")
            # staging 파일 1개 생성(audio_base = DEFAULT_DB_PATH.parent/audio).
            token, _ext = audio_store.save_staging(b"abc", mime_type="audio/webm", filename="a.webm")
            assert audio_store._staging_path(token) is not None
            # 백업 1건 적재.
            mid = "b" * 32
            store.create({"id": mid, "ownerId": "u1", "summary": {}, "actionItems": [],
                          "updatedAt": "2026-01-01T00:00:00.000000+00:00"})
            cur = store.get(mid)
            store.apply_regenerate(mid, {"y": 1}, [], cur["updatedAt"])
            assert store.count_backups() == 1

            observability.reset()
            time.sleep(0.01)
            # max_age=0 → staging·백업 모두 만료로 정리.
            res = maintenance.run_cleanup_once(store, staging_max_age=0, backup_max_age=0)
            assert res == {"stagingRemoved": 1, "backupsRemoved": 1}
            assert audio_store._staging_path(token) is None
            assert store.count_backups() == 0
            snap = observability.snapshot()
            assert snap["cleanup.staging_removed"] == 1
            assert snap["cleanup.backups_removed"] == 1
            assert snap["cleanup.run"] == 1
        finally:
            storemod.DEFAULT_DB_PATH = orig


# ---------- HTTP: 메트릭 엔드포인트 admin 게이트 ----------
@contextlib.contextmanager
def _client_for(td: Path, users: str):
    from fastapi.testclient import TestClient

    tmp_db = td / "meetings.db"
    os.environ["JWT_SECRET"] = "test-secret-observability"
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


def _headers(auth, appmod, username: str) -> dict:
    appmod.users.set_password(username, "newpassword123")
    return {"Authorization": f"Bearer {auth.make_token(username)}"}


def test_metrics_requires_admin():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1,bob:pw2") as (auth, appmod, client):
        # 비admin(bob=developer) → 403.
        r = client.get("/api/admin/metrics", headers=_headers(auth, appmod, "bob"))
        assert r.status_code == 403, r.text
        # admin → 200 + 구조.
        r = client.get("/api/admin/metrics", headers=_headers(auth, appmod, "admin"))
        assert r.status_code == 200, r.text
        body = r.json()
        assert "counters" in body and "disk" in body
        assert body["cleanup"]["enabled"] in (True, False)
        assert "stagingMaxAgeSec" in body["cleanup"]


def test_metrics_counters_reflect_meeting_create():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        appmod.observability.reset()
        r = client.post("/api/meetings", json={"title": "t", "status": "review"}, headers=h)
        assert r.status_code == 200, r.text
        r = client.get("/api/admin/metrics", headers=h)
        assert r.status_code == 200
        counters = r.json()["counters"]
        assert counters.get("meeting.create", 0) >= 1
        assert r.json()["meetings"] >= 1
