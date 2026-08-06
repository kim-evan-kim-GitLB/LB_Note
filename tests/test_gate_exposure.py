"""언어 게이트 판정 노출 회귀 테스트 (설계 docs/2026-08-05-회의록-품질-개선-설계.md 4순위).

배경: 게이트가 무엇을 요약 근거에서 뺐는지는 감사로그에만 남고 사용자에게 도달하지 않았다.
DB 에도 없어 새로고침하면 사라졌다. "왜 이 구간이 요약에 없나"에 답할 수 없었다.

검증 불변식:
  - **제외는 삭제가 아니다** — transcript 본문은 그대로다. 계약은 개수·구간만 주고 문구는 고정.
  - 표시 임계(notable)는 **서버가 계산**한다. 프론트에 흩어지면 게이트를 조정해도 화면이 안 따라온다
    (적응형 재디코딩으로 제외가 12→0 이 된 것처럼 임계는 앞으로도 움직인다).
  - 저신뢰(lowConf)는 알리지 않는다 — 요약에 쓰이고 있고, 숫자만 보이면 근거 없는 불안이 된다.
  - 재요약이 게이트를 다시 돌면 안내도 같이 갱신·되돌림된다(요약과 설명이 어긋나면 안 된다).
  - 레거시(core off) 경로도 같은 모양의 빈 요약을 실어 계약을 일정하게 유지한다.

실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_gate_exposure.py -q
"""
from __future__ import annotations

import contextlib
import importlib
import os
import tempfile
from pathlib import Path

from src.postprocess.web_contract import GATE_SUMMARY_MAX_ITEMS, build_gate_summary

SEGMENTS = [{"id": i, "start": i * 30.0, "end": i * 30.0 + 29.0, "text": "내용"} for i in range(6)]
CORE_META = {"gate": {
    "excluded": {"1": "non_korean", "4": "speech_rate_burst"},
    "lowConf": {"2": "mostly_non_korean"},
}}


# ------------------------------------------------------------------ 요약 구조
def test_제외구간을_사유와_시각으로_알린다():
    g = build_gate_summary(CORE_META, SEGMENTS)
    assert g["excludedCount"] == 2 and g["lowConfCount"] == 1
    assert g["totalSegments"] == 6
    assert [e["segmentId"] for e in g["excluded"]] == [1, 4]      # id 오름차순(화면 순서 고정)
    assert g["excluded"][0]["timestamp"] == "00:30"               # 점프 링크용
    assert g["excluded"][0]["label"] == "한국어가 아닌 문장"       # 코드가 아니라 한글 문구


def test_사유별_집계는_많은_순():
    meta = {"gate": {"excluded": {"1": "non_korean", "2": "non_korean", "3": "boilerplate"}}}
    reasons = build_gate_summary(meta, SEGMENTS)["reasons"]
    assert [r["reason"] for r in reasons] == ["non_korean", "boilerplate"]
    assert reasons[0]["count"] == 2


def test_모든_사유코드에_한글문구가_있다():
    """사유를 추가하고 문구를 빠뜨리면 화면에 영문 코드가 그대로 뜬다."""
    from src.postprocess import language_gate as lg
    codes = [lg.REASON_NON_KOREAN, lg.REASON_MOSTLY_NON_KO,
             lg.REASON_SPEECH_RATE, lg.REASON_BOILERPLATE]
    assert [c for c in codes if lg.reason_label(c) == c] == []


def test_제외구간은_항상_본문에_남는다():
    """점프 링크가 죽지 않는 불변식 — transcript 는 빈 텍스트만 버리고, 게이트는 빈 텍스트를
    제외하지 않는다. 둘 중 하나라도 바뀌면 "눌러도 아무 일 없는 링크"가 생긴다."""
    from src.postprocess import language_gate as lg
    from src.postprocess.web_contract import _transcript_from_segments
    for text in ("", "   ", "네."):
        assert lg.classify(text, 0.0, 30.0)[0] != lg.EXCLUDE
    boiler = [{"id": 0, "start": 0.0, "end": 5.0, "text": "Thank you for watching"}]
    assert lg.classify(boiler[0]["text"], 0.0, 5.0)[0] == lg.EXCLUDE
    assert len(_transcript_from_segments(boiler)) == 1     # 제외돼도 본문엔 남는다


def test_모르는_사유코드는_그대로_보여준다():
    """사유를 추가하고 문구를 빠뜨렸을 때 조용히 감추면 원인을 못 찾는다."""
    g = build_gate_summary({"gate": {"excluded": {"1": "새사유"}}}, SEGMENTS)
    assert g["excluded"][0]["label"] == "새사유"


def test_목록은_상한에서_잘리되_개수는_정확하다():
    """전체가 영어인 회의에서 응답이 비대해지지 않게. 잘렸다는 사실은 숨기지 않는다."""
    n = GATE_SUMMARY_MAX_ITEMS + 10
    meta = {"gate": {"excluded": {str(i): "non_korean" for i in range(n)}}}
    g = build_gate_summary(meta, SEGMENTS)
    assert g["excludedCount"] == n                       # 개수는 전체
    assert len(g["excluded"]) == GATE_SUMMARY_MAX_ITEMS  # 목록만 잘림
    assert g["truncated"] is True


def test_시각을_모르는_구간도_빠지지_않는다():
    """segment 목록에 없는 id(재요약 경로 등) → timestamp 만 생략하고 항목은 남긴다."""
    g = build_gate_summary({"gate": {"excluded": {"99": "non_korean"}}}, SEGMENTS)
    assert g["excluded"][0]["segmentId"] == 99 and "timestamp" not in g["excluded"][0]


# ------------------------------------------------------------------ 표시 임계
def test_제외가_없으면_알리지_않는다():
    """적응형 재디코딩 이후 대부분의 회의가 여기 해당한다 — 0건 배지는 불안만 만든다."""
    g = build_gate_summary({"gate": {"excluded": {}, "lowConf": {"1": "x", "2": "x"}}}, SEGMENTS)
    assert g["notable"] is False
    assert g["lowConfCount"] == 2        # 값은 주되 알리지는 않는다


def test_임계_미만이면_알리지_않는다():
    assert build_gate_summary(CORE_META, SEGMENTS, min_excluded=3)["notable"] is False
    assert build_gate_summary(CORE_META, SEGMENTS, min_excluded=2)["notable"] is True


def test_임계_0은_안내끄기():
    """운영에서 소음이 되면 잠글 수 있어야 한다."""
    assert build_gate_summary(CORE_META, SEGMENTS, min_excluded=0)["notable"] is False


def test_core_메타가_없어도_같은_모양():
    """레거시(core off)·실패 경로에서 필드를 빼면 프론트가 '구버전'과 '0건'을 구분 못 한다."""
    g = build_gate_summary(None, [])
    assert g["excludedCount"] == 0 and g["notable"] is False
    assert set(g) == {"excludedCount", "lowConfCount", "totalSegments", "reasons",
                      "excluded", "truncated", "notable"}


# ------------------------------------------------------------------ 계약 배선
def test_레거시_경로도_gateSummary를_싣는다(monkeypatch):
    from src.web import service
    monkeypatch.setattr(service.config, "CORE_ENABLED", False)
    out = service.enrich_to_contract(SEGMENTS, 100.0)
    assert out["gateSummary"]["notable"] is False


def test_core_경로가_게이트_판정을_계약에_싣는다(monkeypatch):
    from src.web import service
    monkeypatch.setattr(service.config, "CORE_ENABLED", True)
    monkeypatch.setattr(
        service, "run_meeting_core",
        lambda *a, **k: {"summary": None, "actionItems": [], "coreMeta": CORE_META},
    )
    out = service.enrich_to_contract(SEGMENTS, 100.0)
    assert out["gateSummary"]["excludedCount"] == 2
    assert out["gateSummary"]["notable"] is True
    # 제외돼도 transcript 본문에는 그대로 남는다 — 이게 깨지면 문구가 거짓말이 된다.
    assert len(out["transcript"]) == len(SEGMENTS)
    assert 1 in [e["segmentId"] for e in out["transcript"]]


# ------------------------------------------------------------------ 저장·재요약
@contextlib.contextmanager
def _store():
    with tempfile.TemporaryDirectory() as td:
        os.environ["MEETSCRIPT_BLOCK_DEFAULT_DB"] = "1"
        import src.web.store as storemod
        importlib.reload(storemod)
        yield storemod.MeetingStore(Path(td) / "m.db")


def _meeting() -> dict:
    return {"id": "m1", "ownerId": "dev", "title": "t", "summary": {"agenda": []},
            "actionItems": [], "transcript": [], "gateSummary": {"excludedCount": 2}}


def test_재요약_확정이_안내도_갱신한다():
    """갱신하지 않으면 새 요약 옆에 옛 판정이 남아 어긋난 설명을 하게 된다."""
    with _store() as st:
        st.create(_meeting())
        st.apply_regenerate("m1", {"agenda": ["새"]}, [], None,
                            gate_summary={"excludedCount": 0, "notable": False})
        assert st.get("m1")["gateSummary"]["excludedCount"] == 0


def test_undo가_요약과_안내를_함께_되돌린다():
    with _store() as st:
        st.create(_meeting())
        st.apply_regenerate("m1", {"agenda": ["새"]}, [], None,
                            gate_summary={"excludedCount": 0})
        restored, ok = st.restore_latest_backup("m1", None)
        assert ok is True
        assert restored["gateSummary"]["excludedCount"] == 2      # 원래 안내로 복귀


def test_안내_미포함_재요약은_현행을_유지한다():
    """구 클라이언트가 gateSummary 없이 확정해도 기존 안내를 지우지 않는다."""
    with _store() as st:
        st.create(_meeting())
        st.apply_regenerate("m1", {"agenda": ["새"]}, [], None)
        assert st.get("m1")["gateSummary"]["excludedCount"] == 2


def test_구백업_복원은_안내를_지우지_않는다():
    """gateSummary 도입 전 백업에는 이 키가 없다 — 없는 값으로 덮어 안내를 날리면 안 된다."""
    with _store() as st:
        st.create(_meeting())
        st.apply_regenerate("m1", {"agenda": ["새"]}, [], None,
                            gate_summary={"excludedCount": 0})
        # 백업 행을 구 형식(gateSummary 키 없음)으로 바꿔치기
        import json
        with st._lock:
            row = st._conn.execute(
                "SELECT id, data FROM meeting_backup ORDER BY id DESC LIMIT 1").fetchone()
            snap = json.loads(row["data"])
            snap.pop("gateSummary", None)
            st._conn.execute("UPDATE meeting_backup SET data=? WHERE id=?",
                             (json.dumps(snap, ensure_ascii=False), row["id"]))
            st._conn.commit()
        restored, ok = st.restore_latest_backup("m1", None)
        assert ok is True and restored["gateSummary"]["excludedCount"] == 0   # 현행 유지
