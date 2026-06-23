"""SQLite 영속 — Firestore 대체 (플랜 D2).

meetscript-ai 의 meetingService 가 Firestore 에 저장하던 Meeting 을 로컬 SQLite 로 영속한다.
온프렘 전제: 회의 내용이 외부(Firebase 클라우드)로 나가지 않게 한다.

스키마는 프론트 `types.ts` 의 Meeting 을 그대로 보존한다. 인덱싱 키(id, owner_id, status,
created_at)만 컬럼으로 빼고, 나머지(participants/transcript/actionItems/resources 등)는
data JSON 컬럼에 통째로 저장 → 프론트 타입과 1:1, 변환 손실 없음.

무의존성: stdlib sqlite3 + json 만 사용.
"""
from __future__ import annotations

import datetime as _dt
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


class PreconditionFailedError(Exception):
    """If-Match 불일치(낙관적 락 실패). 호출부가 412 로 변환한다.

    current_updated_at 에 저장본의 현재 updatedAt(재조회 힌트)을 실어 보낸다."""

    def __init__(self, current_updated_at: str | None) -> None:
        super().__init__("If-Match precondition failed")
        self.current_updated_at = current_updated_at


def _now_iso_micro() -> str:
    """ETag(updatedAt) 용 타임스탬프. 같은 초 내 연속 갱신도 구분되도록 마이크로초까지."""
    return _dt.datetime.now().isoformat(timespec="microseconds")


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
        """기존 Meeting 에 patch 병합 후 저장. 없으면 None.

        주의: get→merge→create 가 비원자(락 밖 race). 동시성 보장이 필요한 편집 경로는
        update_if_match() 를 쓴다(아래). 본 메서드는 last-write-wins 후방호환 경로 전용.
        """
        cur = self.get(meeting_id)
        if cur is None:
            return None
        cur.update(patch)
        return self.create(cur)

    def update_if_match(
        self, meeting_id: str, patch: dict, expected_updated_at: str | None
    ) -> dict | None:
        """원자 compare-and-update. read+compare+write 를 단일 _lock 구간에서 수행.

        - 없으면 None.
        - expected_updated_at 가 None 이면 비교 생략(무조건 적용, last-write-wins).
        - expected_updated_at 가 주어졌고 저장본 updatedAt 과 다르면 PreconditionFailedError.
        - ownerId 는 patch 로 바꿀 수 없다(불변). updatedAt 은 항상 서버가 새로 부여(ETag).

        store._lock 한 구간 안에서 SELECT→비교→INSERT OR REPLACE 를 끝내므로
        get→update→create(store.update) 의 비원자 race 가 없다.

        락 순서 규약: 잡 스레드는 _stt_semaphore 보유 중 이 락(_lock)을 잡지 않는다
        (STT 추론은 store 비접촉, 추론 후에만 갱신) → 데드락/장시간 점유 없음.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM meetings WHERE id=?", (meeting_id,)
            ).fetchone()
            if row is None:
                return None
            cur = json.loads(row["data"])
            if expected_updated_at is not None and cur.get("updatedAt") != expected_updated_at:
                raise PreconditionFailedError(cur.get("updatedAt"))
            owner = cur.get("ownerId", "local")  # ownerId 불변 보장
            cur.update(patch)
            cur["ownerId"] = owner
            cur["updatedAt"] = _now_iso_micro()  # 갱신마다 새 ETag
            self._conn.execute(
                "INSERT OR REPLACE INTO meetings "
                "(id, owner_id, status, title, created_at, updated_at, data) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    cur["id"],
                    owner,
                    cur.get("status"),
                    cur.get("title"),
                    cur.get("createdAt"),
                    cur["updatedAt"],
                    json.dumps(cur, ensure_ascii=False),
                ),
            )
            self._conn.commit()
        return cur

    def delete(self, meeting_id: str) -> bool:
        """삭제. 존재했으면 True."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM meetings WHERE id=?", (meeting_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0
