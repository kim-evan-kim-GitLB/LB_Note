"""온프렘 로컬 인증 — ID/PW + JWT (프론트 `src/lib/firebase.ts` 계약 충족).

프론트 계약:
  - POST /api/auth/login {username, password} -> {token, user:{id, username, displayName?, role?}}
  - GET  /api/auth/me   (Authorization: Bearer <token>) -> user
  - 모든 /api 요청에 Bearer 자동 주입 → 데이터/AI 엔드포인트도 require_user 로 보호.

설계:
  - 비밀번호: passlib pbkdf2_sha256(순수 파이썬, bcrypt 백엔드 불필요).
  - 토큰: PyJWT HS256, 서명키=env JWT_SECRET. 만료=WEB_AUTH_TOKEN_TTL 초(기본 7일).
  - 사용자: SQLite users 테이블(저장소와 같은 DB 파일). env WEB_AUTH_USERS 가 사용자 목록의
    단일 진실원천 — "user:pass,user2:pass2" 형식, 부팅 시 seed_user(없으면 env 비번으로 생성,
    있으면 **비번 보존**+역할만 동기화 → 셀프 비번 변경이 재기동에 안 되돌아감). 비어있고 사용자도
    없으면 기본 admin/admin 시드(경고 로그) → 운영 전 교체 권장.
  - 셀프 비번 변경: POST /api/auth/change-password (현재 비번 검증 후 set_password).

무거운 의존성 없이 stdlib + 이미 설치된 PyJWT/passlib 만 사용.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import threading

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.hash import pbkdf2_sha256

from src.web.store import DEFAULT_DB_PATH


def _secret() -> str:
    # import 순서와 무관하게 호출 시점에 읽는다(.env 로드 타이밍 방어).
    return os.environ.get("JWT_SECRET", "")


def _ttl() -> int:
    return int(os.environ.get("WEB_AUTH_TOKEN_TTL", str(7 * 24 * 3600)))  # 기본 7일

_USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    display_name  TEXT,
    role          TEXT DEFAULT 'user',   -- 권한(user/developer/admin). 직함과 무관.
    english_name  TEXT,                  -- 영어 이름(이메일 @앞). 아바타 이니셜·보조 표기용.
    job_title     TEXT,                  -- 직함(대표이사/팀장/프로 등). 권한 role 과 별개.
    created_at    TEXT
);
CREATE TABLE IF NOT EXISTS claude_credentials (
    username   TEXT PRIMARY KEY,
    cred_type  TEXT NOT NULL,   -- 'api_key' | 'oauth_token'
    secret     TEXT NOT NULL,   -- 평문 보관(현 .env/~/.claude 수준). API 응답엔 절대 미노출.
    updated_at TEXT
);
"""

# 사용자별 claude 자격증명 종류. api_key=ANTHROPIC_API_KEY(만료 없음, --bare 격리),
# oauth_token=CLAUDE_CODE_OAUTH_TOKEN(구독 토큰, HOME 교정 불필요).
_CRED_TYPES = ("api_key", "oauth_token")


class UserStore:
    """사용자 CRUD(스레드 안전). 저장소와 동일 DB 파일에 users 테이블."""

    def __init__(self, db_path=None) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_USERS_SCHEMA)
            # 기존 DB 마이그레이션: 신규 컬럼이 없으면 추가(이미 있으면 OperationalError 무시).
            for col in ("english_name", "job_title"):
                try:
                    self._conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError:
                    pass  # 이미 존재
            self._conn.commit()

    def upsert(
        self,
        username: str,
        password: str,
        *,
        display_name=None,
        role="user",
        english_name=None,
        job_title=None,
    ) -> None:
        """사용자 생성/비번 갱신(전체 덮어쓰기). 기존이면 비번·표시명·역할·영어이름·직함 갱신."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO users "
                "(username, password_hash, display_name, role, english_name, job_title, created_at) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(username) DO UPDATE SET "
                "password_hash=excluded.password_hash, display_name=excluded.display_name, "
                "role=excluded.role, english_name=excluded.english_name, "
                "job_title=excluded.job_title",
                (
                    username,
                    pbkdf2_sha256.hash(password),
                    display_name or username,
                    role,
                    english_name,
                    job_title,
                    dt.datetime.now().isoformat(timespec="seconds"),
                ),
            )
            self._conn.commit()

    def seed_user(self, username: str, password: str, *, display_name=None, role="user") -> None:
        """부팅 시드용 — 없으면 env 비번으로 생성, 있으면 **비번은 보존**하고 역할·표시명만 동기화.

        upsert 와 달리 기존 사용자의 password_hash 를 덮어쓰지 않는다 → 사용자가 셀프로 바꾼
        비밀번호가 재기동 시 env 값으로 되돌아가지 않는다. (env WEB_AUTH_USERS 는 '누가 존재하나'
        와 '초기 비번·역할'의 원천이지, 변경된 비번까지 강제 동기화하지는 않는다. 관리자가 비번을
        강제 초기화하려면 WEB_AUTH_USERS 에서 해당 계정을 제거(prune 삭제) 후 다시 추가하면 된다.)
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO users (username, password_hash, display_name, role, created_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(username) DO UPDATE SET "
                "display_name=excluded.display_name, role=excluded.role",  # 비번(password_hash) 미갱신
                (
                    username,
                    pbkdf2_sha256.hash(password),
                    display_name or username,
                    role,
                    dt.datetime.now().isoformat(timespec="seconds"),
                ),
            )
            self._conn.commit()

    def set_password(self, username: str, new_password: str) -> bool:
        """사용자 비밀번호만 갱신(셀프 변경용). 존재하면 True. 역할·표시명은 건드리지 않음."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET password_hash=? WHERE username=?",
                (pbkdf2_sha256.hash(new_password), username),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def get(self, username: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT username, password_hash, display_name, role, english_name, job_title "
                "FROM users WHERE username=?",
                (username,),
            ).fetchone()
        return dict(row) if row else None

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]

    def usernames(self) -> list[str]:
        with self._lock:
            return [r["username"] for r in self._conn.execute("SELECT username FROM users")]

    def delete(self, username: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM users WHERE username=?", (username,))
            self._conn.commit()
        return cur.rowcount > 0

    def verify(self, username: str, password: str) -> dict | None:
        """ID/PW 검증. 성공 시 공개 user dict, 실패 시 None."""
        u = self.get(username)
        if not u or not pbkdf2_sha256.verify(password, u["password_hash"]):
            return None
        return public_user(u)

    # ---- 사용자별 claude 자격증명(claude_credentials 테이블) ----
    def set_credential(self, username: str, cred_type: str, secret: str) -> None:
        """사용자 claude 자격증명 저장/갱신. cred_type 검증, secret 평문 보관(로그 금지)."""
        if cred_type not in _CRED_TYPES:
            raise ValueError(
                f"알 수 없는 cred_type: {cred_type!r} (지원: {', '.join(_CRED_TYPES)})"
            )
        secret = (secret or "").strip()
        if not secret:
            raise ValueError("secret 이 비어 있습니다.")
        with self._lock:
            self._conn.execute(
                "INSERT INTO claude_credentials (username, cred_type, secret, updated_at) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(username) DO UPDATE SET "
                "cred_type=excluded.cred_type, secret=excluded.secret, "
                "updated_at=excluded.updated_at",
                (username, cred_type, secret, dt.datetime.now().isoformat(timespec="seconds")),
            )
            self._conn.commit()

    def get_credential(self, username: str) -> dict | None:
        """사용자 자격증명(secret 포함, **내부 주입 전용**). 없으면 None.

        반환: {"type": cred_type, "secret": ..., "updated_at": ...}. 이 dict 는 절대 API 응답에
        그대로 실으면 안 된다(secret 노출). 공개 상태는 credential_status() 사용.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT cred_type, secret, updated_at FROM claude_credentials WHERE username=?",
                (username,),
            ).fetchone()
        if not row:
            return None
        return {"type": row["cred_type"], "secret": row["secret"], "updated_at": row["updated_at"]}

    def clear_credential(self, username: str) -> bool:
        """사용자 자격증명 삭제. 삭제된 행이 있으면 True."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM claude_credentials WHERE username=?", (username,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def credential_status(self, username: str) -> dict:
        """자격증명 공개 상태(secret **비노출**). 설정 UI/상태 표시용.

        반환: {"configured": bool, "type": str|None, "updated_at": str|None}.
        """
        cred = self.get_credential(username)
        if not cred:
            return {"configured": False, "type": None, "updated_at": None}
        return {"configured": True, "type": cred["type"], "updated_at": cred["updated_at"]}


def public_user(u: dict) -> dict:
    """프론트 계약 user 객체(id/username/displayName/role). 비번 해시는 절대 노출 안 함."""
    return {
        "id": u["username"],
        "username": u["username"],
        "displayName": u.get("display_name") or u["username"],
        "role": u.get("role") or "user",
        "englishName": u.get("english_name"),
        "jobTitle": u.get("job_title"),
    }


def make_token(username: str) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {"sub": username, "iat": now, "exp": now + dt.timedelta(seconds=_ttl())}
    return jwt.encode(payload, _secret(), algorithm="HS256")


def _decode(token: str) -> dict:
    return jwt.decode(token, _secret(), algorithms=["HS256"])


# ---- 모듈 싱글턴: app.py 가 init() 호출 ----
_store: UserStore | None = None


def init(db_path=None) -> UserStore:
    """UserStore 생성 + env(WEB_AUTH_USERS) 시드/동기화. JWT_SECRET 미설정이면 즉시 실패."""
    global _store
    if not _secret():
        raise RuntimeError("JWT_SECRET 미설정 — 인증 토큰 서명 불가. .env 에 설정하세요.")
    _store = UserStore(db_path)

    # 정책: 계정은 개발자·어드민만. 자가가입 없음(가입 엔드포인트 미존재) → 발급은 .env 로만.
    # WEB_AUTH_ADMINS 에 적힌 사용자는 role=admin, 그 외 WEB_AUTH_USERS 사용자는 role=developer.
    admins = {
        u.strip() for u in os.environ.get("WEB_AUTH_ADMINS", "admin").split(",") if u.strip()
    }
    listed: list[str] = []
    spec = os.environ.get("WEB_AUTH_USERS", "").strip()
    if spec:
        for pair in spec.split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            username, password = pair.split(":", 1)
            username, password = username.strip(), password.strip()
            if not username or not password:
                continue
            role = "admin" if username in admins else "developer"
            # seed_user: 신규면 env 비번으로 생성, 기존이면 비번 보존(셀프 변경 유지)+역할 동기화.
            _store.seed_user(username, password, role=role)
            listed.append(username)

    # prune: env(WEB_AUTH_USERS)를 계정의 단일 진실원천으로 — 목록에 없는 계정 제거.
    # (다른 경로로 생긴 계정/구 데모 계정 정리). WEB_AUTH_PRUNE=0 으로 끌 수 있음.
    pruned: list[str] = []
    if listed and os.environ.get("WEB_AUTH_PRUNE", "1") != "0":
        for u in _store.usernames():
            if u not in listed:
                _store.delete(u)
                pruned.append(u)

    if _store.count() == 0:
        _store.upsert("admin", "admin", role="admin")
        listed.append("admin")
        print("[auth] 경고: 사용자가 없어 기본 계정 admin/admin 을 생성했습니다. "
              "운영 전 WEB_AUTH_USERS 로 교체하세요.", flush=True)
    print(f"[auth] 사용자 {_store.count()}명 (등록: {', '.join(listed) or '없음'}"
          f"{' / 제거: ' + ', '.join(pruned) if pruned else ''})", flush=True)
    return _store


def store() -> UserStore:
    if _store is None:
        raise RuntimeError("auth.init() 가 호출되지 않았습니다.")
    return _store


# ---- 모듈 레벨 자격증명 헬퍼(싱글턴 store 위임) ----
def set_credential(username: str, cred_type: str, secret: str) -> None:
    store().set_credential(username, cred_type, secret)


def get_credential(username: str) -> dict | None:
    """secret 포함 — 내부 주입 전용. API 응답에 그대로 싣지 말 것."""
    return store().get_credential(username)


def clear_credential(username: str) -> bool:
    return store().clear_credential(username)


def credential_status(username: str) -> dict:
    """secret 비노출 공개 상태."""
    return store().credential_status(username)


# ---- FastAPI 의존성: Bearer 토큰 검증 → 현재 사용자 ----
_bearer = HTTPBearer(auto_error=False)


def require_user(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
    """유효 Bearer 토큰 → 공개 user dict. 없거나 무효/만료/미존재 사용자면 401."""
    if cred is None or not cred.credentials:
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    try:
        payload = _decode(cred.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="토큰이 만료되었습니다.")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="토큰이 유효하지 않습니다.")
    u = store().get(payload.get("sub", ""))
    if not u:
        raise HTTPException(status_code=401, detail="사용자를 찾을 수 없습니다.")
    return public_user(u)
