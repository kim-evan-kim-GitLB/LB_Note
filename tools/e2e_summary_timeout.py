"""요약 단일 콜 타임아웃 베이스라인 측정 (A안).

이전 E2E 산출물(output/e2e-asrtest-contract.json)의 transcript 에서 segments 를 복구해
STT 를 건너뛰고, SummarizeStage(agent_cli) **1콜만** 돌려 소요 시간과 agenda 채움 여부를 측정한다.
AGENT_CLI_TIMEOUT 은 호출 환경변수로 상향(예: 600)해서 단일 콜이 끝까지 가는지 본다.

주의: start/end 는 transcript 의 MM:SS 를 초로 환원한 근사치(원본 sub-second/end 손실). 타임아웃·
agenda 채움 측정에는 영향 없음(id 는 0..N 연속, grounding 의 evidence_seg_ids 매칭은 동일).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from src.web.service import summarize_meeting

CONTRACT = Path("output/e2e-asrtest-contract.json")


def _mmss_to_sec(ts: str) -> float:
    parts = [int(x) for x in ts.split(":")]
    if len(parts) == 3:
        h, m, s = parts
        return h * 3600 + m * 60 + s
    m, s = parts
    return m * 60 + s


def main() -> int:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    transcript = contract.get("transcript") or []
    starts = [_mmss_to_sec(t.get("timestamp", "0:00")) for t in transcript]
    seg_dicts = []
    for i, t in enumerate(transcript):
        start = starts[i]
        end = starts[i + 1] if i + 1 < len(starts) else start + 5.0
        seg_dicts.append(
            {"id": i, "start": start, "end": max(end, start), "cleaned": t.get("text", ""), "text": t.get("text", "")}
        )

    timeout = os.environ.get("AGENT_CLI_TIMEOUT", "(기본120)")
    print(f"[요약타임아웃] segments={len(seg_dicts)} AGENT_CLI_TIMEOUT={timeout}", flush=True)

    t0 = time.monotonic()
    summary = summarize_meeting(seg_dicts, backend_name="agent_cli")
    elapsed = time.monotonic() - t0

    agenda = summary.get("agenda") or []
    idx = summary.get("agenda_index") or []
    print("\n===== 요약 단일콜 결과 =====", flush=True)
    print(f"소요 시간       : {elapsed:.1f}s")
    print(f"agenda          : {len(agenda)} 안건")
    print(f"agenda_index    : {len(idx)} 항목")
    print(f"요약 비어있음?   : {not bool(agenda)}")

    out = Path("output/e2e-summary-only.json")
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[요약타임아웃] 저장: {out}", flush=True)
    if agenda:
        print("\n--- agenda[0] ---")
        print(json.dumps(agenda[0], ensure_ascii=False, indent=2)[:800])

    ok = bool(agenda)
    print(f"\n[요약타임아웃] 판정: {'PASS (요약 채워짐)' if ok else 'FAIL (빔)'} / {elapsed:.1f}s", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
