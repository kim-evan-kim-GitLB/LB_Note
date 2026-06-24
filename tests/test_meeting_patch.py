"""회의 PATCH 교정 + If-Match 낙관적 동시성 회귀 테스트 (계획 v4 트랙 B·Phase 3).

검증 불변식:
  - store.update_if_match: 원자 compare-and-update.
      * expected=None → 비교 생략(last-write-wins), updatedAt 자동 갱신·ownerId 불변.
      * expected 일치 → 적용·새 updatedAt 반환.
      * expected 불일치 → PreconditionFailedError(현재 updatedAt 힌트).
  - PATCH /api/meetings/{id}:
      * If-Match 없음 → 기존 동작(last-write-wins) 그대로(후방호환: finalize/제목/액션 편집).
      * If-Match 일치 → 200 + 새 updatedAt(ETag).
      * If-Match 불일치(stale) → 412(lost-update 방지) + currentUpdatedAt 힌트.
      * transcript text-only 편집 → edited 서버 set·timestamp/speakerId 불변.
      * 엔트리 개수/timestamp 변경 → 422.
      * 저장본 transcript 가 비어있던 초기 상태 → 구조검증 미적용(후방호환).

실 DB(output/web/meetings.db)는 **절대 건드리지 않는다** — tempfile + DEFAULT_DB_PATH 패치로
격리(test_profile_edit.py 와 동일 패턴, try/finally 원복 포함).

실행: sudo PYTHONPATH=/app .venv/bin/python tests/test_meeting_patch.py
"""
from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


# ---------- store 원자 메서드 단위 테스트(앱·HTTP 없이) ----------
def _fresh_store(tmp: Path):
    """임시 DB 로 격리된 MeetingStore. DEFAULT_DB_PATH 전역 미접촉(명시 인자)."""
    from src.web.store import MeetingStore

    return MeetingStore(tmp / "meetings.db")


def _make_meeting(store, *, owner="u1", tr=None) -> dict:
    m = {
        "id": "a" * 32,
        "ownerId": owner,
        "status": "review",
        "title": "회의",
        "createdAt": "2026-06-23T10:00:00",
        "updatedAt": "2026-06-23T10:00:00",
        "transcript": tr if tr is not None else [],
    }
    return store.create(m)


def test_store_update_if_match_no_expected_is_last_write_wins():
    with tempfile.TemporaryDirectory() as td:
        store = _fresh_store(Path(td))
        _make_meeting(store)
        out = store.update_if_match("a" * 32, {"title": "새제목"}, None)
        assert out is not None
        assert out["title"] == "새제목"
        # updatedAt 자동 갱신(새 ETag), ownerId 불변
        assert out["updatedAt"] != "2026-06-23T10:00:00"
        assert out["ownerId"] == "u1"
        # 없는 회의 → None
        assert store.update_if_match("f" * 32, {"title": "x"}, None) is None


def test_store_update_if_match_owner_immutable():
    with tempfile.TemporaryDirectory() as td:
        store = _fresh_store(Path(td))
        _make_meeting(store, owner="u1")
        out = store.update_if_match("a" * 32, {"ownerId": "attacker", "title": "t"}, None)
        assert out["ownerId"] == "u1", "ownerId 는 patch 로 변경 불가"


def test_store_update_if_match_match_and_mismatch():
    from src.web.store import PreconditionFailedError

    with tempfile.TemporaryDirectory() as td:
        store = _fresh_store(Path(td))
        cur = _make_meeting(store)
        etag = cur["updatedAt"]
        # 일치 → 적용·새 updatedAt
        out = store.update_if_match("a" * 32, {"title": "ok"}, etag)
        assert out["title"] == "ok"
        new_etag = out["updatedAt"]
        assert new_etag != etag
        # stale(이전 etag) 재시도 → PreconditionFailedError(현재값 힌트)
        try:
            store.update_if_match("a" * 32, {"title": "stale"}, etag)
            assert False, "stale If-Match 는 예외여야 함"
        except PreconditionFailedError as e:
            assert e.current_updated_at == new_etag
        # 저장본은 stale 쓰기에 오염되지 않음(lost-update 방지)
        assert store.get("a" * 32)["title"] == "ok"


# ---------- HTTP 통합 테스트(임시 DB 격리 app) ----------
@contextlib.contextmanager
def _client_for(td: Path, users: str):
    """임시 DB 로 격리된 app + TestClient. 실 DB 미접촉(test_profile_edit 와 동일 패턴).

    DEFAULT_DB_PATH 전역을 임시경로로 패치 후 auth·app reload → 모듈 레벨
    MeetingStore()/auth.init() 가 임시 DB 만 쓰게 하고, 종료 시 try/finally 원복."""
    from fastapi.testclient import TestClient
    import importlib

    tmp_db = td / "meetings.db"
    os.environ["JWT_SECRET"] = "test-secret-meeting-patch"
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
    """must_change_password 게이트 해제(set_password) 후 Bearer 헤더 반환.

    /api/meetings* 는 require_user_active(must_change_password=1 이면 403)를 거치므로,
    본인이 비번을 바꾼 것처럼 게이트를 해제한다."""
    appmod.users.set_password(username, "newpassword123")
    return {"Authorization": f"Bearer {auth.make_token(username)}"}


def _create_meeting(client, h, *, transcript=None) -> dict:
    body = {"title": "테스트회의", "status": "review"}
    if transcript is not None:
        body["transcript"] = transcript
    r = client.post("/api/meetings", json=body, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_patch_without_if_match_is_backward_compatible():
    """If-Match 없으면 기존 동작(last-write-wins) 그대로 — finalize/제목/액션 편집 비파괴."""
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        m = _create_meeting(client, h)
        r = client.patch(f"/api/meetings/{m['id']}", json={"title": "확정제목", "status": "done"}, headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["title"] == "확정제목" and body["status"] == "done"
        # updatedAt 갱신(ETag), ownerId 보존
        assert body["updatedAt"] != m["updatedAt"]
        assert body["ownerId"] == m["ownerId"]
        # ETag 헤더 노출
        assert r.headers.get("ETag") == f'"{body["updatedAt"]}"'


def test_patch_if_match_success_and_stale_412():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        m = _create_meeting(client, h)
        etag = m["updatedAt"]
        # 일치하는 If-Match → 200 + 새 updatedAt
        r = client.patch(
            f"/api/meetings/{m['id']}", json={"title": "v2"}, headers={**h, "If-Match": f'"{etag}"'}
        )
        assert r.status_code == 200, r.text
        new_etag = r.json()["updatedAt"]
        assert new_etag != etag
        # stale If-Match(이전 etag) → 412 (lost-update 방지)
        r2 = client.patch(
            f"/api/meetings/{m['id']}", json={"title": "v3"}, headers={**h, "If-Match": f'"{etag}"'}
        )
        assert r2.status_code == 412, r2.text
        detail = r2.json()["detail"]
        assert detail["currentUpdatedAt"] == new_etag
        # 저장본은 412 쓰기에 오염되지 않음
        cur = client.get(f"/api/meetings/{m['id']}", headers=h).json()
        assert cur["title"] == "v2"


def test_patch_stale_if_match_prevents_lost_update_sequential():
    """순차 시뮬: A 가 etag0 로 읽고, B 가 먼저 저장(etag1) → A 의 etag0 PATCH 는 412 로 차단."""
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        m = _create_meeting(client, h)
        etag0 = m["updatedAt"]
        # B 가 먼저 성공 저장
        rb = client.patch(
            f"/api/meetings/{m['id']}", json={"title": "B저장"}, headers={**h, "If-Match": f'"{etag0}"'}
        )
        assert rb.status_code == 200
        # A 는 여전히 etag0 을 들고 PATCH → 412(B 의 변경을 덮어쓰지 못함)
        ra = client.patch(
            f"/api/meetings/{m['id']}", json={"title": "A저장"}, headers={**h, "If-Match": f'"{etag0}"'}
        )
        assert ra.status_code == 412
        assert client.get(f"/api/meetings/{m['id']}", headers=h).json()["title"] == "B저장"


def test_patch_transcript_text_only_sets_edited_and_preserves_timestamps():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        tr = [
            {"speakerId": "", "text": "안녕하세요", "timestamp": "00:01"},
            {"speakerId": "", "text": "회의 시작합니다", "timestamp": "00:05"},
        ]
        m = _create_meeting(client, h, transcript=tr)
        # 첫 엔트리 text 만 교정(클라가 edited=False 위조해도 서버가 결정)
        edited_tr = [
            {"speakerId": "", "text": "안녕하십니까", "timestamp": "00:01", "edited": False},
            {"speakerId": "", "text": "회의 시작합니다", "timestamp": "00:05"},
        ]
        r = client.patch(f"/api/meetings/{m['id']}", json={"transcript": edited_tr}, headers=h)
        assert r.status_code == 200, r.text
        out = r.json()["transcript"]
        assert out[0]["text"] == "안녕하십니까"
        assert out[0]["edited"] is True, "text 변경 엔트리는 서버가 edited=True set"
        assert out[0]["timestamp"] == "00:01" and out[0]["speakerId"] == ""
        # 변경 없는 엔트리는 edited 미부여
        assert out[1].get("edited") is not True
        assert out[1]["timestamp"] == "00:05"


def test_patch_transcript_count_change_422():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        tr = [
            {"speakerId": "", "text": "한 줄", "timestamp": "00:01"},
            {"speakerId": "", "text": "두 줄", "timestamp": "00:05"},
        ]
        m = _create_meeting(client, h, transcript=tr)
        # 엔트리 삭제(개수 변경) → 422
        r = client.patch(
            f"/api/meetings/{m['id']}", json={"transcript": tr[:1]}, headers=h
        )
        assert r.status_code == 422, r.text


def test_patch_transcript_timestamp_change_422():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        tr = [{"speakerId": "", "text": "한 줄", "timestamp": "00:01"}]
        m = _create_meeting(client, h, transcript=tr)
        bad = [{"speakerId": "", "text": "한 줄", "timestamp": "09:99"}]
        r = client.patch(f"/api/meetings/{m['id']}", json={"transcript": bad}, headers=h)
        assert r.status_code == 422, r.text


def test_patch_transcript_empty_stored_skips_validation():
    """저장본 transcript 가 비어있던 초기 상태 → 구조검증 미적용(후방호환). finalize 가 처음
    transcript 를 채우는 경로(개수 0→N)를 막지 않는다."""
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        m = _create_meeting(client, h)  # transcript 없음(빈 [])
        new_tr = [
            {"speakerId": "", "text": "처음 채움", "timestamp": "00:01"},
            {"speakerId": "", "text": "두 번째", "timestamp": "00:03"},
        ]
        r = client.patch(f"/api/meetings/{m['id']}", json={"transcript": new_tr}, headers=h)
        assert r.status_code == 200, r.text
        assert len(r.json()["transcript"]) == 2


def test_patch_transcript_with_other_fields_simultaneously():
    """transcript 검증이 다른 필드 동시 patch 를 막지 않는다(summary/actionItems 동시 갱신)."""
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        tr = [{"speakerId": "", "text": "원문", "timestamp": "00:01"}]
        m = _create_meeting(client, h, transcript=tr)
        edited_tr = [{"speakerId": "", "text": "교정문", "timestamp": "00:01"}]
        r = client.patch(
            f"/api/meetings/{m['id']}",
            json={"transcript": edited_tr, "title": "동시갱신", "actionItems": [{"text": "할일"}]},
            headers=h,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["title"] == "동시갱신"
        assert body["transcript"][0]["edited"] is True
        # actionItems 는 구조 잠금 없이 통과하되 item_id 무결성만 부여(재요약 조인키 선결)
        assert len(body["actionItems"]) == 1
        assert body["actionItems"][0]["text"] == "할일"
        assert len(body["actionItems"][0]["item_id"]) == 32


def test_patch_transcript_speaker_change_422():
    """speakerId 변경은 구조보존 위반 → 422(M2·L4)."""
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        tr = [{"speakerId": "spk1", "text": "한 줄", "timestamp": "00:01"}]
        m = _create_meeting(client, h, transcript=tr)
        bad = [{"speakerId": "spk2", "text": "한 줄", "timestamp": "00:01"}]
        r = client.patch(f"/api/meetings/{m['id']}", json={"transcript": bad}, headers=h)
        assert r.status_code == 422, r.text


def test_patch_transcript_preserves_unknown_stored_fields():
    """M3: 저장본의 미지 필드(예: 미래 confidence)는 text 편집 후에도 보존되고, incoming 의
    임의/위조 필드는 반영되지 않는다(text 만 교체)."""
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        # 저장본을 직접 store 로 심어 미지 필드(confidence)를 포함시킨다(create 경로는 임의 dict 보존).
        tr = [{"speakerId": "", "text": "원문", "timestamp": "00:01", "confidence": 0.91}]
        m = _create_meeting(client, h, transcript=tr)
        # 클라가 confidence 위조 + 미지 필드(injected) 추가 + text 교정 시도
        edited = [{
            "speakerId": "", "text": "교정문", "timestamp": "00:01",
            "confidence": 0.0, "injected": "evil", "edited": False,
        }]
        r = client.patch(f"/api/meetings/{m['id']}", json={"transcript": edited}, headers=h)
        assert r.status_code == 200, r.text
        out = r.json()["transcript"][0]
        assert out["text"] == "교정문"          # text 만 교체됨
        assert out["confidence"] == 0.91         # 저장본 미지 필드 보존(위조 무시)
        assert "injected" not in out             # incoming 임의 필드 미반영
        assert out["edited"] is True             # 서버가 edited 결정


def test_patch_if_match_star_matches_when_exists():
    """If-Match `*` → 리소스 존재하면 값 비교 없이 갱신 성공(M4)."""
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        m = _create_meeting(client, h)
        r = client.patch(
            f"/api/meetings/{m['id']}", json={"title": "별표"}, headers={**h, "If-Match": "*"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["title"] == "별표"


def test_patch_if_match_weak_etag_prefix_stripped():
    """If-Match `W/"..."`(약 ETag) → 접두 제거 후 값 비교(M4). 일치하면 200, stale 이면 412."""
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        m = _create_meeting(client, h)
        etag = m["updatedAt"]
        r = client.patch(
            f"/api/meetings/{m['id']}", json={"title": "약태그"},
            headers={**h, "If-Match": f'W/"{etag}"'},
        )
        assert r.status_code == 200, r.text
        # 같은(이제 stale) 약 ETag 재시도 → 412
        r2 = client.patch(
            f"/api/meetings/{m['id']}", json={"title": "다시"},
            headers={**h, "If-Match": f'W/"{etag}"'},
        )
        assert r2.status_code == 412, r2.text


def test_patch_if_match_multi_value_uses_first():
    """If-Match 다중값(콤마) → 첫 토큰(현재 etag)으로 비교 → 200(M4)."""
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        m = _create_meeting(client, h)
        etag = m["updatedAt"]
        r = client.patch(
            f"/api/meetings/{m['id']}", json={"title": "다중"},
            headers={**h, "If-Match": f'"{etag}", "deadbeef"'},
        )
        assert r.status_code == 200, r.text


def test_patch_if_match_malformed_400():
    """비표준/파싱불가 If-Match(빈 따옴표) → 400(M4)."""
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        m = _create_meeting(client, h)
        r = client.patch(
            f"/api/meetings/{m['id']}", json={"title": "x"}, headers={**h, "If-Match": '""'}
        )
        assert r.status_code == 400, r.text


def test_store_etag_monotonic_on_rapid_updates():
    """M1: 같은 마이크로초 내 연속 갱신에도 updatedAt(ETag) 이 단조 증가(충돌 없음)."""
    with tempfile.TemporaryDirectory() as td:
        store = _fresh_store(Path(td))
        _make_meeting(store)
        etags = []
        for i in range(50):
            out = store.update_if_match("a" * 32, {"title": f"t{i}"}, None)
            etags.append(out["updatedAt"])
        assert len(set(etags)) == len(etags), "ETag 중복 발생(단조 증가 위반)"
        assert etags == sorted(etags), "ETag 가 단조 증가하지 않음"


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_meeting_patch ({len(fns)} cases)")


if __name__ == "__main__":
    _run()
