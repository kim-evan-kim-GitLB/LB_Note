"""웹 계약 E2E 검증 드라이버 — asr test.m4a 로 요약+액션아이템 추출이 채워지는지 확인.

process_audio_to_contract 를 agent_cli(클라우드) 백엔드로 직접 호출해
summary/actionItems 가 비어있지 않은지 확인한다. (env 의존 없이 인자로 백엔드 명시)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from src.web.service import process_audio_to_contract

AUDIO = Path("samples/asr test.m4a")


def main() -> int:
    audio_bytes = AUDIO.read_bytes()
    print(f"[E2E] 입력: {AUDIO} ({len(audio_bytes)/1e6:.1f} MB)", flush=True)
    contract = process_audio_to_contract(
        audio_bytes,
        filename=AUDIO.name,
        backend_name="passthrough",      # 정제 OFF (비용)
        extract_backend_name="agent_cli",  # 액션 추출 ON
        summarize_backend_name="agent_cli",  # 요약 ON
    )
    summary = contract.get("summary") or {}
    actions = contract.get("actionItems") or []
    transcript = contract.get("transcript") or []
    agenda = (summary.get("agenda") if isinstance(summary, dict) else None) or []

    print("\n===== E2E 결과 =====", flush=True)
    print(f"transcript segments : {len(transcript)}")
    print(f"duration            : {contract.get('_duration_seconds')}")
    print(f"actionItems         : {len(actions)} 건")
    print(f"summary.agenda      : {len(agenda)} 안건")
    print(f"summary 비어있음?    : {not bool(agenda)}")
    print(f"actionItems 비어있음? : {not bool(actions)}")

    out = Path("output/e2e-asrtest-contract.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[E2E] 전체 계약 저장: {out}", flush=True)

    # 샘플 미리보기
    if actions:
        print("\n--- actionItems[0] ---")
        print(json.dumps(actions[0], ensure_ascii=False, indent=2))
    if agenda:
        print("\n--- agenda[0] ---")
        print(json.dumps(agenda[0], ensure_ascii=False, indent=2))

    ok = bool(actions) and bool(agenda)
    print(f"\n[E2E] 판정: {'PASS (요약+액션 채워짐)' if ok else 'FAIL (둘 중 빔)'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
