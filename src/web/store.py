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

# 가드레일 비교 기준 — **실 운영 DB 의 고정 절대경로**. DEFAULT_DB_PATH 는 테스트가 임시 경로로
# 패치(rebind)하므로 비교 기준으로 쓰면 패치된 임시 경로끼리 같아져 정상 격리 테스트도 막힌다.
# 따라서 모듈 로드 시 1회 계산한 불변 상수를 기준으로, 실 경로를 열 때만 차단한다.
_REAL_DB_PATH = DEFAULT_DB_PATH.resolve()


def _guard_default_db(resolved: Path) -> None:
    """테스트 격리 가드레일 — MEETSCRIPT_BLOCK_DEFAULT_DB=1 일 때 **실 운영 DB 경로** 접촉 거부.

    테스트가 DEFAULT_DB_PATH 패치를 빠뜨려 실 운영 DB(output/web/meetings.db)를 열려 하면
    즉시 RuntimeError 로 막는다(과거 실 DB 2회 변조 사고 재발 방지). env 미설정인 정상 부팅은
    영향 없다(가드 자체가 동작 안 함). 임시 경로로 올바르게 격리된 테스트는 통과한다."""
    import os

    if os.environ.get("MEETSCRIPT_BLOCK_DEFAULT_DB") != "1":
        return
    if Path(resolved).resolve() == _REAL_DB_PATH:
        raise RuntimeError(
            "MEETSCRIPT_BLOCK_DEFAULT_DB=1 인데 기본 실 DB 경로"
            f"({_REAL_DB_PATH})를 열려고 했습니다 — 테스트 격리 누락. "
            "테스트는 DEFAULT_DB_PATH 를 임시 경로로 패치해야 합니다."
        )

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
        _guard_default_db(self.db_path.resolve())
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
