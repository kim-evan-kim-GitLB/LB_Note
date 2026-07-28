"""STT 배치 경계 취소 — "분석 취소"가 GPU 슬롯을 초 단위로 반납하게 하는지 검증.

배경: STT 추론에는 내부 취소 지점이 없어, 취소해도 전체 오디오를 다 디코딩할 때까지 슬롯이
안 풀렸다(뒤 사용자가 그만큼 대기). VAD 청크 배치 루프 경계에 취소 확인을 넣어 다음 배치부터
즉시 이탈하게 한 변경의 회귀 테스트.

모델을 띄우지 않고 processor/model 만 가짜로 갈아끼워 루프 제어 흐름만 본다.
"""
from __future__ import annotations

import threading

import numpy as np
import pytest

from src.backends.cohere import CohereASRBackend
from src.cancellation import OperationCancelled, cancel_requested, use_cancel_event


class _Inputs(dict):
    """processor 반환값 대역 — **inputs 언패킹과 .to(device) 를 흉내낸다."""

    def to(self, *_args, **_kwargs):
        return self


class _Outputs:
    def __init__(self, n: int) -> None:
        self.n = n


class _FakeProcessor:
    def __init__(self) -> None:
        self.batch_calls = 0
        self.on_call = None  # 배치 처리 중 일어나는 일(예: 사용자 취소)을 흉내내는 훅

    def __call__(self, batch, sampling_rate=None, return_tensors=None, language=None):
        self.batch_calls += 1
        self._last_n = len(batch) if isinstance(batch, list) else 1
        if self.on_call is not None:
            self.on_call()
        return _Inputs()

    def batch_decode(self, outputs, skip_special_tokens=True):
        return ["텍스트"] * outputs.n

    def decode(self, outputs, skip_special_tokens=True, audio_chunk_index=None, language=None):
        return "텍스트"


class _FakeModel:
    device = "cpu"
    dtype = None

    def __init__(self, proc: _FakeProcessor) -> None:
        self._proc = proc

    def generate(self, **_kwargs):
        return _Outputs(self._proc._last_n)


def _backend() -> tuple[CohereASRBackend, _FakeProcessor]:
    """load() 없이 내부 상태만 채운 백엔드 — 루프 제어 흐름 전용."""
    be = object.__new__(CohereASRBackend)
    proc = _FakeProcessor()
    be._processor = proc
    be._model = _FakeModel(proc)
    return be, proc


def _audios(n: int) -> list[np.ndarray]:
    return [np.zeros(16000, dtype=np.float32) for _ in range(n)]


def test_cancel_before_start_decodes_nothing():
    """취소된 상태로 진입하면 첫 배치도 돌리지 않고 즉시 이탈한다."""
    be, proc = _backend()
    ev = threading.Event()
    ev.set()
    with use_cancel_event(ev):
        with pytest.raises(OperationCancelled):
            be.transcribe_arrays(_audios(8), batch_size=4)
    assert proc.batch_calls == 0


def test_cancel_midway_stops_at_next_batch_boundary():
    """진행 중 취소하면 현재 배치까지만 돌고 다음 경계에서 멈춘다(전체를 다 돌지 않는다)."""
    be, proc = _backend()
    ev = threading.Event()
    proc.on_call = ev.set  # 첫 배치 처리 중에 사용자가 취소를 누른 상황
    with use_cancel_event(ev):
        with pytest.raises(OperationCancelled):
            be.transcribe_arrays(_audios(20), batch_size=4)  # 취소 없으면 5배치
    assert proc.batch_calls == 1  # 첫 배치만 처리하고 두 번째 경계에서 이탈


def test_no_cancel_event_decodes_all_batches():
    """취소 이벤트가 없으면 기존 동작 그대로 — 전 배치를 돌고 청크 수만큼 세그먼트를 낸다."""
    be, proc = _backend()
    segs = be.transcribe_arrays(_audios(10), batch_size=4)
    assert proc.batch_calls == 3  # 4+4+2
    assert len(segs) == 10
    assert not cancel_requested()


def test_single_path_fallback_also_cancels():
    """batch_size<=1 폴백 경로도 같은 취소 지점을 갖는다."""
    be, proc = _backend()
    ev = threading.Event()
    ev.set()
    with use_cancel_event(ev):
        with pytest.raises(OperationCancelled):
            be.transcribe_arrays(_audios(3), batch_size=1)
    assert proc.batch_calls == 0


def test_agent_cli_cancelled_is_operation_cancelled():
    """agent_cli 취소 예외가 공통 타입의 하위 타입이라 웹 잡의 단일 except 로 잡힌다."""
    from src.postprocess.backends.agent_cli import AgentCLICancelled

    assert issubclass(AgentCLICancelled, OperationCancelled)
