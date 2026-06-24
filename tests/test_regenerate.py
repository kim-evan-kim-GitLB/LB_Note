"""재요약(regenerate) prompt 모드 회귀 테스트 (계획 v4 트랙 C·P8).

검증 불변식:
  - _segments_from_transcript: 저장본 transcript → pseudo-segment(id=segmentId 또는 위치 폴백,
    text=교정문, start=timestamp초, end=다음 start). 빈 줄 skip.
  - store.apply_regenerate: summary+actionItems 전면 교체 + 적용 직전 현행을 meeting_backup 스냅샷.
    If-Match 412, ownerId/transcript/title 보존.
  - store.restore_latest_backup: 최근 백업 복원·소비(1회), 백업 없으면 (현행, False).
  - POST /regenerate: 비동기 잡(passthrough 백엔드=빈 결과)·폴링·빈 transcript 400.
  - POST /regenerate/apply: 전면 교체·백업·item_id 정규화·412. /regenerate/undo: 복원·409(백업없음).

실 DB 미접촉(tempfile + DEFAULT_DB_PATH 패치 격리).

실행: sudo uv run --frozen pytest tests/test_regenerate.py -q
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------- 단위: 재구성 헬퍼 ----------
def test_ts_to_seconds():
    from src.web.app import _ts_to_seconds

    assert _ts_to_seconds("00:12") == 12.0
    assert _ts_to_seconds("03:20") == 200.0
    assert _ts_to_seconds("01:02:03") == 3723.0
    assert _ts_to_seconds("bad") == 0.0
    assert _ts_to_seconds(None) == 0.0


def test_segments_from_transcript_uses_segment_id_and_corrected_text():
    from src.web.app import _segments_from_transcript

    tr = [
        {"text": "교정문1", "timestamp": "00:12", "segmentId": 0},
        {"text": "", "timestamp": "00:30", "segmentId": 1},  # 빈 → skip
        {"text": "교정문2", "timestamp": "03:20", "segmentId": 2},
    ]
    segs = _segments_from_transcript(tr)
    assert [s["id"] for s in segs] == [0, 2], "segmentId 사용, 빈 줄 skip"
    assert [s["text"] for s in segs] == ["교정문1", "교정문2"], "교정문 반영"
    assert segs[0]["start"] == 12.0 and segs[0]["end"] == 200.0, "end=다음 start"
    assert segs[1]["start"] == 200.0


def test_segments_fallback_to_index_when_no_segment_id():
    from src.web.app import _segments_from_transcript

    tr = [{"text": "a", "timestamp": "00:01"}, {"text": "b", "timestamp": "00:02"}]
    segs = _segments_from_transcript(tr)
    assert [s["id"] for s in segs] == [0, 1], "segmentId 부재 시 위치 인덱스 폴백"


# ---------- 단위: store apply/restore ----------
def _store(tmp: Path):
    from src.web.store import MeetingStore

    return MeetingStore(tmp / "meetings.db")


def _seed(store, *, summary=None, actions=None):
    m = {
        "id": "a" * 32,
        "ownerId": "u1",
        "status": "review",
        "title": "회의",
        "createdAt": "2026-06-24T00:00:00",
        "updatedAt": "2026-06-24T00:00:00",
        "transcript": [{"speakerId": "", "text": "t", "timestamp": "00:01", "segmentId": 0}],
        "summary": summary if summary is not None else {"agenda": [{"no": 1, "title": "옛안건", "points": []}]},
        "actionItems": actions if actions is not None else [{"item_id": "old", "text": "옛할일"}],
    }
    return store.create(m)


def test_apply_regenerate_replaces_and_backs_up():
    with tempfile.TemporaryDirectory() as td:
        store = _store(Path(td))
        cur = _seed(store)
        etag = cur["updatedAt"]
        new_sum = {"agenda": [{"no": 1, "title": "새안건", "points": []}]}
        new_act = [{"item_id": "new", "text": "새할일"}]
        out = store.apply_regenerate("a" * 32, new_sum, new_act, etag)
        assert out["summary"] == new_sum and out["actionItems"] == new_act
        assert out["updatedAt"] != etag, "새 ETag"
        assert out["ownerId"] == "u1" and out["title"] == "회의", "다른 필드 보존"
        assert out["transcript"][0]["text"] == "t", "transcript 보존"
        # 백업 1건 생성됨(undo 가능)
        restored, ok = store.restore_latest_backup("a" * 32, out["updatedAt"])
        assert ok and restored["summary"]["agenda"][0]["title"] == "옛안건", "백업=옛 summary"
        assert restored["actionItems"] == [{"item_id": "old", "text": "옛할일"}]


def test_apply_regenerate_if_match_mismatch():
    from src.web.store import PreconditionFailedError

    with tempfile.TemporaryDirectory() as td:
        store = _store(Path(td))
        _seed(store)
        with pytest.raises(PreconditionFailedError):
            store.apply_regenerate("a" * 32, {"agenda": []}, [], "stale-etag")


def test_restore_consumes_backup_one_shot():
    with tempfile.TemporaryDirectory() as td:
        store = _store(Path(td))
        cur = _seed(store)
        out = store.apply_regenerate("a" * 32, {"agenda": []}, [], cur["updatedAt"])
        r1, ok1 = store.restore_latest_backup("a" * 32, out["updatedAt"])
        assert ok1
        # 두 번째 undo → 백업 소비됨 → (현행, False)
        _, ok2 = store.restore_latest_backup("a" * 32, r1["updatedAt"])
        assert ok2 is False


# ---------- HTTP 통합 ----------
@contextlib.contextmanager
def _client_for(td: Path, users: str):
    from fastapi.testclient import TestClient
    import importlib

    tmp_db = td / "meetings.db"
    os.environ["JWT_SECRET"] = "test-secret-regenerate"
    os.environ["WEB_AUTH_USERS"] = users
    os.environ["WEB_AUTH_ADMINS"] = "admin"
    os.environ["WEB_AUTH_TOKEN_TTL"] = "3600"
    os.environ["WEB_AUTH_PRUNE"] = "1"
    # 재요약 잡은 LLM(summarize/extract)을 돈다 → 테스트에선 passthrough 로 강제(claude CLI 불필요).
    # .env 의 agent_cli 를 가리지 않으면 잡이 claude 를 PATH 에서 못 찾아 error 가 된다.
    be_sum_orig = os.environ.get("WEB_SUMMARIZE_BACKEND")
    be_ext_orig = os.environ.get("WEB_EXTRACT_BACKEND")
    os.environ["WEB_SUMMARIZE_BACKEND"] = "passthrough"
    os.environ["WEB_EXTRACT_BACKEND"] = "passthrough"
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
        # backend env 원복(후속 테스트 파일이 .env 의 agent_cli 가정을 유지하도록)
        for k, v in (("WEB_SUMMARIZE_BACKEND", be_sum_orig), ("WEB_EXTRACT_BACKEND", be_ext_orig)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _h(auth, appmod, u="admin"):
    appmod.users.set_password(u, "newpassword123")
    return {"Authorization": f"Bearer {auth.make_token(u)}"}


def _make(client, h, *, transcript=None, summary=None, actions=None):
    body = {"title": "회의", "status": "review"}
    body["transcript"] = transcript if transcript is not None else [
        {"speakerId": "", "text": "한 줄", "timestamp": "00:01", "segmentId": 0}
    ]
    if summary is not None:
        body["summary"] = summary
    if actions is not None:
        body["actionItems"] = actions
    r = client.post("/api/meetings", json=body, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_http_regenerate_job_passthrough_completes():
    """passthrough 백엔드(테스트 기본)에선 빈 결과로 잡이 정상 완료된다(LLM 불필요)."""
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _h(auth, appmod)
        m = _make(client, h)
        r = client.post(f"/api/meetings/{m['id']}/regenerate", headers=h)
        assert r.status_code == 200, r.text
        job_id = r.json()["jobId"]
        # 폴링(잡 스레드 완료까지)
        for _ in range(100):
            jr = client.get(f"/api/ai/jobs/{job_id}", headers=h).json()
            if jr["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert jr["status"] == "done", jr
        assert "summary" in jr["result"] and "actionItems" in jr["result"]


def test_http_regenerate_empty_transcript_400():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _h(auth, appmod)
        m = _make(client, h, transcript=[])
        r = client.post(f"/api/meetings/{m['id']}/regenerate", headers=h)
        assert r.status_code == 400, r.text


def test_http_apply_and_undo_roundtrip():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _h(auth, appmod)
        m = _make(
            client, h,
            summary={"agenda": [{"no": 1, "title": "옛", "points": []}]},
            actions=[{"text": "옛할일"}],
        )
        etag = m["updatedAt"]
        # 확정(apply): 전면 교체 + 백업
        new_sum = {"agenda": [{"no": 1, "title": "새", "points": []}]}
        r = client.post(
            f"/api/meetings/{m['id']}/regenerate/apply",
            json={"summary": new_sum, "actionItems": [{"text": "새할일"}]},
            headers={**h, "If-Match": f'"{etag}"'},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["summary"]["agenda"][0]["title"] == "새"
        assert len(body["actionItems"][0]["item_id"]) == 32, "apply 도 actionItem item_id 부여"
        assert body["updatedAt"] != etag
        # undo → 옛 summary 복원
        ru = client.post(
            f"/api/meetings/{m['id']}/regenerate/undo",
            headers={**h, "If-Match": f'"{body["updatedAt"]}"'},
        )
        assert ru.status_code == 200, ru.text
        assert ru.json()["summary"]["agenda"][0]["title"] == "옛", "undo=옛 summary 복원"
        # 두 번째 undo → 백업 소비됨 → 409
        ru2 = client.post(f"/api/meetings/{m['id']}/regenerate/undo", headers=h)
        assert ru2.status_code == 409, ru2.text


def test_http_apply_stale_if_match_412():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _h(auth, appmod)
        m = _make(client, h, summary={"agenda": []}, actions=[])
        r = client.post(
            f"/api/meetings/{m['id']}/regenerate/apply",
            json={"summary": {"agenda": []}, "actionItems": []},
            headers={**h, "If-Match": '"stale"'},
        )
        assert r.status_code == 412, r.text
