"""모델 로드 경로 회귀 테스트 — 스레드 안전성 / VRAM 상한 / OOM 폴백 실패 처리.

배경(2026-07-28): 웹 STT 슬롯을 1 -> 2 로 올리자 동시 실행이 바로 깨졌다.

    RuntimeError: mat1 and mat2 must have the same dtype, but got BFloat16 and Float

원인은 VRAM 이 아니라 로드 경합이다. transformers 의 from_pretrained 는 로딩 중 프로세스 전역
dtype 상태를 건드리는데, 두 잡 스레드가 동시에 load() 를 호출하면 서로의 상태를 덮어써서 한쪽
모델이 BFloat16/Float 이 섞인 채 올라간다. 슬롯이 1개일 때는 동시 로드가 없어 드러나지 않던
잠복 결함이라, 동시성을 올릴 때마다 재발할 수 있다 → 락을 회귀로 고정한다.

디코딩은 인스턴스별이라 직렬화 대상이 아니다(락 밖). 이 테스트는 '로드만' 줄 서는지 본다.

실제 모델 미로드 — _load_bf16/_load_int8 을 가짜로 갈아끼워 겹침 여부만 관측한다.
실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_backend_load.py
"""
from __future__ import annotations

import threading
import time
from pathlib import Path


def _probe_backend_class():
    """_load_bf16 이 동시에 몇 개까지 겹치는지 세는 서브클래스."""
    from src.backends.cohere import CohereASRBackend

    class Probe(CohereASRBackend):
        counter_lock = threading.Lock()
        inside = 0
        max_inside = 0

        def _load_bf16(self) -> None:
            with Probe.counter_lock:
                Probe.inside += 1
                Probe.max_inside = max(Probe.max_inside, Probe.inside)
            time.sleep(0.05)  # 겹칠 여지를 준다(락이 없으면 여기서 반드시 겹친다)
            with Probe.counter_lock:
                Probe.inside -= 1
            self._model = object()
            self._processor = object()

    return Probe


def test_model_load_is_serialized_across_threads():
    Probe = _probe_backend_class()
    errs: list[str] = []

    def worker():
        try:
            Probe(Path("/nonexistent")).load()
        except Exception as e:  # noqa: BLE001
            errs.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errs, errs
    # 락이 없으면 4개가 동시에 들어가 max_inside > 1 이 된다.
    assert Probe.max_inside == 1, f"동시 로드가 발생했다(max={Probe.max_inside})"


def test_load_lock_is_shared_by_all_instances():
    """웹은 잡마다 새 백엔드 인스턴스를 만든다(src/stt.py:get_backend) → 락은 클래스 공유여야 한다."""
    from src.backends.cohere import CohereASRBackend

    a = CohereASRBackend(Path("/nonexistent"))
    b = CohereASRBackend(Path("/nonexistent"))
    assert a._LOAD_LOCK is b._LOAD_LOCK is CohereASRBackend._LOAD_LOCK


def test_load_lock_released_on_failure():
    """로드 실패로도 락이 새지 않는다 — 한 번 실패하면 이후 모든 잡이 영구 대기하게 된다."""
    from src.backends.cohere import CohereASRBackend

    class Boom(CohereASRBackend):
        def _load_bf16(self) -> None:
            raise RuntimeError("의도적 실패")

    for _ in range(2):  # 두 번째 호출이 락을 얻지 못하면 여기서 멈춘다
        try:
            Boom(Path("/nonexistent")).load()
        except RuntimeError:
            pass
    assert CohereASRBackend._LOAD_LOCK.acquire(blocking=False)
    CohereASRBackend._LOAD_LOCK.release()


# ---------- VRAM 상한 ----------
def test_vram_cap_disabled_when_zero():
    """상한 0 = 제한 없음. GPU 를 독점하는 환경에서 굳이 걸지 않게."""
    import src.backends.cohere as c
    from src import config

    orig, c._vram_cap_applied = config.STT_VRAM_CAP_MB, False
    try:
        config.STT_VRAM_CAP_MB = 0
        assert c.apply_vram_cap() is None
        assert c._vram_cap_applied is False
    finally:
        config.STT_VRAM_CAP_MB = orig
        c._vram_cap_applied = False


def test_vram_cap_applied_once_and_clamped():
    """상한은 프로세스당 1회만 적용되고, 장치 용량을 넘으면 100% 로 clamp 된다.

    clamp 가 없으면 상한이 장치보다 큰 작은 GPU 에서 fraction>1 로 예외가 난다.
    """
    import torch

    import src.backends.cohere as c
    from src import config

    if not torch.cuda.is_available():
        return  # GPU 없는 환경에서는 검증 대상 아님(apply_vram_cap 이 None 반환)
    total_mb = torch.cuda.get_device_properties(0).total_memory / (1024 * 1024)
    orig, c._vram_cap_applied = config.STT_VRAM_CAP_MB, False
    try:
        config.STT_VRAM_CAP_MB = int(total_mb * 10)  # 장치보다 훨씬 큰 값
        msg = c.apply_vram_cap()
        assert msg and "100.0%" in msg, msg
        assert c.apply_vram_cap() is None  # 두 번째 호출은 무동작(멱등)
    finally:
        config.STT_VRAM_CAP_MB = orig
        c._vram_cap_applied = False
        torch.cuda.set_per_process_memory_fraction(1.0)  # 다른 테스트에 영향 없게 복구


# ---------- OOM 폴백 실패가 원인을 가리지 않는가 ----------
def test_int8_fallback_failure_reraises_original_oom():
    """int8 폴백이 실패해도 표면화되는 예외는 원래의 OOM 이어야 한다.

    이 환경의 bitsandbytes 는 torch cu130 과 맞지 않아 int8 경로가 죽어 있다. 예전에는 그
    폴백 실패가 RuntimeError('weights 변환 문제')로 바뀌어 나가서, 로그만 봐서는 VRAM 부족인지
    모델 파일 문제인지 알 수 없었다. 웹은 이 예외 타입으로 stt_vram_exhausted 를 판정한다.
    """
    import torch

    from src.backends.cohere import CohereASRBackend

    class OomThenBrokenInt8(CohereASRBackend):
        def _load_bf16(self) -> None:
            raise torch.cuda.OutOfMemoryError("CUDA out of memory. Tried to allocate 3.85 GiB")

        def _load_int8(self) -> None:
            raise RuntimeError("bitsandbytes 로드 실패(libnvJitLink.so.13 없음)")

    try:
        OomThenBrokenInt8(Path("/nonexistent")).load()
        raise AssertionError("예외가 나야 한다")
    except torch.cuda.OutOfMemoryError as e:
        assert "out of memory" in str(e).lower()
        assert isinstance(e.__cause__, RuntimeError)  # 폴백 오류는 체인에 보존
    finally:
        import src.backends.cohere as c
        c._vram_cap_applied = False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_backend_load ({len(fns)} cases)")
