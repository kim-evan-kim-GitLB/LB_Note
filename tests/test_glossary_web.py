"""용어 사전 DB·병합·미리보기·API 회귀 테스트 (설계 5순위).

검증 불변식:
  - 층위: 파일 씨앗 < 전역(관리자) < 개인. 같은 표기면 개인이 이긴다.
  - DB 가 비어 있으면 **종전과 완전히 같다**(파일 씨앗만) — 기능 추가가 기존 회의를 바꾸면 안 된다.
  - 전역 항목은 owner 가 눕는다(UNIQUE 무력화 방지).
  - 재현성 스탬프(version)가 사전 **내용**을 반영한다 — 파일 버전만으로는 더 이상 재현 보장 불가.
  - 미리보기는 실제 치환과 **같은 규칙**을 쓴다(따로 세면 "미리보기 3건, 실제 0건"이 된다).
  - 잡 배선: 접수 시점에 병합 사전을 확정해 STT 로 넘긴다(잡 스레드엔 요청 사용자가 없다).
  - 소급 적용 안 함이 **계약 필드**로 고정된다(UI 문구가 코드와 갈라지지 않게).

실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_glossary_web.py -q
"""
from __future__ import annotations

import contextlib
import importlib
import os
import tempfile
from pathlib import Path

import pytest


def _client_for(td: Path, users: str = "admin:pw1,dev:pw2"):
    from fastapi.testclient import TestClient

    tmp_db = td / "meetings.db"
    os.environ["JWT_SECRET"] = "test-secret-glossary"
    os.environ["WEB_AUTH_USERS"] = users
    os.environ["WEB_AUTH_ADMINS"] = "admin"
    os.environ["WEB_AUTH_TOKEN_TTL"] = "3600"
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


@contextlib.contextmanager
def _tmp(users: str = "admin:pw1,dev:pw2"):
    with tempfile.TemporaryDirectory() as td:
        yield from _client_for(Path(td), users)


def _headers(auth, appmod, username: str) -> dict:
    appmod.users.set_password(username, "newpassword123")
    return {"Authorization": f"Bearer {auth.make_token(username)}"}


def _wait_job(client, job_id: str, headers: dict, timeout: float = 5.0) -> dict:
    """잡 스레드는 비동기다 — 폴링 없이 단언하면 간헐적으로 통과/실패한다."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/ai/jobs/{job_id}", headers=headers).json()
        if body.get("status") in ("done", "error", "cancelled"):
            return body
        time.sleep(0.01)
    raise AssertionError(f"잡이 {timeout}s 안에 끝나지 않았습니다: {job_id}")


# ------------------------------------------------------------------ 저장소 계층
def test_범위별_저장과_조회():
    with _tmp() as (auth, appmod, _c):
        auth.upsert_glossary_term("global", "", "채집피키", "ChatGPT", by="admin")
        auth.upsert_glossary_term("user", "dev", "콰안", "Qwen", by="dev")
        assert [t["source"] for t in auth.list_glossary_terms("global")] == ["채집피키"]
        assert [t["source"] for t in auth.list_glossary_terms("user", "dev")] == ["콰안"]
        assert auth.list_glossary_terms("user", "admin") == []      # 남의 개인 사전 미노출


def test_전역은_owner를_눕힌다():
    """owner 를 그대로 두면 같은 전역 항목이 사용자 수만큼 중복돼 UNIQUE 가 무의미해진다."""
    with _tmp() as (auth, _a, _c):
        auth.upsert_glossary_term("global", "dev", "가", "나", by="admin")
        auth.upsert_glossary_term("global", "admin", "가", "다", by="admin")
        rows = auth.list_glossary_terms("global")
        assert len(rows) == 1 and rows[0]["target"] == "다"        # 덮어쓰기 1행


def test_잘못된_범위와_빈값_거부():
    with _tmp() as (auth, _a, _c):
        with pytest.raises(ValueError):
            auth.upsert_glossary_term("team", "", "가", "나")
        with pytest.raises(ValueError):
            auth.upsert_glossary_term("user", "", "가", "나")      # 개인인데 owner 없음
        with pytest.raises(ValueError):
            auth.upsert_glossary_term("global", "", "  ", "나")


def test_앞뒤공백은_저장전에_제거된다():
    """공백 하나로 매칭이 조용히 어긋나면 원인을 못 찾는다(사전은 '무반응'으로 실패한다)."""
    with _tmp() as (auth, _a, _c):
        auth.upsert_glossary_term("global", "", "  콴  ", " Qwen ", by="admin")
        assert auth.list_glossary_terms("global")[0] == {
            "source": "콴", "target": "Qwen", "createdBy": "admin",
            "updatedAt": auth.list_glossary_terms("global")[0]["updatedAt"],
        }


# ------------------------------------------------------------------ 병합·버전
def test_DB가_비면_종전과_같다():
    from src.postprocess.glossary import load_glossary
    with _tmp() as (_auth, _a, _c):
        from src.web import glossary_service as gs
        assert gs.merged_for("dev") == load_glossary(None)


def test_개인이_전역과_씨앗을_이긴다():
    with _tmp() as (auth, _a, _c):
        from src.web import glossary_service as gs
        auth.upsert_glossary_term("global", "", "콴", "GLOBAL", by="admin")
        assert gs.merged_for("dev")["콴"] == "GLOBAL"           # 전역이 씨앗을 덮고
        auth.upsert_glossary_term("user", "dev", "콴", "MINE", by="dev")
        assert gs.merged_for("dev")["콴"] == "MINE"             # 개인이 전역을 덮는다
        assert gs.merged_for("admin")["콴"] == "GLOBAL"         # 남에게는 안 샌다


def test_버전은_사전_내용을_반영한다():
    with _tmp() as (auth, _a, _c):
        from src.web import glossary_service as gs
        v0 = gs.version_for("dev")
        auth.upsert_glossary_term("user", "dev", "새표기", "새정답", by="dev")
        v1 = gs.version_for("dev")
        assert v0 != v1
        auth.delete_glossary_term("user", "dev", "새표기")
        assert gs.version_for("dev") == v0                      # 되돌리면 같은 스탬프


def test_검증은_후보와_무관한_경고를_흘리지_않는다():
    with _tmp() as (auth, _a, _c):
        from src.web import glossary_service as gs
        auth.upsert_glossary_term("global", "", "환", "Qwen", by="admin")  # 기존 위험 항목
        w = gs.validate_candidate("dev", "user", "회의", "미팅")
        assert not any("'환'" in m for m in w)                  # 남의 문제는 안 보여준다
        assert any("'환'" in m for m in gs.validate_candidate("dev", "user", "환", "Qwen2"))


# ------------------------------------------------------------------ 미리보기
MEETINGS = [
    {"id": "m1", "title": "주간회의", "transcript": [
        {"text": "채집피키로 요약했습니다."}, {"text": "채집피키가 좋네요."}]},
    {"id": "m2", "title": "설계", "transcript": [{"text": "관련 없는 내용"}]},
]


def test_미리보기가_건수와_예시를_준다():
    from src.web import glossary_service as gs
    out = gs.preview("채집피키", "ChatGPT", MEETINGS)
    assert out["matches"] == 2 and out["meetings"] == 1 and out["scanned"] == 2
    assert out["examples"][0]["after"] == "ChatGPT로 요약했습니다."


def test_미리보기는_실제_치환규칙을_따른다():
    """짧은 키 경계 규칙이 미리보기에도 걸려야 한다 — 안 그러면 신뢰가 깨진다."""
    from src.web import glossary_service as gs
    rows = [{"id": "m", "title": "t", "transcript": [{"text": "회의록과 회의를"}]}]
    out = gs.preview("회의", "미팅", rows)
    assert out["matches"] == 1                                  # '회의록'은 안 센다


def test_미리보기_상한():
    from src.web import glossary_service as gs
    many = [{"id": str(i), "title": "", "transcript": [{"text": "콴"}]} for i in range(100)]
    out = gs.preview("콴", "Qwen", many)
    assert out["scanned"] == gs.PREVIEW_MAX_MEETINGS
    assert len(out["examples"]) <= gs.PREVIEW_MAX_EXAMPLES


# ------------------------------------------------------------------ HTTP
def test_개인항목_등록_삭제_왕복():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "dev")
        r = client.put("/api/glossary/personal", json={"source": "콰안", "target": "Qwen"},
                       headers=h)
        assert r.status_code == 200 and r.json()["ok"] is True
        assert [t["source"] for t in r.json()["personal"]] == ["콰안"]
        r = client.get("/api/glossary", headers=h)
        assert r.json()["effectiveCount"] >= 1
        r = client.delete("/api/glossary/personal", params={"source": "콰안"}, headers=h)
        assert r.json()["deleted"] is True and r.json()["personal"] == []


def test_슬래시가_든_표기도_삭제된다():
    """source 를 경로에 두면 'A/B' 가 라우팅에 걸려 405 → 그 항목을 영영 못 지운다(실측)."""
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "dev")
        client.put("/api/glossary/personal", json={"source": "A/B", "target": "AB"}, headers=h)
        r = client.delete("/api/glossary/personal", params={"source": "A/B"}, headers=h)
        assert r.status_code == 200 and r.json()["deleted"] is True
        assert r.json()["personal"] == []


def test_등록_개수_상한():
    """사전은 전사마다 하나의 정규식으로 합쳐진다 — 무제한 등록은 남의 처리 속도까지 갉는다."""
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "dev")
        for i in range(auth.GLOSSARY_MAX_TERMS):
            auth.upsert_glossary_term("user", "dev", f"낱말{i:04d}", f"T{i}", by="dev")
        r = client.put("/api/glossary/personal", json={"source": "하나더", "target": "X"}, headers=h)
        assert r.status_code == 400 and "최대" in r.json()["detail"]
        # 상한에 걸려도 **기존 항목 수정은 막히지 않는다**(그러면 사전을 고칠 방법이 없어진다).
        r = client.put("/api/glossary/personal", json={"source": "낱말0000", "target": "고침"},
                       headers=h)
        assert r.status_code == 200


def test_용어_길이_상한():
    """문장을 통째로 등록하는 오용 차단 — 사전은 토큰 단위 도구다."""
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "dev")
        r = client.put("/api/glossary/personal",
                       json={"source": "가" * (auth.GLOSSARY_MAX_LEN + 1), "target": "나"},
                       headers=h)
        assert r.status_code == 400 and "자 이하" in r.json()["detail"]


def test_소급미적용이_계약필드로_고정된다():
    """UI 문구만으로 두면 코드와 갈라진다. 사용자는 '지난 회의록도 고쳐지나?'를 반드시 묻는다."""
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "dev")
        assert client.get("/api/glossary", headers=h).json()["retroactive"] is False


def test_같은값_등록은_400():
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "dev")
        r = client.put("/api/glossary/personal", json={"source": "가", "target": "가"}, headers=h)
        assert r.status_code == 400


def test_경고는_반환하되_저장을_막지_않는다():
    """단일 패스 치환 이후엔 연쇄·왕복 파손이 없다 → 경고는 확인용이지 차단 사유가 아니다."""
    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "dev")
        r = client.put("/api/glossary/personal", json={"source": "환", "target": "Qwen"},
                       headers=h)
        assert r.status_code == 200
        assert any("한 글자" in w for w in r.json()["warnings"])


def test_전역항목은_관리자만():
    with _tmp() as (auth, appmod, client):
        body = {"source": "채집피키", "target": "ChatGPT"}
        assert client.put("/api/admin/glossary", json=body,
                          headers=_headers(auth, appmod, "dev")).status_code == 403
        r = client.put("/api/admin/glossary", json=body, headers=_headers(auth, appmod, "admin"))
        assert r.status_code == 200 and [t["source"] for t in r.json()["global"]] == ["채집피키"]


def test_전역항목은_다른_사용자에게도_보인다():
    with _tmp() as (auth, appmod, client):
        client.put("/api/admin/glossary", json={"source": "채집피키", "target": "ChatGPT"},
                   headers=_headers(auth, appmod, "admin"))
        payload = client.get("/api/glossary", headers=_headers(auth, appmod, "dev")).json()
        assert [t["source"] for t in payload["global"]] == ["채집피키"]
        assert payload["personal"] == []
        assert payload["isAdmin"] is False


def test_개인항목이_전역을_덮으면_표시된다():
    with _tmp() as (auth, appmod, client):
        client.put("/api/admin/glossary", json={"source": "콰안", "target": "GLOBAL"},
                   headers=_headers(auth, appmod, "admin"))
        h = _headers(auth, appmod, "dev")
        client.put("/api/glossary/personal", json={"source": "콰안", "target": "MINE"}, headers=h)
        payload = client.get("/api/glossary", headers=h).json()
        assert payload["global"][0]["overriddenByPersonal"] is True
        assert payload["personal"][0]["overridesGlobal"] is True


def test_미인증_차단():
    with _tmp() as (_auth, _appmod, client):
        assert client.get("/api/glossary").status_code == 401


# ------------------------------------------------------------------ 잡 배선
def test_접수시점에_병합사전이_STT로_넘어간다(monkeypatch):
    """잡 스레드엔 요청 사용자가 없다 → 접수 스레드가 확정해 넘겨야 한다.
    처리 중 사용자가 사전을 고쳐도 그 회의 결과가 흔들리면 안 된다."""
    import base64

    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "dev")
        client.put("/api/glossary/personal", json={"source": "콰안", "target": "Qwen"}, headers=h)
        seen: dict = {}

        def _fake_transcribe(audio_bytes, **kw):
            seen["glossary"] = kw.get("glossary")
            return [], None, {}

        monkeypatch.setattr(appmod, "transcribe_to_segments", _fake_transcribe)
        monkeypatch.setattr(appmod, "enrich_to_contract",
                            lambda *a, **k: {"summary": {}, "actionItems": [], "transcript": []})
        r = client.post("/api/ai/process",
                        json={"audioBase64": base64.b64encode(b"x").decode(), "mimeType": "audio/m4a"},
                        headers=h)
        assert r.status_code == 200
        _wait_job(client, r.json()["jobId"], h)
        assert seen["glossary"] is not None
        assert seen["glossary"]["콰안"] == "Qwen"


def test_사전조회_실패해도_전사는_진행된다(monkeypatch):
    """교정은 부가 기능이다 — 사전 때문에 회의가 통째로 죽으면 안 된다."""
    import base64

    with _tmp() as (auth, appmod, client):
        h = _headers(auth, appmod, "dev")
        seen: dict = {"called": False}

        def _boom(_username):
            raise RuntimeError("DB 손상")

        def _fake_transcribe(audio_bytes, **kw):
            seen["called"] = True
            seen["glossary"] = kw.get("glossary")
            return [], None, {}

        monkeypatch.setattr(appmod.glossary_service, "merged_for", _boom)
        monkeypatch.setattr(appmod, "transcribe_to_segments", _fake_transcribe)
        monkeypatch.setattr(appmod, "enrich_to_contract",
                            lambda *a, **k: {"summary": {}, "actionItems": [], "transcript": []})
        r = client.post("/api/ai/process",
                        json={"audioBase64": base64.b64encode(b"x").decode(), "mimeType": "audio/m4a"},
                        headers=h)
        assert r.status_code == 200
        _wait_job(client, r.json()["jobId"], h)
        assert seen["called"] is True and seen["glossary"] is None   # 파일 씨앗으로 폴백
