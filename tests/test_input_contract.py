"""입력 계약 정규화 테스트 (설계 §5·F1).

프로듀서 둘의 스키마가 로더를 통과한 뒤 타임스탬프가 1:1 보존(0.0 폴백 아님)되는지 검증한다.
- 메인 파이프라인 형태: {start, end, text}
- 실험 도구 형태:      {start_sec, end_sec, start_ts, text}
또한 start/start_sec 둘 다 없으면 에러로 드러나는지(무음 폴백 금지) 검증한다.

stdlib assert 스크립트(의존성 없음). 실행:
  sudo .venv/bin/python tests/test_input_contract.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.postprocess.schema import normalize_segments  # noqa: E402


def test_main_pipeline_schema_preserves_timestamps() -> None:
    """메인 파이프라인 {start, end, text} → 타임스탬프 1:1 보존."""
    segs = [
        {"start": 3.69, "end": 33.18, "text": "첫 발화"},
        {"start": 33.18, "end": 60.5, "text": "둘째 발화"},
    ]
    out = normalize_segments(segs)
    assert [s["id"] for s in out] == [0, 1]
    assert out[0]["start"] == 3.69 and out[0]["end"] == 33.18, out[0]
    assert out[1]["start"] == 33.18 and out[1]["end"] == 60.5, out[1]
    # 0.0 무음 폴백이 아님을 명시 확인
    assert out[0]["start"] != 0.0
    assert out[0]["text"] == "첫 발화"


def test_tool_schema_preserves_timestamps() -> None:
    """실험 도구 {start_sec, end_sec, start_ts, text} → 타임스탬프 1:1 보존."""
    segs = [
        {"start_sec": 3.69, "end_sec": 33.18, "start_ts": "00:00:04", "text": "첫 발화"},
        {"start_sec": 100.0, "end_sec": 130.0, "start_ts": "00:01:40", "text": "둘째"},
    ]
    out = normalize_segments(segs)
    assert out[0]["start"] == 3.69 and out[0]["end"] == 33.18, out[0]
    assert out[1]["start"] == 100.0 and out[1]["end"] == 130.0, out[1]
    assert out[0]["start"] != 0.0
    assert out[1]["text"] == "둘째"


def test_both_schemas_equivalent() -> None:
    """같은 타임스탬프를 가진 두 스키마는 동일 내부 표준으로 정규화된다."""
    main = normalize_segments([{"start": 5.0, "end": 9.0, "text": "x"}])
    tool = normalize_segments([{"start_sec": 5.0, "end_sec": 9.0, "text": "x"}])
    assert main[0] == tool[0], (main[0], tool[0])


def test_missing_timestamp_raises() -> None:
    """start/start_sec 둘 다 없으면 0.0 폴백이 아니라 에러(설계 §5)."""
    try:
        normalize_segments([{"text": "타임스탬프 없음"}])
    except ValueError as e:
        assert "start" in str(e)
    else:
        raise AssertionError("타임스탬프 누락인데 에러가 안 났다(무음 폴백 금지 위반)")


def test_missing_end_raises() -> None:
    try:
        normalize_segments([{"start": 1.0, "text": "end 없음"}])
    except ValueError as e:
        assert "end" in str(e)
    else:
        raise AssertionError("end 누락인데 에러가 안 났다(무음 폴백 금지 위반)")


def main() -> int:
    tests = [
        test_main_pipeline_schema_preserves_timestamps,
        test_tool_schema_preserves_timestamps,
        test_both_schemas_equivalent,
        test_missing_timestamp_raises,
        test_missing_end_raises,
    ]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n전체 통과: {len(tests)}개")
    return 0


if __name__ == "__main__":
    sys.exit(main())
