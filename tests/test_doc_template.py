"""전역 Docs 양식(템플릿) 회귀 테스트 — 순수함수 + 저장소 라운드트립.

검증 불변식:
  - google_docs.extract_doc_id: URL/raw id 에서 문서 id 추출, 형식 불명은 None.
  - meeting_doc.render_template_values: 지원 키 전부 존재, 요약/액션/전사 평문 생성, 빈 값은 "".
  - auth doc_template 저장/조회/삭제 라운드트립(임시 DB).

실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_doc_template.py -q
      또는 sudo PYTHONPATH=/app .venv/bin/python tests/test_doc_template.py
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from src.web import google_docs, meeting_doc


def test_extract_doc_id_from_url():
    url = "https://docs.google.com/document/d/1AbC_def-123456789012345/edit?usp=sharing"
    assert google_docs.extract_doc_id(url) == "1AbC_def-123456789012345"


def test_extract_doc_id_raw():
    raw = "1AbC_def-123456789012345678"
    assert google_docs.extract_doc_id(raw) == raw


def test_extract_doc_id_invalid():
    assert google_docs.extract_doc_id("") is None
    assert google_docs.extract_doc_id("   ") is None
    assert google_docs.extract_doc_id("https://example.com/not-a-doc") is None
    assert google_docs.extract_doc_id("short") is None  # 20자 미만 raw 는 거부


_SAMPLE = {
    "title": "주간 팀 미팅",
    "createdAt": "2026-07-20T05:30:00",
    "participants": [
        {"name": "김철수", "email": "cs@x.com"},
        {"name": "이영희"},
    ],
    "summary": {
        "meta": {"department": "연구소", "author": "김철수"},
        "agenda_index": [{"no": 1, "title": "STT 품질", "summary": "WER 개선 논의"}],
        "agenda": [
            {
                "no": 1,
                "title": "STT 품질",
                "time_range": "00:00-05:00",
                "points": [{"text": "WER 0.4 확인"}, {"text": "동음 교정 필요"}],
                "decisions": [{"text": "anchor 도입"}],
                "issues": ["장음원 collapse"],
            }
        ],
    },
    "actionItems": [
        {"text": "동음 사전 보강", "owner": "이영희", "due": "다음 주", "anchor": "03:12"},
        {"text": "벤치 재실행"},
    ],
    "transcript": [
        {"timestamp": "00:01", "speakerId": "김철수", "text": "시작합시다"},
        {"timestamp": "00:05", "speakerId": "", "text": "네"},
    ],
}


def test_render_template_values_keys_and_content():
    v = meeting_doc.render_template_values(_SAMPLE)
    # 지원 플레이스홀더 키 전부 존재
    for k in ("title", "date", "attendees", "department", "author", "summary", "action_items", "transcript"):
        assert k in v, k
    assert v["title"] == "주간 팀 미팅"
    assert v["attendees"] == "김철수, 이영희"
    assert v["department"] == "연구소"
    assert v["author"] == "김철수"
    # 요약 평문에 안건/논의/결정/이슈가 담긴다
    assert "STT 품질" in v["summary"]
    assert "WER 0.4 확인" in v["summary"]
    assert "[결정사항]" in v["summary"] and "anchor 도입" in v["summary"]
    assert "[이슈]" in v["summary"] and "장음원 collapse" in v["summary"]
    # 액션 평문에 담당/기한/시각 표기
    assert "동음 사전 보강" in v["action_items"]
    assert "담당: 이영희" in v["action_items"] and "기한: 다음 주" in v["action_items"]
    # 전사 평문에 라벨+본문
    assert "[00:01 김철수] 시작합시다" in v["transcript"]
    # date 는 KST 스탬프(2026-07-20 14:30)
    assert v["date"] == "2026-07-20 14:30"


def test_render_template_values_empty_meeting():
    v = meeting_doc.render_template_values({"title": "", "participants": [], "summary": {}, "actionItems": [], "transcript": []})
    # 빈 값은 "" — 잔여 플레이스홀더가 남지 않도록
    assert v["summary"] == "" and v["action_items"] == "" and v["transcript"] == ""
    assert v["attendees"] == "" and v["department"] == ""
    # title 폴백(doc_title) — 빈 title 이라도 문자열
    assert isinstance(v["title"], str)


def test_doc_template_store_roundtrip():
    os.environ["JWT_SECRET"] = "test-secret-doc-tmpl"
    os.environ["WEB_AUTH_USERS"] = "admin:pw1"
    import importlib

    import src.web.auth as auth
    importlib.reload(auth)
    with tempfile.TemporaryDirectory() as td:
        store = auth.init(Path(td) / "users.db")
        assert store.get_doc_template() is None
        # 저장
        store.set_doc_template("DOCID123456789012345", "https://docs.google.com/document/d/DOCID123456789012345/edit")
        t = store.get_doc_template()
        assert t is not None
        assert t["template_id"] == "DOCID123456789012345"
        assert t["template_url"].endswith("/edit")
        assert t["updated_at"]
        # 빈 id → ValueError
        try:
            store.set_doc_template("", "x")
            assert False, "ValueError 여야 함"
        except ValueError:
            pass
        # 해제
        assert store.clear_doc_template() is True
        assert store.get_doc_template() is None
        assert store.clear_doc_template() is False  # 이미 없음


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_doc_template ({len(fns)} cases)")


if __name__ == "__main__":
    _run()
