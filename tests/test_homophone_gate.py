"""숫자↔한글 동음 오인식 교정 + 게이트 면제 테스트 (homophone + validate).

핵심 잠금:
- 등재 케이스(5탐→오탐)는 게이트의 숫자보존 검사를 통과한다(되돌리지 않는다).
- 미등재 진짜 숫자(5분/1차…)는 종전대로 보존을 강제한다(누락 시 게이트 실패).
- apply_homophone 은 조사 변형은 잡되 차수(제5탐/15탐)는 건드리지 않는다.

실행: sudo .venv/bin/python tests/test_homophone_gate.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.postprocess.homophone import apply_homophone, excused_digits  # noqa: E402
from src.postprocess.schema import CleanedSegment  # noqa: E402
from src.postprocess.validate import (  # noqa: E402
    content_preserved,
    number_tokens_preserved,
    repair_or_degrade,
)


def test_apply_homophone_scope() -> None:
    # 등재 케이스 + 조사 변형
    assert apply_homophone("5탐을 줄여야") == "오탐을 줄여야"
    assert apply_homophone("5탐이니까 이거 5탐인데") == "오탐이니까 이거 오탐인데"
    # 진짜 숫자는 불변
    assert apply_homophone("1차 때는 5분 걸렸어") == "1차 때는 5분 걸렸어"
    # 차수 오교정 방지: 앞에 숫자/'제'
    assert apply_homophone("제5탐사대") == "제5탐사대"
    assert apply_homophone("15탐") == "15탐"


def test_excused_digits() -> None:
    # 원문 5탐 + 정제 오탐 → '5' 면제
    assert excused_digits("5탐을 줄여야", "오탐을 줄여야") == {"5"}
    # 교정이 일어나지 않았으면 면제 없음
    assert excused_digits("5탐을 줄여야", "5탐을 줄여야") == set()
    assert excused_digits("1차 5분", "1차 5분") == set()


def test_number_preservation_excuses_whitelist() -> None:
    # 5탐→오탐: '5'가 사라져도 면제 → 보존 1.0
    assert number_tokens_preserved("5탐을 줄여야", "오탐을 줄여야") == 1.0
    # 진짜 숫자 누락은 여전히 검출(면제 아님)
    assert number_tokens_preserved("5분 걸려", "걸려") == 0.0
    # 진짜 숫자 보존되면 1.0
    assert number_tokens_preserved("1차 5분", "1차 5분 정도") == 1.0


def test_gate_does_not_revert_whitelisted_fix() -> None:
    """5탐→오탐 정제 segment 가 게이트에서 원문으로 롤백되지 않아야 한다."""
    seg = CleanedSegment(
        id=0, start=0.0, end=2.0,
        original="FP 케이스가 5탐이니까 5탐을 줄여야",
        cleaned="FP 케이스가 오탐이니까 오탐을 줄여야.",
        edits=["text_edited"], flag=None,
    )
    # content_preserved 통과(숫자 5 면제)
    assert content_preserved(seg.original, seg.cleaned) is True
    out = repair_or_degrade(seg, require_edit=True)
    assert out.cleaned == "FP 케이스가 오탐이니까 오탐을 줄여야.", out.cleaned  # 롤백 안 됨
    assert out.flag is None, out.flag


def test_gate_still_reverts_real_number_drop() -> None:
    """진짜 숫자(5분)를 날린 정제는 종전대로 원문 유지 + 확인필요."""
    seg = CleanedSegment(
        id=1, start=0.0, end=2.0,
        original="한 5분 정도 걸립니다",
        cleaned="걸립니다.",  # 5분 통째로 사라짐
        edits=["text_edited"], flag=None,
    )
    out = repair_or_degrade(seg, require_edit=True)
    assert out.cleaned == "한 5분 정도 걸립니다", out.cleaned  # 원문 유지
    assert out.flag == "확인필요", out.flag


def _run() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_homophone_gate ({len(fns)} cases)")


if __name__ == "__main__":
    _run()
