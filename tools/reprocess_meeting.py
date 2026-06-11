"""저장된 회의(DB)의 transcript로 요약·추출만 재처리 — 새 프롬프트 효과 확인용.

STT 는 건너뛰고(이미 transcript 보유) summarize_meeting + extract_action_items 만 다시 돌린다.
방법2(요약→추출 힌트)도 그대로 태운다. 사용:
  sudo env PYTHONPATH=/app PATH=...claude.. .venv/bin/python tools/reprocess_meeting.py test2
"""
from __future__ import annotations

import json
import sqlite3
import sys

from src.web.service import (
    _summary_action_hints,
    extract_action_items,
    summarize_meeting,
)

DB = "output/web/meetings.db"
BACKEND = "agent_cli"


def _mmss(ts: str) -> float:
    p = [int(x) for x in str(ts).split(":")]
    return p[0] * 3600 + p[1] * 60 + p[2] if len(p) == 3 else p[0] * 60 + p[1]


def main(title: str) -> int:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT data FROM meetings WHERE title=?", (title,)).fetchone()
    if not row:
        print(f"회의 '{title}' 없음")
        return 1
    m = json.loads(row["data"])
    transcript = m.get("transcript") or []
    starts = [_mmss(t.get("timestamp", "0:00")) for t in transcript]
    segs = []
    for i, t in enumerate(transcript):
        s = starts[i]
        e = starts[i + 1] if i + 1 < len(starts) else s + 5.0
        segs.append({"id": i, "start": s, "end": max(e, s), "cleaned": t.get("text", ""), "text": t.get("text", "")})

    print(f"[재처리] '{title}' segments={len(segs)} backend={BACKEND}", flush=True)

    summary = summarize_meeting(segs, backend_name=BACKEND)
    hints = _summary_action_hints(summary)
    print(f"\n[요약] agenda={len(summary.get('agenda') or [])}안건, 추출 힌트 {len(hints)}개")
    for h in hints:
        print("   힌트:", h)

    items = extract_action_items(segs, backend_name=BACKEND, summary_hints=hints)
    print(f"\n[액션아이템] {len(items)}건 (방법1 제거 + extract-1.3 능동도출 + 방법2 힌트)")
    for it in items:
        print(f"   - {it.get('text')} | owner={it.get('owner')} due={it.get('due')} anchor={it.get('anchor')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "test2"))
