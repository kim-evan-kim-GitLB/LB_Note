"""재요약 preserve_edited 병합 회귀 테스트 (후속: 편집 항목 별도 보존).

검증 불변식:
  - merge_preserve_edited: actionItems=재생성+현행 edited 덧붙임(비편집 현행 드롭), summary=재생성에
    '사용자 편집 보존' agenda 블록 추가(분류 유지·agenda_index 동기화), 편집 없으면 블록 없음.
  - POST /regenerate/apply?mode=preserve_edited: 현행 edited 항목 보존 병합·백업·undo.

재요약은 항상 새 item_id 라 등치매칭 불가 → 손실 0 으로 편집 보존(중복은 미리보기에서 사용자 정리).
실 DB 미접촉(tempfile + DEFAULT_DB_PATH 패치).

실행: sudo uv run --frozen pytest tests/test_regenerate_preserve.py -q
"""
from __future__ import annotations

import contextlib
import importlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------- 단위: merge_preserve_edited ----------
def _cur_summary():
    return {
        "agenda_index": [{"no": 1, "title": "안건A"}],
        "agenda": [{
            "no": 1, "title": "안건A",
            "points": [
                {"item_id": "p1", "text": "원문 요점", "edited": False},
                {"item_id": "p2", "text": "교정한 요점", "edited": True, "original_text": "원문"},
            ],
            "decisions": [{"item_id": "d1", "text": "교정 결정", "edited": True}],
            "issues": [],
        }],
    }


def test_merge_actionitems_appends_current_edited_only():
    from src.postprocess.web_contract import merge_preserve_edited

    cur_actions = [
        {"item_id": "a1", "text": "비편집", "edited": False},
        {"item_id": "a2", "text": "편집함", "edited": True},
    ]
    regen_actions = [{"item_id": "r1", "text": "재생성 액션"}]
    _, merged = merge_preserve_edited(None, cur_actions, {}, regen_actions)
    texts = [a["text"] for a in merged]
    assert texts == ["재생성 액션", "편집함"], "재생성 + 현행 edited만, 비편집 현행 드롭"
    assert merged[1]["item_id"] == "a2", "편집 항목 item_id 유지"


def test_merge_summary_adds_preserved_block_by_category():
    from src.postprocess.web_contract import PRESERVED_BLOCK_TITLE, merge_preserve_edited

    regen_summary = {
        "agenda_index": [{"no": 1, "title": "새안건"}],
        "agenda": [{"no": 1, "title": "새안건", "points": [{"item_id": "n1", "text": "새 요점"}],
                    "decisions": [], "issues": []}],
    }
    merged, _ = merge_preserve_edited(_cur_summary(), [], regen_summary, [])
    blocks = merged["agenda"]
    assert len(blocks) == 2, "재생성 블록 + 편집 보존 블록"
    pres = blocks[-1]
    assert pres["title"] == PRESERVED_BLOCK_TITLE
    assert pres["no"] == 2, "기존 no 다음으로 부여"
    assert [p["text"] for p in pres["points"]] == ["교정한 요점"], "edited point만 보존"
    assert [d["text"] for d in pres["decisions"]] == ["교정 결정"], "edited decision 분류 유지"
    assert pres["issues"] == []
    # agenda_index 동기화.
    assert merged["agenda_index"][-1] == {"no": 2, "title": PRESERVED_BLOCK_TITLE}


def test_merge_no_edited_means_no_preserved_block():
    from src.postprocess.web_contract import merge_preserve_edited

    cur = {"agenda": [{"no": 1, "title": "A", "points": [{"item_id": "p", "text": "x"}],
                       "decisions": [], "issues": []}]}
    regen = {"agenda": [{"no": 1, "title": "새", "points": [], "decisions": [], "issues": []}]}
    merged, _ = merge_preserve_edited(cur, [], regen, [])
    assert len(merged["agenda"]) == 1, "편집 항목 없으면 보존 블록 미추가"


def test_merge_robust_to_none_and_empty():
    from src.postprocess.web_contract import merge_preserve_edited

    s, a = merge_preserve_edited(None, None, None, None)
    assert s == {} and a == []


# ---------- HTTP: apply mode=preserve_edited ----------
@contextlib.contextmanager
def _client_for(td: Path, users: str):
    from fastapi.testclient import TestClient

    tmp_db = td / "meetings.db"
    os.environ["JWT_SECRET"] = "test-secret-preserve"
    os.environ["WEB_AUTH_USERS"] = users
    os.environ["WEB_AUTH_ADMINS"] = "admin"
    os.environ["WEB_AUTH_TOKEN_TTL"] = "3600"
    os.environ["WEB_AUTH_PRUNE"] = "1"
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


def _h(auth, appmod, username: str) -> dict:
    appmod.users.set_password(username, "newpassword123")
    return {"Authorization": f"Bearer {auth.make_token(username)}"}


def test_apply_preserve_edited_keeps_current_edits():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _h(auth, appmod, "admin")
        # 현행 회의: 편집된 summary point + 편집된 actionItem.
        created = client.post("/api/meetings", json={
            "title": "회의", "status": "review",
            "summary": _cur_summary(),
            "actionItems": [
                {"item_id": "a1", "text": "비편집액션", "edited": False},
                {"item_id": "a2", "text": "편집액션", "edited": True},
            ],
        }, headers=h).json()
        mid, etag = created["id"], created["updatedAt"]
        # 재생성본(완전히 다른 내용)으로 preserve_edited 적용.
        regen = {
            "summary": {"agenda_index": [{"no": 1, "title": "새안건"}],
                        "agenda": [{"no": 1, "title": "새안건", "points": [{"item_id": "n1", "text": "새 요점"}],
                                    "decisions": [], "issues": []}]},
            "actionItems": [{"item_id": "r1", "text": "재생성액션"}],
            "mode": "preserve_edited",
        }
        r = client.post(f"/api/meetings/{mid}/regenerate/apply", json=regen,
                        headers={**h, "If-Match": f'"{etag}"'})
        assert r.status_code == 200, r.text
        body = r.json()
        # actionItems: 재생성 + 현행 편집(비편집은 드롭).
        a_texts = [a["text"] for a in body["actionItems"]]
        assert a_texts == ["재생성액션", "편집액션"]
        # summary: 재생성 블록 + 편집 보존 블록.
        titles = [b["title"] for b in body["summary"]["agenda"]]
        assert "새안건" in titles and "사용자 편집 보존" in titles
        pres = body["summary"]["agenda"][-1]
        assert [p["text"] for p in pres["points"]] == ["교정한 요점"]
        # undo 로 현행 복원(병합 전 상태).
        r2 = client.post(f"/api/meetings/{mid}/regenerate/undo", json={},
                         headers={**h, "If-Match": f'"{body["updatedAt"]}"'})
        assert r2.status_code == 200, r2.text
        assert [a["text"] for a in r2.json()["actionItems"]] == ["비편집액션", "편집액션"]


def test_apply_preserve_edited_twice_no_accumulation():
    """반복 preserve_edited 적용 — 보존 항목이 매번 1개씩만 유지(누적 중복 없음)."""
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _h(auth, appmod, "admin")
        created = client.post("/api/meetings", json={
            "title": "회의", "status": "review",
            "summary": _cur_summary(),
            "actionItems": [{"item_id": "a2", "text": "편집액션", "edited": True}],
        }, headers=h).json()
        mid = created["id"]
        regen = {
            "summary": {"agenda": [{"no": 1, "title": "새", "points": [{"item_id": "n1", "text": "새요점"}],
                                    "decisions": [], "issues": []}]},
            "actionItems": [{"item_id": "r1", "text": "재생성액션"}],
            "mode": "preserve_edited",
        }
        etag = created["updatedAt"]
        for _ in range(2):  # 두 번 연속 적용
            r = client.post(f"/api/meetings/{mid}/regenerate/apply", json=regen,
                            headers={**h, "If-Match": f'"{etag}"'})
            assert r.status_code == 200, r.text
            etag = r.json()["updatedAt"]
        body = r.json()
        # '사용자 편집 보존' 블록은 1개, 교정 요점/결정/액션도 각각 1개씩만(누적 없음).
        pres_blocks = [b for b in body["summary"]["agenda"] if b["title"] == "사용자 편집 보존"]
        assert len(pres_blocks) == 1
        assert [p["text"] for p in pres_blocks[0]["points"]] == ["교정한 요점"]
        assert [a["text"] for a in body["actionItems"]] == ["재생성액션", "편집액션"]


def test_apply_unknown_mode_400():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _h(auth, appmod, "admin")
        created = client.post("/api/meetings", json={"title": "t", "status": "review"}, headers=h).json()
        r = client.post(f"/api/meetings/{created['id']}/regenerate/apply",
                        json={"summary": {}, "actionItems": [], "mode": "bogus"},
                        headers={**h, "If-Match": f'"{created["updatedAt"]}"'})
        assert r.status_code == 400, r.text
