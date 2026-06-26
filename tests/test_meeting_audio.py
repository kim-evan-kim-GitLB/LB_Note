"""원본 오디오 영속 회귀 테스트 (계획 v4 트랙 C·Phase 4, D7-id 옵션B).

검증 불변식:
  - POST /api/meetings/audio/staging: 멀티파트 업로드 → stagingToken(32 hex) + 메타. 빈/과대 거부.
  - create_meeting(audioStagingToken): staging→{id}/source 이동, audioRef 기록. 토큰 없으면 후방호환.
  - GET /api/meetings/{id}/audio: 전체(200)·Range(206), audioRef 없으면 404,
    타 사용자 404, meetingId 화이트리스트 위반 400.
  - DELETE /api/meetings/{id}: 오디오 디렉토리 동반 삭제.
  - 업로드 저장 실패 rollback: 부분파일 미잔존.
  - cleanup_staging: 만료된 미bind staging 삭제.

실 DB(output/web/meetings.db)·실 오디오는 **절대 건드리지 않는다** — tempfile + DEFAULT_DB_PATH
패치로 격리(audio_store.audio_base() 가 DEFAULT_DB_PATH.parent 기준이라 오디오도 임시경로로 격리).

실행: sudo PYTHONPATH=/app .venv/bin/python tests/test_meeting_audio.py
"""
from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


# ---------- audio_store 단위 테스트(앱·HTTP 없이, DEFAULT_DB_PATH 패치 격리) ----------
@contextlib.contextmanager
def _patched_base(td: Path):
    """DEFAULT_DB_PATH 를 임시경로로 패치 → audio_base() 가 임시 output/audio 를 쓰게 한다."""
    import src.web.store as storemod

    orig = storemod.DEFAULT_DB_PATH
    try:
        storemod.DEFAULT_DB_PATH = td / "meetings.db"
        yield
    finally:
        storemod.DEFAULT_DB_PATH = orig


def test_save_staging_and_bind_moves_file():
    from src.web import audio_store

    with tempfile.TemporaryDirectory() as td, _patched_base(Path(td)):
        token, ext = audio_store.save_staging(b"AUDIODATA", mime_type="audio/webm", filename="x.webm")
        assert len(token) == 32 and ext == "webm"
        staged = audio_store._staging_path(token)
        assert staged is not None and staged.read_bytes() == b"AUDIODATA"
        mid = "a" * 32
        ref = audio_store.bind_staging(token, mid)
        assert ref is not None
        assert ref["format"] == "webm" and ref["sizeBytes"] == len(b"AUDIODATA")
        assert "createdAt" in ref
        # staging 은 비고, meeting 디렉토리로 이동됨
        assert audio_store._staging_path(token) is None
        dst = audio_store.audio_base() / mid / "source.webm"
        assert dst.is_file() and dst.read_bytes() == b"AUDIODATA"


def test_bind_missing_token_returns_none():
    from src.web import audio_store

    with tempfile.TemporaryDirectory() as td, _patched_base(Path(td)):
        assert audio_store.bind_staging("f" * 32, "a" * 32) is None


def test_safe_ext_whitelist_and_fallback():
    from src.web import audio_store

    assert audio_store.safe_ext("audio/mpeg", None) == "mp3"
    assert audio_store.safe_ext(None, "rec.wav") == "wav"
    # 알 수 없는 mime/파일명 → bin 폴백(경로조립 안전)
    assert audio_store.safe_ext("application/x-evil", "../../etc/passwd") == "bin"
    assert audio_store.safe_ext(None, None) == "bin"


def test_delete_meeting_audio_removes_dir():
    from src.web import audio_store

    with tempfile.TemporaryDirectory() as td, _patched_base(Path(td)):
        token, _ = audio_store.save_staging(b"X", mime_type="audio/wav", filename=None)
        mid = "b" * 32
        audio_store.bind_staging(token, mid)
        assert (audio_store.audio_base() / mid).is_dir()
        assert audio_store.delete_meeting_audio(mid) is True
        assert not (audio_store.audio_base() / mid).exists()
        # 두 번째 삭제는 False(이미 없음)
        assert audio_store.delete_meeting_audio(mid) is False


def test_save_staging_rollback_no_partial_file():
    """write 실패 시 부분파일이 남지 않는다(rollback)."""
    import src.web.audio_store as audio_store

    with tempfile.TemporaryDirectory() as td, _patched_base(Path(td)):
        sdir = audio_store._staging_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        before = set(sdir.iterdir())
        orig = Path.write_bytes

        def _boom(self, data):  # noqa: ANN001
            # 부분파일을 만든 뒤 실패하는 상황 모사
            orig(self, data[: len(data) // 2])
            raise OSError("disk full")

        Path.write_bytes = _boom  # type: ignore[assignment]
        try:
            raised = False
            try:
                audio_store.save_staging(b"PARTIALDATA", mime_type="audio/webm", filename=None)
            except OSError:
                raised = True
            assert raised, "저장 실패는 예외로 전파되어야 함"
        finally:
            Path.write_bytes = orig  # type: ignore[assignment]
        # 부분파일 미잔존(rollback)
        assert set(sdir.iterdir()) == before


def test_stream_too_large_raises_and_no_partial_file():
    """save_staging_stream 이 작은 max_bytes 초과 시 AudioTooLarge raise + staging 잔존 0개."""
    from src.web import audio_store

    with tempfile.TemporaryDirectory() as td, _patched_base(Path(td)):
        sdir = audio_store._staging_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        before = set(sdir.iterdir())
        data = b"0123456789ABCDEF"  # 16 bytes > max 10
        chunks = iter([data, b""])

        def _reader(_size):  # noqa: ANN001
            return next(chunks, b"")

        raised = False
        try:
            audio_store.save_staging_stream(
                _reader, mime_type="audio/webm", filename=None, max_bytes=10, chunk_size=16
            )
        except audio_store.AudioTooLarge:
            raised = True
        assert raised, "초과 시 AudioTooLarge 가 전파되어야 함"
        # 부분파일 미잔존(rollback)
        assert set(sdir.iterdir()) == before


def test_stream_zero_byte_raises_valueerror_and_no_partial_file():
    """0바이트 reader → ValueError + staging 잔존 0개."""
    from src.web import audio_store

    with tempfile.TemporaryDirectory() as td, _patched_base(Path(td)):
        sdir = audio_store._staging_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        before = set(sdir.iterdir())

        def _empty_reader(_size):  # noqa: ANN001
            return b""

        raised = False
        try:
            audio_store.save_staging_stream(
                _empty_reader, mime_type="audio/webm", filename=None
            )
        except ValueError:
            raised = True
        assert raised, "빈 reader 는 ValueError 가 전파되어야 함"
        # 부분파일 미잔존(rollback)
        assert set(sdir.iterdir()) == before


def test_cleanup_staging_removes_aged():
    from src.web import audio_store

    with tempfile.TemporaryDirectory() as td, _patched_base(Path(td)):
        token, _ = audio_store.save_staging(b"OLD", mime_type="audio/wav", filename=None)
        staged = audio_store._staging_path(token)
        assert staged is not None
        # mtime 을 과거로 — 충분히 오래됨
        old = staged.stat().st_mtime - 10_000
        os.utime(staged, (old, old))
        # 새로 막 만든 파일은 남고, 오래된 것만 삭제(임계값 1시간)
        token_new, _ = audio_store.save_staging(b"NEW", mime_type="audio/wav", filename=None)
        removed = audio_store.cleanup_staging(3600)
        assert removed == 1
        assert audio_store._staging_path(token) is None
        assert audio_store._staging_path(token_new) is not None


# ---------- HTTP 통합 테스트(임시 DB·임시 오디오 격리 app) ----------
@contextlib.contextmanager
def _client_for(td: Path, users: str):
    """임시 DB + 임시 오디오 루트로 격리된 app + TestClient(test_meeting_patch 와 동일 패턴).

    DEFAULT_DB_PATH 를 임시경로로 패치 → store/auth/audio_store 가 모두 임시 output 만 사용.
    audio_store.audio_base() 는 DEFAULT_DB_PATH.parent 기준이므로 별도 패치 불필요."""
    from fastapi.testclient import TestClient
    import importlib

    tmp_db = td / "meetings.db"
    os.environ["JWT_SECRET"] = "test-secret-meeting-audio"
    os.environ["WEB_AUTH_USERS"] = users
    os.environ["WEB_AUTH_ADMINS"] = "admin"
    os.environ["WEB_AUTH_TOKEN_TTL"] = "3600"
    os.environ["WEB_AUTH_PRUNE"] = "1"
    import src.web.store as storemod
    import src.web.auth as auth_pre

    store_orig = storemod.DEFAULT_DB_PATH
    auth_orig = getattr(auth_pre, "DEFAULT_DB_PATH", None)
    try:
        storemod.DEFAULT_DB_PATH = tmp_db
        import src.web.auth as auth
        importlib.reload(auth)
        auth.DEFAULT_DB_PATH = tmp_db
        import src.web.audio_store as audio_store
        importlib.reload(audio_store)  # _storemod.DEFAULT_DB_PATH 패치 반영(매 호출 읽지만 안전)
        import src.web.app as appmod
        importlib.reload(appmod)
        with TestClient(appmod.app) as client:
            yield auth, appmod, client
    finally:
        storemod.DEFAULT_DB_PATH = store_orig
        import src.web.auth as auth_post
        if auth_orig is not None:
            auth_post.DEFAULT_DB_PATH = auth_orig


def _auth_headers(auth, appmod, username: str) -> dict:
    appmod.users.set_password(username, "newpassword123")
    return {"Authorization": f"Bearer {auth.make_token(username)}"}


def _upload_staging(client, h, data: bytes, *, ct="audio/webm", name="rec.webm") -> dict:
    r = client.post(
        "/api/meetings/audio/staging",
        files={"file": (name, data, ct)},
        headers=h,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _create_with_token(client, h, token: str | None) -> dict:
    body = {"title": "오디오회의", "status": "review"}
    if token is not None:
        body["audioStagingToken"] = token
    r = client.post("/api/meetings", json=body, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_staging_upload_returns_token():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        body = _upload_staging(client, h, b"AUDIO-BYTES-DATA")
        assert len(body["stagingToken"]) == 32
        assert body["format"] == "webm"
        assert body["sizeBytes"] == len(b"AUDIO-BYTES-DATA")


def test_staging_empty_rejected():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        r = client.post(
            "/api/meetings/audio/staging", files={"file": ("e.webm", b"", "audio/webm")}, headers=h
        )
        assert r.status_code == 400, r.text


def test_staging_too_large_413():
    """MAX_AUDIO_BYTES 를 작게 monkeypatch → 초과 업로드는 바디 스트리밍 중 413."""
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        from src.web import audio_store

        orig = audio_store.MAX_AUDIO_BYTES
        try:
            audio_store.MAX_AUDIO_BYTES = 4  # 매우 작게
            r = client.post(
                "/api/meetings/audio/staging",
                files={"file": ("big.webm", b"0123456789", "audio/webm")},  # 10 > 4
                headers=h,
            )
            assert r.status_code == 413, r.text
            # 부분파일 미잔존(rollback)
            sdir = audio_store.audio_base() / "_staging"
            assert not (sdir.is_dir() and list(sdir.glob("*")))
        finally:
            audio_store.MAX_AUDIO_BYTES = orig


def test_staging_content_length_oversize_413_before_body():
    """Content-Length 헤더가 상한 초과로 명시되면 바디를 읽기 전 413."""
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        from src.web import audio_store

        orig = audio_store.MAX_AUDIO_BYTES
        try:
            audio_store.MAX_AUDIO_BYTES = 4
            # 작은 바디라도 Content-Length 헤더가 상한 초과면 선검사에서 413
            r = client.post(
                "/api/meetings/audio/staging",
                files={"file": ("x.webm", b"ab", "audio/webm")},
                headers={**h, "Content-Length": "999"},
            )
            assert r.status_code == 413, r.text
        finally:
            audio_store.MAX_AUDIO_BYTES = orig


def test_bind_on_create_records_audioref_and_moves():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        data = b"BIND-AUDIO-PAYLOAD"
        st = _upload_staging(client, h, data)
        m = _create_with_token(client, h, st["stagingToken"])
        assert "audioRef" in m
        assert m["audioRef"]["format"] == "webm"
        assert m["audioRef"]["sizeBytes"] == len(data)
        # audioStagingToken 은 meeting JSON 에 영속하지 않음
        assert "audioStagingToken" not in m
        # 파일이 {id}/source.webm 로 이동됨
        from src.web import audio_store

        dst = audio_store.audio_base() / m["id"] / "source.webm"
        assert dst.is_file() and dst.read_bytes() == data
        # staging 은 비었음
        assert not list((audio_store.audio_base() / "_staging").glob("*"))


def test_create_without_token_backward_compatible():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        m = _create_with_token(client, h, None)
        assert "audioRef" not in m
        # 오디오 GET 은 404
        r = client.get(f"/api/meetings/{m['id']}/audio", headers=h)
        assert r.status_code == 404, r.text


def test_get_audio_full_200():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        data = b"0123456789ABCDEF" * 4  # 64 bytes
        st = _upload_staging(client, h, data)
        m = _create_with_token(client, h, st["stagingToken"])
        r = client.get(f"/api/meetings/{m['id']}/audio", headers=h)
        assert r.status_code == 200, r.text
        assert r.content == data
        assert r.headers.get("Accept-Ranges") == "bytes"


def test_get_audio_range_206():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        data = bytes(range(64))
        st = _upload_staging(client, h, data, ct="audio/wav", name="r.wav")
        m = _create_with_token(client, h, st["stagingToken"])
        r = client.get(
            f"/api/meetings/{m['id']}/audio", headers={**h, "Range": "bytes=10-19"}
        )
        assert r.status_code == 206, r.text
        assert r.content == data[10:20]
        assert r.headers.get("Content-Range") == f"bytes 10-19/{len(data)}"
        assert r.headers.get("Content-Length") == "10"
        # suffix range: 마지막 5바이트
        r2 = client.get(
            f"/api/meetings/{m['id']}/audio", headers={**h, "Range": "bytes=-5"}
        )
        assert r2.status_code == 206, r2.text
        assert r2.content == data[-5:]


def test_get_audio_unsatisfiable_range_416():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        data = b"short"
        st = _upload_staging(client, h, data)
        m = _create_with_token(client, h, st["stagingToken"])
        r = client.get(
            f"/api/meetings/{m['id']}/audio", headers={**h, "Range": "bytes=1000-2000"}
        )
        assert r.status_code == 416, r.text
        assert r.headers.get("Content-Range") == f"bytes */{len(data)}"


def test_get_audio_other_user_404():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1,bob:pw2") as (auth, appmod, client):
        ha = _auth_headers(auth, appmod, "admin")
        hb = _auth_headers(auth, appmod, "bob")
        st = _upload_staging(client, ha, b"OWNERDATA")
        m = _create_with_token(client, ha, st["stagingToken"])
        # 소유자(admin)는 200
        assert client.get(f"/api/meetings/{m['id']}/audio", headers=ha).status_code == 200
        # 타 사용자(bob)는 404(존재 자체 숨김)
        assert client.get(f"/api/meetings/{m['id']}/audio", headers=hb).status_code == 404


def test_get_audio_meetingid_whitelist_400():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        # 화이트리스트(^[0-9a-f]{32}$) 위반 → 400(경로조립 traversal 차단)
        r = client.get("/api/meetings/not-a-valid-id/audio", headers=h)
        assert r.status_code == 400, r.text
        # 대문자/길이초과도 거부
        r2 = client.get(f"/api/meetings/{'A' * 32}/audio", headers=h)
        assert r2.status_code == 400, r2.text


def test_delete_meeting_removes_audio():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        st = _upload_staging(client, h, b"TO-BE-DELETED")
        m = _create_with_token(client, h, st["stagingToken"])
        from src.web import audio_store

        mdir = audio_store.audio_base() / m["id"]
        assert mdir.is_dir()
        r = client.delete(f"/api/meetings/{m['id']}", headers=h)
        assert r.status_code == 200, r.text
        # 회의·오디오 동반 삭제
        assert not mdir.exists()
        assert client.get(f"/api/meetings/{m['id']}", headers=h).status_code == 404


def test_get_audio_zero_byte_200_empty_and_416_on_range():
    """0바이트 오디오: Range 없으면 200 빈본문, Range 오면 불만족 → 416."""
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        st = _upload_staging(client, h, b"NONZERO")
        m = _create_with_token(client, h, st["stagingToken"])
        from src.web import audio_store

        # 소스 파일을 0바이트로 절단(엣지: 빈 파일이지만 파일은 존재)
        src = audio_store.audio_base() / m["id"] / "source.webm"
        src.write_bytes(b"")
        assert src.stat().st_size == 0
        # Range 없음 → 200 빈본문
        r = client.get(f"/api/meetings/{m['id']}/audio", headers=h)
        assert r.status_code == 200, r.text
        assert r.content == b""
        assert r.headers.get("Accept-Ranges") == "bytes"
        # Range 오면 불만족 → 416
        r2 = client.get(
            f"/api/meetings/{m['id']}/audio", headers={**h, "Range": "bytes=0-0"}
        )
        assert r2.status_code == 416, r2.text
        assert r2.headers.get("Content-Range") == "bytes */0"


def test_duplicate_staging_token_bind_second_meeting_no_audioref():
    """동일 stagingToken 으로 두 번 bind: 첫 회의만 audioRef, 두 번째는 staging 부재 → audioRef 없음."""
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        st = _upload_staging(client, h, b"DUPLICATE-BIND-DATA")
        token = st["stagingToken"]
        # 1차 bind: audioRef 기록됨
        m1 = _create_with_token(client, h, token)
        assert "audioRef" in m1
        # 2차 bind(동일 토큰): staging 파일이 이미 이동되어 없음 → graceful, audioRef 미기록
        m2 = _create_with_token(client, h, token)
        assert "audioRef" not in m2
        # 2번째 회의 오디오 GET 은 404
        r = client.get(f"/api/meetings/{m2['id']}/audio", headers=h)
        assert r.status_code == 404, r.text


def test_create_with_bad_token_rejected_400():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        body = {"title": "x", "audioStagingToken": "../../evil"}
        r = client.post("/api/meetings", json=body, headers=h)
        assert r.status_code == 400, r.text


# ---------- 오디오 토큰 + 쿼리 인증 스트리밍(Range 시킹) ----------
def test_audio_token_then_stream_via_query_param():
    """audio-token 발급(Bearer) → access_token 쿼리로 Bearer 없이 스트리밍(200·206)."""
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        data = b"0123456789ABCDEF" * 4  # 64 bytes
        st = _upload_staging(client, h, data)
        m = _create_with_token(client, h, st["stagingToken"])
        # 토큰 발급(소유자, Bearer)
        tk = client.get(f"/api/meetings/{m['id']}/audio-token", headers=h)
        assert tk.status_code == 200, tk.text
        token = tk.json()["token"]
        assert tk.json()["expiresIn"] > 0
        # Bearer 헤더 없이 쿼리 토큰만으로 전체 스트리밍(네이티브 <audio> 흉내)
        r = client.get(f"/api/meetings/{m['id']}/audio", params={"access_token": token})
        assert r.status_code == 200, r.text
        assert r.content == data
        # 쿼리 토큰 + Range → 206
        r2 = client.get(
            f"/api/meetings/{m['id']}/audio",
            params={"access_token": token},
            headers={"Range": "bytes=10-19"},
        )
        assert r2.status_code == 206, r2.text
        assert r2.content == data[10:20]


def test_audio_stream_missing_or_invalid_token_401():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        st = _upload_staging(client, h, b"AUDIO-DATA-XYZ")
        m = _create_with_token(client, h, st["stagingToken"])
        # 인증 전무(헤더·쿼리 없음) → 401
        assert client.get(f"/api/meetings/{m['id']}/audio").status_code == 401
        # 잘못된 쿼리 토큰 → 401
        bad = client.get(f"/api/meetings/{m['id']}/audio", params={"access_token": "not-a-jwt"})
        assert bad.status_code == 401, bad.text


def test_audio_token_other_user_404():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1,bob:pw2") as (auth, appmod, client):
        ha = _auth_headers(auth, appmod, "admin")
        hb = _auth_headers(auth, appmod, "bob")
        st = _upload_staging(client, ha, b"OWNER-ONLY-AUDIO")
        m = _create_with_token(client, ha, st["stagingToken"])
        # 타 사용자는 토큰 발급 불가(소유자 격리) → 404
        assert client.get(f"/api/meetings/{m['id']}/audio-token", headers=hb).status_code == 404
        # 소유자 토큰을 타인이 자기 회의에 못 쓰는 건 _owned_or_404 가 별도 보장(여긴 발급 차단까지 검증)


def test_make_token_ttl_and_user_from_token():
    """make_token(ttl) 만료 단축 + user_from_token 해석."""
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        import jwt as _jwt

        short = auth.make_token("admin", ttl=5)
        payload = _jwt.decode(short, auth._secret(), algorithms=["HS256"])
        assert payload["exp"] - payload["iat"] == 5
        user = auth.user_from_token(short)
        assert user["username"] == "admin"


def test_audio_token_scope_isolation():
    """audio 스코프 토큰은 다른 Bearer 엔드포인트에 재사용 불가, 세션 토큰은 audio 쿼리에 불가(401)."""
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        st = _upload_staging(client, h, b"SCOPE-ISO-AUDIO")
        m = _create_with_token(client, h, st["stagingToken"])
        atok = client.get(f"/api/meetings/{m['id']}/audio-token", headers=h).json()["token"]
        # audio 토큰을 Bearer(세션)로 → 401(스코프 박힌 토큰의 세션 재사용 차단)
        assert client.get("/api/meetings", headers={"Authorization": f"Bearer {atok}"}).status_code == 401
        # audio 토큰을 audio GET 의 Bearer 로 써도 거부(Bearer 경로는 세션 스코프 요구) → 401
        assert client.get(
            f"/api/meetings/{m['id']}/audio", headers={"Authorization": f"Bearer {atok}"}
        ).status_code == 401
        # 세션 토큰(scope 없음)을 audio 쿼리로 → 401(쿼리 경로는 audio 스코프 요구)
        sess = auth.make_token("admin")
        assert client.get(
            f"/api/meetings/{m['id']}/audio", params={"access_token": sess}
        ).status_code == 401


def test_audio_must_change_password_403():
    """초기 비번 미변경(must_change) 사용자는 audio-token 발급·스트리밍 모두 403."""
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1,carol:pw3") as (auth, appmod, client):
        # carol 은 set_password 미호출 → 시드 must_change_password=1 유지.
        h_carol = {"Authorization": f"Bearer {auth.make_token('carol')}"}
        mid = "f" * 32
        # 토큰 발급 차단(require_user_active 403)
        assert client.get(f"/api/meetings/{mid}/audio-token", headers=h_carol).status_code == 403
        # 직접 조립한 audio 스코프 토큰으로 스트리밍 호출해도 _audio_user 403(소유자 검증 전 게이트)
        atok = auth.make_token("carol", ttl=60, scope="audio")
        assert client.get(
            f"/api/meetings/{mid}/audio", params={"access_token": atok}
        ).status_code == 403


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_meeting_audio ({len(fns)} cases)")


if __name__ == "__main__":
    _run()
