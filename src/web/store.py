"""SQLite 영속 — Firestore 대체 (플랜 D2).

meetscript-ai 의 meetingService 가 Firestore 에 저장하던 Meeting 을 로컬 SQLite 로 영속한다.
온프렘 전제: 회의 내용이 외부(Firebase 클라우드)로 나가지 않게 한다.

스키마는 프론트 `types.ts` 의 Meeting 을 그대로 보존한다. 인덱싱 키(id, owner_id, status,
created_at)만 컬럼으로 빼고, 나머지(participants/transcript/actionItems/resources 등)는
data JSON 컬럼에 통째로 저장 → 프론트 타입과 1:1, 변환 손실 없음.

무의존성: stdlib sqlite3 + json 만 사용.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

# 기본 DB 경로: output/ 는 .gitignore 대상이라 커밋 안 됨(시크릿/대용량 정책과 동일 영역).
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "output" / "web" / "meetings.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id         TEXT PRIMARY KEY,
    owner_id   TEXT NOT NULL,
    status     TEXT,
    title      TEXT,
    created_at TEXT,
    updated_at TEXT,
    data       TEXT NOT NULL   -- 전체 Meeting JSON(프론트 types.ts 보존)
);
CREATE INDEX IF NOT EXISTS idx_meetings_owner ON meetings(owner_id, created_at DESC);
"""


class MeetingStore:
    """Meeting CRUD (스레드 안전). 단일 프로세스 FastAPI + BackgroundTasks 가정."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + Lock: BackgroundTask(별도 스레드)에서도 같은 연결 사용.
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def create(self, meeting: dict) -> dict:
        """Meeting dict 저장(id 필수). data JSON + 인덱스 컬럼 동기화."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meetings "
                "(id, owner_id, status, title, created_at, updated_at, data) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    meeting["id"],
                    meeting.get("ownerId", "local"),
                    meeting.get("status"),
                    meeting.get("title"),
                    meeting.get("createdAt"),
                    meeting.get("updatedAt"),
                    json.dumps(meeting, ensure_ascii=False),
                ),
            )
            self._conn.commit()
        return meeting

    def get(self, meeting_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM meetings WHERE id=?", (meeting_id,)
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def list(self, owner_id: str = "local") -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM meetings WHERE owner_id=? ORDER BY created_at DESC",
                (owner_id,),
            ).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def update(self, meeting_id: str, patch: dict) -> dict | None:
        """기존 Meeting 에 patch 병합 후 저장. 없으면 None."""
        cur = self.get(meeting_id)
        if cur is None:
            return None
        cur.update(patch)
        return self.create(cur)

    def delete(self, meeting_id: str) -> bool:
        """삭제. 존재했으면 True."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM meetings WHERE id=?", (meeting_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0
