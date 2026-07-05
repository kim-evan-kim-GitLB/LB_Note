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
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.hash import pbkdf2_sha256

from src.web.store import DEFAULT_DB_PATH, _guard_default_db


def _secret() -> str:
    # import 순서와 무관하게 호출 시점에 읽는다(.env 로드 타이밍 방어).
    return os.environ.get("JWT_SECRET", "")


# ---- 자격증명 at-rest 암호화(Fernet) ----
# CRED_ENC_KEY(Fernet 키, base64 32B)가 설정돼 있으면 claude_credentials.secret 를
# 암호화해 저장한다. 미설정이면 **기존과 동일하게 평문 저장**(하위호환 — 무중단). 읽기는
# 복호 실패 시 평문으로 폴백하므로, 기존 평문 자격증명도 그대로 동작한다(점진 마이그레이션).
def _cred_cipher() -> Fernet | None:
    key = os.environ.get("CRED_ENC_KEY", "").strip()
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except Exception as e:  # noqa: BLE001
        # 잘못된 키로 조용히 평문 저장하면 보안 의도가 무너지므로 즉시 드러낸다.
        raise RuntimeError(
            "CRED_ENC_KEY 형식 오류 — Fernet 키여야 합니다"
            "(생성: python -c \"from cryptography.fernet import Fernet; "
            f"print(Fernet.generate_key().decode())\"). 원인: {e}"
        ) from e


# 암호문 식별 접두사. 저장값이 이걸로 시작하면 "암호문", 아니면 "레거시 평문"으로 결정적으로
# 구분한다(InvalidToken 추측 금지 → 키 교체/불일치 시 암호문을 평문으로 오판해 이중 암호화하는
# 비가역 손상을 방지). 실제 시크릿(sk-ant-.../oauth)은 이 접두사로 시작하지 않는다.
_ENC_PREFIX = "enc:fernet:"


def _enc_secret(secret: str) -> str:
    """저장용 변환. 키 있으면 '접두사+암호문', 없으면 평문 그대로(하위호환)."""
    cipher = _cred_cipher()
    if cipher is None:
        return secret
    return _ENC_PREFIX + cipher.encrypt(secret.encode()).decode()


def _dec_secret(stored: str) -> str:
    """저장값 → 평문. 접두사 없으면 레거시 평문(그대로). 접두사 있는데 키 없음/불일치로 복호
    불가면 빈 문자열(미설정 취급) — 디스크의 암호문은 손상 없이 보존되어, 올바른 키 복원 시
    그대로 복호된다(데이터 유실 없음)."""
    if not stored.startswith(_ENC_PREFIX):
        return stored  # 레거시 평문
    token = stored[len(_ENC_PREFIX):]
    cipher = _cred_cipher()
    if cipher is None:
        return ""  # 암호문인데 키 미설정 → 못 읽음(데이터는 보존)
    try:
        return cipher.decrypt(token.encode()).decode()
    except InvalidToken:
        return ""  # 키 불일치 → 못 읽음(데이터는 보존, 키 복원/재설정 필요)


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
    -- 관리자 부여/시드 비번을 본인이 아직 안 바꿈 → 1. 셀프 변경 시 0. 신규 행 기본 1
    -- (관리자가 준 초기 비번은 반드시 한 번 교체하도록 강제). 데이터 엔드포인트는 1이면 403.
    must_change_password INTEGER NOT NULL DEFAULT 1,
    -- display_name 출처: 'seed'(env/시드) | 'user'(본인 self-edit). 'user' 면 seed 재실행이
    -- display_name 을 덮어쓰지 않는다(role 동기화는 무관하게 유지). english/job 은 seed 미관여.
    name_source   TEXT DEFAULT 'seed',
    created_at    TEXT
);
CREATE TABLE IF NOT EXISTS claude_credentials (
    username   TEXT PRIMARY KEY,
    cred_type  TEXT NOT NULL,   -- 'api_key' | 'oauth_token'
    secret     TEXT NOT NULL,   -- 평문 보관(현 .env/~/.claude 수준). API 응답엔 절대 미노출.
    updated_at TEXT
);
-- 사용자별 Google Drive 연동 자격증명(회의록 동기화). claude_credentials 와 PK(username)가
-- 겹치므로 같은 테이블에 섞지 않고 **별도 테이블**로 둔다. refresh_token 은 장수명 오프라인
-- 자격증명이라 _enc_secret(Fernet)로 암호화 저장한다(CRED_ENC_KEY 있으면). root_folder_id 는
-- drive.file 스코프로 앱이 만든 회의록 루트 폴더 id(재동기화 시 재사용). email 은 표시용.
CREATE TABLE IF NOT EXISTS google_credentials (
    username       TEXT PRIMARY KEY,
    refresh_token  TEXT NOT NULL,   -- _enc_secret(Fernet, 접두사 enc:fernet:). API 응답 절대 미노출.
    email          TEXT,            -- 연결된 구글 계정(표시용). 없으면 None.
    root_folder_id TEXT,            -- drive.file 앱 루트 폴더 id(회의록 저장 위치)
    updated_at     TEXT
);
-- 앱 레벨 Google OAuth 클라이언트 설정(배포당 1개 앱 신분증 = client_id/secret). 사용자별이 아니라
-- provider 단일 행. 관리자(role=admin)가 인앱에서 설정 → .env/재시작 없이 즉시 반영. client_secret 은
-- _enc_secret(Fernet)로 암호화. 미설정이면 google_oauth 가 env(GOOGLE_OAUTH_*)로 폴백한다(하위호환).
CREATE TABLE IF NOT EXISTS app_oauth_config (
    provider      TEXT PRIMARY KEY,   -- 'google'
    client_id     TEXT,
    client_secret TEXT,               -- _enc_secret(Fernet). API 응답 절대 미노출.
    redirect_uri  TEXT,
    updated_at    TEXT
);
"""

# 사용자별 claude 자격증명 종류. oauth_token=CLAUDE_CODE_OAUTH_TOKEN(Claude Code CLI 토큰,
# HOME 교정 불필요)이 운영 방식이다.
# api_key=ANTHROPIC_API_KEY(만료 없음, --bare 격리)는 **더미 유지(deprecated)** — 프론트 UI 에서
# 제거돼 신규 저장 경로가 없다. 후방호환(기존에 저장된 api_key 자격증명 동작·주입)을 위해 코드만
# 남겨두며, 새 기능은 oauth_token 기준으로만 추가한다.
_CRED_TYPES = ("api_key", "oauth_token")


class UserStore:
    """사용자 CRUD(스레드 안전). 저장소와 동일 DB 파일에 users 테이블."""

    def __init__(self, db_path=None) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH
        _guard_default_db(self.db_path)
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
            # name_source: display_name 출처('seed'=env/시드, 'user'=본인 self-edit). 'user' 면
            # seed 재실행 시 display_name 을 username 으로 리셋하지 않고 보존한다(role 동기화는 유지).
            try:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN name_source TEXT DEFAULT 'seed'"
                )
            except sqlite3.OperationalError:
                pass  # 이미 존재(신규 DB 는 CREATE TABLE 에 포함)
            # must_change_password: 기존 DB(공용 시드 비번 사용 중)에 컬럼을 추가하면 DEFAULT 1
            # 이 적용돼 기존 사용자 전원이 '비번 변경 필요' 상태가 된다(의도 — 최초 1회 교체 강제).
            try:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 1"
                )
            except sqlite3.OperationalError:
                pass  # 이미 존재(신규 DB 는 CREATE TABLE 에 포함)
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
            # 관리자/스크립트가 비번을 (재)설정하는 경로 → must_change_password=1 강제
            # (부여받은 초기 비번은 본인이 한 번 바꿔야 데이터 기능 사용 가능).
            self._conn.execute(
                "INSERT INTO users "
                "(username, password_hash, display_name, role, english_name, job_title, "
                "must_change_password, created_at) "
                "VALUES (?,?,?,?,?,?,1,?) "
                "ON CONFLICT(username) DO UPDATE SET "
                "password_hash=excluded.password_hash, display_name=excluded.display_name, "
                "role=excluded.role, english_name=excluded.english_name, "
                "job_title=excluded.job_title, must_change_password=1",
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
                # display_name 은 본인이 self-edit(name_source='user')한 경우 보존, 아니면 시드값
                # 으로 갱신(신규 시 username 폴백). role 은 name_source 무관하게 항상 동기화.
                # 비번(password_hash)·english_name·job_title 은 미갱신(기존 동작 유지).
                "display_name=CASE WHEN users.name_source='user' THEN users.display_name "
                "ELSE excluded.display_name END, "
                "role=excluded.role",
                (
                    username,
                    pbkdf2_sha256.hash(password),
                    display_name or username,
                    role,
                    dt.datetime.now().isoformat(timespec="seconds"),
                ),
            )
            self._conn.commit()

    def update_profile(
        self,
        username: str,
        *,
        display_name=None,
        english_name=None,
        job_title=None,
    ) -> dict | None:
        """본인 표시명 self-edit. None 인 필드는 미변경(보낸 것만 갱신). 1개라도 갱신되면
        name_source='user' 로 표시해 seed 재실행이 display_name 을 덮어쓰지 않게 한다.

        username/role/password 는 이 경로로 변경 불가. 갱신 후 공개 user dict 반환(없으면 None).
        실제 갱신 필드가 0개(전부 None)면 name_source 를 'user' 로 승격하지 않고 현재 값을
        그대로 반환한다(빈 PATCH 가 seed 보호 플래그를 임의로 켜지 않게).
        """
        # 실제 갱신 필드가 없으면 no-op: name_source 승격 없이 현재 사용자 반환(행 없으면 None).
        if display_name is None and english_name is None and job_title is None:
            cur = self.get(username)
            return public_user(cur) if cur else None
        sets: list[str] = ["name_source='user'"]
        params: list = []
        if display_name is not None:
            sets.append("display_name=?")
            params.append(display_name)
        if english_name is not None:
            sets.append("english_name=?")
            params.append(english_name)
        if job_title is not None:
            sets.append("job_title=?")
            params.append(job_title)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE users SET {', '.join(sets)} WHERE username=?",
                (*params, username),
            )
            self._conn.commit()
        if cur.rowcount == 0:
            return None
        return public_user(self.get(username))

    def set_password(self, username: str, new_password: str) -> bool:
        """사용자 비밀번호 갱신(셀프 변경용). 존재하면 True. 역할·표시명은 건드리지 않음.

        본인이 직접 바꾼 비번이므로 must_change_password=0 으로 해제 → 강제변경 게이트 통과.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET password_hash=?, must_change_password=0 WHERE username=?",
                (pbkdf2_sha256.hash(new_password), username),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def get(self, username: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT username, password_hash, display_name, role, english_name, job_title, "
                "must_change_password FROM users WHERE username=?",
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
        stored = _enc_secret(secret)  # CRED_ENC_KEY 있으면 암호화, 없으면 평문(하위호환)
        with self._lock:
            self._conn.execute(
                "INSERT INTO claude_credentials (username, cred_type, secret, updated_at) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(username) DO UPDATE SET "
                "cred_type=excluded.cred_type, secret=excluded.secret, "
                "updated_at=excluded.updated_at",
                (username, cred_type, stored, dt.datetime.now().isoformat(timespec="seconds")),
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
        # 저장값 복호(레거시 평문은 그대로). secret 은 내부 주입 전용 — API/로그 미노출.
        secret = _dec_secret(row["secret"])
        if not secret:
            # 암호문인데 키 없음/불일치로 복호 불가 → 미설정으로 취급(전역 폴백). 디스크 데이터는
            # 보존되어 있으므로 올바른 키 복원 시 자동 복구된다(손상 아님).
            return None
        return {
            "type": row["cred_type"],
            "secret": secret,
            "updated_at": row["updated_at"],
        }

    def migrate_encrypt_credentials(self) -> int:
        """CRED_ENC_KEY 설정 시, 평문으로 남은 자격증명을 재암호화. 재암호화한 행 수 반환.

        부팅 시 1회 호출(init). 키 미설정이면 0(아무것도 안 함). 이미 암호문인 행은 건너뛴다 →
        멱등. 기존 평문 자격증명(57명 환경)을 무중단으로 점차 암호화로 올린다.
        """
        cipher = _cred_cipher()
        if cipher is None:
            return 0
        migrated = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT username, secret FROM claude_credentials"
            ).fetchall()
            for r in rows:
                # 접두사가 있으면 이미 암호문 → 절대 재암호화하지 않는다(키 불일치 암호문을
                # 이중 래핑해 비가역 손상시키는 사고 방지). 접두사 없는 것만 = 레거시 평문 → 암호화.
                if r["secret"].startswith(_ENC_PREFIX):
                    continue
                self._conn.execute(
                    "UPDATE claude_credentials SET secret=? WHERE username=?",
                    (_ENC_PREFIX + cipher.encrypt(r["secret"].encode()).decode(), r["username"]),
                )
                migrated += 1
            # google_credentials.refresh_token 도 동일 규약으로 재암호화(레거시 평문만, 멱등).
            grows = self._conn.execute(
                "SELECT username, refresh_token FROM google_credentials"
            ).fetchall()
            for r in grows:
                if r["refresh_token"].startswith(_ENC_PREFIX):
                    continue
                self._conn.execute(
                    "UPDATE google_credentials SET refresh_token=? WHERE username=?",
                    (
                        _ENC_PREFIX + cipher.encrypt(r["refresh_token"].encode()).decode(),
                        r["username"],
                    ),
                )
                migrated += 1
            # app_oauth_config.client_secret 도 동일 규약(레거시 평문만 암호화, 멱등).
            crows = self._conn.execute(
                "SELECT provider, client_secret FROM app_oauth_config"
            ).fetchall()
            for r in crows:
                sec = r["client_secret"] or ""
                if not sec or sec.startswith(_ENC_PREFIX):
                    continue
                self._conn.execute(
                    "UPDATE app_oauth_config SET client_secret=? WHERE provider=?",
                    (_ENC_PREFIX + cipher.encrypt(sec.encode()).decode(), r["provider"]),
                )
                migrated += 1
            if migrated:
                self._conn.commit()
        return migrated

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

    # ---- 사용자별 Google Drive 자격증명(google_credentials 테이블) ----
    def set_google_credential(
        self, username: str, refresh_token: str, *, email: str | None = None
    ) -> None:
        """Google refresh_token 저장/갱신(재연동). refresh_token 은 _enc_secret 로 암호화 보관.

        재연동(prompt=consent)마다 새 refresh_token 이 오므로 upsert 로 덮어쓴다. root_folder_id 는
        여기서 건드리지 않는다(연동 자체와 폴더 확보는 별개 — 폴더는 첫 동기화 때 set_google_root_folder).
        기존 행 재연동 시에도 root_folder_id 는 보존한다(같은 계정 재연동이면 폴더 유효).
        """
        refresh_token = (refresh_token or "").strip()
        if not refresh_token:
            raise ValueError("refresh_token 이 비어 있습니다.")
        stored = _enc_secret(refresh_token)  # CRED_ENC_KEY 있으면 암호화, 없으면 평문(하위호환)
        with self._lock:
            self._conn.execute(
                "INSERT INTO google_credentials "
                "(username, refresh_token, email, root_folder_id, updated_at) "
                "VALUES (?,?,?,NULL,?) "
                "ON CONFLICT(username) DO UPDATE SET "
                "refresh_token=excluded.refresh_token, email=excluded.email, "
                "updated_at=excluded.updated_at",  # root_folder_id 는 보존(재연동 시 유효 폴더 유지)
                (username, stored, email, dt.datetime.now().isoformat(timespec="seconds")),
            )
            self._conn.commit()

    def get_google_credential(self, username: str) -> dict | None:
        """Google 자격증명(refresh_token 포함, **내부 주입 전용**). 없으면 None.

        반환: {"refresh_token": ..., "email": ..., "root_folder_id": ..., "updated_at": ...}.
        refresh_token 은 절대 API 응답에 싣지 않는다(공개 상태는 google_status()). 암호문인데
        키 없음/불일치로 복호 불가면 미설정으로 취급(None) — 디스크 데이터는 보존.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT refresh_token, email, root_folder_id, updated_at "
                "FROM google_credentials WHERE username=?",
                (username,),
            ).fetchone()
        if not row:
            return None
        refresh_token = _dec_secret(row["refresh_token"])
        if not refresh_token:
            return None  # 암호문인데 키 없음/불일치 → 미설정 취급(데이터는 보존)
        return {
            "refresh_token": refresh_token,
            "email": row["email"],
            "root_folder_id": row["root_folder_id"],
            "updated_at": row["updated_at"],
        }

    def set_google_root_folder(self, username: str, folder_id: str | None) -> None:
        """drive.file 앱 루트 폴더 id 영속(첫 동기화 때 생성 후 재사용). 행 없으면 무시."""
        with self._lock:
            self._conn.execute(
                "UPDATE google_credentials SET root_folder_id=? WHERE username=?",
                (folder_id, username),
            )
            self._conn.commit()

    def clear_google_credential(self, username: str) -> bool:
        """Google 연동 해제(자격증명 삭제). 삭제된 행이 있으면 True."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM google_credentials WHERE username=?", (username,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def google_status(self, username: str) -> dict:
        """Google 연동 공개 상태(refresh_token **비노출**). 설정 UI 용.

        반환: {"connected": bool, "email": str|None, "updatedAt": str|None}.
        """
        cred = self.get_google_credential(username)
        if not cred:
            return {"connected": False, "email": None, "updatedAt": None}
        return {"connected": True, "email": cred["email"], "updatedAt": cred["updated_at"]}

    # ---- 앱 레벨 Google OAuth 클라이언트 설정(app_oauth_config, provider='google') ----
    def set_google_oauth_config(
        self, client_id: str, client_secret: str, redirect_uri: str
    ) -> None:
        """관리자가 앱 OAuth 클라이언트(client_id/secret/redirect_uri) 설정. secret 은 Fernet 암호화.

        단일 provider 행(upsert). 세 값 모두 필수(비면 ValueError) — 부분 설정은 oauth_configured
        판단을 모호하게 하므로 한 번에 완결한다.
        """
        client_id = (client_id or "").strip()
        client_secret = (client_secret or "").strip()
        redirect_uri = (redirect_uri or "").strip()
        if not (client_id and client_secret and redirect_uri):
            raise ValueError("client_id/client_secret/redirect_uri 는 모두 필요합니다.")
        stored = _enc_secret(client_secret)  # CRED_ENC_KEY 있으면 암호화, 없으면 평문(하위호환)
        with self._lock:
            self._conn.execute(
                "INSERT INTO app_oauth_config "
                "(provider, client_id, client_secret, redirect_uri, updated_at) "
                "VALUES ('google',?,?,?,?) "
                "ON CONFLICT(provider) DO UPDATE SET "
                "client_id=excluded.client_id, client_secret=excluded.client_secret, "
                "redirect_uri=excluded.redirect_uri, updated_at=excluded.updated_at",
                (client_id, stored, redirect_uri, dt.datetime.now().isoformat(timespec="seconds")),
            )
            self._conn.commit()

    def get_google_oauth_config(self) -> dict | None:
        """앱 OAuth 설정(client_secret 복호 포함, **내부 전용**). 없으면 None.

        반환: {"client_id", "client_secret", "redirect_uri", "updated_at"}. client_secret 은
        절대 API 응답에 싣지 말 것(공개 상태는 app.py/google_oauth 가 secret 제외 후 구성).
        암호문인데 키 없음/불일치로 복호 불가면 client_secret="" 로 둔다(설정 미완 취급).
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT client_id, client_secret, redirect_uri, updated_at "
                "FROM app_oauth_config WHERE provider='google'"
            ).fetchone()
        if not row:
            return None
        return {
            "client_id": row["client_id"],
            "client_secret": _dec_secret(row["client_secret"] or ""),
            "redirect_uri": row["redirect_uri"],
            "updated_at": row["updated_at"],
        }

    def clear_google_oauth_config(self) -> bool:
        """앱 OAuth 설정 삭제(→ env 폴백으로 복귀). 삭제된 행이 있으면 True."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM app_oauth_config WHERE provider='google'")
            self._conn.commit()
        return cur.rowcount > 0


def public_user(u: dict) -> dict:
    """프론트 계약 user 객체(id/username/displayName/role). 비번 해시는 절대 노출 안 함."""
    return {
        "id": u["username"],
        "username": u["username"],
        "displayName": u.get("display_name") or u["username"],
        "role": u.get("role") or "user",
        "englishName": u.get("english_name"),
        "jobTitle": u.get("job_title"),
        # True 면 프론트가 강제 비번변경 화면을 띄우고, 백엔드는 데이터/AI 엔드포인트를 403 차단.
        "mustChangePassword": bool(u.get("must_change_password", 0)),
    }


def make_token(username: str, ttl: int | None = None, *, scope: str | None = None) -> str:
    """토큰 발급. ttl(초) 지정 시 그 만료를, 미지정 시 기본 _ttl()(7일)을 쓴다.

    scope 지정 시 payload 에 박아 **제한 토큰**으로 만든다(예: scope='audio' = 오디오 스트리밍 전용).
    스코프 토큰은 user_from_token(scope=...) 으로만 통과하고, 일반 세션 검증(scope 미지정)에서는
    거부된다 → URL 쿼리로 노출되는 오디오 토큰이 탈취돼도 다른 Bearer 엔드포인트에 재사용 불가.
    세션 토큰은 scope 없이 발급한다(후방호환).
    """
    now = dt.datetime.now(dt.timezone.utc)
    exp = now + dt.timedelta(seconds=ttl if ttl is not None else _ttl())
    payload = {"sub": username, "iat": now, "exp": exp}
    if scope is not None:
        payload["scope"] = scope
    return jwt.encode(payload, _secret(), algorithm="HS256")


def _decode(token: str) -> dict:
    return jwt.decode(token, _secret(), algorithms=["HS256"])


def user_from_token(token: str, *, scope: str | None = None) -> dict:
    """토큰 문자열 → 공개 user dict. 무효/만료/미존재/스코프불일치면 401(require_user 동일 규약).

    require_user(Bearer 헤더, scope=None) 와 오디오 스트리밍(쿼리, scope='audio') 가 공유한다.
    스코프 규약(토큰 탈취 피해 한정):
      - scope=None(세션 검증): 토큰에 scope 클레임이 박혀 있으면 거부(제한 토큰의 세션 재사용 차단).
      - scope='audio': 토큰 scope 가 정확히 'audio' 여야 통과.
    """
    try:
        payload = _decode(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="토큰이 만료되었습니다.")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="토큰이 유효하지 않습니다.")
    if payload.get("scope") != scope:
        raise HTTPException(status_code=401, detail="토큰 범위가 올바르지 않습니다.")
    u = store().get(payload.get("sub", ""))
    if not u:
        raise HTTPException(status_code=401, detail="사용자를 찾을 수 없습니다.")
    return public_user(u)


# ---- 모듈 싱글턴: app.py 가 init() 호출 ----
_store: UserStore | None = None


def init(db_path=None) -> UserStore:
    """UserStore 생성 + env(WEB_AUTH_USERS) 시드/동기화. JWT_SECRET 미설정이면 즉시 실패."""
    global _store
    if not _secret():
        raise RuntimeError("JWT_SECRET 미설정 — 인증 토큰 서명 불가. .env 에 설정하세요.")
    # 테스트 격리 가드레일: 패치 누락으로 실 DB 를 열려 하면 즉시 거부(아래 UserStore 도 재확인).
    _guard_default_db(db_path or DEFAULT_DB_PATH)
    _store = UserStore(db_path)

    # 자격증명 at-rest 암호화: CRED_ENC_KEY 가 설정돼 있으면 평문으로 남은 자격증명을 재암호화
    # (멱등·무중단). 미설정이면 0(평문 유지 — 하위호환). 키 형식 오류면 _cred_cipher 가 즉시 예외.
    migrated = _store.migrate_encrypt_credentials()
    if migrated:
        print(f"[auth] 자격증명 {migrated}건을 at-rest 암호화로 마이그레이션했습니다.", flush=True)

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


# ---- 모듈 레벨 Google 자격증명 헬퍼(싱글턴 store 위임) ----
def set_google_credential(username: str, refresh_token: str, *, email: str | None = None) -> None:
    store().set_google_credential(username, refresh_token, email=email)


def get_google_credential(username: str) -> dict | None:
    """refresh_token 포함 — 내부 주입 전용. API 응답에 그대로 싣지 말 것."""
    return store().get_google_credential(username)


def set_google_root_folder(username: str, folder_id: str | None) -> None:
    store().set_google_root_folder(username, folder_id)


def clear_google_credential(username: str) -> bool:
    return store().clear_google_credential(username)


def google_status(username: str) -> dict:
    """refresh_token 비노출 공개 상태."""
    return store().google_status(username)


# ---- 모듈 레벨 앱 OAuth 설정 헬퍼(싱글턴 store 위임) ----
def set_google_oauth_config(client_id: str, client_secret: str, redirect_uri: str) -> None:
    store().set_google_oauth_config(client_id, client_secret, redirect_uri)


def get_google_oauth_config() -> dict | None:
    """client_secret 포함 — 내부 전용. API 응답에 그대로 싣지 말 것."""
    return store().get_google_oauth_config()


def clear_google_oauth_config() -> bool:
    return store().clear_google_oauth_config()


def update_profile(username: str, **fields) -> dict | None:
    """본인 표시명 self-edit(싱글턴 store 위임). 공개 user dict 반환(없으면 None)."""
    return store().update_profile(username, **fields)


# ---- FastAPI 의존성: Bearer 토큰 검증 → 현재 사용자 ----
_bearer = HTTPBearer(auto_error=False)


def require_user(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
    """유효 Bearer 토큰 → 공개 user dict. 없거나 무효/만료/미존재 사용자면 401."""
    if cred is None or not cred.credentials:
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    return user_from_token(cred.credentials)
