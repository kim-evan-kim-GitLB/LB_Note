"""WPE 전처리 on/off 의 정확도 대가 측정 — 속도와 WER/CER 을 함께 본다.

tools/bench_wpe_ablation.py 가 "WPE 가 STT 시간의 96% 를 쓴다"를 보였다. 끌지 말지는
정확도 손실을 봐야 결정할 수 있으므로, 정답지가 있는 음원으로 두 설정을 같은 조건에서 채점한다.

reference 가 Clova STT 결과라 ground truth 가 아니므로(CLAUDE.md) **WER 절대값이 아니라
두 설정의 차이(Δ)** 만 의미가 있다.

사용:
  sudo env PYTHONPATH=/app .venv/bin/python tools/bench_wpe_wer.py \
      --audio "samples/ax과제회의(클로바노트)_음성파일.m4a" \
      --reference answer/ax_tf_클로바.txt
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def measure(audio_path: Path, reference: Path, enhancers: str) -> dict:
    import importlib
    import os

    os.environ["ENHANCERS"] = enhancers
    from src import config as _config

    importlib.reload(_config)
    import src.pipeline as _pipeline

    importlib.reload(_pipeline)
    from src.web import service as _service

    importlib.reload(_service)
    from src.scoring import evaluate

    audio_bytes = audio_path.read_bytes()
    t0 = time.monotonic()
    seg_dicts, duration = _service.transcribe_to_segments(
        audio_bytes, mime_type="audio/m4a", backend_name="passthrough"
    )
    sec = round(time.monotonic() - t0, 1)

    hypothesis = " ".join((s.get("text") or "").strip() for s in seg_dicts).strip()
    scores = evaluate(hypothesis, reference)

    row = {
        "enhancers": enhancers or "(none)",
        "sttSec": sec,
        "audioSec": round(duration or 0, 1),
        "effectiveRtfx": round((duration or 0) / max(sec, 0.001), 1),
        "segments": len(seg_dicts),
        "hypChars": len(hypothesis),
    }
    # evaluate 반환 키는 버전에 따라 다를 수 있어 숫자형만 골라 그대로 싣는다.
    for k, v in (scores or {}).items():
        if isinstance(v, (int, float)):
            row[k] = round(float(v), 4)
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--reference", required=True)
    ap.add_argument("--out", default="output/bench-wpe-wer.json")
    args = ap.parse_args()

    audio, ref = Path(args.audio), Path(args.reference)
    rows = []
    for enh in ("wpe", ""):
        row = measure(audio, ref, enh)
        print(f"[wpe-wer] {row}", flush=True)
        rows.append(row)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"audio": str(audio), "reference": str(ref), "rows": rows},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[wpe-wer] 저장: {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
