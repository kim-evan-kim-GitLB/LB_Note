"""Google 연동 진단 — 재연결 실패 원인을 사후에 판별할 수 있는가.

배경(실사용자 제보): "인증 만료가 떠서 다시 연결을 눌렀는데 연결이 안 된다". 조사해 보니
재연결 실패 경로 5개 중 3개(서버 미설정 / redirect_uri 불일치 / state 만료)가 서버에 아무
흔적도 남기지 않아, 관리자가 원인을 판별할 수단이 없었다. 또 만료된 refresh_token 이 있어도
설정 화면은 계속 '연결됨'으로 보였다(캘린더는 401 을 내는데).

검증 불변식:
  - connect 요청 자체가 기록된다(눌렀는지 확인 가능). 미설정 503 도 사유와 함께 남는다.
  - state 무효/만료 콜백이 기록된다(예전에는 401 만 나가고 무기록).
  - 콜백 실패는 프론트로 사유(reason)를 함께 넘긴다.
  - 만료(invalid_grant) 감지 시 needsReconnect=True 로 상태가 바뀌고, 재연동/검증 성공하면 풀린다.
  - GET /api/settings/google/verify: 즉시 재검증(claude verify 와 대칭), 토큰 미노출.
  - GET /api/admin/user-events: admin 전용(비관리자 403), 사용자/이벤트 접두사 필터.
  - 보존 정리(prune_user_events)가 기간·행수 상한을 적용한다.

실제 Google 호출은 하지 않는다(google_oauth 모킹). 임시 DB.
실행: sudo .venv/bin/python -m pytest tests/test_google_diagnostics.py
"""
from __future__ import annotations

import contextlib
import importlib
import os
import tempfile
from pathlib import Path
from unittest import mock


def _client_for(td: Path):
    from fastapi.testclient import TestClient

    tmp_db = td / "meetings.db"
    os.environ["JWT_SECRET"] = "test-secret-gdiag"
    os.environ["WEB_AUTH_USERS"] = "admin:pw1,dev:pw2"
    os.environ["WEB_AUTH_ADMINS"] = "admin"
    os.environ["WEB_AUTH_TOKEN_TTL"] = "3600"
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


@contextlib.contextmanager
def _tmp():
    with tempfile.TemporaryDirectory() as td:
        yield from _client_for(Path(td))


def _hdr(auth, appmod, username: str) -> dict:
    """Bearer 헤더. 시드 사용자는 must_change_password=1 이라 require_user_active 가 막으므로
    비번을 한 번 바꿔 게이트를 해제한다(다른 웹 테스트와 동일 패턴)."""
    appmod.users.set_password(username, "newpassword123")
    return {"Authorization": f"Bearer {auth.make_token(username)}"}


# ---- 연결 시도 기록 ----

def test_connect_start_is_recorded_even_when_not_configured():
    """서버 미설정으로 503 이 나도 '눌렀다'는 사실이 남는다(예전에는 무기록)."""
    with _tmp() as (auth, appmod, client):
        hdr = _hdr(auth, appmod, "dev")
        with mock.patch.object(appmod.google_oauth, "oauth_configured", return_value=False):
            r = client.post("/api/settings/google/connect", headers=hdr)
        assert r.status_code == 503
        events = auth.list_user_events(owner="dev", event_prefix="google.")
        assert [e["event"] for e in events] == ["google.connect_start"]
        assert "not_configured" in (events[0]["detail"] or "")


def test_connect_start_recorded_on_success():
    with _tmp() as (auth, appmod, client):
        hdr = _hdr(auth, appmod, "dev")
        with (
            mock.patch.object(appmod.google_oauth, "oauth_configured", return_value=True),
            mock.patch.object(
                appmod.google_oauth, "build_consent_url", return_value="https://accounts.google/x"
            ),
        ):
            r = client.post("/api/settings/google/connect", headers=hdr)
        assert r.status_code == 200 and r.json()["authUrl"].startswith("https://")
        events = auth.list_user_events(owner="dev", event_prefix="google.connect_start")
        assert "consent_url_issued" in (events[0]["detail"] or "")


# ---- 콜백 실패 갈래 ----

def test_callback_with_invalid_state_is_recorded():
    """state 만료/위조 콜백 — 예전에는 401 만 나가고 아무 기록이 없었다."""
    with _tmp() as (auth, appmod, client):
        from src.web import observability

        observability.reset()
        r = client.get(
            "/api/integrations/google/callback", params={"state": "bogus", "code": "x"}
        )
        assert r.status_code == 401
        # owner 를 모르는 상황이라 사용자별 이력이 아니라 카운터/로그로 남는다.
        assert observability.snapshot().get("google.callback_state_invalid") == 1


def test_callback_denied_carries_reason_to_frontend():
    """동의 거부 시 ?google=error&reason=... 으로 사유를 넘긴다(사유 없이 error 만 주지 않는다)."""
    with _tmp() as (auth, appmod, client):
        tok_state = auth.make_token("dev", ttl=600, scope="google_oauth")
        r = client.get(
            "/api/integrations/google/callback",
            params={"state": tok_state, "error": "access_denied"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        loc = r.headers["location"]
        assert "google=error" in loc and "reason=access_denied" in loc
        events = [e["event"] for e in auth.list_user_events(owner="dev", event_prefix="google.")]
        assert "google.callback_arrived" in events and "google.connect_error" in events


def test_callback_exchange_failure_carries_reason():
    with _tmp() as (auth, appmod, client):
        tok_state = auth.make_token("dev", ttl=600, scope="google_oauth")
        with mock.patch.object(
            appmod.google_oauth,
            "exchange_code",
            side_effect=appmod.google_oauth.GoogleOAuthError("토큰 교환 실패"),
        ):
            r = client.get(
                "/api/integrations/google/callback",
                params={"state": tok_state, "code": "abc"},
                follow_redirects=False,
            )
        assert r.status_code == 302
        assert "reason=exchange_failed" in r.headers["location"]


# ---- 만료 상태 정합 ----

def test_expired_credential_marks_needs_reconnect_and_verify_clears_it():
    """만료 감지 → needsReconnect=True. 이후 검증이 통과하면 해제(자가 치유)."""
    with _tmp() as (auth, appmod, client):
        hdr = _hdr(auth, appmod, "dev")
        auth.set_google_credential("dev", "refresh-xyz", email="me@corp.com")
        assert auth.google_status("dev")["needsReconnect"] is False

        with mock.patch.object(
            appmod.google_oauth,
            "refresh_access_token",
            side_effect=appmod.google_oauth.GoogleAuthExpired("invalid_grant"),
        ):
            r = client.get("/api/settings/google/verify", headers=hdr)
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] is False and body["errorCode"] == "google_auth_expired"
        assert body["status"]["needsReconnect"] is True
        assert body["status"]["connected"] is True  # 행은 유지(하위호환 · 폴더 보존)
        assert "refresh-xyz" not in r.text  # 토큰 절대 미노출

        with mock.patch.object(appmod.google_oauth, "refresh_access_token", return_value="at"):
            r2 = client.get("/api/settings/google/verify", headers=hdr)
        assert r2.json()["valid"] is True
        assert auth.google_status("dev")["needsReconnect"] is False


def test_reconnect_clears_invalid_flag():
    """재연동(set_google_credential)하면 무효 표시가 풀린다."""
    with _tmp() as (auth, appmod, client):
        auth.set_google_credential("dev", "old", email="me@corp.com")
        auth.mark_google_credential_invalid("dev")
        assert auth.google_status("dev")["needsReconnect"] is True
        auth.set_google_credential("dev", "new", email="me@corp.com")
        assert auth.google_status("dev")["needsReconnect"] is False


def test_mark_invalid_keeps_first_detection_time():
    with _tmp() as (auth, appmod, client):
        auth.set_google_credential("dev", "tok", email=None)
        assert auth.mark_google_credential_invalid("dev") is True
        first = auth.google_status("dev")["invalidAt"]
        assert auth.mark_google_credential_invalid("dev") is False  # 이미 표시됨 → 덮어쓰지 않음
        assert auth.google_status("dev")["invalidAt"] == first


# ---- 관리자 조회 창구 ----

def test_admin_user_events_requires_admin_and_filters():
    with _tmp() as (auth, appmod, client):
        admin_hdr = _hdr(auth, appmod, "admin")
        dev_hdr = _hdr(auth, appmod, "dev")

        assert client.get("/api/admin/user-events", headers=dev_hdr).status_code == 403

        auth.record_user_event("dev", "google.connect_error", "reason=access_denied")
        auth.record_user_event("dev", "ai_job.cancel", "job_id=1")
        auth.record_user_event("admin", "google.connect", None)

        r = client.get(
            "/api/admin/user-events", params={"username": "dev"}, headers=admin_hdr
        )
        assert r.status_code == 200
        assert {e["event"] for e in r.json()["events"]} == {"google.connect_error", "ai_job.cancel"}

        r2 = client.get(
            "/api/admin/user-events",
            params={"username": "dev", "event": "google."},
            headers=admin_hdr,
        )
        assert [e["event"] for e in r2.json()["events"]] == ["google.connect_error"]


def test_audit_without_owner_is_not_persisted():
    """owner 없는 이벤트(스케줄러 등)는 사용자 이력에 쌓이지 않는다."""
    with _tmp() as (auth, appmod, client):
        from src.web import observability

        observability.audit("scheduler.start", kind="cleanup")
        assert auth.list_user_events(event_prefix="scheduler.") == []


def test_prune_user_events_by_age_and_rows():
    with _tmp() as (auth, appmod, client):
        for i in range(10):
            auth.record_user_event("dev", f"e.{i}", None)
        assert auth.prune_user_events(max_age_sec=10**9, max_rows=5) == 5  # 행수 상한 초과분
        assert len(auth.list_user_events(owner="dev")) == 5
        # cutoff = now-max_age. 같은 초에 적재됐으므로 음수로 두어 cutoff 를 미래로 밀어 전량 대상.
        assert auth.prune_user_events(max_age_sec=-1, max_rows=5) == 5
        assert auth.list_user_events(owner="dev") == []
