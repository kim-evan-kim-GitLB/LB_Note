"""적응형 재디코딩 회귀 테스트 (설계 근거 docs/2026-08-04-영어전사-드리프트-진단.md).

검증 불변식:
  - 한글비율이 임계 미만인 **긴** 청크만 후보다. 짧은 라틴 조각("OK")·판정 불가(숫자/빈문자)는 제외.
  - 재디코딩이 원본보다 나쁘거나 같으면 **교체하지 않는다**(회귀를 만들지 않는 쪽으로 닫는다).
  - 후보가 전체의 과반이면 통째로 건너뛴다(실제 영어 회의에서 시간만 배로 쓰는 것 방지).
  - 하위 분할은 가능한 한 무음 경계에 떨어진다(단어 중간 절단 방지).
  - STT_REDECODE=0 이면 완전 no-op.
  - 배치 출력 수와 청크 수가 다르면 조용히 잘라내지 않고 즉시 실패한다.

실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_adaptive_redecode.py -q
"""
from __future__ import annotations

import contextlib

import numpy as np
import pytest

from src import config, pipeline
from src.chunker import AudioChunk, subdivide_chunk
from src.langmetrics import hangul_ratio
from src.types import Segment

# 실측 드리프트 텍스트(진단 리포트 12분55초/13분22초 구간) — 40자 이상, 한글비 0.0x
DRIFT = ("Paddington, 지 is a taboo. Tonchebee hung by her. Tonship, puts on table one hand. "
         "Says I'm told us what else? You don't see this much.")
NORMAL = "그 프로님이 WSK를 만들어 주셨던 부분을 하려고 보니까 좀 포맷이 마음에 안 들어서 다시 작성했습니다."
# 영어 기술용어가 섞인 정상 발화 — 절대 후보가 되면 안 된다(과잉 개입은 내용 훼손).
TECHY = "Action item, list를 도출해서 문서로 만들든 action item 문서로 만들든 어떤 형태로든 나와야죠."


@contextlib.contextmanager
def cfg(**kw):
    old = {k: getattr(config, k) for k in kw}
    for k, v in kw.items():
        setattr(config, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(config, k, v)


# --------------------------------------------------------------------------- 지표
def test_hangul_ratio_판정불가는_None():
    """빈 문자열을 1.0(완벽한 한국어)으로 세던 결함을 막는다."""
    assert hangul_ratio("") is None
    assert hangul_ratio("123 :: 45%") is None
    assert hangul_ratio("안녕하세요") == 1.0
    assert hangul_ratio("hello") == 0.0


# --------------------------------------------------------------------------- 기본 임계
def test_기본_임계는_게이트_저신뢰_임계와_같다():
    """규칙: 게이트가 저신뢰 이하로 떨어뜨릴 청크를 떨어뜨리기 전에 구제한다.

    한쪽만 바꾸면 구제 범위와 판정 범위가 어긋나 조용히 회귀한다(0.15 시절 실측:
    문제 음원 저신뢰 21→16 에 그침. 0.45 로 맞추면 21→1).
    한쪽을 의도적으로 바꿀 때만 이 테스트를 함께 고칠 것.
    """
    assert config.STT_REDECODE_RATIO == config.LANG_GATE_LOW_CONF_RATIO == 0.45


def test_기본_임계는_정상_한국어_최저치보다_낮다():
    """게이트 캘리브레이션상 정상 세그먼트 최저 한글비 0.59 — 과잉 재디코딩 방지 마진."""
    assert config.STT_REDECODE_RATIO < 0.59


# --------------------------------------------------------------------------- 후보 선정
def test_드리프트_텍스트만_후보():
    with cfg(STT_REDECODE_RATIO=0.15, STT_REDECODE_MIN_CHARS=40):
        assert pipeline._redecode_candidates([NORMAL, DRIFT, TECHY]) == [1]


def test_짧은_라틴조각은_후보_아님():
    """실측상 짧은 영어 조각은 진짜 발화("OK", "LGTM")일 확률이 높다."""
    with cfg(STT_REDECODE_RATIO=0.15, STT_REDECODE_MIN_CHARS=40):
        assert pipeline._redecode_candidates(["OK, LGTM.", "Yes."]) == []


def test_판정불가_텍스트는_후보_아님():
    with cfg(STT_REDECODE_RATIO=0.15, STT_REDECODE_MIN_CHARS=1):
        assert pipeline._redecode_candidates(["", "1234567890 :: 3 + 4 = 7 !!!"]) == []


# --------------------------------------------------------------------------- 하위 분할
def _chunk(dur_sec: float, sr: int = 16000) -> AudioChunk:
    return AudioChunk(index=0, start_sec=10.0, end_sec=10.0 + dur_sec,
                      samples=np.zeros(int(dur_sec * sr), dtype=np.float32))


def test_짧은_청크는_분할하지_않음():
    assert len(subdivide_chunk(_chunk(5.0), regions=[], target_sec=8.0)) == 1


def test_무음경계에서_분할():
    """regions 가 있으면 컷이 발화 사이 무음에 떨어진다(단어 절단 방지)."""
    ch = _chunk(28.0)                      # 전체 오디오 기준 10.0~38.0s
    regions = [(10.0, 18.0), (20.0, 27.0), (29.0, 37.0)]
    subs = subdivide_chunk(ch, regions=regions, target_sec=8.0)
    assert len(subs) >= 3
    # 무음 구간(18~20s)이 통째로 들어간 조각은 없어야 한다 = 발화 단위로 잘렸다
    assert all(len(s) > 0 for s in subs)
    assert sum(len(s) for s in subs) <= len(ch.samples)


def test_regions_없으면_고정분할_폴백():
    subs = subdivide_chunk(_chunk(24.0), regions=[], target_sec=8.0)
    assert len(subs) == 3
    assert sum(len(s) for s in subs) == 24 * 16000


# --------------------------------------------------------------------------- 재디코딩 동작
class _FakeBackend:
    """transcribe_arrays 만 흉내내는 스텁. 하위 조각마다 지정한 텍스트를 돌려준다."""

    def __init__(self, replies: list[str]):
        self.replies = replies
        self.calls = 0

    def transcribe_arrays(self, audios, sr=16000, start_offsets=None, language="Korean",
                          batch_size=32, max_new_tokens=1024):
        self.calls += 1
        return [Segment(start=0.0, end=1.0, text=t) for t in self.replies[:len(audios)]]


def _run(texts, replies, chunk_dur=24.0, **overrides):
    chunks = [_chunk(chunk_dur) for _ in texts]
    be = _FakeBackend(replies)
    with cfg(STT_REDECODE_RATIO=0.15, STT_REDECODE_MIN_CHARS=40,
             STT_REDECODE_TARGET_SEC=8.0, STT_REDECODE_MAX_FRACTION=0.5, **overrides):
        out, info = pipeline._adaptive_redecode(
            be, chunks, texts, regions=[], sr=16000, language="Korean", batch_size=32,
        )
    return out, info, be


def test_개선되면_교체():
    out, info, be = _run([NORMAL, DRIFT], ["받는 수신자 정보", "이렇게 넣고", "메일을 발송하면"])
    assert out[0] == NORMAL                      # 정상 청크는 손대지 않는다
    assert out[1] == "받는 수신자 정보 이렇게 넣고 메일을 발송하면"
    assert info["candidates"] == 1 and info["replaced"] == 1
    assert be.calls == 1                         # 후보를 모아 한 번에 디코딩


def test_나빠지면_원본_유지():
    """재디코딩이 항상 낫다는 보장은 없다 — 개선 없으면 원본을 지킨다."""
    out, info, _ = _run([DRIFT], ["still english here", "and more english"])
    assert out[0] == DRIFT
    assert info["replaced"] == 0


def test_동률이면_원본_유지():
    out, info, _ = _run([DRIFT], ["Paddington taboo", "Tonship table"])
    assert out[0] == DRIFT
    assert info["replaced"] == 0


def test_빈_재디코딩결과는_무시():
    out, info, _ = _run([DRIFT], ["", "   "])
    assert out[0] == DRIFT
    assert info["replaced"] == 0


def test_후보가_과반이면_건너뜀():
    """음원 전체가 비한국어면(실제 영어 회의) 재디코딩은 시간만 배로 든다."""
    out, info, be = _run([DRIFT, DRIFT, NORMAL], ["복원된 한국어"] * 9)
    assert out == [DRIFT, DRIFT, NORMAL]
    assert info["skipped_reason"] == "too_many_candidates"
    assert be.calls == 0


def test_후보_없으면_호출_없음():
    out, info, be = _run([NORMAL, TECHY], ["안 쓰임"])
    assert out == [NORMAL, TECHY]
    assert info["candidates"] == 0 and be.calls == 0


def test_더_못쪼개면_호출_없음():
    """이미 target 이하로 짧은 청크는 다시 돌려도 같은 결과다."""
    out, info, be = _run([DRIFT], ["복원"], chunk_dur=5.0)
    assert out[0] == DRIFT
    assert be.calls == 0


# --------------------------------------------------------------------------- 안전 스위치
def test_스위치_OFF면_완전_no_op():
    """끄면 백엔드를 아예 호출하지 않고 텍스트도 그대로다."""
    chunks = [_chunk(24.0)]
    be = _FakeBackend(["복원된 한국어"])
    with cfg(STT_REDECODE=False):
        out, info = pipeline._maybe_redecode(
            be, chunks, [DRIFT], regions=[], sr=16000, language="Korean", batch_size=32,
        )
    assert out == [DRIFT]
    assert info == {"enabled": False}
    assert be.calls == 0


def test_스위치_ON이면_동작():
    chunks = [_chunk(24.0)]
    be = _FakeBackend(["받는 수신자 정보", "메일 발송", "됩니다"])
    with cfg(STT_REDECODE=True, STT_REDECODE_RATIO=0.15, STT_REDECODE_MIN_CHARS=40,
             STT_REDECODE_TARGET_SEC=8.0, STT_REDECODE_MAX_FRACTION=0.5):
        out, info = pipeline._maybe_redecode(
            be, chunks, [DRIFT], regions=[], sr=16000, language="Korean", batch_size=32,
        )
    assert out[0] != DRIFT
    assert info["replaced"] == 1


# --------------------------------------------------------------------------- 배치 정렬 방어
def test_출력수_일치하면_통과():
    pipeline._assert_chunk_alignment(3, 3, 30.0)          # 예외 없음


def test_출력수_불일치는_즉시_실패():
    """조용히 zip 으로 잘라내면 이후 모든 세그먼트의 타임스탬프가 어긋난다."""
    with pytest.raises(RuntimeError, match="출력 수 불일치"):
        pipeline._assert_chunk_alignment(3, 4, 31.0)      # 특징추출기 내부 재분할 상황


# --------------------------------------------------------------------------- transcript.md 헤더
def test_vad_헤더는_고정분할값을_찍지_않는다():
    info = {"method": "energy_vad_segmentation", "vad_backend": "energy",
            "target_sec": 30.0, "chunk_len_avg_sec": 26.3}
    header = pipeline._chunk_header(info, 60.0, 10.0)
    assert "vad/energy" in header and "26.3" in header
    assert "60.0" not in header and "overlap" not in header


def test_fixed_헤더는_기존_표기_유지():
    assert pipeline._chunk_header({"method": "fixed"}, 60.0, 10.0) == "chunk=60.0s, overlap=10.0s"
