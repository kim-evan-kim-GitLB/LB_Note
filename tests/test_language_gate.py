"""언어 게이트 회귀 테스트 — 영어 환각 전사 방어 (설계 docs/2026-07-30-영어환각-언어게이트-설계.md).

검증 불변식:
  - 실측 환각 문장(한글비율 0.00~0.01, cps 14+)은 exclude 판정 + 사유 정확.
  - **영어 기술용어가 섞인 정상 한국어 발화는 통과**(오탐 0). 이게 이 기능의 핵심 회귀다 —
    과잉 차단은 회의 내용 유실이라 환각 통과보다 비용이 크다.
  - 자막 상용구·0초 세그먼트·빈 텍스트·숫자만 있는 발화 방어.
  - LANG_GATE_ENABLED=0 이면 전체 no-op(안전 스위치).
  - partition: exclude 는 주입 목록에서 빠지고 low_conf 는 남아 표시 대상이 된다.

실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_language_gate.py -q
"""
from __future__ import annotations

import contextlib

from src import config
from src.postprocess import language_gate as lg

# --- 실측 데이터(output/asr_test*, output/verify) 에서 뽑은 환각 문장 ---
HALLUCINATION_1 = (
    "Okay, let's check out my song. So you need to buy one of the two girls and they were "
    "there. The Belgian could you chair? I have some room to be around the two girls."
)
HALLUCINATION_2 = "offline, sucking on your eye. You go there and take a curco button or something."

# --- 실측 정상 발화(한글비율 0.59~0.76) — 절대 걸러선 안 되는 것들 ---
NORMAL_TECH_1 = "Action item, list를 도출해. 뭐, 문서로 만들든 action item 문서로 만들든 어떤 형태로 만들어도 action item 이 나와야죠."
NORMAL_TECH_2 = "뭐냐, 위스퍼에 그 모델이 뭐, Tini, Beige, Small, Medium, Large 뭐 이렇게 있는데 그 특징마다 아까"
NORMAL_TECH_3 = "이런 length caps. 이 테이블의 master data를 업데이트할 수 있도록 이렇게 2개 기능을 추가했습니다."
NORMAL_TECH_4 = "저번에 말씀드린 것처럼 우리가 비전을 이용한 safety, 그거는 SVM도 마찬가지로, shield도 마찬가지로."


@contextlib.contextmanager
def _cfg(**overrides):
    """config 값 임시 오버라이드(테스트 격리)."""
    old = {k: getattr(config, k) for k in overrides}
    try:
        for k, v in overrides.items():
            setattr(config, k, v)
        yield
    finally:
        for k, v in old.items():
            setattr(config, k, v)


def test_hallucination_excluded_with_reason() -> None:
    # 28.7초 세그먼트에 427자 영어 → 한글비율 0.01, cps 14.9
    verdict, reason = lg.classify(HALLUCINATION_1, 1106.6, 1135.3)
    assert verdict == lg.EXCLUDE, verdict
    assert reason == lg.REASON_NON_KOREAN, reason
    verdict2, reason2 = lg.classify(HALLUCINATION_2, 3767.6, 3793.3)
    assert verdict2 == lg.EXCLUDE and reason2 == lg.REASON_NON_KOREAN


def test_normal_korean_with_english_terms_passes() -> None:
    """오탐 0 회귀 — 영어 용어가 섞였을 뿐인 정상 발화는 전부 ok."""
    for text in (NORMAL_TECH_1, NORMAL_TECH_2, NORMAL_TECH_3, NORMAL_TECH_4):
        verdict, reason = lg.classify(text, 0.0, 26.0)
        assert verdict == lg.OK, f"{verdict}/{reason}: {text[:40]}"


def test_speech_rate_burst_excluded() -> None:
    """짧은 구간에 긴 비한국어 텍스트(문자 폭주) → 환각 특성.

    한글이 5자 이상이라 non_korean 임계는 비껴가지만 cps 가 폭주하는 경로를 검증한다.
    """
    text = "네 그렇습니다 알겠습니다 " + "Okay so let's check that out. " * 12
    verdict, reason = lg.classify(text, 0.0, 5.0)
    assert verdict == lg.EXCLUDE and reason == lg.REASON_SPEECH_RATE, (verdict, reason)


def test_short_latin_fragment_marked_not_dropped() -> None:
    """짧은 라틴 조각은 버리지 않는다 — 실측 환각은 365자+ 였고 짧은 건 진짜 발화일 확률이 높다."""
    for text in ("OK, LGTM", "The migration plan 확인", "Yes. Agreed."):
        verdict, reason = lg.classify(text, 0.0, 5.0)
        assert verdict == lg.LOW_CONF, (text, verdict)
        assert reason == lg.REASON_MOSTLY_NON_KO
    # 반대로 길면(>=40자) 제외된다 — 길이가 환각의 실측 특징이다.
    long_latin = "This is a fabricated english sentence that never happened in the meeting."
    assert lg.classify(long_latin, 0.0, 30.0)[0] == lg.EXCLUDE


def test_fast_korean_speech_not_excluded() -> None:
    """빠른 한국어 발화는 cps 가 높아도 통과해야 한다(AND 조건 검증)."""
    text = "그래서 이번에 말씀드린 대로 진행하겠습니다 " * 6  # 한글 100%
    verdict, _ = lg.classify(text, 0.0, 5.0)
    assert verdict == lg.OK


def test_boilerplate_excluded() -> None:
    for text in ("Thank you for watching!", "Subtitles by the Amara.org community", "자막 제공: 방송사"):
        verdict, reason = lg.classify(text, 0.0, 3.0)
        assert verdict == lg.EXCLUDE and reason == lg.REASON_BOILERPLATE, (text, verdict, reason)


def test_gray_zone_marked_low_conf() -> None:
    """한글비율 0.15~0.45 회색지대는 버리지 않고 표시만."""
    text = "migration plan 재확인 필요하고 rollback 방안도 like this"
    st = lg.segment_stats(text, 0.0, 20.0)
    assert 0.15 <= st["hangul_ratio"] < 0.45, st
    verdict, reason = lg.classify(text, 0.0, 20.0)
    assert verdict == lg.LOW_CONF and reason == lg.REASON_MOSTLY_NON_KO


def test_edge_cases_do_not_crash() -> None:
    assert lg.classify("", 0.0, 0.0)[0] == lg.OK           # 빈 텍스트
    assert lg.classify("네.", 0.0, 0.0)[0] == lg.OK        # 0초 세그먼트(0 division 방어)
    assert lg.classify("3시 30분", 0.0, 2.0)[0] == lg.OK   # 숫자·기호만 → 개입 없음
    st = lg.segment_stats("네", 5.0, 5.0)
    assert st["chars_per_sec"] == 0.0


def test_disabled_switch_is_noop() -> None:
    with _cfg(LANG_GATE_ENABLED=False):
        verdict, reason = lg.classify(HALLUCINATION_1, 1106.6, 1135.3)
        assert verdict == lg.OK and reason is None


def test_threshold_override_from_config() -> None:
    """임계값은 config 로 조정 가능(코드 수정 없이 운영 대응)."""
    with _cfg(LANG_GATE_LOW_CONF_RATIO=0.8):
        verdict, _ = lg.classify(NORMAL_TECH_1, 0.0, 26.0)  # 한글비율 0.6 → 저신뢰로 내려감
        assert verdict == lg.LOW_CONF


def test_partition_splits_kept_low_conf_excluded() -> None:
    segs = [
        {"id": 0, "start": 0.0, "end": 20.0, "text": NORMAL_TECH_1},
        {"id": 1, "start": 20.0, "end": 48.7, "text": HALLUCINATION_1},
        {"id": 2, "start": 48.7, "end": 68.7, "text": "migration plan 재확인 필요하고 rollback 방안도 like this"},
        {"id": 3, "start": 58.7, "end": 70.0, "text": NORMAL_TECH_2},
    ]
    kept, low_conf, excluded = lg.partition(segs)
    assert [s["id"] for s in kept] == [0, 2, 3], "exclude 만 빠진다"
    assert set(low_conf) == {2} and low_conf[2] == lg.REASON_MOSTLY_NON_KO
    assert set(excluded) == {1} and excluded[1] == lg.REASON_NON_KOREAN


def _run() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_language_gate ({len(fns)} cases)")


if __name__ == "__main__":
    _run()
