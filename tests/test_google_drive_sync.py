"""Google Drive 동기화 HTTP 통합 테스트 — OAuth 왕복 + drive-sync 잡(실 API 미호출, 전부 mock).

검증 불변식:
  - /connect: 미설정이면 503, 설정되면 authUrl 반환.
  - /callback: 유효 state(scope='google_oauth')로 code 교환·저장 후 302. 세션토큰/만료/위조 state 401.
  - /status: connected/email + configured 플래그.
  - /drive-sync: 미연동 400(google_not_connected). 소유자 아니면 404.
  - 동기화 잡: 첫 sync=문서 생성(doc_id=None 전달), 재sync=update(같은 docId 전달) → 멱등.
  - gdriveRef 가 meeting.data 에 영속되고 잡 result 에 docUrl 포함.
  - refresh_token 무효 → 잡 error_code=google_auth_expired.
  - 오디오 있으면 upsert_audio 호출, 재sync 시 audioId 전달(skip 신호).
  - 타인 jobId 폴링 404(잡 소유격리).

실 DB·실 오디오 미접촉(tempfile + DEFAULT_DB_PATH 패치). google 라이브러리 불필요(함수 mock).
실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_google_drive_sync.py
"""
from __future__ import annotations

import contextlib
import importlib
import os
import time
import uuid
from pathlib import Path
from unittest import mock


@contextlib.contextmanager
def _client_for(td: Path, users: str):
    """임시 DB·오디오 격리 app + TestClient(test_meeting_audio 와 동일 패턴)."""
    from fastapi.testclient import TestClient

    tmp_db = td / "meetings.db"
    os.environ["JWT_SECRET"] = "test-secret-gdrive"
    os.environ["WEB_AUTH_USERS"] = users
    os.environ["WEB_AUTH_ADMINS"] = "admin"
    os.environ["WEB_AUTH_TOKEN_TTL"] = "3600"
    os.environ["WEB_AUTH_PRUNE"] = "1"
    os.environ.pop("CRED_ENC_KEY", None)
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
    appmod.users.set_password(username, "newpassword123")  # must_change 게이트 해제
    return {"Authorization": f"Bearer {auth.make_token(username)}"}


def _make_meeting(client, headers, *, audio_ref=None) -> str:
    mid = uuid.uuid4().hex
    body = {"id": mid, "title": "테스트 회의", "transcript": [
        {"segmentId": 0, "timestamp": "00:01", "speakerId": "화자1", "text": "안녕"}]}
    if audio_ref:
        body["audioRef"] = audio_ref
    r = client.post("/api/meetings", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return mid


def _wait_job(client, headers, job_id, timeout=5.0) -> dict:
    deadline = time.time() + timeout
    j = {}
    while time.time() < deadline:
        j = client.get(f"/api/ai/jobs/{job_id}", headers=headers).json()
        if j.get("status") in ("done", "error"):
            return j
        time.sleep(0.02)
    raise AssertionError(f"job timeout: {j}")


# ---------- OAuth 왕복 ----------
def test_connect_requires_config():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        with mock.patch.object(appmod.google_oauth, "oauth_configured", return_value=False):
            assert client.post("/api/settings/google/connect", headers=h).status_code == 503
        with mock.patch.object(appmod.google_oauth, "oauth_configured", return_value=True), \
             mock.patch.object(appmod.google_oauth, "build_consent_url", return_value="https://consent/x"):
            r = client.post("/api/settings/google/connect", headers=h)
            assert r.status_code == 200 and r.json()["authUrl"] == "https://consent/x"


def test_callback_exchanges_and_stores():
    with _tmp() as (auth, appmod, client):
        _headers(auth, appmod, "admin")
        state = auth.make_token("admin", ttl=600, scope="google_oauth")
        with mock.patch.object(
            appmod.google_oauth, "exchange_code",
            return_value={"refresh_token": "rt-xyz", "email": "me@corp.com"},
        ):
            r = client.get(
                f"/api/integrations/google/callback?state={state}&code=abc",
                follow_redirects=False,
            )
        assert r.status_code == 302 and "google=connected" in r.headers["location"]
        # 저장 확인(status)
        h = _headers(auth, appmod, "admin")
        st = client.get("/api/settings/google/status", headers=h).json()
        assert st["connected"] is True and st["email"] == "me@corp.com"


def test_callback_rejects_session_token_as_state():
    with _tmp() as (auth, appmod, client):
        _headers(auth, appmod, "admin")
        session_tok = auth.make_token("admin")  # scope 없음(세션 토큰)
        r = client.get(
            f"/api/integrations/google/callback?state={session_tok}&code=abc",
            follow_redirects=False,
        )
        assert r.status_code == 401  # 세션토큰의 state 재사용 차단


def test_callback_rejects_expired_state():
    with _tmp() as (auth, appmod, client):
        _headers(auth, appmod, "admin")
        expired = auth.make_token("admin", ttl=-1, scope="google_oauth")
        r = client.get(
            f"/api/integrations/google/callback?state={expired}&code=abc",
            follow_redirects=False,
        )
        assert r.status_code == 401


def test_callback_error_redirects():
    with _tmp() as (auth, appmod, client):
        _headers(auth, appmod, "admin")
        state = auth.make_token("admin", ttl=600, scope="google_oauth")
        # 동의 거부(error 파라미터) → google=error 리다이렉트(401 아님, state 는 유효)
        r = client.get(
            f"/api/integrations/google/callback?state={state}&error=access_denied",
            follow_redirects=False,
        )
        assert r.status_code == 302 and "google=error" in r.headers["location"]


# ---------- drive-sync ----------
def test_drive_sync_not_connected():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        mid = _make_meeting(client, h)
        r = client.post(f"/api/meetings/{mid}/drive-sync", headers=h)
        assert r.status_code == 400
        assert r.json()["detail"]["error_code"] == "google_not_connected"


def test_drive_sync_idempotent_and_persist():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        appmod.auth.set_google_credential("admin", "rt", email="me@corp.com")
        mid = _make_meeting(client, h)
        doc_calls = []
        doc_meta = []  # (folder, title) — 하위폴더에 "회의록" 이름으로 저장되는지 확인
        sub_calls = []  # ensure_subfolder(parent, name, folder_id) 인자 캡처
        with mock.patch.object(appmod.google_oauth, "refresh_access_token", return_value="AT"), \
             mock.patch.object(appmod.google_drive, "ensure_root_folder", return_value="folder1"), \
             mock.patch.object(
                 appmod.google_drive, "ensure_subfolder",
                 side_effect=lambda at, parent, name, fid: sub_calls.append((parent, name, fid)) or "sub1"), \
             mock.patch.object(
                 appmod.google_drive, "upsert_doc",
                 side_effect=lambda at, fld, html, title, doc_id: (
                     doc_calls.append(doc_id), doc_meta.append((fld, title)))[0] or (doc_id or "doc1")):
            # 첫 동기화: doc_id=None 전달(생성)
            job = client.post(f"/api/meetings/{mid}/drive-sync", headers=h).json()
            done = _wait_job(client, h, job["jobId"])
            assert done["status"] == "done", done
            assert done["result"]["gdriveRef"]["docId"] == "doc1"
            assert "docs.google.com/document/d/doc1" in done["result"]["docUrl"]
            # gdriveRef 영속 확인 — folderId 는 이제 회의별 하위 폴더 id
            m = client.get(f"/api/meetings/{mid}", headers=h).json()
            assert m["gdriveRef"]["docId"] == "doc1" and m["gdriveRef"]["folderId"] == "sub1"
            # 재동기화: 같은 docId 전달(update, 중복 생성 없음)
            job2 = client.post(f"/api/meetings/{mid}/drive-sync", headers=h).json()
            _wait_job(client, h, job2["jobId"])
        assert doc_calls == [None, "doc1"], doc_calls  # 첫=생성, 재=갱신
        # 문서는 하위폴더(sub1)에 "회의록" 이름으로 저장
        assert doc_meta[0] == ("sub1", "회의록"), doc_meta
        # 하위폴더: 첫 sync 는 fid=None(생성), 재sync 는 fid="sub1"(재사용). 이름에 회의명·날짜 포함.
        assert sub_calls[0][0] == "folder1" and sub_calls[0][2] is None
        assert sub_calls[0][1].startswith("테스트 회의_"), sub_calls
        assert sub_calls[1][2] == "sub1", sub_calls


def test_drive_sync_auth_expired():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        appmod.auth.set_google_credential("admin", "rt-bad", email=None)
        mid = _make_meeting(client, h)
        with mock.patch.object(
            appmod.google_oauth, "refresh_access_token",
            side_effect=appmod.google_oauth.GoogleAuthExpired("invalid_grant")):
            job = client.post(f"/api/meetings/{mid}/drive-sync", headers=h).json()
            done = _wait_job(client, h, job["jobId"])
        assert done["status"] == "error" and done["error_code"] == "google_auth_expired"


def test_drive_sync_ownership_and_job_isolation():
    with _tmp("admin:pw1,bob:pw2") as (auth, appmod, client):
        ha = _headers(auth, appmod, "admin")
        hb = _headers(auth, appmod, "bob")
        appmod.auth.set_google_credential("admin", "rt", email=None)
        appmod.auth.set_google_credential("bob", "rt2", email=None)
        mid = _make_meeting(client, ha)  # admin 소유
        # bob 이 admin 회의 동기화 → 404
        assert client.post(f"/api/meetings/{mid}/drive-sync", headers=hb).status_code == 404
        # admin 잡을 bob 이 폴링 → 404(잡 소유격리)
        with mock.patch.object(appmod.google_oauth, "refresh_access_token", return_value="AT"), \
             mock.patch.object(appmod.google_drive, "ensure_root_folder", return_value="f1"), \
             mock.patch.object(appmod.google_drive, "ensure_subfolder", return_value="s1"), \
             mock.patch.object(appmod.google_drive, "upsert_doc", return_value="d1"):
            job = client.post(f"/api/meetings/{mid}/drive-sync", headers=ha).json()
            assert client.get(f"/api/ai/jobs/{job['jobId']}", headers=hb).status_code == 404
            _wait_job(client, ha, job["jobId"])


def test_drive_sync_with_audio_upload_and_skip():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        appmod.auth.set_google_credential("admin", "rt", email=None)
        # 오디오 파일 배치(staging → bind) 후 그 audioRef 로 회의 생성
        mid = uuid.uuid4().hex
        token, _ext = appmod.audio_store.save_staging(b"AUDIO", mime_type="audio/webm", filename="a.webm")
        ref = appmod.audio_store.bind_staging(token, mid)
        assert ref is not None
        r = client.post("/api/meetings", json={"id": mid, "title": "오디오 회의", "audioRef": ref}, headers=h)
        assert r.status_code == 200, r.text
        audio_calls = []
        audio_names = []  # 오디오는 "원본.{ext}" 이름으로 저장되는지 확인
        with mock.patch.object(appmod.google_oauth, "refresh_access_token", return_value="AT"), \
             mock.patch.object(appmod.google_drive, "ensure_root_folder", return_value="folder1"), \
             mock.patch.object(appmod.google_drive, "ensure_subfolder", return_value="sub1"), \
             mock.patch.object(appmod.google_drive, "upsert_doc", return_value="doc1"), \
             mock.patch.object(
                 appmod.google_drive, "upsert_audio",
                 side_effect=lambda at, fld, path, mime, name, audio_id: (
                     audio_calls.append(audio_id), audio_names.append((fld, name)))[0] or (audio_id or "aud1")):
            j1 = client.post(f"/api/meetings/{mid}/drive-sync", headers=h).json()
            d1 = _wait_job(client, h, j1["jobId"])
            assert d1["result"]["gdriveRef"]["audioId"] == "aud1"
            # 업로드 성공 → 로컬 원본 정리(디스크 회수). audioRef 메타는 DB 에 남는다.
            assert appmod.audio_store.meeting_audio_path(mid, ref) is None, "로컬 원본이 삭제돼야 함"
            j2 = client.post(f"/api/meetings/{mid}/drive-sync", headers=h).json()
            d2 = _wait_job(client, h, j2["jobId"])
            # 재sync: 로컬 원본이 없으므로 재업로드 안 함. audioId 는 gref 에서 보존.
            assert d2["result"]["gdriveRef"]["audioId"] == "aud1"
        # 첫 sync 만 업로드(로컬 삭제 후 재sync 는 upsert_audio 미호출) — Drive 원본은 불변 보존.
        assert audio_calls == [None], audio_calls
        # 하위폴더(sub1)에 "원본.webm" 이름으로 저장
        assert audio_names[0] == ("sub1", "원본.webm"), audio_names


def test_audio_stream_proxies_from_drive_after_prune():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        appmod.auth.set_google_credential("admin", "rt", email=None)
        mid = uuid.uuid4().hex
        token, _ext = appmod.audio_store.save_staging(b"AUDIO", mime_type="audio/webm", filename="a.webm")
        ref = appmod.audio_store.bind_staging(token, mid)
        client.post("/api/meetings", json={"id": mid, "title": "오디오 회의", "audioRef": ref}, headers=h)
        # 동기화 → 업로드 성공 → 로컬 원본 삭제
        with mock.patch.object(appmod.google_oauth, "refresh_access_token", return_value="AT"), \
             mock.patch.object(appmod.google_drive, "ensure_root_folder", return_value="folder1"), \
             mock.patch.object(appmod.google_drive, "ensure_subfolder", return_value="sub1"), \
             mock.patch.object(appmod.google_drive, "upsert_doc", return_value="doc1"), \
             mock.patch.object(appmod.google_drive, "upsert_audio", return_value="aud1"):
            j = client.post(f"/api/meetings/{mid}/drive-sync", headers=h).json()
            _wait_job(client, h, j["jobId"])
        assert appmod.audio_store.meeting_audio_path(mid, ref) is None  # 로컬 정리됨
        # 로컬 부재 → Drive 프록시. stream_media mock 으로 206 릴레이 확인.
        import io

        def _fake_stream(access, file_id, range_header):
            assert file_id == "aud1"  # gref.audioId 로 프록시
            return 206, {"Content-Range": "bytes 0-4/5", "Content-Length": "5"}, io.BytesIO(b"AUDIO")

        with mock.patch.object(appmod.google_oauth, "refresh_access_token_cached", return_value="AT"), \
             mock.patch.object(appmod.google_drive, "stream_media", side_effect=_fake_stream):
            r = client.get(f"/api/meetings/{mid}/audio", headers={**h, "Range": "bytes=0-"})
        assert r.status_code == 206, r.text
        assert r.content == b"AUDIO"
        assert r.headers.get("Content-Range") == "bytes 0-4/5"


def test_audio_stream_404_when_local_gone_and_no_drive():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        mid = uuid.uuid4().hex
        token, _ext = appmod.audio_store.save_staging(b"AUDIO", mime_type="audio/webm", filename="a.webm")
        ref = appmod.audio_store.bind_staging(token, mid)
        client.post("/api/meetings", json={"id": mid, "title": "x", "audioRef": ref}, headers=h)
        appmod.audio_store.delete_meeting_audio(mid)  # 로컬 삭제 + gdriveRef 없음(미업로드)
        r = client.get(f"/api/meetings/{mid}/audio", headers=h)
        assert r.status_code == 404  # 프록시 대상(audioId) 없음 → 존재 은닉 404


def test_refresh_access_token_cached_reuses_until_expiry():
    import datetime as _dt

    import src.web.google_oauth as go

    go._token_cache.clear()
    calls = []
    # 만료 1시간 후 → 캐시 유지. creds.expiry 는 실제로 naive UTC 이므로 tz 제거해 동일하게.
    future = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) + _dt.timedelta(hours=1)

    class _Creds:
        def __init__(self, tok, exp):
            self.token = tok
            self.expiry = exp

    def _fake(rt):
        calls.append(rt)
        return _Creds("AT1", future)

    try:
        with mock.patch.object(go, "_refresh_creds", side_effect=_fake):
            assert go.refresh_access_token_cached("rt-x") == "AT1"
            assert go.refresh_access_token_cached("rt-x") == "AT1"  # 캐시 재사용
        assert calls == ["rt-x"], calls  # 두번째 호출은 네트워크 미발생
    finally:
        go._token_cache.clear()


def test_drive_sync_dedup_inflight():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        appmod.auth.set_google_credential("admin", "rt", email=None)
        mid = _make_meeting(client, h)
        import threading as _t

        gate = _t.Event()

        def _blocking_doc(at, fld, html, title, doc_id):
            gate.wait(timeout=5)  # 첫 잡을 processing 에 붙잡아 둠(in-flight 유지)
            return "doc1"

        with mock.patch.object(appmod.google_oauth, "refresh_access_token", return_value="AT"), \
             mock.patch.object(appmod.google_drive, "ensure_root_folder", return_value="f1"), \
             mock.patch.object(appmod.google_drive, "ensure_subfolder", return_value="s1"), \
             mock.patch.object(appmod.google_drive, "upsert_doc", side_effect=_blocking_doc):
            j1 = client.post(f"/api/meetings/{mid}/drive-sync", headers=h).json()
            # 첫 잡 진행 중 두번째 요청 → 같은 jobId 재사용(중복 잡 생성 안 함)
            j2 = client.post(f"/api/meetings/{mid}/drive-sync", headers=h).json()
            assert j2["jobId"] == j1["jobId"], (j1, j2)
            gate.set()
            _wait_job(client, h, j1["jobId"])
        # 완료 후 in-flight 해제 → 재요청은 새 jobId
        with mock.patch.object(appmod.google_oauth, "refresh_access_token", return_value="AT"), \
             mock.patch.object(appmod.google_drive, "ensure_root_folder", return_value="f1"), \
             mock.patch.object(appmod.google_drive, "ensure_subfolder", return_value="s1"), \
             mock.patch.object(appmod.google_drive, "upsert_doc", return_value="doc1"):
            j3 = client.post(f"/api/meetings/{mid}/drive-sync", headers=h).json()
            assert j3["jobId"] != j1["jobId"]
            _wait_job(client, h, j3["jobId"])


def test_delete_meeting_removes_drive_subfolder():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        appmod.auth.set_google_credential("admin", "rt", email="me@corp.com")
        appmod.auth.set_google_root_folder("admin", "root1")  # 루트와 하위폴더 구분용
        mid = _make_meeting(client, h)
        # gdriveRef: 하위폴더 sub1(!= root1) → 삭제 시 폴더째 정리 대상
        appmod.store.update_if_match(
            mid, {"gdriveRef": {"docId": "d1", "audioId": "a1", "folderId": "sub1"}}, None
        )
        deleted = []
        with mock.patch.object(appmod, "DRIVE_DELETE_ON_MEETING_DELETE", True), \
             mock.patch.object(appmod.google_oauth, "refresh_access_token", return_value="AT"), \
             mock.patch.object(
                 appmod.google_drive, "delete_files",
                 side_effect=lambda at, ids: deleted.append(list(ids)) or len(ids)):
            client.delete(f"/api/meetings/{mid}", headers=h)
            for _ in range(100):  # 백그라운드 삭제 스레드 대기
                if deleted:
                    break
                time.sleep(0.02)
        assert deleted and deleted[0] == ["sub1"], deleted  # 하위폴더째(내용 동반) — 빈 폴더 잔존 방지


def test_status_configured_flag():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        with mock.patch.object(appmod.google_oauth, "oauth_configured", return_value=True):
            st = client.get("/api/settings/google/status", headers=h).json()
        assert st["connected"] is False and st["configured"] is True


def test_disconnect_clears():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "admin")
        appmod.auth.set_google_credential("admin", "rt", email="me@corp.com")
        with mock.patch.object(appmod.google_drive, "revoke") as revoke:
            r = client.delete("/api/settings/google", headers=h)
        assert r.status_code == 200 and r.json()["cleared"] is True
        revoke.assert_called_once()
        assert client.get("/api/settings/google/status", headers=h).json()["connected"] is False


# ---------- 하위 폴더 이름/ensure_subfolder 단위 ----------
def test_kst_stamp_and_subfolder_name():
    import src.web.app as appmod

    # UTC → KST(+9) 변환. 2026-07-05T23:30Z → 07-06 08:30 KST
    assert appmod._kst_stamp("2026-07-05T23:30:00+00:00") == "2026-07-06_0830"
    assert appmod._kst_stamp("2026-07-06T00:00:00") == "2026-07-06_0900"  # tz 없으면 UTC 가정
    assert appmod._kst_stamp("garbage") == ""
    # 폴더 이름: {회의명}_{YYYY-MM-DD}_{HHMM}
    name = appmod._drive_subfolder_name({"title": "주간 회의", "createdAt": "2026-07-05T23:30:00+00:00"})
    assert name == "주간 회의_2026-07-06_0830", name
    # 경로 구분자·제어문자 정리, 제목 없으면 폴백
    assert appmod._sanitize_drive_name("a/b\\c") == "a-b-c"
    assert appmod._sanitize_drive_name("  ") == "회의록"
    assert appmod._drive_subfolder_name({"title": "회의"}) == "회의"  # createdAt 없으면 스탬프 생략


def test_ensure_subfolder_reuse_search_create():
    import src.web.google_drive as gd

    class _Files:
        def __init__(self):
            self.created = []
            self.listed = []

        def get(self, fileId, fields):
            return _Exec({"id": fileId, "trashed": False})

        def list(self, q, fields, pageSize, spaces):
            self.listed.append(q)
            return _Exec({"files": []})  # 검색 결과 없음 → 생성

        def create(self, body, fields):
            self.created.append(body)
            return _Exec({"id": "new-sub"})

    class _Exec:
        def __init__(self, val):
            self._val = val

        def execute(self):
            return self._val

    class _Service:
        def __init__(self, files):
            self._files = files

        def files(self):
            return self._files

    files = _Files()
    with mock.patch.object(gd, "_drive", return_value=_Service(files)):
        # folder_id 유효 → 재사용(검색·생성 없음)
        assert gd.ensure_subfolder("AT", "parent", "n", "existing") == "existing"
        assert files.created == [] and files.listed == []
        # folder_id 없음 → 검색(없음) → 생성
        assert gd.ensure_subfolder("AT", "parent", "새 회의_2026-07-06", None) == "new-sub"
        assert len(files.created) == 1
        assert files.created[0]["parents"] == ["parent"]
        assert "새 회의_2026-07-06" in files.listed[0] and "'parent' in parents" in files.listed[0]


# ---------- 헬퍼 ----------
@contextlib.contextmanager
def _tmp(users: str = "admin:pw1"):
    import tempfile

    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), users) as ctx:
        yield ctx


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_google_drive_sync ({len(fns)} cases)")


if __name__ == "__main__":
    _run()
