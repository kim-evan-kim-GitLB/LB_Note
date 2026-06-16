"""요약 스테이지 회귀 테스트 — SummarizeStage + ground_summary + service.summarize_meeting.

설계 docs/2026-06-09-summarize-stage-design.md. 잠그는 불변식:
  - anchor/time_range 는 LLM 출력을 무시하고 evidence_seg_ids 로 결정적 산출(§7).
  - 근거(evidence) 없는 요약 항목은 드롭(그라운딩 필수 = 환각 차단).
  - 존재하지 않는 seg_id 인용은 제거, 그 결과 근거가 비면 항목·블록 드롭.
  - passthrough/비-JSON 백엔드 → 빈 요약(MeetingSummary.empty()).
  - 하이브리드: points/decisions/issues 분리 보존.
가짜 백엔드라 클라우드 호출 없음.

실행: sudo .venv/bin/python tests/test_summarize.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.postprocess.backends.base import LLMBackend, LLMCapabilities  # noqa: E402
from src.postprocess.backends.passthrough import PassthroughBackend  # noqa: E402
from src.postprocess.stages.summarize import SummarizeStage, parse_summarize_output  # noqa: E402
from src.postprocess.summarize_schema import MeetingSummary, ground_summary  # noqa: E402
from src.web import service  # noqa: E402

SEGS = [
    {"id": 0, "start": 12.0, "end": 30.0, "text": "3D SVM 첫 적용 사례다"},
    {"id": 1, "start": 95.0, "end": 130.0, "text": "애니메이션 효과 추가 필요"},
    {"id": 2, "start": 370.0, "end": 400.0, "text": "심플 구성으로 진행 결정"},
    {"id": 3, "start": 1961.0, "end": 1980.0, "text": "DVR 이슈 우려가 큼"},
]

# LLM 출력(가짜): anchor/time_range 는 일부러 틀린 값/누락 → ground 가 덮어써야 함.
SUM_OUT = {
    "meta": {"subject": "EYEL-S3000ABR Demo시연"},
    "agenda_index": [
        {"no": 1, "title": "3D SVM 뷰 검토", "summary": "11개 뷰, 애니메이션 추가"},
        {"no": 2, "title": "DVR 이슈", "summary": "우려 공유"},
    ],
    "agenda": [
        {
            "no": 1,
            "title": "3D SVM 뷰 검토",
            "time_range": "99:99 ~ 99:99",  # 틀린 값 → 덮어써야
            "points": [
                {"text": "3D SVM 첫 적용 사례.", "evidence_seg_ids": [0]},
                {"text": "애니메이션 효과 추가 필요.", "evidence_seg_ids": [1], "anchor": "잘못"},
            ],
            "decisions": [
                {"text": "심플 구성으로 진행 결정.", "evidence_seg_ids": [2]},
            ],
            "issues": [
                # 존재하지 않는 seg_id(99) 인용 → 제거되어 근거 0 → 드롭
                {"text": "환각 이슈.", "evidence_seg_ids": [99]},
            ],
        },
        {
            "no": 2,
            "title": "DVR 이슈",
            "points": [{"text": "DVR 우려가 큼.", "evidence_seg_ids": [3]}],
            "decisions": [],
            "issues": [],
        },
        {
            # 블록 전체가 근거 없음 → 드롭
            "no": 3,
            "title": "근거 없는 안건",
            "points": [{"text": "유령", "evidence_seg_ids": []}],
            "decisions": [],
            "issues": [],
        },
    ],
}


class _FakeBackend(LLMBackend):
    name = "fake"

    def generate(self, messages, *, schema=None, temperature=0.0, max_tokens=2048, seed=0):
        return json.dumps(SUM_OUT, ensure_ascii=False)

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(json_mode=True, ctx_window=200_000, tool_call=False, determinism="full")


def test_ground_deterministic_anchor_and_time_range() -> None:
    summary = ground_summary(parse_summarize_output(json.dumps(SUM_OUT)), SEGS)
    d = summary.to_dict()
    # 블록 3(근거 없음) 드롭 → 2개 남음
    assert [b["no"] for b in d["agenda"]] == [1, 2], d["agenda"]
    blk1 = d["agenda"][0]
    # time_range = min start(12s=00:12) ~ max end(seg2 end 400s=06:40)
    assert blk1["time_range"] == "00:12 ~ 06:40", blk1["time_range"]
    # points anchor = 각 evidence min start
    assert blk1["points"][0]["anchor"] == "00:12", blk1["points"][0]
    assert blk1["points"][1]["anchor"] == "01:35", blk1["points"][1]  # 95s, LLM '잘못' 덮어씀
    assert blk1["decisions"][0]["anchor"] == "06:10", blk1["decisions"]  # 370s
    # issues: seg99(없음) 제거 → 근거 0 → 드롭
    assert blk1["issues"] == [], blk1["issues"]


def test_hybrid_sections_preserved() -> None:
    summary = ground_summary(parse_summarize_output(json.dumps(SUM_OUT)), SEGS)
    blk1 = summary.to_dict()["agenda"][0]
    assert len(blk1["points"]) == 2
    assert len(blk1["decisions"]) == 1
    assert blk1["decisions"][0]["text"] == "심플 구성으로 진행 결정."


def test_agenda_index_and_meta_passthrough() -> None:
    summary = ground_summary(parse_summarize_output(json.dumps(SUM_OUT)), SEGS)
    d = summary.to_dict()
    assert d["meta"]["subject"] == "EYEL-S3000ABR Demo시연"
    assert len(d["agenda_index"]) == 2
    assert d["agenda_index"][0]["title"] == "3D SVM 뷰 검토"


def test_stage_stamps_version_and_backend() -> None:
    summary = SummarizeStage().run(SEGS, _FakeBackend())
    assert summary.prompt_version == "summarize-ko-1.0", summary.prompt_version
    assert summary.backend == "fake", summary.backend


def test_passthrough_yields_empty_summary() -> None:
    # passthrough 는 JSON 요약을 못 내므로 빈 요약(파싱 실패 폴백).
    summary = SummarizeStage().run(SEGS, PassthroughBackend())
    d = summary.to_dict()
    assert d["agenda"] == [] and d["agenda_index"] == [], d


def test_service_summarize_meeting_grounds() -> None:
    orig = service.get_llm_backend
    service.get_llm_backend = lambda name: _FakeBackend()
    try:
        out = service.summarize_meeting(SEGS, backend_name="fake")
    finally:
        service.get_llm_backend = orig
    assert [b["no"] for b in out["agenda"]] == [1, 2], out["agenda"]
    assert out["agenda"][0]["time_range"] == "00:12 ~ 06:40"


def test_empty_summary_contract_shape() -> None:
    d = MeetingSummary.empty().to_dict()
    assert d["agenda"] == [] and d["agenda_index"] == []
    assert d["meta"]["subject"] == "" and d["meta"]["attendees"] == []
    assert d["schema_version"] == "sum-1.0"


def test_codefence_output_is_parsed() -> None:
    # 실제 LLM(agent_cli)은 ```json 펜스로 감싸는 경우가 잦다 → 견고 파싱.
    fenced = "```json\n" + json.dumps(SUM_OUT, ensure_ascii=False) + "\n```"
    summary = parse_summarize_output(fenced)
    assert summary.agenda_index[0].title == "3D SVM 뷰 검토"
    assert len(summary.agenda) == 3  # ground 전이라 아직 전부


def test_service_summarize_meeting_returns_dict_with_schema_version() -> None:
    # 계약 경계: summary 는 항상 dict(구조체) + 버전 키 — 프론트 string→object 전환 잠금.
    orig = service.get_llm_backend
    service.get_llm_backend = lambda name: _FakeBackend()
    try:
        out = service.summarize_meeting(SEGS, backend_name="fake")
    finally:
        service.get_llm_backend = orig
    assert isinstance(out, dict)
    assert out["schema_version"] == "sum-1.0"


def test_ground_empty_segments_drops_all_blocks() -> None:
    # STT가 아무것도 인식 못 한 빈 회의 → 모든 근거 무효 → 전 블록/인덱스 드롭.
    summary = ground_summary(parse_summarize_output(json.dumps(SUM_OUT)), [])
    d = summary.to_dict()
    assert d["agenda"] == [] and d["agenda_index"] == [], d


def test_parse_missing_meta_and_index_graceful() -> None:
    # LLM이 meta/agenda_index 키를 생략해도 예외 없이 빈 폴백.
    minimal = {"agenda": [{"no": 1, "title": "t", "points": [{"text": "x", "evidence_seg_ids": [0]}]}]}
    summary = parse_summarize_output(json.dumps(minimal))
    d = summary.to_dict()
    assert d["agenda_index"] == []
    assert d["meta"]["subject"] == "" and d["meta"]["attendees"] == []


def test_agenda_index_synced_after_block_drop() -> None:
    # 근거 없는 블록이 드롭되면 그 안건의 agenda_index 줄도 제거(목록↔상세 불일치 방지).
    data = {
        "agenda_index": [
            {"no": 1, "title": "살아남음"},
            {"no": 2, "title": "근거없어 드롭"},
            {"no": 3, "title": "아예 상세 없음"},
        ],
        "agenda": [
            {"no": 1, "title": "살아남음", "points": [{"text": "a", "evidence_seg_ids": [0]}]},
            {"no": 2, "title": "근거없어 드롭", "points": [{"text": "b", "evidence_seg_ids": [99]}]},
        ],
    }
    d = ground_summary(parse_summarize_output(json.dumps(data)), SEGS).to_dict()
    assert [b["no"] for b in d["agenda"]] == [1], d["agenda"]
    assert [e["no"] for e in d["agenda_index"]] == [1], d["agenda_index"]


def test_time_range_reverse_guard() -> None:
    # end<start(STT 노이즈) → 역순 time_range 방지(hi>=lo).
    bad_seg = [{"id": 0, "start": 100.0, "end": 50.0, "text": "x"}]
    data = {"agenda": [{"no": 1, "title": "t", "points": [{"text": "a", "evidence_seg_ids": [0]}]}]}
    d = ground_summary(parse_summarize_output(json.dumps(data)), bad_seg).to_dict()
    # lo=100s=01:40, hi 는 50s 가 아니라 lo 로 정규화.
    assert d["agenda"][0]["time_range"] == "01:40 ~ 01:40", d["agenda"][0]


def test_llm_no_preserved_and_evidence_deduped() -> None:
    # LLM이 보낸 no(5)는 보존(위치로 덮어쓰지 않음), evidence 중복은 제거.
    data = {"agenda": [{"no": 5, "title": "t", "points": [{"text": "a", "evidence_seg_ids": [1, 1, 2]}]}]}
    d = ground_summary(parse_summarize_output(json.dumps(data)), SEGS).to_dict()
    blk = d["agenda"][0]
    assert blk["no"] == 5, blk
    assert blk["points"][0]["evidence_seg_ids"] == [1, 2], blk["points"][0]


def _run() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_summarize ({len(fns)} cases)")


if __name__ == "__main__":
    _run()
