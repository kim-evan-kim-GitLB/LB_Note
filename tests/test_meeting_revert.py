"""회의 '원본으로 원복'(revert) store 단위 테스트.

검증 불변식:
  - save_original_snapshot: 최초 생성본을 1회 보관(idempotent — 재호출·편집 후에도 원본 불변).
  - restore_original: title/participants/summary/actionItems/transcript 를 원본으로 되돌리고
    status/createdAt/ownerId 는 보존. 원본은 소비하지 않아 반복 원복 가능.
  - 재요약 undo(restore_latest_backup)는 reason='original' 을 대상에서 제외(오소비/삭제 금지).
  - prune_expired_backups 는 원본을 지우지 않는다(영구 보존).
  - 원본 없는 회의 restore_original → (현행, False).

실 DB 미접촉(tempfile 격리).
실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_meeting_revert.py
"""
from __future__ import annotations

import tempfile
from pathlib import Path


def _store(tmp: Path):
    from src.web.store import MeetingStore

    return MeetingStore(tmp / "meetings.db")


def _seed(store):
    m = {
        "id": "a" * 32,
        "ownerId": "u1",
        "status": "review",
        "title": "원본 제목",
        "createdAt": "2026-07-01T00:00:00",
        "updatedAt": "2026-07-01T00:00:00",
        "participants": [{"id": "p1", "name": "김윤희", "color": "blue"}],
        "transcript": [{"speakerId": "", "text": "원본 발화", "timestamp": "00:01", "segmentId": 0}],
        "summary": {"agenda": [{"no": 1, "title": "원본 안건", "points": []}]},
        "actionItems": [{"item_id": "o1", "text": "원본 할일"}],
    }
    return store.create(m)


def _edit_everything(store, etag):
    # 편집(제목·참석자·요약·액션·전사 전부 변경)
    return store.update_if_match(
        "a" * 32,
        {
            "title": "편집된 제목",
            "participants": [{"id": "p1", "name": "김윤희", "color": "blue"}, {"id": "g1", "name": "게스트", "color": "red"}],
            "summary": {"agenda": [{"no": 1, "title": "편집 안건", "points": []}]},
            "actionItems": [{"item_id": "n1", "text": "새 할일"}],
            "transcript": [{"speakerId": "", "text": "편집 발화", "timestamp": "00:02", "segmentId": 0}],
        },
        etag,
    )


def test_snapshot_and_revert_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        store = _store(Path(td))
        cur = _seed(store)
        store.save_original_snapshot(cur)
        assert store.has_original("a" * 32) is True

        edited = _edit_everything(store, cur["updatedAt"])
        assert edited["title"] == "편집된 제목"

        reverted, ok = store.restore_original("a" * 32, edited["updatedAt"])
        assert ok is True
        assert reverted["title"] == "원본 제목"
        assert reverted["summary"]["agenda"][0]["title"] == "원본 안건"
        assert reverted["actionItems"] == [{"item_id": "o1", "text": "원본 할일"}]
        assert reverted["transcript"][0]["text"] == "원본 발화"
        assert len(reverted["participants"]) == 1
        # 보존 필드: status/createdAt/ownerId 는 그대로
        assert reverted["status"] == "review"
        assert reverted["createdAt"] == "2026-07-01T00:00:00"
        assert reverted["ownerId"] == "u1"
        assert reverted["updatedAt"] != edited["updatedAt"], "새 ETag"


def test_revert_is_repeatable_original_not_consumed():
    with tempfile.TemporaryDirectory() as td:
        store = _store(Path(td))
        cur = _seed(store)
        store.save_original_snapshot(cur)
        e1 = _edit_everything(store, cur["updatedAt"])
        r1, ok1 = store.restore_original("a" * 32, e1["updatedAt"])
        assert ok1 is True
        # 다시 편집 후 또 원복 가능(원본 소비 안 됨)
        e2 = _edit_everything(store, r1["updatedAt"])
        r2, ok2 = store.restore_original("a" * 32, e2["updatedAt"])
        assert ok2 is True and r2["title"] == "원본 제목"
        assert store.has_original("a" * 32) is True


def test_snapshot_idempotent_keeps_first():
    with tempfile.TemporaryDirectory() as td:
        store = _store(Path(td))
        cur = _seed(store)
        store.save_original_snapshot(cur)
        edited = _edit_everything(store, cur["updatedAt"])
        # 편집본으로 다시 스냅샷 시도 → 무시(원본 불변)
        store.save_original_snapshot(edited)
        reverted, ok = store.restore_original("a" * 32, edited["updatedAt"])
        assert ok is True and reverted["title"] == "원본 제목", "두 번째 스냅샷이 원본을 덮지 않음"


def test_regenerate_undo_ignores_original():
    with tempfile.TemporaryDirectory() as td:
        store = _store(Path(td))
        cur = _seed(store)
        store.save_original_snapshot(cur)
        # 재요약 백업이 하나도 없고 원본만 있는 상태 → undo 는 복원할 백업 없음(원본 미소비)
        _, ok = store.restore_latest_backup("a" * 32, cur["updatedAt"])
        assert ok is False, "재요약 undo 가 원본 스냅샷을 집어삼키지 않음"
        assert store.has_original("a" * 32) is True, "원본은 그대로 보존"


def test_prune_keeps_original():
    with tempfile.TemporaryDirectory() as td:
        store = _store(Path(td))
        cur = _seed(store)
        store.save_original_snapshot(cur)
        # 매우 짧은 만료(미래 cutoff)로 전부 만료 대상이 되게 → 그래도 원본은 남는다
        store.prune_expired_backups(max_age_seconds=-1)
        assert store.has_original("a" * 32) is True, "prune 이 원본을 지우지 않음"


def test_revert_without_original_returns_current():
    with tempfile.TemporaryDirectory() as td:
        store = _store(Path(td))
        cur = _seed(store)  # 스냅샷 저장 안 함(구 회의 모사)
        result, ok = store.restore_original("a" * 32, cur["updatedAt"])
        assert ok is False
        assert result is not None and result["title"] == "원본 제목", "원본 없으면 현행 반환(변경 없음)"


def test_revert_missing_meeting():
    with tempfile.TemporaryDirectory() as td:
        store = _store(Path(td))
        result, ok = store.restore_original("f" * 32, None)
        assert result is None and ok is False
