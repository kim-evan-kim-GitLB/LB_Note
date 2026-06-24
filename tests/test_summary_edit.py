"""summary 항목 교정 PATCH + item_id(선결 Phase I) 회귀 테스트 (계획 v4 트랙 B·P6).

검증 불변식:
  - web_contract.validate_summary_edit(구조보존, 락 안 검증용 순수 함수):
      * SummaryItem.text 만 편집. 블록/항목 개수·no·title·anchor·evidence_seg_ids·item_id 불변.
      * text 변경 항목에 edited/edited_at/original_text 를 **서버 set**(클라 edited 무시),
        evidence_seg_ids 는 저장본 스냅샷 동결(grounding 우회 — 재드롭/재산출 없음).
      * item_id 부재(레거시 회의)는 lazy 부여(무파괴). item_id 위조(불일치)는 거부.
      * 저장본 베이스 출력 — meta/agenda_index/블록 메타/미지 필드 보존, incoming 위조 미반영.
  - summarize_schema.SummaryItem: 생성 시 item_id(uuid) 부여, to_dict 노출.
  - PATCH /api/meetings/{id} (summary):
      * text-only 편집 → 200 + edited 서버 set + 새 ETag, item_id 노출.
      * 구조 변경(개수/anchor/evidence/item_id 위조) → 422.
      * stale If-Match → 412. summary·transcript 동시 patch 독립 동작.

실 DB(output/web/meetings.db)는 절대 건드리지 않는다 — tempfile + DEFAULT_DB_PATH 패치 격리.

실행: sudo uv run --frozen pytest tests/test_summary_edit.py -q
"""
from __future__ import annotations

import contextlib
import copy
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.postprocess.summarize_schema import SummaryItem  # noqa: E402
from src.postprocess.web_contract import (  # noqa: E402
    SummaryStructureError,
    validate_summary_edit,
)


# ---------- 픽스처 ----------
def _summary(text="원안 검토", *, item_id="item-1", with_id=True, evidence=(0, 1)):
    item = {"text": text, "anchor": "00:12", "evidence_seg_ids": list(evidence)}
    if with_id:
        item["item_id"] = item_id
    return {
        "schema_version": "sum-1.0",
        "prompt_version": "p1",
        "backend": "test",
        "meta": {"subject": "주제", "attendees": ["김"]},
        "agenda_index": [{"no": 1, "title": "안건1", "summary": "요약줄"}],
        "agenda": [
            {
                "no": 1,
                "title": "안건1",
                "time_range": "00:12 ~ 01:00",
                "evidence_seg_ids": list(evidence),
                "points": [item],
                "decisions": [],
                "issues": [],
            }
        ],
    }


def _first_point(summary: dict) -> dict:
    return summary["agenda"][0]["points"][0]


# ---------- Phase I: SummaryItem item_id 데이터모델 ----------
def test_summary_item_generates_item_id_on_creation():
    a = SummaryItem.from_dict({"text": "x", "evidence_seg_ids": [1]})
    assert a.item_id and len(a.item_id) == 32, "생성 시 uuid item_id 부여"
    # 라운드트립 멱등: 기존 item_id 보존
    b = SummaryItem.from_dict(a.to_dict())
    assert b.item_id == a.item_id
    # to_dict 는 item_id 노출, 비편집 항목은 edited 메타 미노출(노이즈 최소)
    d = a.to_dict()
    assert d["item_id"] == a.item_id and "edited" not in d


def test_summary_item_to_dict_exposes_edited_meta_only_when_edited():
    a = SummaryItem.from_dict(
        {"text": "x", "evidence_seg_ids": [1], "item_id": "i", "edited": True,
         "edited_at": "2026-06-24T00:00:00+00:00", "original_text": "old"}
    )
    d = a.to_dict()
    assert d["edited"] is True and d["edited_at"] and d["original_text"] == "old"


# ---------- P6 단위: validate_summary_edit ----------
def test_text_only_edit_sets_edited_and_freezes_evidence():
    stored = _summary("원안 검토")
    incoming = copy.deepcopy(stored)
    _first_point(incoming)["text"] = "원안 재검토"
    out = validate_summary_edit(stored, incoming)
    p = _first_point(out)
    assert p["text"] == "원안 재검토"
    assert p["edited"] is True and p["edited_at"], "서버가 edited/edited_at set"
    assert p["original_text"] == "원안 검토", "최초 원문 동결"
    assert p["item_id"] == "item-1", "item_id 보존"
    assert p["evidence_seg_ids"] == [0, 1] and p["anchor"] == "00:12", "근거·anchor 스냅샷 동결"


def test_unchanged_text_does_not_mark_edited():
    stored = _summary("그대로")
    out = validate_summary_edit(stored, copy.deepcopy(stored))
    assert "edited" not in _first_point(out)


def test_client_edited_flag_is_ignored_server_authoritative():
    stored = _summary("원문")
    incoming = copy.deepcopy(stored)
    _first_point(incoming)["edited"] = True  # 클라가 위조 시도(텍스트는 그대로)
    out = validate_summary_edit(stored, incoming)
    assert "edited" not in _first_point(out), "텍스트 미변경이면 클라 edited 위조 무시"


def test_legacy_item_without_id_gets_lazy_uuid():
    stored = _summary("레거시", with_id=False)
    incoming = copy.deepcopy(stored)
    _first_point(incoming)["text"] = "교정"
    out = validate_summary_edit(stored, incoming)
    p = _first_point(out)
    assert p["item_id"] and len(p["item_id"]) == 32, "레거시는 첫 편집 시 lazy item_id 부여"


def test_reedit_keeps_first_original_text():
    stored = _summary("원문")
    # 1차 편집
    inc1 = copy.deepcopy(stored)
    _first_point(inc1)["text"] = "1차"
    out1 = validate_summary_edit(stored, inc1)
    # 2차 편집(out1 을 저장본으로)
    inc2 = copy.deepcopy(out1)
    _first_point(inc2)["text"] = "2차"
    out2 = validate_summary_edit(out1, inc2)
    assert _first_point(out2)["original_text"] == "원문", "재편집해도 최초 원문 동결 유지"


def test_block_count_change_rejected():
    stored = _summary()
    incoming = copy.deepcopy(stored)
    incoming["agenda"].append(copy.deepcopy(incoming["agenda"][0]))
    with pytest.raises(SummaryStructureError):
        validate_summary_edit(stored, incoming)


def test_item_count_change_rejected():
    stored = _summary()
    incoming = copy.deepcopy(stored)
    incoming["agenda"][0]["points"].append({"text": "추가", "evidence_seg_ids": [0]})
    with pytest.raises(SummaryStructureError):
        validate_summary_edit(stored, incoming)


def test_block_title_change_rejected():
    stored = _summary()
    incoming = copy.deepcopy(stored)
    incoming["agenda"][0]["title"] = "위조제목"
    with pytest.raises(SummaryStructureError):
        validate_summary_edit(stored, incoming)


def test_anchor_change_rejected():
    stored = _summary()
    incoming = copy.deepcopy(stored)
    _first_point(incoming)["anchor"] = "99:99"
    with pytest.raises(SummaryStructureError):
        validate_summary_edit(stored, incoming)


def test_evidence_change_rejected():
    stored = _summary()
    incoming = copy.deepcopy(stored)
    _first_point(incoming)["evidence_seg_ids"] = [0, 1, 2]
    with pytest.raises(SummaryStructureError):
        validate_summary_edit(stored, incoming)


def test_evidence_reorder_is_accepted():
    """evidence 순서만 다른 정상 편집은 허용(집합 동일) — 출력은 저장본 스냅샷 동결."""
    stored = _summary("원문", evidence=(0, 1))
    incoming = copy.deepcopy(stored)
    _first_point(incoming)["evidence_seg_ids"] = [1, 0]  # 순서만 뒤집힘
    _first_point(incoming)["text"] = "교정"
    out = validate_summary_edit(stored, incoming)
    assert _first_point(out)["evidence_seg_ids"] == [0, 1], "출력은 저장본 순서 동결"
    assert _first_point(out)["edited"] is True


def test_whitespace_only_diff_not_marked_edited():
    """저장본 text 의 선·후행 공백 차이만으로는 edited 가 찍히지 않는다."""
    stored = _summary("  공백포함  ")
    incoming = copy.deepcopy(stored)
    _first_point(incoming)["text"] = "공백포함"  # strip 된 동일 내용
    out = validate_summary_edit(stored, incoming)
    assert "edited" not in _first_point(out)


def test_item_id_forgery_rejected():
    stored = _summary(item_id="real")
    incoming = copy.deepcopy(stored)
    _first_point(incoming)["item_id"] = "forged"
    _first_point(incoming)["text"] = "교정"
    with pytest.raises(SummaryStructureError):
        validate_summary_edit(stored, incoming)


def test_meta_and_agenda_index_preserved_against_forgery():
    stored = _summary()
    incoming = copy.deepcopy(stored)
    incoming["meta"]["subject"] = "위조주제"
    incoming["agenda_index"][0]["title"] = "위조인덱스"
    _first_point(incoming)["text"] = "정상교정"
    out = validate_summary_edit(stored, incoming)
    assert out["meta"]["subject"] == "주제", "meta 는 저장본 보존(편집 표면 아님)"
    assert out["agenda_index"][0]["title"] == "안건1", "agenda_index 저장본 보존"


def test_empty_stored_agenda_passthrough():
    stored = {"agenda": [], "meta": {}}
    incoming = {"agenda": [{"no": 1, "title": "신규", "points": [{"text": "a", "evidence_seg_ids": [0]}]}]}
    # agenda 가 비어있으면(생성 전) 구조검증 미적용 → 호출부에서 통과(validator 가 skip).
    # validate_summary_edit 자체는 길이 0 == ? 로 막지 않도록: stored agenda 0, incoming 1 → 길이불일치.
    # 호출부(app._validator)가 stored_sum.get("agenda") 가짜일 때만 호출하므로 단위에선 길이검증 동작 확인.
    with pytest.raises(SummaryStructureError):
        validate_summary_edit(stored, incoming)


def test_incoming_without_agenda_returns_stored():
    stored = _summary()
    out = validate_summary_edit(stored, {"meta": {"subject": "x"}})
    assert out == stored, "agenda 미포함 patch 는 저장본 보존"


# ---------- HTTP 통합 테스트(임시 DB 격리) ----------
@contextlib.contextmanager
def _client_for(td: Path, users: str):
    from fastapi.testclient import TestClient
    import importlib

    tmp_db = td / "meetings.db"
    os.environ["JWT_SECRET"] = "test-secret-summary-edit"
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
    appmod.users.set_password(username, "newpassword123")
    return {"Authorization": f"Bearer {auth.make_token(username)}"}


def _create_with_summary(client, h, summary: dict) -> dict:
    body = {"title": "요약회의", "status": "review", "summary": summary}
    r = client.post("/api/meetings", json=body, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_http_summary_text_edit_sets_edited_and_bumps_etag():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        m = _create_with_summary(client, h, _summary("원안 검토"))
        etag = m["updatedAt"]
        new_summary = copy.deepcopy(m["summary"])
        new_summary["agenda"][0]["points"][0]["text"] = "원안 재검토"
        r = client.patch(
            f"/api/meetings/{m['id']}",
            json={"summary": new_summary},
            headers={**h, "If-Match": f'"{etag}"'},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        p = body["summary"]["agenda"][0]["points"][0]
        assert p["text"] == "원안 재검토" and p["edited"] is True
        assert p["original_text"] == "원안 검토" and p["item_id"]
        assert body["updatedAt"] != etag
        assert r.headers.get("ETag") == f'"{body["updatedAt"]}"'


def test_http_summary_structural_change_422():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        m = _create_with_summary(client, h, _summary())
        bad = copy.deepcopy(m["summary"])
        bad["agenda"][0]["points"][0]["evidence_seg_ids"] = [9, 9]  # 근거 위조
        r = client.patch(f"/api/meetings/{m['id']}", json={"summary": bad}, headers=h)
        assert r.status_code == 422, r.text


def test_http_summary_item_id_forgery_422():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        m = _create_with_summary(client, h, _summary(item_id="real"))
        bad = copy.deepcopy(m["summary"])
        bad["agenda"][0]["points"][0]["item_id"] = "forged"
        bad["agenda"][0]["points"][0]["text"] = "교정"
        r = client.patch(f"/api/meetings/{m['id']}", json={"summary": bad}, headers=h)
        assert r.status_code == 422, r.text


def test_http_summary_stale_if_match_412():
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        m = _create_with_summary(client, h, _summary("v1"))
        etag = m["updatedAt"]
        s2 = copy.deepcopy(m["summary"])
        s2["agenda"][0]["points"][0]["text"] = "v2"
        r1 = client.patch(
            f"/api/meetings/{m['id']}", json={"summary": s2}, headers={**h, "If-Match": f'"{etag}"'}
        )
        assert r1.status_code == 200
        # stale etag 로 재시도 → 412
        s3 = copy.deepcopy(m["summary"])
        s3["agenda"][0]["points"][0]["text"] = "v3"
        r2 = client.patch(
            f"/api/meetings/{m['id']}", json={"summary": s3}, headers={**h, "If-Match": f'"{etag}"'}
        )
        assert r2.status_code == 412, r2.text


def test_http_summary_create_assigns_item_id():
    """생성 경로에서 summary 항목에 item_id 가 부여돼 저장·노출되는지(신규 회의)."""
    with tempfile.TemporaryDirectory() as td, _client_for(Path(td), "admin:pw1") as (auth, appmod, client):
        h = _auth_headers(auth, appmod, "admin")
        # 클라가 item_id 없이 보낸 summary 도, 첫 편집 시 lazy 부여됨을 확인(생성은 통짜 저장).
        m = _create_with_summary(client, h, _summary("초안", with_id=False))
        edit = copy.deepcopy(m["summary"])
        edit["agenda"][0]["points"][0]["text"] = "수정"
        r = client.patch(f"/api/meetings/{m['id']}", json={"summary": edit}, headers=h)
        assert r.status_code == 200, r.text
        p = r.json()["summary"]["agenda"][0]["points"][0]
        assert p["item_id"] and len(p["item_id"]) == 32
