"""GPU 여유 관측 — 다른 작업이 GPU 를 점유해 STT 가 실패할 상황을 미리 알린다.

배경: 이 서버의 GPU 는 STT 전용이 아니다. 다른 작업자의 학습/실험이 VRAM 을 가져가면 STT 잡은
'대기'하는 게 아니라 **OOM 으로 죽는다**. 재현 결과(2026-07-28):
  - 여유 < 모델 크기        -> load() OOM -> int8 폴백 시도 -> 폴백도 실패 -> RuntimeError
  - 여유 < 배치 필요량      -> 디코딩 중 OOM (cohere.py 의 폴백은 load() 만 감싼다)
둘 다 사용자에게는 'STT 엔진 오류로 전사에 실패했습니다'로만 보인다 — 엔진 결함과 자원 부족을
구분할 수 없어, 엔진을 뒤지게 만든다.

여기서는 nvidia-smi 한 줄 조회로 여유 VRAM 을 읽고 1잡 실행 추정 필요량과 비교해 압박 수준을
낸다. 조회 실패(nvidia-smi 없음/GPU 없음/타임아웃)는 기능 OFF 로 취급한다 — 관측이 본 기능을
막지 않는다는 원칙(observability.audit 의 sink 정책과 동일).

필요량 추정은 실측 기준이다(ax 음원, RTX PRO 6000, 2026-07-28):

  필요량 = 모델 가중치 + CUDA 컨텍스트/할당자 오버헤드 + 청크당 VRAM x batch_size

기준은 torch 의 allocated 가 아니라 **reserved** 다 — 남에게서 실제로 빼앗는 양이 reserved 이고,
경고의 목적이 "남이 쓸 자리가 있는가"이기 때문이다. 둘은 꽤 벌어진다(캐싱 할당자가 버킷 단위로
잡아 작은 배치일수록 더 오버슈트한다):
  bs=8  allocated 증가분 790MiB  vs reserved 증가분 1,886MiB
  bs=32 allocated 증가분 3,060MiB vs reserved 증가분 3,396MiB
또 프로세스가 살아 있는 한 CUDA 컨텍스트가 약 1GB 상주한다(unload 후에도 여유가 그만큼 안 돌아옴).

측정된 reserved peak: bs=8 5,834 / bs=16 7,322 / bs=32 7,344 / bs=64 10,326 MiB (+ 컨텍스트 약 1GB)

nvidia-smi 는 프로세스당 수십 ms 가 드는 서브프로세스라 TTL 캐시를 둔다(폴링이 잦다).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

# 기능 스위치. GPU 없는 환경(테스트/CI)에서도 안전하지만 명시적으로 끌 수 있게 둔다.
MONITOR_ENABLED = os.environ.get("WEB_GPU_MONITOR", "1") != "0"

# 실측 기반 기본값(reserved 기준). 모델/배치 설정이 바뀌면 env 로 조정한다.
MODEL_VRAM_MB = int(os.environ.get("WEB_GPU_MODEL_VRAM_MB", "4000"))  # 실측 3,948
# CUDA 컨텍스트 + 캐싱 할당자 오버헤드. 배치와 무관한 고정비라 따로 둔다 — 이게 없으면
# 작은 배치에서 필요량을 1GB 넘게 과소평가해, 실제로는 OOM 이 날 상황을 'ok' 로 보고한다.
CONTEXT_VRAM_MB = int(os.environ.get("WEB_GPU_CONTEXT_VRAM_MB", "1500"))
CHUNK_VRAM_MB = int(os.environ.get("WEB_GPU_CHUNK_VRAM_MB", "100"))  # reserved 증가분 기준
# 여유가 필요량의 이 배수 미만이면 'tight' — 다른 잡과 겹치는 순간 실패할 수 있는 구간.
TIGHT_RATIO = float(os.environ.get("WEB_GPU_TIGHT_RATIO", "1.5"))
SNAPSHOT_TTL_SEC = float(os.environ.get("WEB_GPU_SNAPSHOT_TTL_SEC", "5"))
QUERY_TIMEOUT_SEC = float(os.environ.get("WEB_GPU_QUERY_TIMEOUT_SEC", "3"))

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "raw": None}


def required_mb(batch_size: int) -> int:
    """STT 잡 1건 실행에 필요한 VRAM 추정치(MiB, reserved 기준 + 컨텍스트 고정비)."""
    return MODEL_VRAM_MB + CONTEXT_VRAM_MB + CHUNK_VRAM_MB * max(1, batch_size)


def _query_nvidia_smi() -> tuple[int, int] | None:
    """(free_mb, total_mb) 또는 None(조회 불가). 이 프로세스 몫이 아니라 장치 전체 기준."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=memory.free,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=QUERY_TIMEOUT_SEC,
        )
        if out.returncode != 0:
            return None
        # 멀티 GPU 면 첫 장치 기준(이 서버는 1장). 값이 비거나 파싱 실패하면 None.
        free_s, total_s = out.stdout.strip().splitlines()[0].split(",")
        return int(free_s.strip()), int(total_s.strip())
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


# 테스트가 갈아끼울 수 있게 모듈 속성으로 둔다(서브프로세스 없이 결정적으로 단언).
_QUERY = _query_nvidia_smi


def _cached_raw(force: bool = False) -> tuple[int, int] | None:
    """TTL 캐시된 (free, total). force=True 면 캐시를 무시하고 다시 조회."""
    now = time.monotonic()
    with _lock:
        if not force and _cache["raw"] is not None and now - _cache["at"] < SNAPSHOT_TTL_SEC:
            return _cache["raw"]
    raw = _QUERY()  # 락 밖에서 조회(서브프로세스가 요청 스레드를 서로 막지 않게)
    with _lock:
        _cache["raw"] = raw
        _cache["at"] = now
    return raw


def snapshot(batch_size: int, *, force: bool = False) -> dict:
    """GPU 여유/압박 스냅샷.

    pressure:
      ok           — 여유 충분
      tight        — 여유가 필요량의 TIGHT_RATIO 배 미만. 다른 잡과 겹치면 실패 가능
      insufficient — 여유가 필요량 미만. 지금 실행하면 OOM 으로 실패
      unknown      — 조회 불가(nvidia-smi 없음/모니터 OFF). 판단하지 않는다
    """
    req = required_mb(batch_size)
    raw = _cached_raw(force) if MONITOR_ENABLED else None
    if raw is None:
        return {"available": False, "pressure": "unknown", "requiredMb": req}
    free, total = raw
    if free < req:
        pressure = "insufficient"
    elif free < req * TIGHT_RATIO:
        pressure = "tight"
    else:
        pressure = "ok"
    return {
        "available": True,
        "pressure": pressure,
        "freeMb": free,
        "totalMb": total,
        "usedMb": total - free,
        "requiredMb": req,
    }


def warning_text(snap: dict) -> str | None:
    """스냅샷 → 사용자가 읽는 경고 문구. 압박 없음/판단 불가면 None(조용히).

    '엔진 점검' 안내로 가지 않게 원인을 명시한다 — 자원 부족은 엔진 결함이 아니고,
    사용자가 할 수 있는 조치(잠시 후 재시도)도 다르다.

    점유자를 '다른 작업'이라고 단정하지 않는다 — 우리 STT 잡이 이미 슬롯을 쥐고 있어서
    여유가 줄었을 수도 있다. 외부 점유가 확실한 상황(우리 슬롯이 비어 있을 때)의 문구는
    호출부가 따로 만든다.
    """
    p = snap.get("pressure")
    if p == "insufficient":
        return (
            f"GPU 여유 메모리가 부족합니다"
            f"(여유 {snap['freeMb']}MB / 필요 {snap['requiredMb']}MB) "
            f"— 지금 처리하면 실패할 수 있습니다. 잠시 후 다시 시도해 주세요."
        )
    if p == "tight":
        return (
            f"GPU 여유 메모리가 빠듯합니다"
            f"(여유 {snap['freeMb']}MB / 필요 {snap['requiredMb']}MB) "
            f"— 다른 처리와 겹치면 실패할 수 있습니다."
        )
    return None


def reset_cache() -> None:
    """캐시 초기화(테스트 격리용)."""
    with _lock:
        _cache["raw"] = None
        _cache["at"] = 0.0
