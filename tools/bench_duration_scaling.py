"""음원 길이별 처리 시간 실측 — STT / 요약 / 추출 단계 분해.

웹 잡(_run_ai_job)과 동일한 순서로 in-process 실행하고 단계별 소요를 잰다:
  transcribe_to_segments(GPU)  ->  summarize_meeting(agent_cli)  ->  extract_action_items(agent_cli)

HTTP/base64/staging 오버헤드는 제외한다(분 단위 지연의 원인 규명이 목적).
AGENT_CLI_TIMEOUT 은 호출 환경변수를 그대로 따르며, 실측을 위해 운영값(600)보다 크게 주는 것을
권장한다 — 운영값이었다면 실패했을 지점은 결과에 timeout_would_fail 로 표시한다.

사용:
  sudo PATH=/home/evan/.npm-global/bin:$PATH AGENT_CLI_TIMEOUT=1800 \
      .venv/bin/python tools/bench_duration_scaling.py --audio-dir <dir> --out output/bench-duration.json
"""
from __future__ import annotations

import argparse
import json
import re
import time
import traceback
from pathlib import Path

from src.web.service import (
    _summary_action_hints,
    extract_action_items,
    summarize_meeting,
    transcribe_to_segments,
)

PROD_AGENT_CLI_TIMEOUT = 600  # .env 운영값 — 초과 구간 표시용


def _log(msg: str) -> None:
    print(f"[bench] {msg}", flush=True)


def _transcript_chars(seg_dicts: list[dict]) -> int:
    return sum(len(s.get("cleaned") or s.get("text") or "") for s in seg_dicts)


def run_one(path: Path, audio_min: float) -> dict:
    """음원 1개 처리 → 단계별 소요 dict. 실패해도 예외를 삼키고 error 를 담아 반환."""
    row: dict = {"audioMinutes": audio_min, "file": path.name}
    audio_bytes = path.read_bytes()
    row["fileMb"] = round(len(audio_bytes) / (1024 * 1024), 1)
    # 프론트는 base64 로 올린다 — 업로드 크기 참고치(전송 시간은 이 벤치에 미포함).
    row["base64Mb"] = round(len(audio_bytes) * 4 / 3 / (1024 * 1024), 1)

    try:
        _log(f"{audio_min}m STT 시작 ({row['fileMb']}MB)")
        t0 = time.monotonic()
        seg_dicts, duration = transcribe_to_segments(
            audio_bytes, mime_type="audio/wav", backend_name="passthrough"
        )
        t1 = time.monotonic()
        row["sttSec"] = round(t1 - t0, 1)
        row["segments"] = len(seg_dicts)
        row["transcriptChars"] = _transcript_chars(seg_dicts)
        row["decodedDurationSec"] = round(duration or 0, 1)
        # STT 실시간 배속 — 모델 load/unload·디코딩 포함한 실효값(순수 추론 RTFx 아님)
        row["effectiveRtfx"] = round((duration or 0) / max(row["sttSec"], 0.001), 1)
        _log(f"{audio_min}m STT 완료 {row['sttSec']}s / seg={row['segments']} / chars={row['transcriptChars']}")

        _log(f"{audio_min}m 요약 시작")
        t2 = time.monotonic()
        summary = summarize_meeting(seg_dicts, backend_name="agent_cli")
        t3 = time.monotonic()
        row["summarizeSec"] = round(t3 - t2, 1)
        row["agenda"] = len(summary.get("agenda") or [])
        _log(f"{audio_min}m 요약 완료 {row['summarizeSec']}s / agenda={row['agenda']}")

        _log(f"{audio_min}m 추출 시작")
        t4 = time.monotonic()
        actions = extract_action_items(
            seg_dicts, backend_name="agent_cli", summary_hints=_summary_action_hints(summary)
        )
        t5 = time.monotonic()
        row["extractSec"] = round(t5 - t4, 1)
        row["actionItems"] = len(actions)
        _log(f"{audio_min}m 추출 완료 {row['extractSec']}s / actions={row['actionItems']}")

        row["llmSec"] = round(row["summarizeSec"] + row["extractSec"], 1)
        row["totalSec"] = round(t5 - t0, 1)
        # 운영 타임아웃(600s)이었다면 어느 단계가 죽었을지
        over = [
            name
            for name, sec in (("summarize", row["summarizeSec"]), ("extract", row["extractSec"]))
            if sec > PROD_AGENT_CLI_TIMEOUT
        ]
        row["timeoutWouldFail"] = over
        row["ok"] = True
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        row["ok"] = False
        row["error"] = f"{type(e).__name__}: {e}"
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio-dir", required=True, help="audio_<N>m.wav 들이 있는 디렉토리")
    ap.add_argument("--out", default="output/bench-duration.json")
    args = ap.parse_args()

    audio_dir = Path(args.audio_dir)
    files = sorted(
        audio_dir.glob("audio_*m.wav"),
        key=lambda p: int(re.search(r"audio_(\d+)m", p.name).group(1)),
    )
    if not files:
        _log(f"음원 없음: {audio_dir}")
        return 1

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    started = time.monotonic()
    for p in files:
        minutes = int(re.search(r"audio_(\d+)m", p.name).group(1))
        rows.append(run_one(p, minutes))
        # 매 건마다 저장 — 중간에 죽어도 앞 결과는 남는다.
        out_path.write_text(
            json.dumps({"rows": rows, "elapsedSec": round(time.monotonic() - started, 1)},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _log(f"저장: {out_path} ({len(rows)}/{len(files)})")

    _log(f"전체 완료 {round(time.monotonic() - started, 1)}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
