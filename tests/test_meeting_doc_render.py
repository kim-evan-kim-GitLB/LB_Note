"""회의록 HTML 렌더러(meeting_doc) 단위 테스트 — Drive Docs 변환 소스.

검증 불변식:
  - summary.agenda / actionItems / transcript / participants 가 HTML 에 반영된다.
  - actionItems 의 owner/due/anchor 메타가 표기된다.
  - decisions/issues 가 문자열이든 {text:...} dict 든 모두 렌더된다(방어적).
  - HTML 특수문자는 이스케이프된다(인젝션/깨짐 방지).
  - 빈 summary/transcript 는 graceful(섹션 생략, 예외 없음).
  - max_transcript_segments 로 transcript 를 자르고 안내 문구를 넣는다.
  - doc_title 폴백(title 없으면 createdAt 기반).

실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_meeting_doc_render.py
"""
from __future__ import annotations

from src.web import meeting_doc

_MEETING = {
    "title": "6월 기획 회의",
    "createdAt": "2026-06-30T01:00:00.000000+00:00",
    "participants": [{"name": "김윤희"}, "박대표"],
    "summary": {
        "agenda": [
            {
                "no": 1,
                "title": "출시 일정",
                "time_range": "12:00 ~ 15:30",
                "points": [
                    {"text": "베타 피드백 반영 후 배포 일정을 확정했다", "anchor": "12:10"},
                    {"text": "QA 범위를 핵심 플로우로 좁히기로 논의", "anchor": "13:05"},
                ],
                "decisions": [{"text": "7월 2주차 배포"}, "QA 우선"],
                "issues": ["도메인 확보 필요"],
            }
        ]
    },
    "actionItems": [
        {"text": "도메인 신청", "owner": "김윤희", "due": "7/8", "anchor": "12:30", "item_id": "x1"},
        {"text": "메모만", "item_id": "x2"},
    ],
    "transcript": [
        {"segmentId": 0, "timestamp": "00:01", "speakerId": "화자1", "text": "안녕하세요"},
        {"segmentId": 1, "timestamp": "00:05", "speakerId": "화자2", "text": "시작합시다 <b>"},
    ],
}


def test_render_contains_all_sections():
    html = meeting_doc.render_meeting_html(_MEETING)
    # 제목 뒤에 KST 날짜·시간 스탬프(UTC 01:00 → KST 10:00)
    assert "<h1>6월 기획 회의 (2026-06-30 10:00)</h1>" in html
    assert "요약" in html and "출시 일정" in html
    assert "12:00 ~ 15:30" in html  # 안건 time_range 가 제목에 표기
    # points(논의 본문)가 실제로 렌더된다 — 이게 빠지면 안건 제목만 남는 회귀
    assert "베타 피드백 반영 후 배포 일정을 확정했다" in html
    assert "QA 범위를 핵심 플로우로 좁히기로 논의" in html
    assert "12:10" in html  # point anchor 시각
    assert "7월 2주차 배포" in html and "QA 우선" in html  # dict·str 둘 다
    assert "도메인 확보 필요" in html
    assert "액션 아이템" in html and "도메인 신청" in html
    assert "담당: 김윤희" in html and "기한: 7/8" in html and "시각: 12:30" in html
    assert "김윤희" in html and "박대표" in html  # 참석자
    assert "안녕하세요" in html and "[00:01 화자1]" in html


def test_html_escaped():
    html = meeting_doc.render_meeting_html(_MEETING)
    # transcript 의 '<b>' 는 이스케이프되어 실제 태그로 새지 않는다
    assert "&lt;b&gt;" in html
    assert "시작합시다 <b>" not in html


def test_empty_summary_and_transcript_graceful():
    m = {"title": "빈 회의", "summary": {}, "actionItems": [], "transcript": []}
    html = meeting_doc.render_meeting_html(m)
    assert "<h1>빈 회의</h1>" in html
    # 섹션은 생략되지만 예외 없이 문서가 만들어진다
    assert "요약" not in html and "액션 아이템" not in html


def test_max_transcript_segments_truncates():
    m = {
        "title": "긴 회의",
        "transcript": [{"segmentId": i, "text": f"seg{i}", "timestamp": "00:00"} for i in range(5)],
    }
    html = meeting_doc.render_meeting_html(m, max_transcript_segments=2)
    assert "seg0" in html and "seg1" in html
    assert "seg2" not in html
    assert "일부만 표시" in html


def test_doc_title_stamp_and_fallback():
    # title + createdAt → 뒤에 KST 날짜·시간 스탬프(UTC 01:00 → KST 10:00)
    assert (
        meeting_doc.doc_title({"title": "제목", "createdAt": "2026-06-30T01:00:00+00:00"})
        == "제목 (2026-06-30 10:00)"
    )
    # createdAt 없으면 스탬프 없이 title 그대로
    assert meeting_doc.doc_title({"title": "제목"}) == "제목"
    # title 없으면 생성일 기반 폴백(스탬프 미적용)
    assert meeting_doc.doc_title({"createdAt": "2026-06-30"}) == "회의록 2026-06-30"
    assert meeting_doc.doc_title({}) == "회의록"


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_meeting_doc_render ({len(fns)} cases)")


if __name__ == "__main__":
    _run()
