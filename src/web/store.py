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
from collections.abc import Callable
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
    """ETag(updatedAt) 용 타임스탬프. UTC·마이크로초 기준(M1: create/update 포맷 통일)."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="microseconds")


def _next_etag(prev: str | None) -> str:
    """단조 증가하는 새 ETag(updatedAt) 생성.

    같은 마이크로초 안에서 연속 갱신되면 now() 가 직전 updatedAt 과 동일해 ETag 충돌(낙관적
    락 우회) 위험이 있다 → 직전 값과 같거나 더 작으면 직전 +1µs 로 보정해 단조 증가를 보장한다.
    prev 가 다른 포맷(예: 초 단위·naive)이라 파싱 불가하면 비교 없이 현재값을 그대로 쓴다.
    """
    now = _now_iso_micro()
    if not prev or now > prev:
        return now
    try:
        prev_dt = _dt.datetime.fromisoformat(prev)
    except ValueError:
        return now
    if prev_dt.tzinfo is None:  # naive 저장본은 비교 불가 → 현재값 사용
        return now
    return (prev_dt + _dt.timedelta(microseconds=1)).isoformat(timespec="microseconds")


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
        self,
        meeting_id: str,
        patch: dict,
        expected_updated_at: str | None,
        *,
        validator: Callable[[dict, dict], dict] | None = None,
    ) -> dict | None:
        """원자 compare-and-update. read+compare+(검증)+write 를 단일 _lock 구간에서 수행.

        - 없으면 None.
        - expected_updated_at 가 None 이면 비교 생략(무조건 적용, last-write-wins).
        - expected_updated_at 가 주어졌고 저장본 updatedAt 과 다르면 PreconditionFailedError.
        - ownerId 는 patch 로 바꿀 수 없다(불변). updatedAt 은 항상 서버가 새로 부여(ETag).

        validator(M2, TOCTOU 차단): 주어지면 **락 안에서 재조회한 바로 그 저장본(cur)** 과
        patch 를 받아 검증·정규화된 새 patch 를 반환한다(원본 patch 비파괴). 검증 실패는
        validator 가 예외를 던지며, 그 예외는 락 밖으로 그대로 전파되어 호출부가 422 로 변환한다.
        즉 "검증에 쓴 스냅샷 == write 대상 스냅샷" 이 보장된다(락 밖 읽기본 기준 검증 금지).

        store._lock 한 구간 안에서 SELECT→비교→검증→INSERT OR REPLACE 를 끝내므로
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
            # M2: 락 안에서 재조회한 cur 기준으로 검증·정규화(write 대상과 동일 스냅샷).
            if validator is not None:
                patch = validator(cur, patch)
            owner = cur.get("ownerId", "local")  # ownerId 불변 보장
            prev_updated = cur.get("updatedAt")
            cur.update(patch)
            cur["ownerId"] = owner
            cur["updatedAt"] = _next_etag(prev_updated)  # 갱신마다 새 ETag(단조 증가)
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
