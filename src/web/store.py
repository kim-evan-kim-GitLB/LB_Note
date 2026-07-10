"""SQLite 영속 — Firestore 대체 (플랜 D2).

meetscript-ai 의 meetingService 가 Firestore 에 저장하던 Meeting 을 로컬 SQLite 로 영속한다.
온프렘 전제: 회의 내용이 외부(Firebase 클라우드)로 나가지 않게 한다.

스키마는 프론트 `types.ts` 의 Meeting 을 그대로 보존한다. 인덱싱 키(id, owner_id, status,
created_at)만 컬럼으로 빼고, 나머지(participants/transcript/actionItems/resources 등)는
data JSON 컬럼에 통째로 저장 → 프론트 타입과 1:1, 변환 손실 없음.

무의존성: stdlib sqlite3 + json 만 사용.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import sqlite3
import threading
from collections.abc import Callable
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

-- 재요약 undo 백업: 적용 직전 summary/actionItems 스냅샷(별도 저장, data 인라인 비대 회피).
CREATE TABLE IF NOT EXISTS meeting_backup (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reason     TEXT,
    data       TEXT NOT NULL   -- {"summary":..., "actionItems":...} JSON 스냅샷
);
CREATE INDEX IF NOT EXISTS idx_backup_meeting ON meeting_backup(meeting_id, id DESC);

-- Slack 봇 요구사항/건의 적재(help·요구사항 저장). source='slack'|'web', status=open|done|dropped.
CREATE TABLE IF NOT EXISTS requirements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL DEFAULT 'slack',
    reporter   TEXT,                          -- slack 이메일/표시명(있으면)
    text       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requirements_status ON requirements(status, id DESC);

-- 공지사항(웹 관리자 콘솔 작성 → Slack 봇 `공지` 가 최신 활성 공지를 읽어 배포). active=1 만 노출.
CREATE TABLE IF NOT EXISTS notices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT,                            -- 선택(제목)
    body       TEXT NOT NULL,                   -- 공지 본문
    active     INTEGER NOT NULL DEFAULT 1,      -- 1=활성(배포/노출 대상), 0=숨김
    created_by TEXT,                            -- 작성 관리자 username
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notices_active ON notices(active, id DESC);
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
            self._persist_locked(cur)  # 단일 write 경로(컬럼 동기화 일원화)
            self._conn.commit()
        return cur

    def _persist_locked(self, cur: dict) -> None:
        """meetings 행 기록(이미 _lock 보유 중에만 호출). data + 인덱스 컬럼 동기화."""
        self._conn.execute(
            "INSERT OR REPLACE INTO meetings "
            "(id, owner_id, status, title, created_at, updated_at, data) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                cur["id"],
                cur.get("ownerId", "local"),
                cur.get("status"),
                cur.get("title"),
                cur.get("createdAt"),
                cur.get("updatedAt"),
                json.dumps(cur, ensure_ascii=False),
            ),
        )

    def apply_regenerate(
        self,
        meeting_id: str,
        summary: dict,
        action_items: list,
        expected_updated_at: str | None,
    ) -> dict | None:
        """재요약 결과(summary+actionItems) 전면 교체 — 적용 직전 현행을 meeting_backup 에 스냅샷.

        구조 전면 교체이므로 summary 편집(text-only) 검증을 거치지 않는 별도 경로다. compare(If-Match)
        +백업+교체를 단일 _lock 구간에서 원자 수행한다(undo 안전·lost-update 방지). ownerId/transcript/
        title 등 다른 필드는 보존하고 summary·actionItems·updatedAt 만 갱신한다. 없으면 None.
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
            # 적용 직전 현행 스냅샷 백업(undo 용). 별도 테이블에 기록.
            snapshot = {
                "summary": cur.get("summary"),
                "actionItems": cur.get("actionItems", []),
            }
            self._conn.execute(
                "INSERT INTO meeting_backup (meeting_id, created_at, reason, data) VALUES (?,?,?,?)",
                (meeting_id, _now_iso_micro(), "regenerate", json.dumps(snapshot, ensure_ascii=False)),
            )
            cur["summary"] = summary
            cur["actionItems"] = action_items
            cur["updatedAt"] = _next_etag(cur.get("updatedAt"))
            self._persist_locked(cur)
            self._conn.commit()
        return cur

    def restore_latest_backup(
        self, meeting_id: str, expected_updated_at: str | None
    ) -> tuple[dict | None, bool]:
        """가장 최근 meeting_backup 을 복원(재요약 undo). 복원 후 그 백업은 소비(삭제)한다.

        반환: (갱신된 meeting | None, restored). meeting 없으면 (None, False), 백업 없으면
        (현행 meeting, False). compare(If-Match)+복원+백업삭제를 _lock 구간에서 원자 수행한다.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM meetings WHERE id=?", (meeting_id,)
            ).fetchone()
            if row is None:
                return None, False
            cur = json.loads(row["data"])
            if expected_updated_at is not None and cur.get("updatedAt") != expected_updated_at:
                raise PreconditionFailedError(cur.get("updatedAt"))
            brow = self._conn.execute(
                "SELECT id, data FROM meeting_backup WHERE meeting_id=? ORDER BY id DESC LIMIT 1",
                (meeting_id,),
            ).fetchone()
            if brow is None:
                return cur, False
            snap = json.loads(brow["data"])
            cur["summary"] = snap.get("summary")
            cur["actionItems"] = snap.get("actionItems", [])
            cur["updatedAt"] = _next_etag(cur.get("updatedAt"))
            self._persist_locked(cur)
            self._conn.execute("DELETE FROM meeting_backup WHERE id=?", (brow["id"],))
            self._conn.commit()
        return cur, True

    def delete(self, meeting_id: str) -> bool:
        """삭제. 존재했으면 True. 재요약 백업(meeting_backup)도 동반 삭제(고아 행 방지)."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM meetings WHERE id=?", (meeting_id,)
            )
            self._conn.execute("DELETE FROM meeting_backup WHERE meeting_id=?", (meeting_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def prune_expired_backups(self, max_age_seconds: float) -> int:
        """created_at 이 max_age_seconds 초 이전인 재요약 백업 삭제 → 삭제 개수(정리배치 P10).

        apply 후 undo 하지 않은 백업(meeting_backup)이 무한정 쌓여 data 누적·디스크 잠식되는 것을
        막는다. created_at 은 _now_iso_micro(UTC ISO·마이크로초)라 ISO 문자열 비교로 cutoff 판단이
        가능하다(동일 포맷·타임존 일관 → 사전식 비교 == 시간 비교). 회의 자체나 최신 상태는 건드리지
        않는다(이미 apply 로 본문에 반영됨 — 백업은 undo 여력일 뿐, 만료 시 undo 불가만 됨).
        """
        cutoff = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=max_age_seconds)
        ).isoformat(timespec="microseconds")
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM meeting_backup WHERE created_at < ?", (cutoff,)
            )
            self._conn.commit()
        return cur.rowcount

    def count_backups(self) -> int:
        """현재 meeting_backup 행 수(메트릭/관측성용)."""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM meeting_backup").fetchone()
        return int(row["n"]) if row else 0

    def count_meetings(self) -> int:
        """현재 meetings 행 수(메트릭/관측성용)."""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM meetings").fetchone()
        return int(row["n"]) if row else 0

    def backup_to(self, dest: Path | str) -> Path:
        """현재 DB 의 일관 스냅샷을 dest 파일로 기록(sqlite 온라인 backup API) → dest 경로.

        파일 복사가 아니라 sqlite backup API 를 쓴다 — 쓰기 진행 중에도 트랜잭션 일관 스냅샷을
        보장한다(부분 기록본 방지). 과거 meetings.db 무백업 prune 사고(사용자 비번 전부 리셋·
        복원불가) 재발 방지용 안전장치다.

        락 점유 최소화: dest 연결 생성·디렉토리 준비는 락 밖에서 한다(소스 일관성과 무관). 실제
        backup() 만 _lock 구간에서 수행해 백업 중 본 연결의 동시 쓰기를 막는다(백업 동안 다른
        store 쓰기/읽기도 직렬화됨 — DB 가 작아 점유는 짧다). 스냅샷은 비번 해시 포함 DB 사본이라
        파일 0600·디렉토리 0700 로 제한한다. backup 실패 시 부분 기록본을 정리(unlink)한다.
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            dest.parent.chmod(0o700)
        target = sqlite3.connect(str(dest))
        try:
            with self._lock:
                self._conn.commit()  # 보류 트랜잭션을 닫아 일관 스냅샷 보장(방어)
                self._conn.backup(target)
        except Exception:
            target.close()
            dest.unlink(missing_ok=True)  # 부분 기록본 제거(잘못된 백업 오인 복원 방지)
            raise
        target.close()
        with contextlib.suppress(OSError):
            dest.chmod(0o600)  # 비번 해시 포함 사본 — 소유자만 접근
        return dest


class RequirementStore:
    """Slack 봇 요구사항/건의 적재(스레드 안전). MeetingStore 와 동일 패턴의 경량 스토어.

    같은 DB 파일을 공유하지만 자체 연결을 연다(MeetingStore 와 독립 락). 스키마는 _SCHEMA 를
    실행해 idempotent 하게 보장한다(CREATE ... IF NOT EXISTS 라 재실행 무해).
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        _guard_default_db(self.db_path.resolve())
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def add(
        self, text: str, *, source: str = "slack", reporter: str | None = None
    ) -> dict:
        """요구사항 1건 적재 → 생성된 행 dict(id 포함). created_at 은 UTC ISO·마이크로초."""
        created_at = _now_iso_micro()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO requirements (source, reporter, text, status, created_at) "
                "VALUES (?,?,?,?,?)",
                (source, reporter, text, "open", created_at),
            )
            self._conn.commit()
            rowid = cur.lastrowid
        return {
            "id": rowid,
            "text": text,
            "source": source,
            "reporter": reporter,
            "status": "open",
            "created_at": created_at,
        }

    def list(self, status: str | None = None) -> list[dict]:
        """요구사항 목록(최신 id 우선). status 지정 시 그 상태만 필터."""
        with self._lock:
            if status is not None:
                rows = self._conn.execute(
                    "SELECT id, source, reporter, text, status, created_at "
                    "FROM requirements WHERE status=? ORDER BY id DESC",
                    (status,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, source, reporter, text, status, created_at "
                    "FROM requirements ORDER BY id DESC"
                ).fetchall()
        return [dict(r) for r in rows]


class NoticeStore:
    """공지사항 저장(스레드 안전). 웹 관리자 콘솔이 작성/관리하고 Slack 봇이 최신 활성 공지를 읽는다.

    RequirementStore 와 동일 패턴(같은 DB, 독립 연결/락). 스키마는 _SCHEMA 로 idempotent 보장.
    """

    _COLS = "id, title, body, active, created_by, created_at, updated_at"

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        _guard_default_db(self.db_path.resolve())
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def add(self, body: str, *, title: str | None = None, created_by: str | None = None) -> dict:
        """공지 1건 작성(active=1) → 생성 행 dict. created_at/updated_at 은 동일 시각."""
        now = _now_iso_micro()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO notices (title, body, active, created_by, created_at, updated_at) "
                "VALUES (?,?,1,?,?,?)",
                (title, body, created_by, now, now),
            )
            self._conn.commit()
            rowid = cur.lastrowid
        return self.get(rowid)  # type: ignore[return-value]

    def get(self, notice_id: int) -> dict | None:
        """단건 조회. 없으면 None."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._COLS} FROM notices WHERE id=?", (notice_id,)
            ).fetchone()
        return dict(row) if row else None

    def latest(self) -> dict | None:
        """가장 최근 **활성**(active=1) 공지. 없으면 None. 봇 `공지` 가 읽는 진입점."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._COLS} FROM notices WHERE active=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def list(self) -> list[dict]:
        """전체 공지 목록(최신 id 우선) — 관리자 콘솔용."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._COLS} FROM notices ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def update(
        self,
        notice_id: int,
        *,
        body: str | None = None,
        title: str | None = None,
        active: bool | None = None,
    ) -> dict | None:
        """지정 필드만 갱신(None 은 미변경). 대상 없으면 None. updated_at 갱신."""
        sets: list[str] = []
        params: list = []
        if body is not None:
            sets.append("body=?")
            params.append(body)
        if title is not None:
            sets.append("title=?")
            params.append(title)
        if active is not None:
            sets.append("active=?")
            params.append(1 if active else 0)
        if not sets:
            return self.get(notice_id)  # 변경 없음 → 현재 상태 반환
        sets.append("updated_at=?")
        params.append(_now_iso_micro())
        params.append(notice_id)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE notices SET {', '.join(sets)} WHERE id=?", params
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get(notice_id)

    def delete(self, notice_id: int) -> bool:
        """공지 삭제. 삭제된 행이 있으면 True."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM notices WHERE id=?", (notice_id,))
            self._conn.commit()
            return cur.rowcount > 0
