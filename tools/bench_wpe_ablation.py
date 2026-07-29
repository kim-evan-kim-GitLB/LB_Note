"""WPE 전처리 on/off 가 STT 소요에 미치는 영향 측정 (STT 단계만).

실효 RTFx 가 문서상 RTFx(232) 대비 크게 낮은 원인을 가르기 위한 ablation.
ENHANCERS 환경변수를 런타임에 바꿔가며 같은 음원으로 transcribe 단계만 잰다.

사용:
  sudo env PYTHONPATH=/app .venv/bin/python tools/bench_wpe_ablation.py --audio <wav>
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def measure(audio_path: Path, enhancers: str) -> dict:
    """ENHANCERS 를 설정한 뒤 STT 1회 수행 → 소요/세그먼트 수."""
    import importlib
    import os

    os.environ["ENHANCERS"] = enhancers
    # config 는 import 시점에 os.getenv 를 읽으므로 재로딩해야 값이 반영된다.
    from src import config as _config

    importlib.reload(_config)
    import src.pipeline as _pipeline

    importlib.reload(_pipeline)
    from src.web import service as _service

    importlib.reload(_service)

    audio_bytes = audio_path.read_bytes()
    t0 = time.monotonic()
    seg_dicts, duration = _service.transcribe_to_segments(
        audio_bytes, mime_type="audio/wav", backend_name="passthrough"
    )
    sec = round(time.monotonic() - t0, 1)
    return {
        "enhancers": enhancers or "(none)",
        "sttSec": sec,
        "segments": len(seg_dicts),
        "chars": sum(len(s.get("text") or "") for s in seg_dicts),
        "audioSec": round(duration or 0, 1),
        "effectiveRtfx": round((duration or 0) / max(sec, 0.001), 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", default="output/bench-wpe-ablation.json")
    args = ap.parse_args()

    audio = Path(args.audio)
    rows = []
    # 기본(wpe) 먼저, 그다음 전처리 없음. 같은 프로세스라 모델 로드 비용 조건이 유사하다.
    for enh in ("wpe", ""):
        row = measure(audio, enh)
        print(f"[wpe-ablation] {row}", flush=True)
        rows.append(row)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"audio": str(audio), "rows": rows}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"[wpe-ablation] 저장: {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
