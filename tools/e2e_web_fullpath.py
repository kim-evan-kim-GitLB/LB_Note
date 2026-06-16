"""웹 풀 경로 E2E — 프론트와 동일하게 HTTP 로 process→폴링.

POST /api/ai/process (audioBase64) → {jobId} → GET /api/ai/jobs/{id} 폴링 →
done 시 result(summary/actionItems/transcript) 검증. 서버는 :8001 에 떠 있다고 가정.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.request
from pathlib import Path

BASE = "http://localhost:8001"
AUDIO = Path("samples/asr test.m4a")


def _post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def _get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read())


def main() -> int:
    b64 = base64.b64encode(AUDIO.read_bytes()).decode()
    print(f"[풀경로] POST /api/ai/process ({AUDIO.name}, b64={len(b64)/1e6:.0f}MB)", flush=True)
    t0 = time.monotonic()
    resp = _post("/api/ai/process", {"audioBase64": b64, "mimeType": "audio/mp4", "title": "asr test"})
    job_id = resp.get("jobId")
    print(f"[풀경로] jobId={job_id} status={resp.get('status')}", flush=True)

    # 폴링 (프론트와 동일). STT+추출+요약 끝까지 대기.
    while True:
        time.sleep(10)
        job = _get(f"/api/ai/jobs/{job_id}")
        st = job.get("status")
        el = time.monotonic() - t0
        print(f"[풀경로] +{el:.0f}s status={st}", flush=True)
        if st in ("done", "error"):
            break

    el = time.monotonic() - t0
    if st == "error":
        print(f"\n[풀경로] FAIL: job error = {job.get('error')} / {el:.0f}s", flush=True)
        return 1

    result = job.get("result") or {}
    summary = result.get("summary") or {}
    actions = result.get("actionItems") or []
    transcript = result.get("transcript") or []
    agenda = (summary.get("agenda") if isinstance(summary, dict) else None) or []

    print("\n===== 풀경로 결과 =====", flush=True)
    print(f"총 소요          : {el:.0f}s")
    print(f"duration         : {result.get('duration')}")
    print(f"transcript       : {len(transcript)} segments")
    print(f"actionItems      : {len(actions)} 건")
    print(f"summary.agenda   : {len(agenda)} 안건")

    out = Path("output/e2e-fullpath-result.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[풀경로] 저장: {out}", flush=True)

    ok = bool(actions) and bool(agenda)
    print(f"\n[풀경로] 판정: {'PASS (요약+액션 모두 채워짐)' if ok else 'FAIL'} / {el:.0f}s", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
